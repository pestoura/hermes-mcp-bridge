#!/usr/bin/env bash
# Controlled 0.9.0 deployment. DRY-RUN BY DEFAULT.
#
# Mutation requires EXECUTE_DEPLOYMENT=YES and EXPECTED_SHA == REQUIRED_SHA.

set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$HERE/lib.sh"

REQUIRED_SHA="${REQUIRED_SHA:-}"
COMPOSE_FILE="${COMPOSE_FILE:-$HERE/compose.candidate.yml}"
CANDIDATE_IMAGE="${CANDIDATE_IMAGE:-hermes-mcp-bridge:0.9.0-candidate}"
BACKUP_DIR="${BACKUP_DIR:-/home/estourpm/hermes-mcp-bridge-deploy/backups}"
STATE_DB="${STATE_DB:-/home/estourpm/hermes-mcp-bridge/data/state.sqlite3}"
HEALTH_SETTLE_SECONDS="${HEALTH_SETTLE_SECONDS:-}"

require_cmd docker python3
require_file "$COMPOSE_FILE"
[ -n "$REQUIRED_SHA" ] || fail "REQUIRED_SHA obrigatorio, inclusive em dry-run"

EXPECTED_SHA_0_9_0="$REQUIRED_SHA" \
CANDIDATE_IMAGE="$CANDIDATE_IMAGE" \
  bash "$HERE/preflight.sh" || fail "preflight NO-GO"

if ! is_execute_mode "$REQUIRED_SHA"; then
  log "DRY_RUN: nenhuma accao mutavel executada."
  log "DRY_RUN: projeto compose fixo = $COMPOSE_PROJECT"
  log "DRY_RUN: passos previstos:"
  log "  1. backup SQLite por API sqlite3 -> $BACKUP_DIR"
  log "  2. docker compose -p $COMPOSE_PROJECT -f $COMPOSE_FILE up -d"
  log "  3. aguardar health com budget derivado"
  log "  4. validar contrato 0.9.0 e readiness de seguranca"
  compose_config_check "$COMPOSE_FILE"
  log "DEPLOY_0_9_0: DRY_RUN OK"
  exit 0
fi

current_image=""
if docker inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
  current_image="$(docker inspect "$CONTAINER_NAME" --format '{{.Config.Image}}')"
fi

if [ "$current_image" = "$CANDIDATE_IMAGE" ]; then
  ok "container ja na imagem candidata; apenas revalidar"
else
  mkdir -p "$BACKUP_DIR"
  chmod 700 "$BACKUP_DIR"
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  backup_path="$BACKUP_DIR/state-$ts.sqlite3"
  require_file "$STATE_DB"
  python3 - "$STATE_DB" "$backup_path" <<'PY'
import sqlite3
import sys

src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
try:
    src.backup(dst)
finally:
    dst.close()
    src.close()
PY
  chmod 600 "$backup_path"
  ok "backup SQLite criado com permissoes 0600"

  compose "$COMPOSE_FILE" up -d
  export HEALTH_REQUIRE_HEALTHCHECK=1
  wait_for_health "$CONTAINER_NAME" "$HEALTH_SETTLE_SECONDS" \
    || fail "health nao estabilizou; executar rollback.sh"
fi

MCP_PORT="${MCP_PORT:-8765}" \
EXPECT_BRIDGE_VERSION="$BRIDGE_VERSION" \
EXPECT_TOOL_COUNT="$EXPECTED_TOOL_COUNT" \
EXPECT_SCHEMA_VERSION="$SCHEMA_VERSION" \
REQUIRE_0_9_SECURITY=1 \
  bash "$HERE/validate.sh" || fail "validacao falhou; executar rollback.sh"

log "DEPLOY_0_9_0: PASS"
