import os
import stat


def test_state_paths_and_lease_repr_do_not_expose_credentials(monkeypatch, tmp_path):
    from ghost_browser.gateway import Allocation
    from ghost_browser.paths import session_paths

    monkeypatch.setenv("APIFY_TOKEN", "caller-super-secret")
    monkeypatch.setenv(
        "GHOST_GATEWAY_URL", "https://gateway.example?token=gateway-secret"
    )
    monkeypatch.setenv("GHOST_BROWSER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("GHOST_BROWSER_RUNTIME_DIR", str(tmp_path / "runtime"))

    paths = session_paths(tmp_path / "project")
    allocation = Allocation(
        gateway_url="https://gateway.example?token=gateway-secret",
        websocket_url="wss://run.example/devtools/browser/id?token=ws-secret",
        browser_id="browser-secret-id",
    )

    displayed = " ".join(str(value) for value in vars(paths).values()) + repr(allocation)
    for secret in (
        "caller-super-secret",
        "gateway-secret",
        "ws-secret",
        "browser-secret-id",
    ):
        assert secret not in displayed
    assert stat.S_IMODE(os.stat(tmp_path / "home").st_mode) == 0o700
    assert stat.S_IMODE(os.stat(tmp_path / "runtime").st_mode) == 0o700
    assert stat.S_IMODE(os.stat(paths.workspace).st_mode) == 0o700


def test_pending_release_does_not_persist_caller_or_gateway_token(
    monkeypatch, tmp_path
):
    from ghost_browser.gateway import Allocation
    from ghost_browser.paths import session_paths
    from ghost_browser.release_state import save_release_handle

    monkeypatch.setenv("APIFY_TOKEN", "caller-super-secret")
    monkeypatch.setenv(
        "GHOST_GATEWAY_URL", "https://gateway.example?token=gateway-secret"
    )
    monkeypatch.setenv("GHOST_BROWSER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("GHOST_BROWSER_RUNTIME_DIR", str(tmp_path / "runtime"))
    paths = session_paths(tmp_path / "project")

    save_release_handle(
        paths,
        Allocation(
            gateway_url="https://gateway.example?token=gateway-secret",
            websocket_url=(
                "wss://run.example/devtools/browser/id"
                "?token=gateway-secret&region=eu"
            ),
            browser_id="browser-secret-id",
            caller_token="caller-super-secret",
        ),
    )

    state = paths.pending_release.read_text(encoding="utf-8")
    assert "caller-super-secret" not in state
    assert "gateway-secret" not in state
    assert "region=eu" in state
    assert stat.S_IMODE(paths.pending_release.stat().st_mode) == 0o600
