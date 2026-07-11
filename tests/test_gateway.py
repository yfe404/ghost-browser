import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@contextmanager
def fake_gateway(
    *,
    ws_url="ws://127.0.0.1:9222/devtools/browser/opaque?token=ws-secret",
    status=200,
    browser_id="browser-42",
):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.command, self.path))
            if status != 200:
                payload = b"upstream detail containing caller-secret"
                self.send_response(status)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            payload = json.dumps({"webSocketDebuggerUrl": ws_url}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            if browser_id is not None:
                self.send_header("X-Ghost-Session", browser_id)
            self.end_headers()
            self.wfile.write(payload)

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
        yield f"http://127.0.0.1:{server.server_port}/tenant?region=eu", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_allocate_browser_preserves_query_and_returned_websocket(monkeypatch, capsys):
    from ghost_browser.gateway import allocate_browser

    monkeypatch.setenv("APIFY_TOKEN", "caller-secret")
    with fake_gateway() as (gateway_url, requests):
        allocation = allocate_browser(gateway_url, timeout=2)

    assert requests == [
        ("GET", "/tenant/json/version?region=eu&token=caller-secret")
    ]
    assert allocation.browser_id == "browser-42"
    assert allocation.websocket_url == (
        "ws://127.0.0.1:9222/devtools/browser/opaque?token=ws-secret"
    )
    assert capsys.readouterr() == ("", "")


def test_allocation_errors_never_expose_request_credentials(monkeypatch):
    from ghost_browser.gateway import GatewayError, allocate_browser

    monkeypatch.setenv("APIFY_TOKEN", "caller-secret")
    with fake_gateway(status=402) as (gateway_url, _requests):
        with pytest.raises(GatewayError) as raised:
            allocate_browser(gateway_url, timeout=2)

    assert "HTTP 402" in str(raised.value)
    assert "caller-secret" not in str(raised.value)
    assert "token=" not in str(raised.value)


def test_release_uses_opaque_websocket_authority_and_credentials(monkeypatch):
    from ghost_browser.gateway import Allocation, release_browser

    monkeypatch.setenv("APIFY_TOKEN", "caller-secret")
    with fake_gateway() as (gateway_url, requests):
        port = gateway_url.split(":")[2].split("/")[0]
        allocation = Allocation(
            gateway_url="https://shared.example/gateway?token=caller-secret",
            websocket_url=(
                f"ws://127.0.0.1:{port}/devtools/browser/opaque"
                "?token=opaque-run-secret&region=eu"
            ),
            browser_id="browser id/42",
        )
        release_browser(allocation, timeout=2)

    assert requests == [
        (
            "DELETE",
            "/v1/sessions/browser%20id%2F42?token=opaque-run-secret&region=eu",
        )
    ]


def test_existing_gateway_token_wins_without_duplication(monkeypatch):
    from ghost_browser.gateway import allocate_browser

    monkeypatch.setenv("APIFY_TOKEN", "caller-secret")
    with fake_gateway() as (gateway_url, requests):
        allocate_browser(f"{gateway_url}&token=explicit-secret", timeout=2)

    assert requests == [
        ("GET", "/tenant/json/version?region=eu&token=explicit-secret")
    ]


def test_remote_plaintext_gateway_is_rejected_without_a_request(monkeypatch):
    from ghost_browser.gateway import GatewayError, allocate_browser

    monkeypatch.setenv("APIFY_TOKEN", "caller-secret")
    with pytest.raises(GatewayError, match="must use HTTPS"):
        allocate_browser("http://gateway.example", timeout=2)


def test_missing_session_header_falls_back_to_websocket_browser_id(monkeypatch):
    from ghost_browser.gateway import allocate_browser

    monkeypatch.setenv("APIFY_TOKEN", "caller-secret")
    with fake_gateway(browser_id=None) as (gateway_url, _requests):
        allocation = allocate_browser(gateway_url, timeout=2)

    assert allocation.browser_id == "opaque"


def test_remote_gateway_cannot_redirect_to_plaintext_loopback(monkeypatch):
    from ghost_browser.gateway import GatewayError, allocate_browser

    class Response:
        headers = {"X-Ghost-Session": "browser-42"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def read(self, _limit):
            return json.dumps(
                {
                    "webSocketDebuggerUrl": (
                        "ws://127.0.0.1:9222/devtools/browser/opaque?token=secret"
                    )
                }
            ).encode()

    monkeypatch.setenv("APIFY_TOKEN", "caller-secret")
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: Response())

    with pytest.raises(GatewayError, match="insecure WebSocket"):
        allocate_browser("https://gateway.example", timeout=2)
