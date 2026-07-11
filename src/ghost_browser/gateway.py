"""Credential-safe Ghost Gateway allocation and release."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, NoReturn
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

from websockets.sync.client import connect as websocket_connect


DEFAULT_GATEWAY = "https://straightforward-under--ghost-gateway.apify.actor"


@dataclass(frozen=True)
class Allocation:
    """An allocated live browser. Values are secrets and must never be logged."""

    gateway_url: str = field(repr=False)
    websocket_url: str = field(repr=False)
    browser_id: str | None = field(repr=False)
    caller_token: str = field(default="", repr=False)

    @property
    def gateway_host(self) -> str:
        return urlsplit(self.gateway_url).hostname or "configured gateway"


class GatewayError(RuntimeError):
    """A sanitized Gateway failure safe to display to the user."""


def gateway_url_from_env() -> str:
    return (
        os.environ.get("GHOST_GATEWAY_URL")
        or os.environ.get("GHOST_STANDBY_URL")
        or DEFAULT_GATEWAY
    ).strip()


def _loopback(hostname: str | None) -> bool:
    return hostname in {"localhost", "127.0.0.1", "::1"}


def _validate_gateway_url(value: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise GatewayError("gateway URL must be HTTP(S)")
    if parsed.scheme != "https" and not _loopback(parsed.hostname):
        raise GatewayError("gateway URL must use HTTPS unless it is local")


def _validate_websocket_url(value: object, gateway_url: str) -> str:
    if not isinstance(value, str):
        raise GatewayError("gateway allocation returned no WebSocket URL")
    parsed = urlsplit(value)
    if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
        raise GatewayError("gateway allocation returned an invalid WebSocket URL")
    gateway = urlsplit(gateway_url)
    if parsed.scheme != "wss" and not (
        _loopback(parsed.hostname) and _loopback(gateway.hostname)
    ):
        raise GatewayError("gateway returned insecure WebSocket transport")
    return value


def _browser_id_from_websocket(websocket_url: str) -> str | None:
    parts = [part for part in urlsplit(websocket_url).path.split("/") if part]
    if len(parts) >= 3 and parts[-3:-1] == ["devtools", "browser"]:
        return unquote(parts[-1]) or None
    return None


def _allocation_url(gateway_url: str, token: str) -> str:
    parsed = urlsplit(gateway_url)
    path = f"{parsed.path.rstrip('/')}/json/version"
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if not any(key == "token" for key, _value in query):
        query.append(("token", token))
    return urlunsplit(
        (parsed.scheme, parsed.netloc, path, urlencode(query), "")
    )


def _cleanup_websocket_url(
    gateway_url: str, browser_id: str, caller_token: str
) -> str:
    gateway = urlsplit(gateway_url)
    scheme = "wss" if gateway.scheme == "https" else "ws"
    query = parse_qsl(gateway.query, keep_blank_values=True)
    if not any(key == "token" for key, _value in query) and caller_token:
        query.append(("token", caller_token))
    path = f"/devtools/browser/{quote(browser_id, safe='')}"
    return urlunsplit((scheme, gateway.netloc, path, urlencode(query), ""))


def _fail_successful_allocation(
    message: str,
    gateway_url: str,
    browser_id: str | None,
    caller_token: str,
    timeout: float,
    websocket_url: str | None,
    on_unreleased: Callable[[Allocation | None], None] | None,
) -> NoReturn:
    cleanup = None
    if browser_id:
        cleanup = Allocation(
            gateway_url,
            _cleanup_websocket_url(gateway_url, browser_id, caller_token),
            browser_id,
            caller_token,
        )
    elif websocket_url:
        cleanup = Allocation(
            gateway_url,
            websocket_url,
            None,
            caller_token,
        )
    if cleanup is None:
        if on_unreleased:
            on_unreleased(None)
        raise GatewayError(message)
    try:
        release_browser(cleanup, timeout=min(timeout, 15))
    except GatewayError:
        if on_unreleased:
            on_unreleased(cleanup)
        raise GatewayError(message) from None
    raise GatewayError(message)


def allocate_browser(
    gateway_url: str,
    *,
    token: str | None = None,
    timeout: float = 180,
    _on_unreleased: Callable[[Allocation | None], None] | None = None,
) -> Allocation:
    """Allocate exactly one browser through ``/json/version``."""

    _validate_gateway_url(gateway_url)
    caller_token = token if token is not None else os.environ.get("APIFY_TOKEN", "")
    query_has_token = any(
        key == "token" for key, _value in parse_qsl(urlsplit(gateway_url).query)
    )
    if (
        not caller_token
        and not query_has_token
        and not _loopback(urlsplit(gateway_url).hostname)
    ):
        raise GatewayError("APIFY_TOKEN is required for the configured gateway")
    request = urllib.request.Request(_allocation_url(gateway_url, caller_token))
    browser_id: str | None = None
    successful_response = False
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            successful_response = True
            browser_id = response.headers.get("X-Ghost-Session")
            raw = response.read(1_048_577)
            if len(raw) > 1_048_576:
                raise GatewayError("gateway allocation response is too large")
            payload = json.loads(raw)
            candidate = payload["webSocketDebuggerUrl"]
            if not browser_id and isinstance(candidate, str):
                browser_id = _browser_id_from_websocket(candidate)
            websocket_url = _validate_websocket_url(
                candidate, gateway_url
            )
    except GatewayError as error:
        _fail_successful_allocation(
            str(error),
            gateway_url,
            browser_id,
            caller_token,
            timeout,
            None,
            _on_unreleased,
        )
    except urllib.error.HTTPError as error:
        raise GatewayError(f"gateway allocation failed: HTTP {error.code}") from None
    except (OSError, TimeoutError) as error:
        message = f"gateway allocation failed: {type(error).__name__}"
        if successful_response:
            _fail_successful_allocation(
                message,
                gateway_url,
                browser_id,
                caller_token,
                timeout,
                None,
                _on_unreleased,
            )
        raise GatewayError(message) from None
    except (KeyError, TypeError, ValueError):
        _fail_successful_allocation(
            "gateway allocation returned an invalid response",
            gateway_url,
            browser_id,
            caller_token,
            timeout,
            None,
            _on_unreleased,
        )
    if not browser_id:
        _fail_successful_allocation(
            "gateway allocation returned no browser identifier",
            gateway_url,
            None,
            caller_token,
            timeout,
            websocket_url,
            _on_unreleased,
        )
    return Allocation(gateway_url, websocket_url, browser_id, caller_token)


def release_browser(allocation: Allocation, *, timeout: float = 15) -> None:
    """Release through exact-owner DELETE, or normal WS close without an ID."""

    if not allocation.browser_id:
        try:
            with websocket_connect(
                allocation.websocket_url,
                open_timeout=timeout,
                close_timeout=10,
                ping_interval=None,
                proxy=None,
            ) as websocket:
                websocket.close(code=1000, reason="Ghost Browser released")
                if websocket.close_code != 1000:
                    raise GatewayError(
                        "gateway release failed: WebSocket close was abnormal"
                    )
        except GatewayError:
            raise
        except Exception as error:
            raise GatewayError(
                f"gateway release failed: {type(error).__name__}"
            ) from None
        return
    websocket = urlsplit(allocation.websocket_url)
    gateway = urlsplit(allocation.gateway_url)
    scheme = "https" if websocket.scheme == "wss" else "http"
    path = f"/v1/sessions/{quote(allocation.browser_id, safe='')}"
    primary_query = parse_qsl(websocket.query, keep_blank_values=True)
    if not any(key == "token" for key, _value in primary_query):
        gateway_token = next(
            (
                value
                for key, value in parse_qsl(
                    gateway.query, keep_blank_values=True
                )
                if key == "token"
            ),
            "",
        )
        token = gateway_token or allocation.caller_token
        if token:
            primary_query.append(("token", token))
    primary = urlunsplit(
        (scheme, websocket.netloc, path, urlencode(primary_query), "")
    )

    fallback_query = parse_qsl(gateway.query, keep_blank_values=True)
    if not any(key == "token" for key, _value in fallback_query):
        token = allocation.caller_token or os.environ.get("APIFY_TOKEN", "")
        if token:
            fallback_query.append(("token", token))
    fallback_path = f"{gateway.path.rstrip('/')}{path}"
    fallback = urlunsplit(
        (
            gateway.scheme,
            gateway.netloc,
            fallback_path,
            urlencode(fallback_query),
            "",
        )
    )

    candidates = [(primary, True)]
    if fallback != primary:
        candidates.append((fallback, False))
    last_error: Exception | None = None
    for url, exact_owner in candidates:
        request = urllib.request.Request(url, method="DELETE")
        try:
            with urllib.request.urlopen(request, timeout=timeout):
                return
        except urllib.error.HTTPError as error:
            if error.code == 404 and exact_owner:
                return
            last_error = error
        except (OSError, TimeoutError) as error:
            last_error = error
    if isinstance(last_error, urllib.error.HTTPError):
        raise GatewayError(
            f"gateway release failed: HTTP {last_error.code}"
        ) from None
    raise GatewayError(
        f"gateway release failed: {type(last_error).__name__}"
    ) from None
