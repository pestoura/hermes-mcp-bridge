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

## MCP tools

- `hermes_prompt`: delegates work and remains connected until completion by default.
- `hermes_status`: retrieves a run after a detached or interrupted request.
- `hermes_stop`: requests safe cancellation of a run.
- `hermes_health`: checks Hermes liveness/readiness.

`agent` and `subagents` are optional hints translated into Hermes instructions. Omitting them lets Hermes choose its own orchestration.

## Connected long-running execution

Version 0.2 keeps `hermes_prompt` connected for operations that take minutes or hours:

1. The bridge creates a native Hermes run.
2. It subscribes to `GET /v1/runs/{run_id}/events`.
3. Significant Hermes lifecycle events become MCP progress notifications.
4. A heartbeat is emitted every 15 seconds.
5. If the Hermes event stream ends unexpectedly, the bridge falls back to status polling.
6. The final Hermes output is returned by the original MCP tool call.

The default connected budget is two hours:

```text
HERMES_RUN_MAX_WAIT_SECONDS=7200
HERMES_PROGRESS_INTERVAL_SECONDS=15
```

The explicit detached mode remains available:

```json
{
  "prompt": "Execute a long task in the background.",
  "wait_seconds": 0
}
```

A disconnected client does not stop Hermes by default. The run remains recoverable through `hermes_status`. Set `stop_on_disconnect=true` only when a disconnected or cancelled MCP request must also stop the Hermes run.

The MCP server uses SSE response mode rather than JSON-only responses. This allows progress notifications and transport keepalives to flow while the tool call remains open.

## Session continuity

Omit `session_id` on the first `hermes_prompt` call. The bridge creates a native Hermes session and returns its identifier. Reuse that returned identifier on later calls.

Every new native session title combines a bounded prompt summary with a random MCP suffix. If Hermes still reports a duplicate-title collision, the bridge generates another title and retries up to three times.

The bridge does not keep its own conversation database. Before each follow-up it loads the persisted messages from Hermes and passes them to the new run. If Hermes compacts or advances a session, the resolved native session identifier is returned to the MCP client.

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
  -H "Authorization: Bearer $API_SERVER_KEY" \
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
