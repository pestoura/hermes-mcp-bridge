# Architecture

## Responsibility split

| Component | Responsibility |
|---|---|
| MCP client | Formulates objectives, receives progress, reuses session IDs and evaluates final output |
| Bridge | MCP transport, validation, Hermes API translation, connected waiting, idempotent submission and normalized results |
| Hermes | Session persistence, planning, agents/subagents, skills, tools, credentials, execution, correction and evidence |
| Cloudflare | Tunnel, remote access policy and preservation of streaming traffic |

## Connected execution flow

1. `hermes_submit` creates a Hermes run and returns `execution_id` and `session_id` immediately.
2. For direct follow-up, `hermes_prompt` receives an objective.
3. When no `session_id` is supplied, the bridge creates a native Hermes session.
4. For a follow-up, the bridge loads the native session history.
5. The bridge submits the prompt, resolved native session ID and prior conversation history to Hermes.
6. Hermes executes using its own runtime.
7. The bridge subscribes to the Hermes run event stream.
8. Safe lifecycle events and periodic heartbeats become MCP progress notifications.
9. On a terminal event, the bridge reads the authoritative run state.
10. If the Hermes event stream ends unexpectedly, the bridge polls the run status endpoint for the remaining wait budget.
11. The original MCP tool call returns `session_id`, `execution_id`, `status`, `output`, `error` and bounded metadata.

The default wait budget is 45 seconds; the explicit hard cap remains two hours. `wait_seconds=0` explicitly selects detached execution.

The bridge intentionally has no conversation database. Hermes remains the source of truth for sessions, execution state and output.

## Progress boundary

MCP progress includes only bounded lifecycle summaries such as:

- run accepted;
- tool started/completed;
- subagent started/completed;
- approval required/resolved;
- heartbeat;
- run completed/failed/cancelled;
- event-stream fallback;
- wait budget expired.

The bridge does not forward reasoning text, assistant deltas, tool arguments, tool outputs, approval commands or free-form subagent output as progress.

## Disconnection policy

A disconnected MCP request does not stop the Hermes run by default. The run remains recoverable through `hermes_status`, `hermes_wait` and `recent_runs`, and cancellable through `hermes_stop`.

`stop_on_disconnect=true` is an explicit opt-in for operations where cancellation of the MCP request must also request cancellation in Hermes.

## Wait budget contract

- omitted `wait_seconds` on `hermes_prompt` or `hermes_wait` uses the bridge default: **45 seconds**;
- explicit `wait_seconds` is capped at **900 seconds** in production profiles;
- `wait_seconds=0` is detached mode and returns immediately;
- wait expiry reports current run state without cancelling Hermes.

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

The Compose deployment uses `network_mode: host` because a container cannot otherwise reach a service bound to the host's `127.0.0.1`. The container is non-root, read-only, drops all Linux capabilities, enables `no-new-privileges`, mounts a tmpfs `/tmp`, and does not mount the Docker socket or Hermes files.

## Runtime hardening summary

- Linux host network, only MCP bridge port `8765` listening in the container;
- container user is non-root;
- root filesystem is read-only;
- tmpfs `/tmp`;
- `cap_drop ALL`;
- `no-new-privileges`;
- healthcheck validates TCP, Hermes API health, and registry availability;
- no inbound ports are published.

## Trust boundary

The bridge does not possess SSH credentials, cloud tokens, Kanban credentials or execution logic. It possesses only the dedicated bearer key required to call the local Hermes API. All operational authority remains in Hermes and in the tools and credentials already configured there.

## Tool inventory

Bridge version 0.8.0 (contract), built on the 0.6.1 runtime surface plus the
Block 2 observability slice, exposes **27** tools (the 26 pre-existing tools and
the read-only `hermes_readiness`):

- `hermes_submit`
- `hermes_prompt`
- `hermes_wait`
- `hermes_status`
- `hermes_stop`
- `hermes_health`
- `hermes_readiness`
- `hermes_capabilities`
- `hermes_agent_card`
- `hermes_recent_runs`
- `hermes_policy_evaluate`
- `hermes_approval_create`
- `hermes_approval_respond`
- `hermes_approval_status`
- `hermes_result_manifest`
- `hermes_plan`
- `hermes_execute_approved_plan`
- `hermes_checkpoint_create`
- `hermes_checkpoint_status`
- `hermes_continue`
- `hermes_saga_start`
- `hermes_saga_status`
- `hermes_saga_compensate`
- `hermes_lock_acquire`
- `hermes_lock_status`
- `hermes_lock_release`
- `hermes_quota_status`

## Protocol Foundations

Bridge version 0.6.1 extends policy evaluation, persistent approvals, sanitized result manifests, plans, checkpoints, continuations, sagas, locks and quotas while preserving the original tool surface.

- Execution envelope: `schema_version`, `payload_version`, `origin_type`, `context_key`, `project_key`, `correlation_id`, `causation_id`, `principal`, `delegation_chain`.
- Event types: `MessageType` and `EventType` enums with typed models (`ProgressEvent`, `ApprovalEvent`, `ToolEvent`, `LifecycleEvent`, `UnknownEvent`).
- Capability negotiation: canonical `CapabilityManifest` with deterministic JSON and SHA-256 hash; `hermes_capabilities` tool; upstream `/v1/capabilities` used when available, otherwise `source=fallback`.
- Agent cards: versioned `AgentCard` via `hermes_agent_card`.
- Health extension: `manifest_version`, `manifest_hash`, `bridge_version`, `schema_version`.
- Policy engine: deterministic ALLOW/DENY/REQUIRE_APPROVAL decisions; high-risk trust labels with mutation require approval by default.
- Approvals: persistent, single-use, transactional registry with expiry, stale detection, and atomic consumption.
- Provenance: optional HMAC-SHA256 signing; result manifests include sanitized metadata only.
