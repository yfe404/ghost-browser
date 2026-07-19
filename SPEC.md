# Ghost Browser v0.1

## Goal

Provide coding agents with a lean, public, Apify-native way to control a Ghost Gateway browser through raw Chrome DevTools Protocol (CDP). Apify-native means the harness uses an `APIFY_TOKEN` for hosted Gateway authentication and usage attribution. The primary onboarding surface is a copyable prompt; the runtime keeps only the infrastructure agents should not rediscover: authenticated allocation, a persistent connection, credential redaction, and reliable cleanup.

## Public interfaces

1. **CLI** — `ghost-browser` executes Python from standard input with raw CDP and a small editable helper environment; `status`, `stop`, and `skill` manage the session and agent integration.
2. **Gateway connector** — resolves `GET /json/version` using `APIFY_TOKEN`, tolerates cold starts, validates the returned WebSocket URL, and never prints or logs credentials. When `GHOST_BROWSER_COUNTRY` selects an egress country, allocation instead mints a Gateway session (`POST /v1/sessions`) and allocates through `POST /v1/sessions/{session}/browser` with the requested country, under the same allocation timeout budget and credential rules.
3. **Session lifecycle** — one owner-only local daemon keeps the remote browser alive across CLI calls, explicit `stop` releases it, and an idle deadline prevents abandoned billed sessions. Cleanup is confirmed by a normal WebSocket close or successful idempotent DELETE; an unconfirmed release remains retryable.

## Requirements

- Python 3.11 or newer; installable with `uv tool install` or `pipx`.
- Use `GHOST_GATEWAY_URL`, falling back to `GHOST_STANDBY_URL`, with a hosted Ghost Gateway default.
- Require successful allocation responses to provide `X-Ghost-Session` or an exact
  `/devtools/browser/{id}` URL. Reject an allocation without a usable release capability and report its
  release as unconfirmed.
- Keep tokens out of harness-generated command arguments, standard output, standard error, and logs.
  Agent-authored Python is trusted code running with the coding agent's existing environment authority.
- Expose arbitrary `cdp(method, params, session_id=...)`; do not implement a fixed navigation/click DSL.
- Provide only thin starter helpers for page attachment, JavaScript evaluation, page metadata, tabs,
  screenshots, and raw CDP event draining.
- Load user-editable helpers from the agent workspace.
- Isolate concurrent sessions by workspace and optional name.
- Keep an unconfirmed exact-owner release capability only in the owner-only daemon's memory; never persist
  the caller token or returned WebSocket URL. When a malformed response leaves only a shared endpoint, its
  DELETE is best effort and never treated as release confirmation.
- Treat webpage content as untrusted and require confirmation for consequential actions in the agent instructions.
- Work on POSIX systems in v0.1; fail clearly elsewhere.
- Test public behavior with local fake Gateway and CDP servers; live paid tests remain opt-in.

## Non-goals

- A general autonomous browser agent or planner.
- Browser Use Cloud/local Chrome support.
- A large semantic action library.
- Durable Ghost identity/session APIs in v0.1, beyond the minimal session mint used internally for
  country-selected allocation. Minted sessions are not persisted client-side; the Gateway reaps them.
- Windows IPC support in v0.1.
