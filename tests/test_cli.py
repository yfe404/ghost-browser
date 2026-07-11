import json
import os
import subprocess
import sys
import threading
import time
from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from websockets.sync.server import serve


@contextmanager
def fake_cdp():
    messages = []
    connections = []

    def handler(websocket):
        connections.append(websocket)
        for raw in websocket:
            request = json.loads(raw)
            messages.append(request)
            method = request["method"]
            if method == "Target.getTargets":
                result = {
                    "targetInfos": [
                        {
                            "targetId": "page-1",
                            "type": "page",
                            "title": "Blank",
                            "url": "about:blank",
                        }
                    ]
                }
            elif method == "Target.attachToTarget":
                result = {"sessionId": "session-1"}
            elif method == "Browser.getVersion":
                result = {"product": "Ghost/1.0"}
            elif method == "Runtime.evaluate":
                websocket.send(
                    json.dumps(
                        {
                            "method": "Page.loadEventFired",
                            "params": {"timestamp": 1},
                            "sessionId": "session-1",
                        }
                    )
                )
                result = {"result": {"type": "string", "value": "Example"}}
            elif method == "Test.neverRespond":
                continue
            else:
                result = {}
            websocket.send(json.dumps({"id": request["id"], "result": result}))

    server = serve(handler, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.socket.getsockname()[1]
        yield f"ws://127.0.0.1:{port}/devtools/browser/opaque?token=ws-secret", messages, connections
    finally:
        server.shutdown()
        thread.join()


@contextmanager
def fake_gateway(websocket_url):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.command, self.path))
            resolved_websocket = websocket_url or (
                f"ws://127.0.0.1:{self.server.server_port}"
                "/devtools/browser/opaque?token=opaque-run-secret"
            )
            body = json.dumps({"webSocketDebuggerUrl": resolved_websocket}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Ghost-Session", "browser-42")
            self.end_headers()
            self.wfile.write(body)

        def do_DELETE(self):
            requests.append((self.command, self.path))
            self.send_response(204)
            self.end_headers()

        def log_message(self, _format, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def run_cli(env, *args, code=None):
    return subprocess.run(
        [sys.executable, "-m", "ghost_browser.cli", *args],
        input=code,
        text=True,
        capture_output=True,
        env=env,
        timeout=20,
        check=False,
    )


def test_cli_reuses_one_raw_cdp_connection_and_stop_releases_it(tmp_path):
    with ExitStack() as stack:
        websocket_url, messages, connections = stack.enter_context(fake_cdp())
        gateway_url, gateway_requests = stack.enter_context(
            fake_gateway(websocket_url)
        )
        env = {
            **os.environ,
            "APIFY_TOKEN": "caller-secret",
            "GHOST_GATEWAY_URL": gateway_url,
            "GHOST_BROWSER_HOME": str(tmp_path / "home"),
            "GHOST_BROWSER_RUNTIME_DIR": str(tmp_path / "runtime"),
            "GHOST_BROWSER_IDLE_SECONDS": "60",
        }
        try:
            first = run_cli(
                env,
                code='print(cdp("Browser.getVersion", session_id=None)["product"])',
            )
            second = run_cli(env, code='print(js("document.title"))')
            event = run_cli(env, code='print(drain_events()[0]["method"])')
            stopped = run_cli(env, "stop")
        finally:
            run_cli(env, "stop")

    assert (first.returncode, first.stdout, first.stderr) == (0, "Ghost/1.0\n", "")
    assert (second.returncode, second.stdout, second.stderr) == (0, "Example\n", "")
    assert (event.returncode, event.stdout, event.stderr) == (
        0,
        "Page.loadEventFired\n",
        "",
    )
    assert (stopped.returncode, stopped.stdout, stopped.stderr) == (
        0,
        "released browser\n",
        "",
    )
    assert sum(method == "GET" for method, _path in gateway_requests) == 1
    assert sum(method == "DELETE" for method, _path in gateway_requests) == 1
    assert len(connections) == 1
    assert any(message["method"] == "Browser.getVersion" for message in messages)
    assert any(message["method"] == "Runtime.evaluate" for message in messages)


def test_failed_websocket_handshake_is_redacted_and_released(tmp_path):
    with fake_gateway(None) as (gateway_url, gateway_requests):
        env = {
            **os.environ,
            "APIFY_TOKEN": "caller-super-secret",
            "GHOST_GATEWAY_URL": gateway_url,
            "GHOST_BROWSER_HOME": str(tmp_path / "home"),
            "GHOST_BROWSER_RUNTIME_DIR": str(tmp_path / "runtime"),
            "GHOST_BROWSER_ALLOCATION_TIMEOUT": "2",
            "GHOST_BROWSER_WS_TIMEOUT": "2",
        }
        failed = run_cli(env, code='print("never reached")')

    assert failed.returncode == 1
    assert failed.stdout == ""
    assert "caller-super-secret" not in failed.stderr
    assert "opaque-run-secret" not in failed.stderr
    assert "token=" not in failed.stderr
    assert sum(
        method == "GET" and path.startswith("/json/version")
        for method, path in gateway_requests
    ) == 1
    assert sum(method == "DELETE" for method, _path in gateway_requests) == 1


def test_syntax_error_is_reported_before_browser_allocation(tmp_path):
    with fake_gateway(None) as (gateway_url, gateway_requests):
        env = {
            **os.environ,
            "APIFY_TOKEN": "caller-secret",
            "GHOST_GATEWAY_URL": gateway_url,
            "GHOST_BROWSER_HOME": str(tmp_path / "home"),
            "GHOST_BROWSER_RUNTIME_DIR": str(tmp_path / "runtime"),
        }
        failed = run_cli(env, code="if :")

    assert failed.returncode == 1
    assert "invalid syntax" in failed.stderr
    assert gateway_requests == []


def test_editable_agent_helpers_are_seeded_and_loaded(tmp_path):
    with ExitStack() as stack:
        websocket_url, _messages, _connections = stack.enter_context(fake_cdp())
        gateway_url, gateway_requests = stack.enter_context(
            fake_gateway(websocket_url)
        )
        env = {
            **os.environ,
            "APIFY_TOKEN": "caller-secret",
            "GHOST_GATEWAY_URL": gateway_url,
            "GHOST_BROWSER_HOME": str(tmp_path / "home"),
            "GHOST_BROWSER_RUNTIME_DIR": str(tmp_path / "runtime"),
        }
        workspace_result = run_cli(env, "workspace")
        workspace = workspace_result.stdout.strip()
        helper_path = os.path.join(workspace, "agent_helpers.py")
        seed = open(helper_path, encoding="utf-8").read()
        with open(helper_path, "w", encoding="utf-8") as helper_file:
            helper_file.write("def helper_value():\n    return 'editable'\n")
        try:
            executed = run_cli(env, code="print(helper_value())")
        finally:
            run_cli(env, "stop")

    assert workspace_result.returncode == 0
    assert "Input.dispatchMouseEvent" in seed
    assert "mouseMoved" in seed
    assert "Input.dispatchKeyEvent" in seed
    assert "Input.insertText" not in seed
    assert (executed.returncode, executed.stdout, executed.stderr) == (
        0,
        "editable\n",
        "",
    )
    assert sum(method == "GET" for method, _path in gateway_requests) == 1


def test_idle_daemon_releases_the_browser(tmp_path):
    with ExitStack() as stack:
        websocket_url, _messages, _connections = stack.enter_context(fake_cdp())
        gateway_url, gateway_requests = stack.enter_context(
            fake_gateway(websocket_url)
        )
        env = {
            **os.environ,
            "APIFY_TOKEN": "caller-secret",
            "GHOST_GATEWAY_URL": gateway_url,
            "GHOST_BROWSER_HOME": str(tmp_path / "home"),
            "GHOST_BROWSER_RUNTIME_DIR": str(tmp_path / "runtime"),
            "GHOST_BROWSER_IDLE_SECONDS": "1",
        }
        started = run_cli(
            env,
            code='print(cdp("Browser.getVersion", session_id=None)["product"])',
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if any(method == "DELETE" for method, _path in gateway_requests):
                break
            time.sleep(0.05)
        status = run_cli(env, "status")

    assert started.returncode == 0
    assert status.stdout == "stopped\n"
    assert sum(method == "DELETE" for method, _path in gateway_requests) == 1


def test_concurrent_first_calls_share_one_allocation(tmp_path):
    with ExitStack() as stack:
        websocket_url, _messages, connections = stack.enter_context(fake_cdp())
        gateway_url, gateway_requests = stack.enter_context(
            fake_gateway(websocket_url)
        )
        env = {
            **os.environ,
            "APIFY_TOKEN": "caller-secret",
            "GHOST_GATEWAY_URL": gateway_url,
            "GHOST_BROWSER_HOME": str(tmp_path / "home"),
            "GHOST_BROWSER_RUNTIME_DIR": str(tmp_path / "runtime"),
        }
        command = [sys.executable, "-m", "ghost_browser.cli"]
        processes = [
            subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            for _ in range(2)
        ]
        try:
            results = [
                process.communicate(
                    'print(cdp("Browser.getVersion", session_id=None)["product"])',
                    timeout=20,
                )
                for process in processes
            ]
        finally:
            run_cli(env, "stop")

    assert all(process.returncode == 0 for process in processes)
    assert sorted(stdout for stdout, _stderr in results) == ["Ghost/1.0\n"] * 2
    assert all(stderr == "" for _stdout, stderr in results)
    assert sum(
        method == "GET" and path.startswith("/json/version")
        for method, path in gateway_requests
    ) == 1
    assert len(connections) == 1


def test_skill_and_status_do_not_allocate_a_browser(tmp_path):
    with fake_gateway(None) as (gateway_url, gateway_requests):
        env = {
            **os.environ,
            "APIFY_TOKEN": "caller-secret",
            "GHOST_GATEWAY_URL": gateway_url,
            "GHOST_BROWSER_HOME": str(tmp_path / "home"),
            "GHOST_BROWSER_RUNTIME_DIR": str(tmp_path / "runtime"),
        }
        skill = run_cli(env, "skill")
        status = run_cli(env, "status")

    assert skill.returncode == 0
    assert "name: ghost-browser" in skill.stdout
    assert "raw Chrome DevTools Protocol" in skill.stdout
    assert skill.stderr == ""
    assert (status.returncode, status.stdout, status.stderr) == (
        0,
        "stopped\n",
        "",
    )
    assert gateway_requests == []


def test_arbitrary_agent_exceptions_are_redacted(tmp_path):
    with ExitStack() as stack:
        websocket_url, _messages, _connections = stack.enter_context(fake_cdp())
        gateway_url, _gateway_requests = stack.enter_context(
            fake_gateway(websocket_url)
        )
        env = {
            **os.environ,
            "APIFY_TOKEN": "caller-secret",
            "GHOST_GATEWAY_URL": gateway_url,
            "GHOST_BROWSER_HOME": str(tmp_path / "home"),
            "GHOST_BROWSER_RUNTIME_DIR": str(tmp_path / "runtime"),
        }
        try:
            failed = run_cli(
                env,
                code=(
                    'raise KeyError("caller-secret at '
                    'wss://run.example/path?token=opaque-secret")'
                ),
            )
        finally:
            run_cli(env, "stop")

    assert failed.returncode == 1
    assert "caller-secret" not in failed.stderr
    assert "opaque-secret" not in failed.stderr
    assert "token=" not in failed.stderr
    assert "<redacted>" in failed.stderr


def test_sent_command_timeout_is_not_replayed(tmp_path):
    with ExitStack() as stack:
        websocket_url, messages, _connections = stack.enter_context(fake_cdp())
        gateway_url, _gateway_requests = stack.enter_context(
            fake_gateway(websocket_url)
        )
        env = {
            **os.environ,
            "APIFY_TOKEN": "caller-secret",
            "GHOST_GATEWAY_URL": gateway_url,
            "GHOST_BROWSER_HOME": str(tmp_path / "home"),
            "GHOST_BROWSER_RUNTIME_DIR": str(tmp_path / "runtime"),
        }
        try:
            failed = run_cli(
                env,
                code='cdp("Test.neverRespond", timeout=0.1)',
            )
        finally:
            run_cli(env, "stop")

    assert failed.returncode == 1
    assert "outcome is unknown" in failed.stderr
    assert sum(
        message["method"] == "Test.neverRespond" for message in messages
    ) == 1


def test_seed_input_helpers_use_ghost_humanization_path(tmp_path):
    with ExitStack() as stack:
        websocket_url, messages, _connections = stack.enter_context(fake_cdp())
        gateway_url, _gateway_requests = stack.enter_context(
            fake_gateway(websocket_url)
        )
        env = {
            **os.environ,
            "APIFY_TOKEN": "caller-secret",
            "GHOST_GATEWAY_URL": gateway_url,
            "GHOST_BROWSER_HOME": str(tmp_path / "home"),
            "GHOST_BROWSER_RUNTIME_DIR": str(tmp_path / "runtime"),
        }
        try:
            executed = run_cli(env, code='click_at(10, 20)\ntype_text("ab")')
        finally:
            run_cli(env, "stop")

    assert executed.returncode == 0
    inputs = [
        message
        for message in messages
        if message["method"].startswith("Input.dispatch")
    ]
    assert [message["params"]["type"] for message in inputs] == [
        "mouseMoved",
        "mousePressed",
        "mouseReleased",
        "keyDown",
        "keyUp",
        "keyDown",
        "keyUp",
    ]
    assert all(message.get("sessionId") == "session-1" for message in inputs)
