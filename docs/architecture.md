# Architecture

## Responsibility split

| Component | Responsibility |
|---|---|
| MCP client | Formulates objectives, receives progress, reuses session IDs and evaluates final output |
| Bridge | MCP transport, validation, Hermes API translation, connected waiting and normalized results |
| Hermes | Session persistence, planning, agents/subagents, skills, tools, credentials, execution, correction and evidence |
| Cloudflare | Tunnel, remote access policy and preservation of streaming traffic |

## Connected execution flow

1. `hermes_prompt` receives an objective.
2. When no `session_id` is supplied, the bridge creates a native Hermes session through `POST /api/sessions`.
3. For a follow-up, the bridge loads the native session history through `GET /api/sessions/{session_id}/messages`.
4. The bridge calls `POST /v1/runs` with the prompt, resolved native session ID and prior conversation history.
5. Hermes executes using its own runtime.
6. The bridge subscribes to `GET /v1/runs/{run_id}/events`.
7. Safe lifecycle events and periodic heartbeats become MCP progress notifications.
8. On a terminal event, the bridge reads the authoritative state from `GET /v1/runs/{run_id}`.
9. If the Hermes event stream ends unexpectedly, the bridge polls the status endpoint for the remaining wait budget.
10. The original MCP tool call returns `session_id`, `execution_id`, `status`, `output`, `error` and bounded metadata.

The default connected budget is two hours. `wait_seconds=0` explicitly selects detached execution.

The bridge intentionally has no conversation database. Hermes remains the source of truth for sessions, execution state and output.

## Progress boundary

MCP progress includes only bounded lifecycle summaries such as:

- run accepted;
- tool started/completed;
- subagent started/completed;
- approval required/resolved;
- heartbeat;
- run completed/failed/cancelled;
- event-stream fallback.

The bridge does not forward reasoning text, assistant deltas, tool arguments, tool outputs, approval commands or free-form subagent output as progress.

## Disconnection policy

A disconnected MCP request does not stop the Hermes run by default. The run remains recoverable through `hermes_status` and cancellable through `hermes_stop`.

`stop_on_disconnect=true` is an explicit opt-in for operations where cancellation of the MCP request must also request cancellation in Hermes.

## Network model

```text
Internet
  │
  ▼
Cloudflare Tunnel / supported authentication policy
  │ Streamable HTTP + SSE
  ▼
127.0.0.1:8765  Hermes MCP Bridge
  │ local HTTP + bearer key
  ▼
127.0.0.1:8642  Hermes API server
```

Only the bridge is a tunnel origin. The Hermes API remains loopback-only.

## MCP transport

The bridge uses Streamable HTTP with:

```text
json_response=false
stateless_http=true
```

SSE response mode allows progress notifications and the final JSON-RPC result to travel in the same tool call. Stateless HTTP avoids storing application state in the MCP transport; native Hermes sessions provide continuity.

## Docker choice

The Compose deployment uses `network_mode: host` because a container cannot otherwise reach a service bound to the host's `127.0.0.1`. The container is non-root, read-only, drops all Linux capabilities, enables `no-new-privileges`, and does not mount the Docker socket or Hermes files.

## Trust boundary

The bridge does not possess SSH credentials, cloud tokens, Kanban credentials or execution logic. It possesses only the dedicated bearer key required to call the local Hermes API. All operational authority remains in Hermes and in the tools and credentials already configured there.
