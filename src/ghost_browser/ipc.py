"""Owner-only Unix-socket IPC and daemon startup."""

from __future__ import annotations

import fcntl
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .paths import SessionPaths


MAX_MESSAGE_BYTES = 64 * 1024 * 1024


class IPCError(RuntimeError):
    """A safe daemon or IPC failure."""


def request(
    paths: SessionPaths,
    payload: dict[str, Any],
    *,
    timeout: float = 30,
) -> Any:
    encoded = json.dumps(payload, separators=(",", ":")).encode() + b"\n"
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise IPCError("request is too large")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(os.fspath(paths.socket))
        client.sendall(encoded)
        chunks = bytearray()
        while b"\n" not in chunks:
            chunk = client.recv(min(65_536, MAX_MESSAGE_BYTES + 1 - len(chunks)))
            if not chunk:
                raise IPCError("browser daemon closed without a response")
            chunks.extend(chunk)
            if len(chunks) > MAX_MESSAGE_BYTES:
                raise IPCError("browser daemon response is too large")
    except (FileNotFoundError, ConnectionRefusedError, socket.timeout, OSError) as error:
        raise IPCError(f"browser daemon unavailable: {type(error).__name__}") from None
    finally:
        client.close()
    try:
        response = json.loads(bytes(chunks).split(b"\n", 1)[0])
    except (TypeError, ValueError):
        raise IPCError("browser daemon returned malformed JSON") from None
    if not response.get("ok"):
        raise IPCError(response.get("error") or "browser daemon command failed")
    return response.get("result")


def ping(paths: SessionPaths, *, timeout: float = 0.5) -> dict[str, Any] | None:
    try:
        result = request(paths, {"op": "ping"}, timeout=timeout)
    except IPCError:
        return None
    return result if isinstance(result, dict) else None


def _read_startup_error(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def ensure_daemon(paths: SessionPaths) -> dict[str, Any]:
    """Start one daemon under an exclusive per-session lock, then wait for it."""

    if running := ping(paths):
        return running
    paths.lock.touch(mode=0o600, exist_ok=True)
    with paths.lock.open("r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if running := ping(paths):
            return running
        try:
            paths.socket.unlink()
        except FileNotFoundError:
            pass
        try:
            paths.startup_error.unlink()
        except FileNotFoundError:
            pass
        process = subprocess.Popen(
            [sys.executable, "-m", "ghost_browser.daemon"],
            cwd=os.getcwd(),
            env=os.environ.copy(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
        allocation_timeout = float(
            os.environ.get("GHOST_BROWSER_ALLOCATION_TIMEOUT", "180")
        )
        deadline = time.monotonic() + allocation_timeout + 40
        while time.monotonic() < deadline:
            if running := ping(paths):
                return running
            if process.poll() is not None:
                detail = _read_startup_error(paths.startup_error)
                raise IPCError(detail or "browser daemon failed to start")
            time.sleep(0.05)
        raise IPCError("browser daemon did not become ready before timeout")


def wait_until_stopped(paths: SessionPaths, *, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ping(paths) is None and not paths.socket.exists():
            return
        time.sleep(0.05)
    raise IPCError("browser daemon did not stop before timeout")
