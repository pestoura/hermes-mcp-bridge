# Connected-run validation campaign

This document defines the evidence required before version 0.2 can be merged.

## Static validation

```bash
python -m compileall src tests scripts
python -m ruff check .
python -m pytest -q
```

Expected test count for this branch: 22 tests.

## Runtime baseline

- bridge version: `0.2.0`;
- MCP SDK: `>=1.29.0,<2`;
- Hermes API: loopback on `127.0.0.1:8642`;
- MCP bridge: loopback on `127.0.0.1:8765`;
- container: healthy;
- no tunnel or DNS change during local validation.

## Required MCP evidence

1. Tool discovery returns exactly the four expected tools.
2. `hermes_prompt` schema includes `stop_on_disconnect`.
3. A connected call uses `text/event-stream`.
4. The progress callback receives safe lifecycle messages.
5. No reasoning text, message deltas, tool arguments or tool output appears in progress.
6. An idle run produces heartbeats near the configured interval.
7. A five-minute run returns final output through the original `call_tool` request.
8. Detached `wait_seconds=0` still returns immediately.
9. Interrupting a default connected client leaves the Hermes run active.
10. Interrupting with `stop_on_disconnect=true` moves the Hermes run toward cancellation.
11. `hermes_status` and `hermes_stop` remain functional.
12. Session continuity remains isolated.

## Decision

Use one of:

```text
CONNECTED_LONG_RUN_LOCAL_PASS
CONNECTED_LONG_RUN_LOCAL_PARTIAL
CONNECTED_LONG_RUN_LOCAL_FAIL
```

A local pass authorizes supervisory review and merge. It does not prove Cloudflare or ChatGPT duration limits; those require separate remote campaigns.
