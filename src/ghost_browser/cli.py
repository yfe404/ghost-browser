"""Command-line entry point for Python-as-browser-action."""

from __future__ import annotations

import os
import sys
import time
from importlib import resources

from . import __version__
from .client import BrowserClient, script_namespace
from .ipc import (
    daemon_locked,
    ensure_daemon,
    ping,
    read_shutdown_result,
    request,
    request_startup_stop,
    wait_until_stopped,
)
from .paths import session_paths
from .redaction import redact
from .workspace import ensure_agent_helpers, load_agent_helpers


USAGE = """Ghost Browser — Python directly over a private Ghost CDP session.

Usage:
  ghost-browser                  execute Python from stdin
  ghost-browser status           show sanitized session status
  ghost-browser stop             release browser and stop the local daemon
  ghost-browser skill            print the optional agent skill
  ghost-browser workspace        print the editable helper directory
  ghost-browser --version        print the installed version

Python primitives: cdp, drain_events, ensure_page, js, page_info, tabs,
capture_screenshot. Browser actions remain ordinary Python and raw CDP.
"""


def _print_error(error: object) -> None:
    print(
        f"ERROR: {redact(error, os.environ.get('GHOST_GATEWAY_URL'), os.environ.get('GHOST_STANDBY_URL'))}",
        file=sys.stderr,
    )


def _stop(paths) -> int:
    running = ping(paths)
    if running is not None:
        request(paths, {"op": "stop"}, timeout=5)
    elif daemon_locked(paths):
        previous = read_shutdown_result(paths)
        previous_marker = _result_marker(paths)
        request_startup_stop(paths)
        if previous and not previous.get("released"):
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if (
                    not daemon_locked(paths)
                    or _result_marker(paths) != previous_marker
                ):
                    break
                time.sleep(0.05)
            result = read_shutdown_result(paths)
            if result and result.get("released"):
                wait_until_stopped(paths, timeout=3)
                print("released browser")
                return 0
            raise RuntimeError(
                (result or {}).get("error")
                or "browser release outcome is unknown"
            )
    else:
        previous = read_shutdown_result(paths)
        if previous and not previous.get("released"):
            raise RuntimeError(
                previous.get("error") or "browser release outcome is unknown"
            )
        print("browser already stopped")
        return 0
    wait_until_stopped(paths)
    result = read_shutdown_result(paths)
    if not result or not result.get("released"):
        raise RuntimeError(
            (result or {}).get("error") or "browser release outcome is unknown"
        )
    print("released browser")
    return 0


def _result_marker(paths) -> tuple[int, int] | None:
    try:
        info = paths.shutdown_result.stat()
        return info.st_ino, info.st_mtime_ns
    except OSError:
        return None


def _skill_text() -> str:
    try:
        return resources.files("ghost_browser").joinpath("SKILL.md").read_text(
            encoding="utf-8"
        )
    except (FileNotFoundError, ModuleNotFoundError):
        raise RuntimeError("packaged skill is unavailable") from None


def main(argv: list[str] | None = None) -> int:
    if os.name != "posix":
        _print_error("Ghost Browser v0.1 requires a POSIX system")
        return 1
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if arguments == ["--version"]:
            print(__version__)
            return 0
        if arguments == ["skill"]:
            print(_skill_text(), end="")
            return 0
        read_only = arguments in (["status"], ["stop"])
        paths = session_paths(create=not read_only)
        if arguments == ["status"]:
            state = ping(paths)
            if state is not None:
                print(f"connected {state['gateway']}")
            elif (result := read_shutdown_result(paths)) and not result.get(
                "released"
            ):
                print("release-failed")
            elif daemon_locked(paths):
                print("starting")
            else:
                print("stopped")
            return 0
        if arguments == ["stop"]:
            return _stop(paths)
        if arguments == ["workspace"]:
            ensure_agent_helpers(paths)
            print(paths.workspace)
            return 0
        if arguments:
            print(USAGE, file=sys.stderr, end="")
            return 2

        code = sys.stdin.read()
        if not code.strip():
            print(USAGE, file=sys.stderr, end="")
            return 2
        compiled = compile(code, "<ghost-browser>", "exec")
        ensure_daemon(paths)
        namespace = script_namespace(BrowserClient(paths))
        namespace.update({"__name__": "__main__", "__file__": "<ghost-browser>"})
        load_agent_helpers(ensure_agent_helpers(paths), namespace)
        exec(compiled, namespace)
        return 0
    except Exception as error:
        _print_error(error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
