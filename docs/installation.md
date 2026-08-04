# Installation on the Hermes host

This procedure installs the bridge as a separate Docker service on the same Linux host as hermes-agent.

## Target layout

```text
/opt/hermes-mcp-bridge/         cloned repository and local .env
~/.hermes/.env                  Hermes API server settings
127.0.0.1:8642                 Hermes HTTP API
127.0.0.1:8765/mcp             Hermes MCP Bridge
```

The Hermes API and MCP bridge remain loopback-only during local validation.

## Preconditions

- hermes-agent gateway is managed by `systemctl --user`.
- Hermes provides native sessions, runs, run events, status and stop endpoints.
- Docker Engine and Docker Compose are available.
- The operator can read and update `~/.hermes/.env`.
- The private GitHub repository is accessible from the host.
- Ports `8642` and `8765` are free or already owned by the expected services.

## Phase 1 — install and validate locally

### 1. Capture the baseline

```bash
hermes --version
hermes status
systemctl --user status hermes-gateway.service --no-pager
ss -lntp | grep -E ':(8642|8765)\b' || true
docker version
docker compose version
```

### 2. Clone the repository

```bash
sudo install -d -o "$USER" -g "$USER" /opt/hermes-mcp-bridge
git clone https://github.com/pestoura/hermes-mcp-bridge.git /opt/hermes-mcp-bridge
cd /opt/hermes-mcp-bridge
git checkout main
git status --short
```

For pre-merge validation, check out the explicitly approved branch and commit instead of `main`.

### 3. Enable the Hermes API on loopback

Create a protected backup before changing the Hermes environment file:

```bash
install -m 600 ~/.hermes/.env ~/.hermes/.env.pre-hermes-mcp-bridge
```

Generate a dedicated bearer key without printing it:

```bash
umask 077
API_SERVER_KEY_VALUE="$(openssl rand -hex 32)"
```

Update `~/.hermes/.env` idempotently so it contains:

```text
API_SERVER_ENABLED=true
API_SERVER_KEY=<dedicated value>
```

Do not bind the Hermes API to a non-loopback address. Restart only the user gateway:

```bash
systemctl --user restart hermes-gateway.service
systemctl --user is-active hermes-gateway.service
```

Validate:

```bash
curl -fsS http://127.0.0.1:8642/health
curl -fsS \
  -H "Authorization: Bearer $API_SERVER_KEY_VALUE" \
  http://127.0.0.1:8642/v1/capabilities
```

The capabilities response must advertise:

- native session creation and message history;
- run submission;
- run status;
- run events;
- run stop.

### 4. Prepare the bridge state directory

Create the bridge state directory with restrictive permissions and ownership:

```bash
sudo install -d -o "$BRIDGE_UID" -g "$BRIDGE_GID" -m 700 "$BRIDGE_STATE_DIR"
```

This directory is bound into the container as `${BRIDGE_STATE_DIR:-./data}` and hosts the SQLite registry. Prepare it before `compose up`.

### 5. Configure the bridge

```bash
cd /opt/hermes-mcp-bridge
cp .env.example .env
chmod 600 .env
```

Set the following values without logging the key:

```text
HERMES_API_BASE_URL=http://127.0.0.1:8642
HERMES_API_KEY=<same dedicated value>
HERMES_MODEL=hermes-agent
HERMES_REQUEST_TIMEOUT_SECONDS=30
HERMES_RUN_POLL_INTERVAL_SECONDS=1
HERMES_RUN_MAX_WAIT_SECONDS=900
HERMES_PROGRESS_INTERVAL_SECONDS=15
HERMES_EVENT_STREAM_CONNECT_TIMEOUT_SECONDS=30
HERMES_RUN_DEFAULT_WAIT_SECONDS=45
MCP_HOST=127.0.0.1
MCP_PORT=8765
MCP_PATH=/mcp
LOG_LEVEL=INFO
BRIDGE_STATE_DIR=/opt/hermes-mcp-bridge/data
BRIDGE_UID=<numeric host UID for the container user>
BRIDGE_GID=<numeric host GID for the container user>
```

Unset the temporary shell variable after writing the protected files:

```bash
unset API_SERVER_KEY_VALUE
```

### 6. Validate and start the container

```bash
cd /opt/hermes-mcp-bridge
docker compose config
docker compose build
docker compose up -d
docker compose ps
docker compose logs --tail=100 hermes-mcp-bridge
```

Confirm the security posture:

```bash
docker inspect hermes-mcp-bridge
ss -lntp | grep -E ':(8642|8765)\b'
```

Expected properties:

- non-root container user;
- read-only root filesystem;
- all Linux capabilities dropped;
- `no-new-privileges` enabled;
- tmpfs `/tmp`;
- no Docker socket or Hermes directory mounts;
- API and bridge listeners restricted to loopback.

### 7. Run static and unit validation

Use a development environment on the host, not inside the runtime container:

```bash
cd /opt/hermes-mcp-bridge
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
python -m compileall src tests scripts
python -m ruff check .
python -m pytest -q
```

### 8. Run MCP discovery and health

```bash
python scripts/smoke_test.py --url http://127.0.0.1:8765/mcp
```

Confirm the 26 expected tools and the updated schemas.

### 9. Validate connected execution

Run an explicitly read-only prompt and display progress from the original MCP call:

```bash
python scripts/smoke_test.py \
  --url http://127.0.0.1:8765/mcp \
  --wait-seconds 1800 \
  --prompt "Execute a read-only validation lasting several minutes and return one final summary."
```

Confirm:

- MCP response mode is SSE, not JSON-only;
- progress callback receives run acceptance and lifecycle messages;
- heartbeat is emitted at the configured interval when no significant event occurs;
- the original `call_tool` remains open;
- the final Hermes output returns through that same call;
- no manual `hermes_status` call is needed during normal connected operation.

Also validate:

- `wait_seconds=0` returns immediately for detached operation;
- a disconnected client leaves the Hermes run active by default;
- `stop_on_disconnect=true` requests Hermes cancellation;
- `hermes_status`, `hermes_wait` and `hermes_stop` remain functional recovery tools.

Do not create a remote tunnel until connected execution passes locally.

## Phase 2 — remote exposure

After local approval, point a dedicated Cloudflare Tunnel hostname only to:

```text
http://127.0.0.1:8765
```

Do not expose `127.0.0.1:8642`.

For the MCP route:

- preserve streaming responses and MCP headers;
- disable caching, transformation and response buffering;
- configure an authentication flow supported by the intended MCP client;
- test 5, 15, 30 and 60 minute connected runs through the real tunnel.

A heartbeat prevents an idle connection, but does not prove that Cloudflare or the final client has no absolute duration limit.

## Upgrade

```bash
cd /opt/hermes-mcp-bridge
git fetch --all --prune
git checkout main
git pull --ff-only
docker compose build
docker compose up -d
docker compose ps
```

Run discovery, health and a connected smoke test after every upgrade.

## Rollback

Stop and remove only the bridge:

```bash
cd /opt/hermes-mcp-bridge
docker compose down
```

Restore the Hermes environment backup when the API server was enabled solely for this integration:

```bash
install -m 600 ~/.hermes/.env.pre-hermes-mcp-bridge ~/.hermes/.env
systemctl --user restart hermes-gateway.service
```

Confirm the previous state:

```bash
systemctl --user is-active hermes-gateway.service
ss -lntp | grep -E ':(8642|8765)\b' || true
docker ps -a --filter name=hermes-mcp-bridge
```

When rolling back from 0.3 to 0.2, preserve the state database directory. The older version ignores the registry and does not delete state data; leave the directory intact for future upgrades.

Repository removal is a separate, explicit action and is not required for rollback.
