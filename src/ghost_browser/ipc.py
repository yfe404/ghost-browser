"""Owner-only Unix-socket IPC and daemon startup."""

from __future__ import annotations

import fcntl
import json
import os
import socket
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from .paths import SessionPaths
from .release_state import retry_pending_release
from .settings import startup_wait_timeout, stop_wait_timeout


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


def daemon_locked(paths: SessionPaths) -> bool:
    """Whether a starting or connected daemon owns the lifetime lock."""

    try:
        lock = paths.lock.open("r+")
    except OSError:
        return False
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(lock, fcntl.LOCK_UN)
        return False
    finally:
        lock.close()


def request_startup_stop(paths: SessionPaths) -> None:
    paths.stop_requested.touch(mode=0o600, exist_ok=True)
    paths.stop_requested.chmod(0o600)


def read_shutdown_result(paths: SessionPaths) -> dict[str, Any] | None:
    try:
        result = json.loads(paths.shutdown_result.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return None
    return result if isinstance(result, dict) else None


def _startup_deadline() -> float:
    return time.monotonic() + startup_wait_timeout()


def _wait_for_existing(paths: SessionPaths, deadline: float) -> dict[str, Any]:
    while time.monotonic() < deadline:
        if running := ping(paths):
            return running
        if not daemon_locked(paths):
            detail = _read_startup_error(paths.startup_error)
            raise IPCError(detail or "browser daemon stopped during startup")
        time.sleep(0.05)
    raise IPCError("browser daemon is running but did not become ready")


def ensure_daemon(paths: SessionPaths) -> dict[str, Any]:
    """Start one daemon under an exclusive per-session lock, then wait for it."""

    if running := ping(paths):
        return running
    paths.lock.touch(mode=0o600, exist_ok=True)
    with paths.lock.open("r+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return _wait_for_existing(paths, _startup_deadline())
        if running := ping(paths):
            return running
        try:
            retry_pending_release(paths)
        except Exception as error:
            raise IPCError(str(error)) from None
        for stale in (
            paths.socket,
            paths.startup_error,
            paths.stop_requested,
            paths.shutdown_result,
        ):
            with suppress(FileNotFoundError):
                stale.unlink()
        child_env = os.environ.copy()
        child_env["GHOST_BROWSER_LOCK_FD"] = str(lock.fileno())
        child_env["GHOST_BROWSER_PARENT_PID"] = str(os.getpid())
        process = subprocess.Popen(
            [sys.executable, "-m", "ghost_browser.daemon"],
            cwd=os.getcwd(),
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            pass_fds=(lock.fileno(),),
            start_new_session=True,
        )
        try:
            deadline = _startup_deadline()
            while time.monotonic() < deadline:
                if running := ping(paths):
                    return running
                if process.poll() is not None:
                    detail = _read_startup_error(paths.startup_error)
                    raise IPCError(detail or "browser daemon failed to start")
                time.sleep(0.05)
            request_startup_stop(paths)
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5)
            raise IPCError("browser daemon did not become ready before timeout")
        except BaseException:
            if process.poll() is None:
                request_startup_stop(paths)
            raise


def wait_until_stopped(
    paths: SessionPaths, *, timeout: float | None = None
) -> None:
    if timeout is None:
        timeout = stop_wait_timeout()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (
            ping(paths) is None
            and not paths.socket.exists()
            and not daemon_locked(paths)
        ):
            return
        time.sleep(0.05)
    raise IPCError("browser daemon did not stop before timeout")
