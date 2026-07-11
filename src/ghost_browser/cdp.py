"""Raw Chrome DevTools Protocol correlation over one WebSocket."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from contextlib import suppress
from typing import Any


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

    async def close(self) -> bool:
        """Request a normal close and report whether code 1000 was confirmed."""

        close_error: Exception | None = None
        try:
            await self.websocket.close(code=1000, reason="Ghost Browser released")
        except Exception as error:
            close_error = error
        if self.reader_task:
            with suppress(asyncio.CancelledError, Exception):
                await self.reader_task
        if close_error:
            raise close_error
        return self.websocket.close_code == 1000
