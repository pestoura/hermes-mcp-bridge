# Architecture

## Responsibility split

| Component | Responsibility |
|---|---|
| MCP client | Formulates objectives, reuses returned session IDs and evaluates Hermes output |
| Bridge | MCP transport, validation, Hermes API translation and normalized results |
| Hermes | Session persistence, planning, agents/subagents, skills, tools, credentials, execution, correction and evidence |
| Cloudflare | Tunnel and remote access policy |

## Execution flow

1. `hermes_prompt` receives an objective.
2. When no `session_id` is supplied, the bridge creates a native Hermes session through `POST /api/sessions`.
3. For a follow-up, the bridge loads the native session history through `GET /api/sessions/{session_id}/messages`.
4. The bridge calls `POST /v1/runs` with the prompt, resolved native session ID and prior conversation history.
5. Hermes executes using its own runtime.
6. The bridge polls `GET /v1/runs/{run_id}` until a terminal state or wait-budget exhaustion.
7. The bridge returns `session_id`, `execution_id`, `status`, `output`, `error` and bounded metadata.
8. The client can call `hermes_status`, request `hermes_stop`, or continue with the returned `session_id`.

The bridge intentionally has no conversation database. Hermes remains the source of truth for sessions and execution history.

## Network model

```text
Internet
  │
  ▼
Cloudflare Tunnel / supported authentication policy
  │
  ▼
127.0.0.1:8765  Hermes MCP Bridge
  │
  ▼
127.0.0.1:8642  Hermes API server
```

Only the bridge is a tunnel origin. The Hermes API remains loopback-only.

## Docker choice

The Compose deployment uses `network_mode: host` because a container cannot otherwise reach a service bound to the host's `127.0.0.1`. The container is non-root, read-only, drops all Linux capabilities, enables `no-new-privileges`, and does not mount the Docker socket or Hermes files.

## Trust boundary

The bridge does not possess SSH credentials, cloud tokens, Kanban credentials or execution logic. It possesses only the dedicated bearer key required to call the local Hermes API. All operational authority remains in Hermes and in the tools and credentials already configured there.
