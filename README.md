# Hermes MCP Bridge

A deliberately thin MCP bridge that lets a remote MCP client delegate a natural-language objective to **hermes-agent** and receive the resulting output.

```text
ChatGPT / MCP client
        │ Streamable HTTP + progress
        ▼
Hermes MCP Bridge
        │ loopback HTTP + bearer key
        ▼
Hermes API server
        ▼
Hermes agents, subagents, skills, tools, Kanban and configured servers
```

The bridge does **not** execute shell commands or manage infrastructure itself. Hermes remains the executor. The bridge translates MCP tool calls into Hermes' native session, run and event-stream APIs.

## Version and contract

Bridge contract version: **0.6.1**.

## MCP tools

- `hermes_submit`: creates a Hermes run, returns `execution_id` and `session_id`, and optionally reuses an existing identical run by `client_request_id`.
- `hermes_prompt`: delegates work and remains connected until completion by default.
- `hermes_wait`: attaches to an existing `execution_id` and returns completion or a wait-budget expiry.
- `hermes_status`: retrieves a run after a detached or interrupted request.
- `hermes_stop`: requests safe cancellation of a run.
- `hermes_health`: checks Hermes liveness/readiness and bridge registry state, including manifest metadata.
- `hermes_recent_runs`: lists recent registry entries by status or recency.
- `hermes_capabilities`: returns the canonical capability manifest for this bridge.
- `hermes_agent_card`: returns the versioned agent card for this bridge.
- `hermes_policy_evaluate`: evaluates an allow/deny/require-approval policy decision for an action and trust/mutation context.
- `hermes_approval_create`: creates a persistent approval request for high-risk mutations.
- `hermes_approval_respond`: responds to an approval request with approved/rejected.
- `hermes_approval_status`: returns the current status of an approval request.
- `hermes_result_manifest`: returns a sanitized result manifest for an execution.
- `hermes_plan`: creates an executable plan/DAG without executing mutations.
- `hermes_execute_approved_plan`: executes a previously approved plan with policy gating.
- `hermes_checkpoint_create`: creates a bridge-side checkpoint without storing blobs.
- `hermes_checkpoint_status`: queries checkpoint state for a run or plan.
- `hermes_continue`: resumes a previous execution or checkpoint idempotently.
- `hermes_saga_start`: starts a saga and registers compensation contracts.
- `hermes_saga_status`: reads saga state and compensation evidence.
- `hermes_saga_compensate`: records or inspects a compensation event for a saga.
- `hermes_lock_acquire`: acquires a typed resource lock with TTL.
- `hermes_lock_status`: inspects active locks and expiry state.
- `hermes_lock_release`: releases a held lock idempotently.
- `hermes_quota_status`: returns current quota and budget evaluation.

`agent` and `subagents` are optional hints translated into Hermes instructions. Omitting them lets Hermes choose its own orchestration.

## Connected long-running execution

`hermes_prompt` keeps the MCP request connected for operations that take minutes or hours:

1. The bridge creates a native Hermes run.
2. It subscribes to `GET /v1/runs/{run_id}/events`.
3. Significant Hermes lifecycle events become MCP progress notifications.
4. A heartbeat is emitted at the configured interval.
5. If the Hermes event stream ends unexpectedly, the bridge falls back to status polling.
6. The final Hermes output is returned by the original MCP tool call.

### Recommended long-run/automation flow

1. Submit with a stable `client_request_id`.
2. Store the returned `execution_id` and `session_id`.
3. Recover later with `hermes_status`, `hermes_wait`, or `recent_runs`.
4. Use connected `hermes_prompt` only when the caller can keep the tool call open; otherwise use `wait_seconds=0`.

### Wait and timeout contract

- `hermes_wait` and `hermes_prompt` use an explicit `wait_seconds` value when provided; when omitted, the effective default in this bridge is **45 seconds**.
- Explicit wait values are capped at **900 seconds** in production profiles.
- `wait_seconds=0` is the explicit detached mode: the tool returns immediately with the execution identifiers.
- Wait-budget expiry returns control to the caller with the current run state; it does **not** cancel the Hermes run.

The explicit detached mode remains available:

```json
{
  "prompt": "Execute a long task in the background.",
  "wait_seconds": 0
}
```

A disconnected client does not stop Hermes by default. The run remains recoverable through `hermes_status` or `hermes_wait`. Set `stop_on_disconnect=true` only when a disconnected or cancelled MCP request must also stop the Hermes run.

The MCP server uses SSE response mode rather than JSON-only responses. This allows progress notifications and transport keepalives to flow while the tool call remains open.

This path was observed to encounter an external tool-call timeout of **60000 ms** in this MCP stack; it is documented as an observed path-specific limit, not as a universal OpenAI policy.

## Session continuity

Omit `session_id` on the first `hermes_prompt` call. The bridge creates a native Hermes session and returns its identifier. Reuse that returned identifier on later calls.

Every new native session title combines a bounded prompt summary with a random MCP suffix. If Hermes still reports a duplicate-title collision, the bridge generates another title and retries up to three times.

The bridge does not keep its own conversation database. Before each follow-up it loads the persisted messages from Hermes and passes them to the new run. If Hermes compacts or advances a session, the resolved native session identifier is returned to the MCP client.

## Idempotency and key reuse

The bridge supports idempotent reuse by canonical request fingerprint:

- identical `client_request_id` with the same prompt, session, agent, subagents and orchestration reuses the existing mapping;
- identical key with a different request is rejected;
- locking is per key within a single bridge instance/process.

This is **not** distributed multi-instance coordination. Do not assume two bridge instances will deduplicate the same key.

Persisted state is protected against request cancellation: Hermes submission, registry record and status recovery use shielded or post-cancel persistence paths so the mapping is not silently lost.

## Registry

The registry stores lightweight mappings in SQLite. The default container path is:

```text
/var/lib/hermes-mcp-bridge/state.sqlite3
```

The host path is bound through `${BRIDGE_STATE_DIR:-./data}`.

The bridge stores only operational mapping fields required for recovery:

- `client_request_id`
- `fingerprint`
- `execution_id`
- `session_id`
- `last_status`
- `created_at`
- `updated_at`

The registry never stores prompt text, output, error payloads, tokens, or secrets.

`recent_runs` exposes client request identifiers, execution identifiers, session identifiers, status and timestamps; it does not expose fingerprints.

Host preparation required before compose up:

1. Create the state directory.
2. Set mode `700`.
3. Set ownership to `BRIDGE_UID:BRIDGE_GID`.

## Requirements

- Debian/Linux host running hermes-agent with session, run, event and stop endpoints.
- Hermes API server enabled on `127.0.0.1:8642` with a dedicated bearer key.
- Docker Engine + Compose for the provided deployment, or Python 3.11+ for native execution.
- MCP Python SDK 1.29 or newer within the supported 1.x line.
- For ChatGPT Web custom MCP usage, a plan/workspace with the required custom-app and Developer Mode capabilities.

## Hermes configuration

Add a dedicated key to `~/.hermes/.env` without publishing the API server beyond loopback:

```bash
API_SERVER_ENABLED=true
API_SERVER_KEY=<dedicated-random-secret>
```

Restart the Hermes gateway and verify locally:

```bash
curl -fsS http://127.0.0.1:8642/health
curl -fsS \
  -H "Authorization: Bearer ***" \
  http://127.0.0.1:8642/v1/capabilities
```

The capabilities response must advertise run submission, status, events, stop and native session resources.

## Bridge configuration

```bash
cp .env.example .env
chmod 600 .env
# Set HERMES_API_KEY to the same dedicated key.
```

## Run with Docker Compose

The container uses Linux host networking so it can reach Hermes' loopback-only API without exposing that API to Docker networks.

```bash
docker compose config
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 hermes-mcp-bridge
```

The MCP endpoint is:

```text
http://127.0.0.1:8765/mcp
```

## Local validation

Discover the MCP tools and call `hermes_health`:

```bash
python scripts/smoke_test.py --url http://127.0.0.1:8765/mcp
```

Run a connected read-only prompt and display progress:

```bash
python scripts/smoke_test.py \
  --url http://127.0.0.1:8765/mcp \
  --wait-seconds 1800 \
  --prompt "Execute a read-only validation that takes several minutes."
```

The official MCP Inspector can also connect to:

```text
http://127.0.0.1:8765/mcp
```

See [docs/installation.md](docs/installation.md) for installation and rollback, and [docs/long-running-runs.md](docs/long-running-runs.md) for the connected execution contract.

## Remote exposure

Do not expose the Hermes API server. A dedicated Cloudflare Tunnel may target only the bridge:

```text
hermes-mcp.hex0r.xyz -> http://127.0.0.1:8765
```

Keep the bridge on loopback and disable caching and response buffering for the MCP route. Remote authentication remains a deployment concern and must be validated with the intended MCP client before production use.

Long remote calls must be tested through the real proxy and client at increasing durations. Progress and SSE keepalives reduce idle-timeout risk, but they do not prove that every external client permits a two-hour tool call.

## Development

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
python -m compileall src tests scripts
python -m ruff check .
python -m pytest -q
```

## Scope boundaries

- No shell implementation in the bridge.
- No direct access to SSH credentials, Docker socket, secrets or Kanban.
- No duplication of Hermes' agent, subagent, skill or tool catalog.
- No public exposure of the Hermes API server.
- No reasoning or partial assistant text is forwarded as MCP progress.
- Backward-compatible MCP inputs should be preferred because approved client tool schemas may require an explicit refresh after changes.
