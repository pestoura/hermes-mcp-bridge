# Connected-run validation campaign

This document defines the evidence required before version 0.3 can be merged.

## Static validation

```bash
python -m compileall src tests scripts
python -m ruff check .
python -m pytest -q
```

Current baseline: 79 tests.

## Runtime baseline

- bridge version: `0.3.0`;
- MCP SDK: `>=1.29.0,<2`;
- Hermes API: loopback on `127.0.0.1:8642`;
- MCP bridge: loopback on `127.0.0.1:8765`;
- container: healthy;
- no tunnel or DNS change during local validation;
- runtime isolated on host port `18765` during validation;
- healthcheck combines TCP, Hermes API health and bridge registry state;
- production was not restarted during validation.

## Required MCP evidence

1. Tool discovery returns exactly the seven expected tools.
2. `hermes_submit` supports stable `client_request_id` reuse and fingerprint validation.
3. `hermes_prompt` schema includes `stop_on_disconnect`, `wait_seconds`, and session continuity fields.
4. A connected call uses `text/event-stream`.
5. The progress callback receives safe lifecycle messages.
6. No reasoning text, message deltas, tool arguments or tool output appears in progress.
7. An idle run produces heartbeats near the configured interval.
8. A five-minute run returns final output through the original `call_tool` request.
9. Detached `wait_seconds=0` still returns immediately.
10. Interrupting a default connected client leaves the Hermes run active.
11. Interrupting with `stop_on_disconnect=true` moves the Hermes run toward cancellation.
12. `hermes_status`, `hermes_wait` and `hermes_stop` remain functional.
13. `recent_runs` returns limited recovery metadata and no fingerprint data.
14. Session continuity remains isolated.

## Validation artifact

- image short ID candidate: `016137fd8387`
- approximate size: `~178MB`

## Decision

Use one of:

```text
CONNECTED_LONG_RUN_LOCAL_PASS
CONNECTED_LONG_RUN_LOCAL_PARTIAL
CONNECTED_LONG_RUN_LOCAL_FAIL
```

A local pass authorizes supervisory review and merge. It does not prove Cloudflare or ChatGPT duration limits; those require separate remote campaigns.
