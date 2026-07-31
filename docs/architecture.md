# Architecture

## Responsibility split

| Component | Responsibility |
|---|---|
| MCP client | Formulates objectives and evaluates Hermes output |
| Bridge | MCP transport, input validation, Hermes API translation, normalized result |
| Hermes | Planning, agents/subagents, skills, tools, credentials, execution, correction and evidence |
| Cloudflare | Tunnel and remote access policy |

## Execution flow

1. `hermes_prompt` receives an objective.
2. The bridge calls `POST /v1/runs` on Hermes over loopback.
3. Hermes executes using its own runtime.
4. The bridge polls `GET /v1/runs/{run_id}` until terminal state or wait-budget exhaustion.
5. The bridge returns `session_id`, `execution_id`, `status`, `output`, and `error`.
6. The client can call `hermes_status` or send a follow-up using the same `session_id`.

## Network model

```text
Internet
  │
  ▼
Cloudflare Tunnel / OAuth policy
  │
  ▼
127.0.0.1:8765  Hermes MCP Bridge
  │
  ▼
127.0.0.1:8642  Hermes API server
```

Only the bridge is a tunnel origin. The Hermes API remains loopback-only.

## Docker choice

The Compose deployment uses `network_mode: host` because a container cannot otherwise reach a service bound to the host's `127.0.0.1`. The container is non-root, read-only, drops all Linux capabilities, and does not mount the Docker socket or Hermes files.
