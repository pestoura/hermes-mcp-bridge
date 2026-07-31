# Hermes MCP Bridge

A deliberately thin MCP bridge that lets a remote MCP client delegate a natural-language objective to **hermes-agent** and receive the resulting output.

```text
ChatGPT / MCP client
        │ Streamable HTTP
        ▼
Hermes MCP Bridge
        │ loopback HTTP + bearer key
        ▼
Hermes API server
        ▼
Hermes agents, subagents, skills, tools, Kanban and configured servers
```

The bridge does **not** execute shell commands or manage infrastructure itself. Hermes remains the executor. The bridge translates MCP tool calls into Hermes' native session and `/v1/runs` APIs.

## MCP tools

- `hermes_prompt`: submits a prompt/objective to Hermes and waits for output up to a configurable budget.
- `hermes_status`: retrieves a run after `hermes_prompt` returns before completion.
- `hermes_stop`: requests safe cancellation of a run.
- `hermes_health`: checks Hermes liveness/readiness.

`agent` and `subagents` are optional hints translated into Hermes instructions. Omitting them lets Hermes choose its own orchestration.

## Session continuity

Omit `session_id` on the first `hermes_prompt` call. The bridge creates a native Hermes session and returns its identifier. Reuse that returned identifier on later calls.

Every new native session title combines a bounded prompt summary with a random MCP suffix. If Hermes still reports a duplicate-title collision, the bridge generates another title and retries up to three times.

The bridge does not keep its own conversation database. Before each follow-up it loads the persisted messages from Hermes and passes them to the new run. If Hermes compacts or advances a session, the resolved native session identifier is returned to the MCP client.

## Requirements

- Debian/Linux host running hermes-agent with the HTTP API session and runs endpoints.
- Hermes API server enabled on `127.0.0.1:8642` with a dedicated bearer key.
- Docker Engine + Compose for the provided deployment, or Python 3.11+ for native execution.
- For ChatGPT Web custom MCP usage, a ChatGPT plan/workspace with the required custom-app and Developer Mode capabilities.

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

The capabilities response must advertise at least:

- run submission and status;
- run stop;
- session resources;
- session chat/history.

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

Use the included client to discover the MCP tools and call `hermes_health`:

```bash
python scripts/smoke_test.py --url http://127.0.0.1:8765/mcp
```

Execute a read-only Hermes prompt as an explicit opt-in:

```bash
python scripts/smoke_test.py \
  --url http://127.0.0.1:8765/mcp \
  --prompt "Indica a versão atual do Hermes e apresenta o estado geral, sem efetuar alterações."
```

The official MCP Inspector can also connect to:

```text
http://127.0.0.1:8765/mcp
```

See [docs/installation.md](docs/installation.md) for the phased installation and rollback procedure.

## Remote exposure

Do not expose the Hermes API server. Point a dedicated Cloudflare Tunnel hostname only at the bridge:

```text
hermes-mcp.hex0r.xyz -> http://127.0.0.1:8765
```

Remote authentication is intentionally a deployment concern, not a static shared-secret shortcut embedded in the bridge. Keep the bridge on loopback until an authentication method supported by the intended MCP client has been validated end to end.

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
- Backward-compatible MCP inputs should be preferred because approved client tool schemas may require an explicit refresh after changes.
