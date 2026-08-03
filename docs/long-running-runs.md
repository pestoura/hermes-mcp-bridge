# Connected long-running runs

## Objective

`hermes_prompt` should behave like a normal long-running tool call: the MCP client submits one request, the bridge keeps the Streamable HTTP response active, Hermes performs the work, and the final output returns through the original request.

```text
MCP client
    │ tools/call
    ▼
Hermes MCP Bridge
    │ POST /v1/runs
    ▼
Hermes
    │ GET /v1/runs/{run_id}/events
    ▼
MCP progress notifications and heartbeat
    │
    ▼
final tool result
```

## Connected mode

Connected mode is the default when `wait_seconds` is omitted or greater than zero.
The bridge:

1. creates or resumes a native Hermes session;
2. submits the run;
3. reports the allocated `session_id` and `execution_id` as progress;
4. subscribes to the Hermes SSE event stream;
5. forwards only safe lifecycle summaries as MCP progress;
6. emits a heartbeat at the configured interval;
7. fetches the authoritative terminal run status;
8. returns the final normalized result.

The bridge deliberately suppresses:

- `message.delta` content;
- `reasoning.available` content;
- tool arguments and tool outputs;
- approval commands;
- free-form subagent output.

This prevents progress messages from leaking reasoning, secrets or large intermediate payloads.

## Recommended automation flow

For long-running or automated work:

1. call `hermes_submit` with a stable `client_request_id`;
2. store the returned `execution_id` and `session_id`;
3. recover later using `hermes_status`, `hermes_wait` or `recent_runs`;
4. use connected `hermes_prompt` only when the caller can keep the tool call open.

## Fallback behavior

Hermes exposes each run event stream as a single live queue. If the stream closes early or cannot be opened, the run itself continues and the bridge switches to `GET /v1/runs/{run_id}` polling for the remaining wait budget.

A polling fallback is reported to the MCP client as progress. It is not treated as a run failure unless the Hermes status endpoint also fails.

## Disconnection and cancellation

The default is:

```text
stop_on_disconnect=false
```

If the MCP client disconnects or cancels the request, the Hermes run continues. This protects 10–60 minute operations from accidental browser, proxy or network interruption. The `execution_id` remains usable with `hermes_status`, `hermes_wait` and `recent_runs` while Hermes retains the run status.

For operations where client cancellation must also stop Hermes:

```json
{
  "prompt": "Run a cancellable validation.",
  "stop_on_disconnect": true
}
```

Because an MCP transport close and an explicit client cancellation can reach the server through the same cancellation path, this option must remain explicit rather than becoming the default.

## Wait budgets

Default production values:

```text
HERMES_RUN_MAX_WAIT_SECONDS=7200
HERMES_RUN_DEFAULT_WAIT_SECONDS=45
HERMES_PROGRESS_INTERVAL_SECONDS=15
HERMES_EVENT_STREAM_CONNECT_TIMEOUT_SECONDS=30
HERMES_RUN_POLL_INTERVAL_SECONDS=1
```

`wait_seconds` is always capped by `HERMES_RUN_MAX_WAIT_SECONDS`.

Examples:

| Requirement | Input |
|---|---|
| Connected until completion | omit `wait_seconds` |
| Connected for at most 30 minutes | `wait_seconds=1800` |
| Detached immediately | `wait_seconds=0` |
| Stop Hermes if the MCP request is cancelled | `stop_on_disconnect=true` |

## Transport requirements

The FastMCP server uses:

```text
json_response=false
stateless_http=true
```

`json_response=false` is required so a `tools/call` response can use SSE and carry MCP progress notifications before the final JSON-RPC response. The transport also emits SSE keepalives independently of progress-token support.

For a reverse proxy or tunnel:

- preserve streaming responses;
- disable response buffering;
- disable cache for `/mcp`;
- do not impose an idle timeout below the heartbeat interval;
- do not expose the Hermes API on port 8642;
- test with the intended MCP client at 5, 15, 30 and 60 minutes.

## Validation matrix

| Test | Expected result |
|---|---|
| Event stream | Hermes lifecycle events observed |
| Progress | Safe MCP progress messages received |
| Heartbeat | At least one message per configured interval |
| Final result | Original tool call returns `completed` output |
| Event-stream failure | Automatic polling fallback |
| Client disconnect | Hermes run continues by default |
| Explicit stop-on-disconnect | Hermes stop endpoint called |
| Detached mode | Immediate `execution_id` response |
| Loopback | Ports 8642 and 8765 remain on `127.0.0.1` |
| Submit reuse | Same `client_request_id` and fingerprint returns same execution |
| Fingerprint conflict | Same key with different request is rejected |

## Known boundary

The bridge can maintain an MCP Streamable HTTP response and emit progress, but an external client or proxy may still enforce its own absolute maximum tool-call duration. Therefore local success does not replace a real end-to-end duration campaign through Cloudflare and ChatGPT.
