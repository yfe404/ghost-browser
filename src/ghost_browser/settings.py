"""Validated lifecycle timings shared by the launcher and daemon."""

from __future__ import annotations

import math
import os


def _seconds(name: str, default: float) -> float:
    raw = os.environ.get(name)
    try:
        value = default if raw is None else float(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be a positive number") from None
    if not math.isfinite(value) or value <= 0:
        raise RuntimeError(f"{name} must be a positive number")
    return value


def allocation_timeout() -> float:
    return _seconds("GHOST_BROWSER_ALLOCATION_TIMEOUT", 180)


def websocket_timeout() -> float:
    return _seconds("GHOST_BROWSER_WS_TIMEOUT", 30)


def idle_timeout() -> float:
    return _seconds("GHOST_BROWSER_IDLE_SECONDS", 600)


def startup_wait_timeout() -> float:
    return allocation_timeout() + websocket_timeout() + 10


def stop_wait_timeout() -> float:
    return allocation_timeout() + websocket_timeout() + 15
