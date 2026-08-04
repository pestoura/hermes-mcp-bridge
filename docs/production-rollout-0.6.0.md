# Production rollout runbook — Hermes MCP Bridge 0.6.0

## Purpose

Deploy Hermes MCP Bridge `0.6.0` from an explicitly approved Git commit, with a verified rollback path, while leaving RITMO and all dispatchers unchanged until the bridge passes production validation.

Initial approved code commit:

```text
c79d5f6dde6170700f722393cfa9265b4a1e1e14
```

Record the exact final deployment SHA; a later documentation-only commit may become the final `main` SHA.

## Production timeout policy

```text
HERMES_RUN_DEFAULT_WAIT_SECONDS=45
HERMES_RUN_MAX_WAIT_SECONDS=900
```

The production maximum connected wait is 900 seconds. Longer work must use detached submission and later recovery through `execution_id`. The package default and older documents may still mention 7200 seconds; the protected production `.env` values above are authoritative for this rollout.

## Safety boundaries

- Do not modify RITMO tasks, runs, leases or dispatchers.
- Do not reconcile historical RITMO runs.
- Do not expose the Hermes API outside loopback.
- Do not print, copy into evidence or commit `.env` values, API keys, tokens or credentials.
- Do not retain full `docker inspect` or rendered Compose output: both can contain secret environment values.
- Do not prune images, containers or volumes.
- Do not delete the previous image or SQLite backup until rollback has been exercised.
- Stop if the active deployment, database or rollback image cannot be identified deterministically.

## Phase 0 — read-only preflight

```bash
set -eu
cd /opt/hermes-mcp-bridge

hostname
id
command -v git
command -v docker
command -v sqlite3
git remote -v
git status --short --branch
git rev-parse HEAD
git rev-parse origin/main

docker compose version
docker compose config --services
docker compose ps
docker ps -a --filter name=hermes-mcp-bridge

docker inspect hermes-mcp-bridge --format \
  'name={{.Name}} image_ref={{.Config.Image}} image_id={{.Image}} status={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} restart={{.HostConfig.RestartPolicy.Name}} network={{.HostConfig.NetworkMode}} user={{.Config.User}} readonly={{.HostConfig.ReadonlyRootfs}}'

docker inspect hermes-mcp-bridge --format '{{range .Mounts}}{{println .Type .Source .Destination .RW}}{{end}}'
ss -lntp | grep -E ':(8642|8765)\b' || true
df -h / /opt/hermes-mcp-bridge
docker system df
```

Required facts:

- active directory: `/opt/hermes-mcp-bridge`;
- active service/container: `hermes-mcp-bridge`;
- listeners: Hermes API on `127.0.0.1:8642`, bridge on `127.0.0.1:8765`;
- current Git SHA, image ID, state mount and restart policy recorded;
- enough disk capacity for one candidate image, one rollback tag and SQLite evidence.

## Phase 1 — create sanitized rollback evidence

```bash
umask 077
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
EVIDENCE_DIR="/opt/hermes-mcp-bridge/rollout-evidence/$STAMP"
install -d -m 700 "$EVIDENCE_DIR"

CURRENT_GIT_SHA="$(git rev-parse HEAD)"
CURRENT_IMAGE_ID="$(docker inspect -f '{{.Image}}' hermes-mcp-bridge)"
CURRENT_IMAGE_REF="$(docker inspect -f '{{.Config.Image}}' hermes-mcp-bridge)"
ROLLBACK_TAG="hermes-mcp-bridge:rollback-$STAMP"

printf '%s\n' "$CURRENT_GIT_SHA" > "$EVIDENCE_DIR/pre-deploy-git-sha.txt"
printf '%s\n' "$CURRENT_IMAGE_ID" > "$EVIDENCE_DIR/rollback-image-id.txt"
printf '%s\n' "$CURRENT_IMAGE_REF" > "$EVIDENCE_DIR/pre-deploy-image-ref.txt"
printf '%s\n' "$ROLLBACK_TAG" > "$EVIDENCE_DIR/rollback-image-tag.txt"

docker inspect hermes-mcp-bridge --format \
  'name={{.Name}} image_ref={{.Config.Image}} image_id={{.Image}} status={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} restart={{.HostConfig.RestartPolicy.Name}} network={{.HostConfig.NetworkMode}} user={{.Config.User}} readonly={{.HostConfig.ReadonlyRootfs}} cap_drop={{json .HostConfig.CapDrop}} security_opt={{json .HostConfig.SecurityOpt}}' \
  > "$EVIDENCE_DIR/pre-deploy-container-metadata.txt"

docker inspect hermes-mcp-bridge --format '{{range .Mounts}}{{println .Type .Source .Destination .RW}}{{end}}' \
  > "$EVIDENCE_DIR/pre-deploy-mounts.txt"

docker image inspect "$CURRENT_IMAGE_ID" --format \
  'id={{.Id}} tags={{json .RepoTags}} created={{.Created}} size={{.Size}} architecture={{.Architecture}} os={{.Os}}' \
  > "$EVIDENCE_DIR/pre-deploy-image-metadata.txt"

docker tag "$CURRENT_IMAGE_ID" "$ROLLBACK_TAG"
```

Do not run or store full `docker inspect`, `docker compose config`, `env`, `set` or `printenv` output.

## Phase 2 — locate, validate and back up SQLite

Resolve the state mount from `pre-deploy-mounts.txt`. The expected host database is:

```text
/opt/hermes-mcp-bridge/data/state.sqlite3
```

Use the actual resolved path:

```bash
STATE_DB="/opt/hermes-mcp-bridge/data/state.sqlite3"
STATE_BACKUP="$EVIDENCE_DIR/state.pre-0.6.0.sqlite3"

stat -c 'path=%n owner=%u:%g mode=%a size=%s' "$STATE_DB" \
  > "$EVIDENCE_DIR/pre-deploy-state-metadata.txt"
sqlite3 "$STATE_DB" 'PRAGMA integrity_check;'
sqlite3 "$STATE_DB" ".backup '$STATE_BACKUP'"
chmod 600 "$STATE_BACKUP"
sqlite3 "$STATE_BACKUP" 'PRAGMA integrity_check;'
sqlite3 "$STATE_DB" '.tables' > "$EVIDENCE_DIR/pre-deploy-tables.txt"
sqlite3 "$STATE_DB" 'PRAGMA journal_mode; PRAGMA synchronous; PRAGMA user_version;' \
  > "$EVIDENCE_DIR/pre-deploy-sqlite-pragmas.txt"
```

Do not use plain `cp` against the live database while Write-Ahead Logging (WAL) may be active.

## Phase 3 — validate the exact source

```bash
cd /opt/hermes-mcp-bridge
git fetch --all --prune
git checkout main
git pull --ff-only
TARGET_SHA="$(git rev-parse HEAD)"
git status --short
```

Confirm that `TARGET_SHA` is approved and the worktree is clean.

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

Expected initial baseline:

```text
107 passed
26 MCP tools
```

## Phase 4 — build an immutable candidate

```bash
SHORT_SHA="$(git rev-parse --short=12 HEAD)"
CANDIDATE_TAG="hermes-mcp-bridge:0.6.0-$SHORT_SHA"
docker build --pull --tag "$CANDIDATE_TAG" .
docker image inspect "$CANDIDATE_TAG" --format \
  'id={{.Id}} tags={{json .RepoTags}} created={{.Created}} size={{.Size}} architecture={{.Architecture}} os={{.Os}}' \
  > "$EVIDENCE_DIR/candidate-image-metadata.txt"
```

## Phase 5 — isolated migration and smoke

Create the candidate state from the consistent backup and preserve the production database owner:

```bash
CANDIDATE_STATE_DIR="$EVIDENCE_DIR/candidate-state"
STATE_OWNER="$(stat -c '%u:%g' "$STATE_DB")"
install -d -m 700 "$CANDIDATE_STATE_DIR"
cp "$STATE_BACKUP" "$CANDIDATE_STATE_DIR/state.sqlite3"
chown -R "$STATE_OWNER" "$CANDIDATE_STATE_DIR"
chmod 600 "$CANDIDATE_STATE_DIR/state.sqlite3"
```

Run on an alternate loopback port:

```bash
docker run --rm -d \
  --name hermes-mcp-bridge-candidate \
  --network host \
  --user "$STATE_OWNER" \
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

Validate without displaying secret environment values:

```bash
python scripts/smoke_test.py --url http://127.0.0.1:18765/mcp
sqlite3 "$CANDIDATE_STATE_DIR/state.sqlite3" 'PRAGMA integrity_check;'
docker logs --tail=200 hermes-mcp-bridge-candidate
```

Required gates:

- bridge and schema version `0.6.0`;
- exactly 26 expected tools;
- health `ok`, registry `up`;
- capability manifest and agent card valid;
- approvals, plans, checkpoints, continuations, sagas, locks and quotas available;
- migrated database copy passes `integrity_check`;
- output contains no secret values.

```bash
docker stop hermes-mcp-bridge-candidate
```

## Phase 6 — promote the exact candidate

Verify only the two non-secret timeout entries:

```bash
awk -F= '$1=="HERMES_RUN_DEFAULT_WAIT_SECONDS" || $1=="HERMES_RUN_MAX_WAIT_SECONDS" {print $1"="$2}' .env
```

Required values:

```text
HERMES_RUN_DEFAULT_WAIT_SECONDS=45
HERMES_RUN_MAX_WAIT_SECONDS=900
```

The Compose file currently names the production image `ghcr.io/pestoura/hermes-mcp-bridge:local`. Promote the already-tested candidate to that local tag; do not rebuild between candidate smoke and production promotion.

```bash
PRODUCTION_IMAGE_TAG="ghcr.io/pestoura/hermes-mcp-bridge:local"
docker tag "$CANDIDATE_TAG" "$PRODUCTION_IMAGE_TAG"
docker compose stop hermes-mcp-bridge
docker compose up -d --no-build --no-deps hermes-mcp-bridge
docker compose ps
docker compose logs --tail=200 hermes-mcp-bridge
```

The production database migration occurs on startup. Keep `STATE_BACKUP` intact.

## Phase 7 — production smoke

```bash
python scripts/smoke_test.py --url http://127.0.0.1:8765/mcp
```

Confirm and record:

- version `0.6.0` and 26 tools;
- `default_wait_seconds=45`, `max_wait_seconds=900`;
- registry, approvals, plans, checkpoints, continuations, sagas, locks and quotas;
- harmless read-only detached submission returns an `execution_id`;
- status recovery works;
- listeners remain loopback-only;
- Cloudflare Tunnel exposes only the bridge, never port 8642.

Perform one controlled restart and repeat health, database integrity and tool discovery.

## Phase 8 — ChatGPT rediscovery

1. Refresh or reconnect the custom MCP app.
2. Confirm exactly 26 tools.
3. Validate health and one harmless detached read-only execution from at least two conversations.
4. Confirm distinct execution/session identifiers.
5. Do not invoke or alter RITMO.

## Phase 9 — mandatory rollback exercise

Stop the new container and restore the pre-deployment database:

```bash
docker compose stop hermes-mcp-bridge
install -m 600 -o "$(stat -c '%u' "$STATE_DB")" -g "$(stat -c '%g' "$STATE_DB")" \
  "$STATE_BACKUP" "$STATE_DB"
sqlite3 "$STATE_DB" 'PRAGMA integrity_check;'
```

Restore the previous image deterministically:

```bash
docker tag "$ROLLBACK_TAG" "$PRODUCTION_IMAGE_TAG"
docker compose up -d --no-build --no-deps hermes-mcp-bridge
docker compose ps
python scripts/smoke_test.py --url http://127.0.0.1:8765/mcp
```

Required rollback checks:

- container uses `CURRENT_IMAGE_ID`;
- previous version and tool inventory are restored;
- health is `ok`;
- SQLite integrity passes;
- no RITMO state was changed.

Redeploy the approved candidate after rollback proof:

```bash
docker tag "$CANDIDATE_TAG" "$PRODUCTION_IMAGE_TAG"
docker compose stop hermes-mcp-bridge
docker compose up -d --no-build --no-deps hermes-mcp-bridge
python scripts/smoke_test.py --url http://127.0.0.1:8765/mcp
```

## Evidence and decision

Record only sanitized evidence:

- pre/post Git SHA;
- previous, candidate and final image IDs;
- SQLite backup path and integrity results;
- tool inventories and health outputs;
- candidate, production and rollback smoke results;
- listener/tunnel checks;
- exact rollback commands;
- final decision.

Allowed decisions:

```text
PRODUCTION_0_6_0_PASS
PRODUCTION_0_6_0_PARTIAL
PRODUCTION_0_6_0_FAIL_ROLLED_BACK
```

Do not start RITMO lifecycle integration until the decision is `PRODUCTION_0_6_0_PASS`.
