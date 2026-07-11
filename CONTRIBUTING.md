# Contributing

Keep Ghost Browser a thin harness around a large action space.

## Engineering invariants

- Raw `cdp(method, params, session_id)` remains the primary browser interface. The protected package may
  contain only the thin starter helpers named in `SPEC.md`; task-specific actions belong in the editable,
  project-scoped agent workspace after repeated use proves them valuable.
- One logical local session performs one Gateway allocation and holds one WebSocket. Discovery endpoints are
  allocation operations, not health probes or retry-safe GETs.
- Harness-owned code keeps credentials, token-bearing URLs, browser identifiers, and URL query values out of
  process arguments, filenames, logs, exceptions, `repr`, stdout, and stderr. Agent-authored Python is trusted
  code with the parent coding agent's environment authority.
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
