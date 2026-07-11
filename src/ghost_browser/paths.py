"""Private filesystem locations for one workspace-scoped browser daemon."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SessionPaths:
    socket: Path
    lock: Path
    pid: Path
    startup_error: Path
    stop_requested: Path
    shutdown_result: Path
    workspace: Path


def _private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("Ghost Browser state path must be a real directory")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise RuntimeError("Ghost Browser state path must be owned by this user")
    path.chmod(0o700)
    return path


def _home() -> Path:
    configured = os.environ.get("GHOST_BROWSER_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return (config / "ghost-browser").resolve()


def _runtime() -> Path:
    configured = os.environ.get("GHOST_BROWSER_RUNTIME_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return (Path(xdg) / "ghost-browser").resolve()
    uid = os.getuid() if hasattr(os, "getuid") else "user"
    return Path(tempfile.gettempdir(), f"ghost-browser-{uid}").resolve()


def session_paths(
    cwd: str | os.PathLike[str] | None = None, *, create: bool = True
) -> SessionPaths:
    """Return secret-free paths unique to workspace, name, gateway, and caller."""

    workspace_root = Path(cwd or os.getcwd()).resolve()
    gateway = os.environ.get("GHOST_GATEWAY_URL") or os.environ.get(
        "GHOST_STANDBY_URL", ""
    )
    token_digest = hashlib.sha256(
        os.environ.get("APIFY_TOKEN", "").encode()
    ).hexdigest()
    identity = json.dumps(
        [
            str(workspace_root),
            os.environ.get("GHOST_BROWSER_NAME", "default"),
            gateway,
            token_digest,
        ],
        separators=(",", ":"),
    )
    key = hashlib.sha256(identity.encode()).hexdigest()[:24]
    runtime = _runtime()
    home = _home()
    if create:
        runtime = _private_directory(runtime)
        home = _private_directory(home)
    workspace = home / "agent-workspaces" / key
    if create:
        workspace = _private_directory(workspace)
    prefix = runtime / f"ghost-{key}"
    socket_path = Path(f"{prefix}.sock")
    if len(os.fsencode(socket_path)) >= 100:
        uid = os.getuid() if hasattr(os, "getuid") else "user"
        short_runtime = Path(
            tempfile.gettempdir(), f"ghost-browser-{uid}"
        ).resolve()
        if create:
            short_runtime = _private_directory(short_runtime)
        socket_path = short_runtime / f"ghost-{key}.sock"
    return SessionPaths(
        socket=socket_path,
        lock=Path(f"{prefix}.lock"),
        pid=Path(f"{prefix}.pid"),
        startup_error=Path(f"{prefix}.error"),
        stop_requested=Path(f"{prefix}.stop"),
        shutdown_result=Path(f"{prefix}.result"),
        workspace=workspace,
    )
