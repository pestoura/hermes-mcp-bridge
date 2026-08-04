# Production rollout runbook — Hermes MCP Bridge 0.6.0

## Purpose

Deploy Hermes MCP Bridge `0.6.0` from an explicitly approved Git commit while preserving a verified rollback path and leaving RITMO and its dispatchers unchanged until the bridge is validated.

Initial approved code commit:

```text
c79d5f6dde6170700f722393cfa9265b4a1e1e14
```

A later documentation-only commit may become the final deployment commit. Record the exact deployed SHA in the evidence.

## Production timeout policy

The production profile is:

```text
HERMES_RUN_DEFAULT_WAIT_SECONDS=45
HERMES_RUN_MAX_WAIT_SECONDS=900
```

`900` seconds is the maximum connected wait budget for the production bridge. Longer jobs must use detached submission and later recovery through `execution_id`. Do not increase the connected wait budget to support long-running scheduled work.

The package default and older documentation may still mention `7200`; the production `.env` value above is authoritative for this rollout.

## Safety boundaries

During this rollout:

- do not modify RITMO tasks, runs, leases or dispatchers;
- do not reconcile old RITMO runs;
- do not expose the Hermes API server outside loopback;
- do not print `.env` values, bearer keys, tokens or other secrets;
- do not prune Docker images, containers or volumes;
- do not delete the previous image, container metadata or SQLite backup until rollback has been exercised;
- stop immediately if the current deployment cannot be identified deterministically.

## Phase 0 — read-only preflight

Run from the Hermes host:

```bash
set -eu
cd /opt/hermes-mcp-bridge

hostname
id
git remote -v
git status --short --branch
git rev-parse HEAD
git rev-parse origin/main

docker compose version
docker compose config --services
docker compose ps
docker ps -a --filter name=hermes-mcp-bridge
docker inspect hermes-mcp-bridge --format '{{json .Config.Image}} {{json .Image}} {{json .State.Status}} {{json .State.Health.Status}}'

df -h / /opt/hermes-mcp-bridge
docker system df
```

Confirm that:

- `/opt/hermes-mcp-bridge` is the active deployment directory;
- the running container is `hermes-mcp-bridge`;
- listeners remain on `127.0.0.1:8642` and `127.0.0.1:8765`;
- the bridge health endpoint is reachable;
- the current image ID and Git SHA are recorded;
- sufficient disk space exists for the candidate and rollback images.

Do not continue if any of these facts are ambiguous.

## Phase 1 — capture rollback evidence

Create an evidence directory with restrictive permissions:

```bash
umask 077
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
EVIDENCE_DIR="/opt/hermes-mcp-bridge/rollout-evidence/$STAMP"
install -d -m 700 "$EVIDENCE_DIR"
```

Record sanitized deployment metadata:

```bash
git rev-parse HEAD > "$EVIDENCE_DIR/pre-deploy-git-sha.txt"
docker inspect hermes-mcp-bridge > "$EVIDENCE_DIR/pre-deploy-container-inspect.json"
docker image inspect "$(docker inspect -f '{{.Image}}' hermes-mcp-bridge)" > "$EVIDENCE_DIR/pre-deploy-image-inspect.json"
docker compose config --no-interpolate > "$EVIDENCE_DIR/compose-config-sanitized.txt"
```

Review the compose output before retaining it. Remove any accidental secret values; `--no-interpolate` should preserve variable references.

Tag the current image without rebuilding it:

```bash
CURRENT_IMAGE_ID="$(docker inspect -f '{{.Image}}' hermes-mcp-bridge)"
ROLLBACK_TAG="hermes-mcp-bridge:rollback-$STAMP"
docker tag "$CURRENT_IMAGE_ID" "$ROLLBACK_TAG"
printf '%s\n' "$CURRENT_IMAGE_ID" > "$EVIDENCE_DIR/rollback-image-id.txt"
printf '%s\n' "$ROLLBACK_TAG" > "$EVIDENCE_DIR/rollback-image-tag.txt"
```

## Phase 2 — back up and validate SQLite

Resolve the host state directory from the container mount. Do not assume the path if the mount differs from the documented default.

Expected database:

```text
/opt/hermes-mcp-bridge/data/state.sqlite3
```

Create a SQLite-consistent backup:

```bash
STATE_DB="/opt/hermes-mcp-bridge/data/state.sqlite3"
STATE_BACKUP="$EVIDENCE_DIR/state.pre-0.6.0.sqlite3"

sqlite3 "$STATE_DB" 'PRAGMA integrity_check;'
sqlite3 "$STATE_DB" ".backup '$STATE_BACKUP'"
chmod 600 "$STATE_BACKUP"
sqlite3 "$STATE_BACKUP" 'PRAGMA integrity_check;'
```

Record schema-only evidence:

```bash
sqlite3 "$STATE_DB" '.tables' > "$EVIDENCE_DIR/pre-deploy-tables.txt"
sqlite3 "$STATE_DB" 'PRAGMA journal_mode; PRAGMA user_version;' > "$EVIDENCE_DIR/pre-deploy-sqlite-pragmas.txt"
```

Do not copy the live database with plain `cp` while Write-Ahead Logging (WAL) is active unless the service is stopped and the WAL files are handled consistently.

## Phase 3 — prepare exact candidate source

```bash
cd /opt/hermes-mcp-bridge
git fetch --all --prune
git checkout main
git pull --ff-only
TARGET_SHA="$(git rev-parse HEAD)"
git status --short
```

Confirm that `TARGET_SHA` is the approved deployment SHA and that the working tree is clean.

Run the full validation suite on the exact source:

```bash
python3 -m venv .venv-rollout
. .venv-rollout/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
python -m compileall src tests scripts
python -m ruff check .
python -m pytest -q
git diff --check
```

Expected baseline for the initial 0.6.0 code commit:

```text
107 passed
26 MCP tools
```

## Phase 4 — build immutable candidate

```bash
SHORT_SHA="$(git rev-parse --short=12 HEAD)"
CANDIDATE_TAG="hermes-mcp-bridge:0.6.0-$SHORT_SHA"
docker build --pull --tag "$CANDIDATE_TAG" .
docker image inspect "$CANDIDATE_TAG" > "$EVIDENCE_DIR/candidate-image-inspect.json"
```

Do not overwrite the rollback tag.

## Phase 5 — isolated candidate migration and smoke

Use a copy of the production backup, not the production database:

```bash
CANDIDATE_STATE_DIR="$EVIDENCE_DIR/candidate-state"
install -d -m 700 "$CANDIDATE_STATE_DIR"
cp "$STATE_BACKUP" "$CANDIDATE_STATE_DIR/state.sqlite3"
chmod 600 "$CANDIDATE_STATE_DIR/state.sqlite3"
```

Run the candidate on an alternate loopback port using the protected production `.env` plus explicit non-secret overrides:

```bash
docker run --rm -d \
  --name hermes-mcp-bridge-candidate \
  --network host \
  --user "${BRIDGE_UID:-1000}:${BRIDGE_GID:-1000}" \
  --read-only \
  --security-opt no-new-privileges:true \
  --cap-drop ALL \
  --tmpfs /tmp:size=32m,mode=1777 \
  --env-file .env \
  -e MCP_PORT=18765 \
  -e HERMES_RUN_DEFAULT_WAIT_SECONDS=45 \
  -e HERMES_RUN_MAX_WAIT_SECONDS=900 \
  -e BRIDGE_STATE_DB_PATH=/var/lib/hermes-mcp-bridge/state.sqlite3 \
  -v "$CANDIDATE_STATE_DIR:/var/lib/hermes-mcp-bridge" \
  "$CANDIDATE_TAG"
```

Validate:

```bash
python scripts/smoke_test.py --url http://127.0.0.1:18765/mcp
sqlite3 "$CANDIDATE_STATE_DIR/state.sqlite3" 'PRAGMA integrity_check;'
docker logs --tail=200 hermes-mcp-bridge-candidate
```

Required results:

- bridge/schema version `0.6.0`;
- exactly 26 expected tools;
- health `ok`;
- registry `up`;
- capability manifest and agent card valid;
- approvals, plans, checkpoints, continuations, sagas, locks and quotas available;
- no secrets in output;
- migrated SQLite copy passes `integrity_check`.

Stop the isolated candidate after evidence is captured:

```bash
docker stop hermes-mcp-bridge-candidate
```

## Phase 6 — production promotion

Only continue after every candidate gate passes.

Record the intended production variables without printing secret values:

```bash
for key in \
  HERMES_API_BASE_URL HERMES_API_KEY HERMES_MODEL \
  HERMES_RUN_DEFAULT_WAIT_SECONDS HERMES_RUN_MAX_WAIT_SECONDS \
  MCP_HOST MCP_PORT MCP_PATH BRIDGE_STATE_DIR BRIDGE_STATE_DB_PATH; do
  if grep -q "^${key}=" .env; then
    printf '%s=SET\n' "$key"
  else
    printf '%s=MISSING\n' "$key"
  fi
done
```

Set or confirm the production timeout values in `.env` without exposing the file:

```text
HERMES_RUN_DEFAULT_WAIT_SECONDS=45
HERMES_RUN_MAX_WAIT_SECONDS=900
```

Promote using Compose:

```bash
docker compose stop hermes-mcp-bridge
docker compose build --no-cache hermes-mcp-bridge
docker compose up -d --no-deps hermes-mcp-bridge
docker compose ps
docker compose logs --tail=200 hermes-mcp-bridge
```

The production database migration occurs on startup. Do not delete the backup.

## Phase 7 — production smoke

```bash
python scripts/smoke_test.py --url http://127.0.0.1:8765/mcp
```

Confirm and record:

- version `0.6.0`;
- 26 tools;
- health and registry status;
- `default_wait_seconds=45`;
- `max_wait_seconds=900`;
- persistent approvals, plans, checkpoints, continuations, sagas, locks and quotas;
- detached submission returns an `execution_id`;
- status recovery works;
- registry remains valid after one controlled container restart;
- listeners remain loopback-only;
- Cloudflare Tunnel exposes only the bridge endpoint, never the Hermes API.

Use a harmless read-only objective for any real Hermes execution.

## Phase 8 — ChatGPT MCP rediscovery

After production smoke passes:

1. refresh or reconnect the custom MCP app in ChatGPT;
2. confirm the client discovers exactly 26 tools;
3. validate health and a read-only detached execution from at least two conversations;
4. confirm each conversation receives distinct session/execution identifiers;
5. do not test or modify RITMO yet.

## Phase 9 — rollback exercise

Rollback is mandatory before declaring the rollout complete.

Stop the new container:

```bash
docker compose stop hermes-mcp-bridge
```

Restore the pre-deployment SQLite backup before running the previous image:

```bash
install -m 600 "$STATE_BACKUP" "$STATE_DB"
sqlite3 "$STATE_DB" 'PRAGMA integrity_check;'
```

Start the previous image with the same runtime security controls and configuration, or temporarily point Compose at `ROLLBACK_TAG`. Verify the exact rollback image ID before starting it.

Required rollback checks:

- previous version starts;
- previous tool inventory is restored;
- health is `ok`;
- state database integrity passes;
- no Hermes or RITMO state was modified unexpectedly.

After rollback is proven, redeploy the approved 0.6.0 candidate and repeat the production smoke.

## Evidence and final decision

Record:

- pre- and post-deploy Git SHAs;
- previous, candidate and final image IDs;
- SQLite integrity results and backup path;
- tool inventories;
- health outputs with secrets sanitized;
- candidate, production and rollback smoke results;
- listener and tunnel checks;
- exact rollback command used;
- final decision.

Allowed decisions:

```text
PRODUCTION_0_6_0_PASS
PRODUCTION_0_6_0_PARTIAL
PRODUCTION_0_6_0_FAIL_ROLLED_BACK
```

Do not start RITMO lifecycle integration work until the final decision is `PRODUCTION_0_6_0_PASS`.
