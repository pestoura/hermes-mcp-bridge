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

The bridge does **not** execute shell commands or manage infrastructure itself. Hermes remains the executor. The bridge only translates MCP tool calls into Hermes' native `/v1/runs` API.

## MCP tools

- `hermes_prompt`: submits a prompt/objective to Hermes and waits for output up to a configurable budget.
- `hermes_status`: retrieves a run after `hermes_prompt` returns before completion.
- `hermes_stop`: requests safe cancellation of a run.
- `hermes_health`: checks Hermes liveness/readiness.

`agent` and `subagents` are optional hints translated into Hermes instructions. Omitting them lets Hermes choose its own orchestration.

## Requirements

- Debian/Linux host running hermes-agent.
- Hermes API server enabled on `127.0.0.1:8642` with a dedicated bearer key.
- Docker Engine + Compose for the provided deployment, or Python 3.11+ for native execution.
- For ChatGPT Web full write-capable custom MCP usage, an eligible ChatGPT workspace plan and Developer Mode are required.

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

## Bridge configuration

```bash
cp .env.example .env
chmod 600 .env
# Set HERMES_API_KEY to the same dedicated key.
```

## Run with Docker Compose

The container uses Linux host networking so it can reach Hermes' loopback-only API without exposing that API to Docker networks.

```bash
docker compose up -d --build
docker compose logs -f hermes-mcp-bridge
```

The MCP endpoint is:

```text
http://127.0.0.1:8765/mcp
```

## Local validation

Use the official MCP Inspector against:

```text
http://127.0.0.1:8765/mcp
```

Start with:

```json
{
  "prompt": "Indica a versão atual do Hermes e apresenta o estado geral, sem efetuar alterações.",
  "wait_seconds": 120
}
```

## Remote exposure

Do not expose the Hermes API server. Point a dedicated Cloudflare Tunnel hostname only at the bridge:

```text
hermes-mcp.hex0r.xyz -> http://127.0.0.1:8765
```

Authentication for the remote MCP endpoint is intentionally a deployment concern, not embedded as a static shared-secret shortcut in the MVP. Use an OAuth-compatible protection layer supported by the MCP client before enabling remote use.

## Development

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pytest
ruff check .
```

## Scope boundaries

- No shell implementation in the bridge.
- No direct access to SSH credentials, Docker socket, secrets or Kanban.
- No duplication of Hermes' agent, subagent, skill or tool catalog.
- Backward-compatible MCP inputs should be preferred because approved ChatGPT app tool schemas are not refreshed automatically.
