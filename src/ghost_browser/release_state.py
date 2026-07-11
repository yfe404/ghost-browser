"""Owner-only release handle retained until cleanup is confirmed."""

from __future__ import annotations

import json
import os
from contextlib import suppress
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .gateway import (
    Allocation,
    GatewayError,
    gateway_url_from_env,
    release_browser,
)
from .paths import SessionPaths


def save_release_handle(paths: SessionPaths, allocation: Allocation) -> None:
    """Persist only the opaque run handle; never persist the caller token."""

    websocket = urlsplit(allocation.websocket_url)
    gateway_tokens = {
        value
        for key, value in parse_qsl(
            urlsplit(allocation.gateway_url).query, keep_blank_values=True
        )
        if key == "token" and value
    }
    caller_tokens = gateway_tokens | (
        {allocation.caller_token} if allocation.caller_token else set()
    )
    query = [
        (key, value)
        for key, value in parse_qsl(websocket.query, keep_blank_values=True)
        if not (key == "token" and value in caller_tokens)
    ]
    saved_websocket_url = urlunsplit(
        (
            websocket.scheme,
            websocket.netloc,
            websocket.path,
            urlencode(query),
            "",
        )
    )
    payload = {
        "websocket_url": saved_websocket_url,
        "browser_id": allocation.browser_id,
    }
    temporary = paths.pending_release.with_name(
        f"{paths.pending_release.name}.{os.getpid()}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(payload, separators=(",", ":")), encoding="utf-8"
        )
        temporary.chmod(0o600)
        temporary.replace(paths.pending_release)
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()


def clear_release_handle(paths: SessionPaths) -> None:
    with suppress(FileNotFoundError):
        paths.pending_release.unlink()


def write_shutdown_result(paths: SessionPaths, result: dict[str, object]) -> None:
    temporary = paths.shutdown_result.with_name(
        f"{paths.shutdown_result.name}.{os.getpid()}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(result, separators=(",", ":")), encoding="utf-8"
        )
        temporary.chmod(0o600)
        temporary.replace(paths.shutdown_result)
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()


def _load_release_handle(paths: SessionPaths) -> Allocation | None:
    try:
        payload = json.loads(paths.pending_release.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, TypeError, ValueError):
        raise GatewayError("saved browser release handle is invalid") from None
    websocket_url = payload.get("websocket_url") if isinstance(payload, dict) else None
    browser_id = payload.get("browser_id") if isinstance(payload, dict) else None
    if not isinstance(websocket_url, str) or not isinstance(browser_id, str):
        raise GatewayError("saved browser release handle is invalid")
    return Allocation(
        gateway_url_from_env(),
        websocket_url,
        browser_id,
        os.environ.get("APIFY_TOKEN", ""),
    )


def retry_pending_release(paths: SessionPaths) -> bool:
    """Retry a prior unconfirmed release, retaining the handle on failure."""

    allocation = _load_release_handle(paths)
    if allocation is None:
        return False
    release_browser(allocation)
    clear_release_handle(paths)
    write_shutdown_result(paths, {"released": True})
    return True
