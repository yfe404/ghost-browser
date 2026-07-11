"""Agent-facing Python primitives over the local daemon."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from .ipc import request
from .paths import SessionPaths


class BrowserClient:
    def __init__(self, paths: SessionPaths) -> None:
        self.paths = paths

    def cdp(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float = 30,
        **keyword_params: Any,
    ) -> dict[str, Any]:
        """Send one arbitrary CDP command. Commands are never replayed."""

        if not isinstance(method, str) or not method:
            raise TypeError("cdp method must be a non-empty string")
        merged = dict(params or {})
        merged.update(keyword_params)
        result = request(
            self.paths,
            {
                "op": "cdp",
                "method": method,
                "params": merged,
                "session_id": session_id,
                "timeout": timeout,
            },
            timeout=timeout + 2,
        )
        return result if isinstance(result, dict) else {}

    def drain_events(self) -> list[dict[str, Any]]:
        result = request(self.paths, {"op": "drain_events"})
        return result if isinstance(result, list) else []

    def ensure_page(self) -> dict[str, str]:
        result = request(self.paths, {"op": "ensure_page"})
        return result

    def js(self, expression: str, *, await_promise: bool = False) -> Any:
        page = self.ensure_page()
        response = self.cdp(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
            },
            session_id=page["session_id"],
        )
        if response.get("exceptionDetails"):
            details = response["exceptionDetails"]
            raise RuntimeError(details.get("text") or "page JavaScript failed")
        result = response.get("result") or {}
        if "value" in result:
            return result["value"]
        if "unserializableValue" in result:
            return result["unserializableValue"]
        return None

    def page_info(self) -> dict[str, Any]:
        raw = self.js(
            "JSON.stringify({url:location.href,title:document.title,"
            "width:innerWidth,height:innerHeight})"
        )
        return json.loads(raw)

    def tabs(self) -> list[dict[str, Any]]:
        response = self.cdp("Target.getTargets", session_id=None)
        return [
            target
            for target in response.get("targetInfos", [])
            if target.get("type") == "page"
        ]

    def capture_screenshot(
        self, path: str | Path, *, full_page: bool = False
    ) -> str:
        page = self.ensure_page()
        response = self.cdp(
            "Page.captureScreenshot",
            {"format": "png", "captureBeyondViewport": full_page},
            session_id=page["session_id"],
        )
        destination = Path(path).expanduser().resolve()
        destination.write_bytes(base64.b64decode(response["data"], validate=True))
        return str(destination)


def script_namespace(client: BrowserClient) -> dict[str, Any]:
    """The deliberately small built-in environment for agent-written Python."""

    return {
        "cdp": client.cdp,
        "drain_events": client.drain_events,
        "ensure_page": client.ensure_page,
        "js": client.js,
        "page_info": client.page_info,
        "tabs": client.tabs,
        "capture_screenshot": client.capture_screenshot,
    }
