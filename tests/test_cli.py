import json
import os
import select
import signal
import socket
import subprocess
import sys
import threading
import time
from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from websockets.sync.server import serve


@contextmanager
def fake_cdp(*, disconnect_abnormally_after=None):
    messages = []
    connections = []

    def handler(websocket):
        connections.append(websocket)
        for raw in websocket:
            request = json.loads(raw)
            messages.append(request)
            method = request["method"]
            if method == disconnect_abnormally_after:
                websocket.close_socket()
                return
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
            elif method == "Test.respondLate":
                time.sleep(1.3)
                result = {"ok": True}
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
def fake_gateway(
    websocket_url, *, allocation_delay=0, delete_status=204, raw_body=None
):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.headers.get("Upgrade", "").lower() == "websocket":
                self._proxy_websocket()
                return
            requests.append((self.command, self.path))
            if self.path.startswith("/json/version") and allocation_delay:
                time.sleep(allocation_delay)
            if websocket_url:
                upstream = urlsplit(websocket_url)
                resolved_websocket = (
                    f"ws://127.0.0.1:{self.server.server_port}"
                    f"{upstream.path}"
                    f"?{upstream.query}"
                )
            else:
                resolved_websocket = (
                    f"ws://127.0.0.1:{self.server.server_port}"
                    "/devtools/browser/opaque?token=opaque-run-secret"
                )
            body = (
                raw_body
                if raw_body is not None
                else json.dumps(
                    {"webSocketDebuggerUrl": resolved_websocket}
                ).encode()
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Ghost-Session", "browser-42")
            self.end_headers()
            self.wfile.write(body)

        def _proxy_websocket(self):
            if not websocket_url:
                self.send_error(502)
                return
            target = urlsplit(websocket_url)
            upstream = socket.create_connection(
                (target.hostname, target.port), timeout=2
            )
            try:
                path = target.path or "/"
                if target.query:
                    path += f"?{target.query}"
                headers = [f"GET {path} HTTP/1.1", f"Host: {target.netloc}"]
                headers.extend(
                    f"{key}: {value}"
                    for key, value in self.headers.items()
                    if key.lower() != "host"
                )
                upstream.sendall(("\r\n".join(headers) + "\r\n\r\n").encode())
                response = bytearray()
                while b"\r\n\r\n" not in response:
                    chunk = upstream.recv(4096)
                    if not chunk:
                        return
                    response.extend(chunk)
                self.connection.sendall(response)
                peers = {
                    self.connection: upstream,
                    upstream: self.connection,
                }
                while True:
                    readable, _writable, _errors = select.select(peers, [], [], 5)
                    if not readable:
                        continue
                    for source in readable:
                        data = source.recv(65_536)
                        if not data:
                            return
                        peers[source].sendall(data)
            finally:
                upstream.close()

        def do_DELETE(self):
            requests.append((self.command, self.path))
            status = delete_status() if callable(delete_status) else delete_status
            self.send_response(status)
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
    assert "Project-scoped helpers" in seed
    assert "def click" not in seed
    assert "def type" not in seed
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
        version = run_cli(env, "--version")

    assert skill.returncode == 0
    assert "name: ghost-browser" in skill.stdout
    assert "raw Chrome DevTools Protocol" in skill.stdout
    assert skill.stderr == ""
    assert (status.returncode, status.stdout, status.stderr) == (
        0,
        "stopped\n",
        "",
    )
    assert (version.returncode, version.stdout, version.stderr) == (
        0,
        "0.1.0\n",
        "",
    )
    assert gateway_requests == []
    assert not (tmp_path / "home").exists()
    assert not (tmp_path / "runtime").exists()


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
                    'wss://run.example/path?token=opaque-secret&region=eu")'
                ),
            )
        finally:
            run_cli(env, "stop")

    assert failed.returncode == 1
    assert "caller-secret" not in failed.stderr
    assert "opaque-secret" not in failed.stderr
    assert "token=" not in failed.stderr
    assert "region=eu" not in failed.stderr
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


def test_stop_cancels_a_cold_start_before_websocket_connect(tmp_path):
    with ExitStack() as stack:
        websocket_url, _messages, connections = stack.enter_context(fake_cdp())
        gateway_url, gateway_requests = stack.enter_context(
            fake_gateway(websocket_url, allocation_delay=0.8)
        )
        env = {
            **os.environ,
            "APIFY_TOKEN": "caller-secret",
            "GHOST_GATEWAY_URL": gateway_url,
            "GHOST_BROWSER_HOME": str(tmp_path / "home"),
            "GHOST_BROWSER_RUNTIME_DIR": str(tmp_path / "runtime"),
            "GHOST_BROWSER_ALLOCATION_TIMEOUT": "3",
        }
        process = subprocess.Popen(
            [sys.executable, "-m", "ghost_browser.cli"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        process.stdin.write('print(cdp("Browser.getVersion")["product"])')
        process.stdin.close()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if any(
                method == "GET" and path.startswith("/json/version")
                for method, path in gateway_requests
            ):
                break
            time.sleep(0.02)
        starting = run_cli(env, "status")
        stopped = run_cli(env, "stop")
        process.wait(timeout=10)
        launch_stderr = process.stderr.read()

    assert starting.stdout == "starting\n"
    assert (stopped.returncode, stopped.stdout, stopped.stderr) == (
        0,
        "released browser\n",
        "",
    )
    assert process.returncode == 1
    assert "cancelled" in launch_stderr
    assert len(connections) == 0
    assert sum(method == "DELETE" for method, _path in gateway_requests) == 1


def test_idle_deadline_waits_for_an_active_command(tmp_path):
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
            "GHOST_BROWSER_IDLE_SECONDS": "1",
        }
        try:
            executed = run_cli(
                env,
                code='print(cdp("Test.respondLate", timeout=3)["ok"])',
            )
        finally:
            run_cli(env, "stop")

    assert (executed.returncode, executed.stdout, executed.stderr) == (
        0,
        "True\n",
        "",
    )


def test_launcher_death_cancels_cold_start_and_releases_allocation(tmp_path):
    with ExitStack() as stack:
        websocket_url, _messages, connections = stack.enter_context(fake_cdp())
        gateway_url, gateway_requests = stack.enter_context(
            fake_gateway(websocket_url, allocation_delay=0.8)
        )
        env = {
            **os.environ,
            "APIFY_TOKEN": "caller-secret",
            "GHOST_GATEWAY_URL": gateway_url,
            "GHOST_BROWSER_HOME": str(tmp_path / "home"),
            "GHOST_BROWSER_RUNTIME_DIR": str(tmp_path / "runtime"),
            "GHOST_BROWSER_ALLOCATION_TIMEOUT": "3",
        }
        process = subprocess.Popen(
            [sys.executable, "-m", "ghost_browser.cli"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        process.stdin.write('print(cdp("Browser.getVersion")["product"])')
        process.stdin.close()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if any(
                method == "GET" and path.startswith("/json/version")
                for method, path in gateway_requests
            ):
                break
            time.sleep(0.02)
        process.kill()
        process.wait(timeout=5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if any(method == "DELETE" for method, _path in gateway_requests):
                break
            time.sleep(0.05)
        status = run_cli(env, "status")

    assert process.returncode != 0
    assert status.stdout == "stopped\n"
    assert len(connections) == 0
    assert sum(method == "DELETE" for method, _path in gateway_requests) == 1


def test_daemon_signal_uses_the_release_path(tmp_path):
    with ExitStack() as stack:
        websocket_url, _messages, _connections = stack.enter_context(fake_cdp())
        gateway_url, gateway_requests = stack.enter_context(
            fake_gateway(websocket_url)
        )
        runtime = tmp_path / "runtime"
        env = {
            **os.environ,
            "APIFY_TOKEN": "caller-secret",
            "GHOST_GATEWAY_URL": gateway_url,
            "GHOST_BROWSER_HOME": str(tmp_path / "home"),
            "GHOST_BROWSER_RUNTIME_DIR": str(runtime),
        }
        started = run_cli(
            env,
            code='print(cdp("Browser.getVersion")["product"])',
        )
        pid_file = next(runtime.glob("*.pid"))
        os.kill(int(pid_file.read_text(encoding="ascii")), signal.SIGTERM)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if any(method == "DELETE" for method, _path in gateway_requests):
                break
            time.sleep(0.05)
        status = run_cli(env, "status")

    assert started.returncode == 0
    assert status.stdout == "stopped\n"
    assert sum(method == "DELETE" for method, _path in gateway_requests) == 1


def test_failed_cleanup_is_reported_instead_of_false_release_success(tmp_path):
    delete = {"status": 500}
    with fake_gateway(None, delete_status=lambda: delete["status"]) as (
        gateway_url,
        gateway_requests,
    ):
        env = {
            **os.environ,
            "APIFY_TOKEN": "caller-secret",
            "GHOST_GATEWAY_URL": gateway_url,
            "GHOST_BROWSER_HOME": str(tmp_path / "home"),
            "GHOST_BROWSER_RUNTIME_DIR": str(tmp_path / "runtime"),
            "GHOST_BROWSER_ALLOCATION_TIMEOUT": "2",
            "GHOST_BROWSER_WS_TIMEOUT": "2",
        }
        failed_start = run_cli(env, code='print("never reached")')
        status = run_cli(env, "status")
        stopped = run_cli(env, "stop")
        delete["status"] = 204
        recovered = run_cli(env, "stop")

    assert failed_start.returncode == 1
    assert status.stdout == "release-failed\n"
    assert stopped.returncode == 1
    assert "gateway release failed: HTTP 500" in stopped.stderr
    assert "released browser" not in stopped.stdout
    assert recovered.returncode == 0
    assert recovered.stdout == "released browser\n"
    assert sum(method == "DELETE" for method, _path in gateway_requests) >= 4


def test_failed_release_can_be_retried_by_stop(tmp_path):
    delete = {"status": 500}
    runtime = tmp_path / "runtime"
    with fake_gateway(None, delete_status=lambda: delete["status"]) as (
        gateway_url,
        gateway_requests,
    ):
        env = {
            **os.environ,
            "APIFY_TOKEN": "caller-secret",
            "GHOST_GATEWAY_URL": gateway_url,
            "GHOST_BROWSER_HOME": str(tmp_path / "home"),
            "GHOST_BROWSER_RUNTIME_DIR": str(runtime),
            "GHOST_BROWSER_ALLOCATION_TIMEOUT": "2",
            "GHOST_BROWSER_WS_TIMEOUT": "2",
        }
        failed_start = run_cli(env, code='print("never reached")')
        status = run_cli(env, "status")
        first_stop = run_cli(env, "stop")
        persisted_state = "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in runtime.iterdir()
            if path.is_file()
        )
        delete["status"] = 204
        retried_stop = run_cli(env, "stop")

    assert failed_start.returncode == 1
    assert status.stdout == "release-failed\n"
    assert first_stop.returncode == 1
    assert "released browser" not in first_stop.stdout
    assert "caller-secret" not in persisted_state
    assert "opaque-run-secret" not in persisted_state
    assert list(runtime.glob("*.release")) == []
    assert (retried_stop.returncode, retried_stop.stdout, retried_stop.stderr) == (
        0,
        "released browser\n",
        "",
    )
    assert sum(method == "DELETE" for method, _path in gateway_requests) >= 3


def test_malformed_allocation_retains_failed_release_for_retry(tmp_path):
    delete = {"status": 500}
    with fake_gateway(
        None,
        raw_body=b"{not-json",
        delete_status=lambda: delete["status"],
    ) as (gateway_url, gateway_requests):
        env = {
            **os.environ,
            "APIFY_TOKEN": "caller-secret",
            "GHOST_GATEWAY_URL": gateway_url,
            "GHOST_BROWSER_HOME": str(tmp_path / "home"),
            "GHOST_BROWSER_RUNTIME_DIR": str(tmp_path / "runtime"),
            "GHOST_BROWSER_ALLOCATION_TIMEOUT": "2",
        }
        failed_start = run_cli(env, code='print("never reached")')
        status = run_cli(env, "status")
        delete["status"] = 204
        stopped = run_cli(env, "stop")

    assert failed_start.returncode == 1
    assert "invalid response" in failed_start.stderr
    assert status.stdout == "release-failed\n"
    assert (stopped.returncode, stopped.stdout, stopped.stderr) == (
        0,
        "released browser\n",
        "",
    )
    assert sum(method == "DELETE" for method, _path in gateway_requests) >= 3


def test_abnormal_websocket_close_is_not_false_release_success(tmp_path):
    delete = {"status": 500}
    with ExitStack() as stack:
        websocket_url, _messages, _connections = stack.enter_context(
            fake_cdp(disconnect_abnormally_after="Browser.getVersion")
        )
        gateway_url, gateway_requests = stack.enter_context(
            fake_gateway(websocket_url, delete_status=lambda: delete["status"])
        )
        env = {
            **os.environ,
            "APIFY_TOKEN": "caller-secret",
            "GHOST_GATEWAY_URL": gateway_url,
            "GHOST_BROWSER_HOME": str(tmp_path / "home"),
            "GHOST_BROWSER_RUNTIME_DIR": str(tmp_path / "runtime"),
        }
        failed = run_cli(
            env,
            code='cdp("Browser.getVersion", session_id=None)',
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = run_cli(env, "status")
            if status.stdout == "release-failed\n":
                break
            time.sleep(0.05)
        delete["status"] = 204
        recovered = run_cli(env, "stop")

    assert failed.returncode == 1
    assert status.stdout == "release-failed\n"
    assert recovered.stdout == "released browser\n"
    assert sum(method == "DELETE" for method, _path in gateway_requests) >= 2


def test_launcher_death_before_first_cdp_command_releases_ready_browser(tmp_path):
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
            "GHOST_BROWSER_IDLE_SECONDS": "60",
        }
        process = subprocess.Popen(
            [sys.executable, "-m", "ghost_browser.cli"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        process.stdin.write("import time; time.sleep(10)")
        process.stdin.close()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if run_cli(env, "status").stdout == "connected 127.0.0.1\n":
                break
            time.sleep(0.05)
        process.kill()
        process.wait(timeout=5)
        released_before_stop = False
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if any(method == "DELETE" for method, _path in gateway_requests):
                released_before_stop = True
                break
            time.sleep(0.05)
        if not released_before_stop:
            run_cli(env, "stop")

    assert len(connections) == 1
    assert released_before_stop
