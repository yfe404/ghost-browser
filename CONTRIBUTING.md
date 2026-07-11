# Contributing

Keep Ghost Browser a thin harness around a large action space.

## Engineering invariants

- Raw `cdp(method, params, session_id)` remains the primary browser interface. Add semantic helpers only to
  the editable agent workspace after repeated use proves them valuable.
- One logical local session performs one Gateway allocation and holds one WebSocket. Discovery endpoints are
  allocation operations, not health probes or retry-safe GETs.
- Credentials, token-bearing URLs, browser identifiers, and URL query values never appear in process
  arguments, filenames, logs, exceptions, `repr`, stdout, or stderr.
- Every exit path closes the WebSocket and attempts idempotent HTTP release. Ambiguous commands are not
  replayed.
- Browser interactions that need behavioral fidelity use real CDP mouse and key events.
- Tests assert behavior through the CLI, Gateway connector, or lifecycle boundary. Add one failing test, then
  the smallest implementation that makes it pass.

Run the full suite with:

```sh
uv sync --extra dev
uv run pytest
```
