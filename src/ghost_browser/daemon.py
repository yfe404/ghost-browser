"""Persistent Ghost WebSocket holder and raw CDP relay."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from collections import deque
from contextlib import suppress
from typing import Any

from websockets.asyncio.client import connect

from .gateway import (
    Allocation,
    allocate_browser,
    gateway_url_from_env,
    release_browser,
)
from .ipc import MAX_MESSAGE_BYTES
from .paths import SessionPaths, session_paths
from .redaction import redact


class CDPError(RuntimeError):
    """A Chrome DevTools Protocol or transport error."""


class CDPConnection:
    def __init__(self, websocket: Any) -> None:
        self.websocket = websocket
        self.next_id = 0
        self.pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.events: deque[dict[str, Any]] = deque(maxlen=512)
        self.send_lock = asyncio.Lock()
        self.closed = asyncio.Event()
        self.reader_task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self.reader_task = asyncio.create_task(self._read(), name="ghost-cdp-reader")

    async def _read(self) -> None:
        failure = CDPError("CDP connection closed")
        try:
            async for raw in self.websocket:
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError):
                    raise CDPError("CDP returned malformed JSON") from None
                message_id = message.get("id")
                if message_id in self.pending:
                    future = self.pending.pop(message_id)
                    if message.get("error"):
                        error = message["error"]
                        future.set_exception(
                            CDPError(
                                f"CDP {error.get('code', 'error')}: "
                                f"{error.get('message', 'command failed')}"
                            )
                        )
                    else:
                        future.set_result(message.get("result") or {})
                elif message.get("method"):
                    self.events.append(message)
        except asyncio.CancelledError:
            failure = CDPError("CDP connection closed")
            raise
        except Exception as error:
            failure = error if isinstance(error, CDPError) else CDPError(
                f"CDP transport failed: {type(error).__name__}"
            )
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(failure)
            self.pending.clear()
            self.closed.set()

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float = 30,
    ) -> dict[str, Any]:
        if self.closed.is_set():
            raise CDPError("CDP connection is closed")
        loop = asyncio.get_running_loop()
        async with self.send_lock:
            self.next_id += 1
            message_id = self.next_id
            future: asyncio.Future[dict[str, Any]] = loop.create_future()
            self.pending[message_id] = future
            message: dict[str, Any] = {
                "id": message_id,
                "method": method,
                "params": params or {},
            }
            if session_id is not None:
                message["sessionId"] = session_id
            try:
                await self.websocket.send(json.dumps(message, separators=(",", ":")))
            except Exception as error:
                self.pending.pop(message_id, None)
                raise CDPError(
                    f"CDP command was not sent: {type(error).__name__}"
                ) from None
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
        except TimeoutError:
            self.pending.pop(message_id, None)
            future.cancel()
            raise CDPError(
                "CDP command timed out after it was sent; outcome is unknown"
            ) from None

    def drain_events(self) -> list[dict[str, Any]]:
        events = list(self.events)
        self.events.clear()
        return events

    async def close(self) -> None:
        with suppress(Exception):
            await self.websocket.close(code=1000, reason="Ghost Browser released")
        if self.reader_task:
            with suppress(asyncio.CancelledError, Exception):
                await self.reader_task


class BrowserDaemon:
    def __init__(
        self,
        paths: SessionPaths,
        allocation: Allocation,
        cdp: CDPConnection,
    ) -> None:
        self.paths = paths
        self.allocation = allocation
        self.cdp = cdp
        self.stop = asyncio.Event()
        self.last_activity = time.monotonic()
        self.active_session: str | None = None
        self.active_target: str | None = None
        self.attach_lock = asyncio.Lock()
        self.server: asyncio.AbstractServer | None = None

    async def ensure_page(self) -> dict[str, str]:
        async with self.attach_lock:
            if self.active_session and self.active_target:
                return {
                    "session_id": self.active_session,
                    "target_id": self.active_target,
                }
            targets = await self.cdp.send("Target.getTargets", session_id=None)
            pages = [
                target
                for target in targets.get("targetInfos", [])
                if target.get("type") == "page"
                and not str(target.get("url", "")).startswith("devtools://")
            ]
            if pages:
                target_id = pages[0]["targetId"]
            else:
                created = await self.cdp.send(
                    "Target.createTarget", {"url": "about:blank"}, session_id=None
                )
                target_id = created["targetId"]
            attached = await self.cdp.send(
                "Target.attachToTarget",
                {"targetId": target_id, "flatten": True},
                session_id=None,
            )
            self.active_target = target_id
            self.active_session = attached["sessionId"]
            return {
                "session_id": self.active_session,
                "target_id": self.active_target,
            }

    async def dispatch(self, request: dict[str, Any]) -> Any:
        operation = request.get("op")
        if operation == "ping":
            return {
                "state": "connected",
                "gateway": self.allocation.gateway_host,
                "pid": os.getpid(),
            }
        if operation == "cdp":
            self.last_activity = time.monotonic()
            return await self.cdp.send(
                request["method"],
                request.get("params"),
                session_id=request.get("session_id"),
                timeout=float(request.get("timeout", 30)),
            )
        if operation == "drain_events":
            self.last_activity = time.monotonic()
            return self.cdp.drain_events()
        if operation == "ensure_page":
            self.last_activity = time.monotonic()
            return await self.ensure_page()
        if operation == "stop":
            asyncio.get_running_loop().call_soon(self.stop.set)
            return {"released": True}
        raise RuntimeError("unknown browser daemon operation")

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        response: dict[str, Any]
        try:
            raw = await reader.readline()
            if not raw or len(raw) > MAX_MESSAGE_BYTES:
                raise RuntimeError("invalid browser daemon request")
            request = json.loads(raw)
            result = await self.dispatch(request)
            response = {"ok": True, "result": result}
        except Exception as error:
            response = {
                "ok": False,
                "error": redact(
                    error,
                    self.allocation.gateway_url,
                    self.allocation.websocket_url,
                    self.allocation.browser_id,
                ),
            }
        encoded = json.dumps(response, separators=(",", ":")).encode() + b"\n"
        if len(encoded) > MAX_MESSAGE_BYTES:
            encoded = b'{"ok":false,"error":"browser daemon response is too large"}\n'
        writer.write(encoded)
        with suppress(Exception):
            await writer.drain()
        writer.close()
        with suppress(Exception):
            await writer.wait_closed()

    async def idle_watch(self) -> None:
        idle_seconds = max(
            1.0, float(os.environ.get("GHOST_BROWSER_IDLE_SECONDS", "600"))
        )
        while not self.stop.is_set():
            remaining = idle_seconds - (time.monotonic() - self.last_activity)
            if remaining <= 0:
                self.stop.set()
                return
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=min(remaining, 1.0))
            except TimeoutError:
                pass

    async def serve(self) -> None:
        try:
            self.paths.socket.unlink()
        except FileNotFoundError:
            pass
        self.server = await asyncio.start_unix_server(
            self.handle,
            path=os.fspath(self.paths.socket),
            limit=MAX_MESSAGE_BYTES,
        )
        self.paths.socket.chmod(0o600)
        idle = asyncio.create_task(self.idle_watch(), name="ghost-idle-release")
        closed = asyncio.create_task(self.cdp.closed.wait(), name="ghost-cdp-closed")
        stopped = asyncio.create_task(self.stop.wait(), name="ghost-stop")
        done, pending = await asyncio.wait(
            {idle, closed, stopped}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in done | pending:
            with suppress(asyncio.CancelledError, Exception):
                await task
        self.server.close()
        await self.server.wait_closed()


async def run() -> None:
    paths = session_paths()
    allocation: Allocation | None = None
    cdp: CDPConnection | None = None
    started = False
    paths.pid.write_text(str(os.getpid()), encoding="ascii")
    paths.pid.chmod(0o600)
    try:
        timeout = float(os.environ.get("GHOST_BROWSER_ALLOCATION_TIMEOUT", "180"))
        allocation = await asyncio.to_thread(
            allocate_browser, gateway_url_from_env(), timeout=timeout
        )
        websocket = await connect(
            allocation.websocket_url,
            open_timeout=float(os.environ.get("GHOST_BROWSER_WS_TIMEOUT", "30")),
            close_timeout=10,
            ping_interval=30,
            ping_timeout=30,
            max_size=32 * 1024 * 1024,
            proxy=None,
        )
        cdp = CDPConnection(websocket)
        cdp.start()
        daemon = BrowserDaemon(paths, allocation, cdp)
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            with suppress(NotImplementedError):
                loop.add_signal_handler(signum, daemon.stop.set)
        started = True
        await daemon.serve()
    except Exception as error:
        if not started:
            paths.startup_error.write_text(
                redact(
                    error,
                    allocation.gateway_url if allocation else None,
                    allocation.websocket_url if allocation else None,
                    allocation.browser_id if allocation else None,
                ),
                encoding="utf-8",
            )
            paths.startup_error.chmod(0o600)
    finally:
        if cdp:
            await cdp.close()
        if allocation:
            with suppress(Exception):
                await asyncio.to_thread(release_browser, allocation)
        for path in (paths.socket, paths.pid):
            with suppress(FileNotFoundError):
                path.unlink()


def main() -> None:
    if os.name != "posix":
        raise SystemExit("Ghost Browser v0.1 requires a POSIX system")
    asyncio.run(run())


if __name__ == "__main__":
    main()
