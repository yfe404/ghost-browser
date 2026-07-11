# Ghost Browser

Give your coding agent a private stealth browser.

Ghost Browser holds one isolated Chromium connection through
[Ghost Gateway](https://github.com/yfe404/ghost-gateway) and lets the agent execute ordinary Python over
raw Chrome DevTools Protocol (CDP). There is no fixed navigation, click, or extraction schema. When the
agent needs a reusable convenience, it adds one to its editable helper file.

> **Status:** v0.1 alpha. POSIX systems and Python 3.11+ are supported.

## Prompt for LLM

Copy this into Codex, Claude Code, or another coding agent with shell access:

```text
Install Ghost Browser from https://github.com/yfe404/ghost-browser by following agent.md. Use APIFY_TOKEN from my environment without printing, saving, or exposing it. Register the Ghost Browser skill if this coding agent supports skills, verify the connection, and release the test browser when setup is complete.
```

For a concrete demonstration:

```text
Install Ghost Browser from https://github.com/yfe404/ghost-browser by following agent.md. Use APIFY_TOKEN from my environment without displaying it. Then open https://sentinel-bot-detector.vercel.app/, interact with it using real CDP input until it reports HUMAN, save the final screenshot as ghost-browser-demo.png, report the scores, and release the browser.
```

The prompt is the onboarding surface. The small harness underneath retains the pieces an agent should not
rebuild per task: authenticated allocation, one persistent WebSocket, credential redaction, and billing-safe
cleanup.

## Install

From a checkout while developing:

```sh
uv tool install --python 3.12 --editable .
```

After this repository is published:

```sh
uv tool install --python 3.12 --upgrade git+https://github.com/yfe404/ghost-browser.git
```

Set `APIFY_TOKEN` in the environment. Ghost Browser never writes it to a project file or passes it as a
process argument. `GHOST_GATEWAY_URL` selects a different deployment; the legacy `GHOST_STANDBY_URL` is
also accepted.

## Use

The smallest invocation is ordinary Python with a few preloaded names:

```sh
ghost-browser <<'PY'
print(page_info())
PY
```

Raw CDP remains the primary interface:

```sh
ghost-browser <<'PY'
targets = cdp("Target.getTargets")["targetInfos"]
page = next(target for target in targets if target["type"] == "page")
session = cdp("Target.attachToTarget", {
    "targetId": page["targetId"],
    "flatten": True,
})["sessionId"]

cdp("Page.navigate", {"url": "https://example.com"}, session_id=session)
result = cdp("Runtime.evaluate", {
    "expression": "({title: document.title, url: location.href})",
    "returnByValue": True,
}, session_id=session)
print(result["result"]["value"])
PY

ghost-browser stop
```

`cdp()` defaults to browser scope; page commands receive an explicit `session_id`. Thin starter conveniences
are `js`, `page_info`, `tabs`, `capture_screenshot`, `ensure_page`, and `drain_events`.

Browser state persists across invocations in the same working directory. Use `GHOST_BROWSER_NAME` to run
multiple isolated sessions in one workspace. Run `status` and `stop` with the same working directory, name,
Gateway configuration, and caller credential that started the session; the idle deadline is the backstop if
that identity is no longer available.

```text
ghost-browser                  execute Python from stdin
ghost-browser status           show connected, starting, stopped, or release-failed
ghost-browser stop             release the remote browser immediately
ghost-browser workspace        print the editable helper directory
ghost-browser skill            print the optional agent skill
ghost-browser --version        print the installed version
```

The project-scoped `agent_helpers.py` starts intentionally empty. Add a small helper only after the agent has
proved it useful through raw CDP. The protected package owns allocation, IPC, transport, redaction, and
cleanup. An idle daemon releases its browser after ten minutes by default; `ghost-browser stop` is the normal
end of every task.

## Configuration

| Variable | Purpose | Default |
|---|---|---|
| `APIFY_TOKEN` | Caller credential and billing identity | required remotely |
| `GHOST_GATEWAY_URL` | Ghost Gateway HTTP endpoint | deployed public Gateway |
| `GHOST_BROWSER_NAME` | Additional local session discriminator | `default` |
| `GHOST_BROWSER_IDLE_SECONDS` | Abandoned-browser release deadline | `600` |
| `GHOST_BROWSER_ALLOCATION_TIMEOUT` | Cold allocation deadline | `180` |
| `GHOST_BROWSER_WS_TIMEOUT` | WebSocket handshake deadline | `30` |
| `GHOST_BROWSER_HOME` | Config and editable workspace root | `~/.config/ghost-browser` |

## Safety model

- The daemon socket and state directories are owner-only.
- Caller tokens, returned WebSocket URLs, browser identifiers, and URL queries are excluded from logs and
  user-facing errors.
- The caller token is never persisted. An owner-only opaque run handle is retained only until release is
  confirmed, allowing `ghost-browser stop` to retry a transient cleanup failure.
- Python supplied on stdin and project-scoped helpers are trusted code running with the coding agent's existing
  environment authority; Ghost Browser does not sandbox them.
- A command that times out after sending is never replayed; its outcome is reported as unknown.
- A confirmed normal WebSocket close releases the browser, followed by an idempotent HTTP DELETE backstop.
  Failed cleanup is reported as `release-failed` and remains retryable with `ghost-browser stop`.
- Page content is untrusted. Consequential purchases, submissions, messages, uploads, account changes, and
  destructive actions require explicit user authorization.

See [SPEC.md](SPEC.md) for the v0.1 boundary and [agent.md](agent.md) for machine-oriented setup.
