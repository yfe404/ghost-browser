"""Credential-safe Ghost Gateway allocation and release."""

from __future__ import annotations

import json
import os
import time
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
    exact_owner: bool = field(default=True, repr=False)

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


def _validate_country(value: str) -> str:
    country = value.strip().upper()
    if len(country) != 2 or not country.isalpha() or not country.isascii():
        raise GatewayError(
            "country must be ISO-3166 alpha-2 (e.g. KR)"
        )
    return country


def country_from_env() -> str | None:
    raw = os.environ.get("GHOST_BROWSER_COUNTRY", "").strip()
    if not raw:
        return None
    return _validate_country(raw)


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


def _gateway_api_url(gateway_url: str, token: str, suffix: str) -> str:
    parsed = urlsplit(gateway_url)
    path = f"{parsed.path.rstrip('/')}{suffix}"
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if not any(key == "token" for key, _value in query):
        query.append(("token", token))
    return urlunsplit(
        (parsed.scheme, parsed.netloc, path, urlencode(query), "")
    )


def _allocation_url(gateway_url: str, token: str) -> str:
    return _gateway_api_url(gateway_url, token, "/json/version")


def _read_capped_json(response, too_large_message: str):
    raw = response.read(1_048_577)
    if len(raw) > 1_048_576:
        raise GatewayError(too_large_message)
    return json.loads(raw)


def _mint_session(gateway_url: str, token: str, timeout: float) -> str:
    request = urllib.request.Request(
        _gateway_api_url(gateway_url, token, "/v1/sessions"),
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = _read_capped_json(
                response, "gateway session response is too large"
            )
            session = payload["session"]
            if not isinstance(session, str) or not session:
                raise KeyError("session")
            return session
    except GatewayError:
        raise
    except urllib.error.HTTPError as error:
        raise GatewayError(
            f"gateway session creation failed: HTTP {error.code}"
        ) from None
    except (OSError, TimeoutError) as error:
        raise GatewayError(
            f"gateway session creation failed: {type(error).__name__}"
        ) from None
    except (KeyError, TypeError, ValueError):
        raise GatewayError(
            "gateway session creation returned an invalid response"
        ) from None


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
            _loopback(urlsplit(gateway_url).hostname),
        )
    elif websocket_url:
        cleanup = Allocation(
            gateway_url,
            websocket_url,
            None,
            caller_token,
            False,
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
    country: str | None = None,
    timeout: float = 180,
    _on_unreleased: Callable[[Allocation | None], None] | None = None,
) -> Allocation:
    """Allocate exactly one browser.

    Without ``country`` this uses the plain ``/json/version`` discovery path.
    With ``country`` (ISO-3166 alpha-2) it mints a Gateway session and allocates
    through ``POST /v1/sessions/{session}/browser`` so the egress matches.
    """

    _validate_gateway_url(gateway_url)
    if country is not None:
        country = _validate_country(country)
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
    request_timeout = timeout
    if country is None:
        request = urllib.request.Request(_allocation_url(gateway_url, caller_token))
    else:
        started = time.monotonic()
        session = _mint_session(gateway_url, caller_token, timeout)
        request_timeout = timeout - (time.monotonic() - started)
        if request_timeout <= 0:
            raise GatewayError("gateway allocation timed out during session creation")
        request = urllib.request.Request(
            _gateway_api_url(
                gateway_url,
                caller_token,
                f"/v1/sessions/{quote(session, safe='')}/browser",
            ),
            data=json.dumps({"country": country}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
    browser_id: str | None = None
    successful_response = False
    try:
        with urllib.request.urlopen(request, timeout=request_timeout) as response:
            successful_response = True
            browser_id = response.headers.get("X-Ghost-Session")
            payload = _read_capped_json(
                response, "gateway allocation response is too large"
            )
            if country is None:
                candidate = payload["webSocketDebuggerUrl"]
            else:
                identifier = (
                    payload.get("browserId") if isinstance(payload, dict) else None
                )
                if isinstance(identifier, str) and identifier:
                    browser_id = identifier
                candidate = payload["cdp"]["webSocketDebuggerUrl"]
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
                if not allocation.exact_owner:
                    raise GatewayError(
                        "gateway release could not be confirmed for an unsupported WebSocket contract"
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

    request = urllib.request.Request(primary, method="DELETE")
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            if not allocation.exact_owner:
                raise GatewayError(
                    "gateway release could not be confirmed on a shared endpoint"
                )
            return
    except urllib.error.HTTPError as error:
        if error.code == 404 and allocation.exact_owner:
            return
        last_error: Exception = error
    except (OSError, TimeoutError) as error:
        last_error = error
    if isinstance(last_error, urllib.error.HTTPError):
        raise GatewayError(
            f"gateway release failed: HTTP {last_error.code}"
        ) from None
    raise GatewayError(
        f"gateway release failed: {type(last_error).__name__}"
    ) from None
