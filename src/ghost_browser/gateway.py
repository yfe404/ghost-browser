"""Credential-safe Ghost Gateway allocation and release."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit


DEFAULT_GATEWAY = "https://straightforward-under--ghost-gateway.apify.actor"


@dataclass(frozen=True)
class Allocation:
    """An allocated live browser. Values are secrets and must never be logged."""

    gateway_url: str = field(repr=False)
    websocket_url: str = field(repr=False)
    browser_id: str | None = field(repr=False)

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


def allocate_browser(
    gateway_url: str,
    *,
    token: str | None = None,
    timeout: float = 180,
) -> Allocation:
    """Allocate exactly one browser through ``/json/version``."""

    _validate_gateway_url(gateway_url)
    caller_token = token if token is not None else os.environ.get("APIFY_TOKEN", "")
    query_has_token = any(
        key == "token" for key, _value in parse_qsl(urlsplit(gateway_url).query)
    )
    if not caller_token and not query_has_token and not _loopback(urlsplit(gateway_url).hostname):
        raise GatewayError("APIFY_TOKEN is required for the configured gateway")
    request = urllib.request.Request(_allocation_url(gateway_url, caller_token))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(1_048_577)
            if len(raw) > 1_048_576:
                raise GatewayError("gateway allocation response is too large")
            payload = json.loads(raw)
            websocket_url = _validate_websocket_url(
                payload["webSocketDebuggerUrl"], gateway_url
            )
            browser_id = response.headers.get(
                "X-Ghost-Session"
            ) or _browser_id_from_websocket(websocket_url)
    except GatewayError:
        raise
    except urllib.error.HTTPError as error:
        raise GatewayError(f"gateway allocation failed: HTTP {error.code}") from None
    except (OSError, TimeoutError) as error:
        raise GatewayError(
            f"gateway allocation failed: {type(error).__name__}"
        ) from None
    except (KeyError, TypeError, ValueError):
        raise GatewayError("gateway allocation returned an invalid response") from None
    return Allocation(gateway_url, websocket_url, browser_id)


def release_browser(allocation: Allocation, *, timeout: float = 15) -> None:
    """Best-effort idempotent release on the replica that owns the browser."""

    if not allocation.browser_id:
        return
    websocket = urlsplit(allocation.websocket_url)
    scheme = "https" if websocket.scheme == "wss" else "http"
    path = f"/v1/sessions/{quote(allocation.browser_id, safe='')}"
    primary = urlunsplit((scheme, websocket.netloc, path, websocket.query, ""))

    gateway = urlsplit(allocation.gateway_url)
    fallback_query = parse_qsl(gateway.query, keep_blank_values=True)
    if not any(key == "token" for key, _value in fallback_query):
        token = os.environ.get("APIFY_TOKEN", "")
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

    last_error: Exception | None = None
    for url in dict.fromkeys((primary, fallback)):
        request = urllib.request.Request(url, method="DELETE")
        try:
            with urllib.request.urlopen(request, timeout=timeout):
                return
        except urllib.error.HTTPError as error:
            if error.code == 404:
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
