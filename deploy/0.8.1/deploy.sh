#!/usr/bin/env bash
# Controlled 0.8.1 deployment. DRY-RUN BY DEFAULT.
#
# Mutating mode requires BOTH:
#   EXECUTE_DEPLOYMENT=YES
#   EXPECTED_SHA=<release sha>   (must equal REQUIRED_SHA below or $REQUIRED_SHA env)
#
# Idempotent: re-running in execute mode with the container already on the
# candidate image performs no compose recreation and re-validates only.

set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$HERE/lib.sh"

REQUIRED_SHA="${REQUIRED_SHA:-}"
COMPOSE_FILE="${COMPOSE_FILE:-$HERE/compose.candidate.yml}"
CANDIDATE_IMAGE="${CANDIDATE_IMAGE:-hermes-mcp-bridge:0.8.1-candidate}"
BACKUP_DIR="${BACKUP_DIR:-/home/estourpm/hermes-mcp-bridge-deploy/backups}"
STATE_DB="${STATE_DB:-/home/estourpm/hermes-mcp-bridge/data/state.sqlite3}"
SETTLE_SECONDS="${SETTLE_SECONDS:-12}"

require_cmd docker python3
require_file "$COMPOSE_FILE"

CANDIDATE_IMAGE="$CANDIDATE_IMAGE" bash "$HERE/preflight.sh" || fail "preflight NO-GO"

if [ -z "$REQUIRED_SHA" ]; then
  warn "REQUIRED_SHA nao definido: apenas dry-run e permitido"
fi

if [ -z "$REQUIRED_SHA" ] || ! is_execute_mode "$REQUIRED_SHA"; then
  log "DRY_RUN: nenhuma accao mutavel executada."
  log "DRY_RUN: projeto compose fixo = $COMPOSE_PROJECT"
  log "DRY_RUN: passos que seriam executados:"
  log "  1. backup SQLite (python sqlite3 .backup) -> $BACKUP_DIR"
  log "  2. docker compose -p $COMPOSE_PROJECT -f $COMPOSE_FILE up -d"
  log "  3. validate.sh (contrato $BRIDGE_VERSION, $EXPECTED_TOOL_COUNT tools)"
  compose_config_check "$COMPOSE_FILE"
  log "DEPLOY: DRY_RUN OK"
  exit 0
fi

current_image=""
if docker inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
  current_image="$(docker inspect "$CONTAINER_NAME" --format '{{.Config.Image}}')"
fi

if [ "$current_image" = "$CANDIDATE_IMAGE" ]; then
  ok "container ja na imagem candidata; nao recria (idempotente)"
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
  ok "backup em $backup_path"

  compose "$COMPOSE_FILE" up -d
  sleep "$SETTLE_SECONDS"
fi

MCP_PORT="${MCP_PORT:-8765}" \
EXPECT_BRIDGE_VERSION="$BRIDGE_VERSION" \
EXPECT_TOOL_COUNT="$EXPECTED_TOOL_COUNT" \
  bash "$HERE/validate.sh" || {
    log "VALIDACAO FALHOU -> executar rollback.sh"
    exit 1
  }

log "DEPLOY: PASS"
