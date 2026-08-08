#!/usr/bin/env bash
# Controlled 1.0.0/1.x deployment. DRY-RUN BY DEFAULT.
#
# Mutation requires EXECUTE_DEPLOYMENT=YES and EXPECTED_SHA == REQUIRED_SHA.

set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$HERE/lib.sh"

REQUIRED_SHA="${REQUIRED_SHA:-}"
COMPOSE_FILE="${COMPOSE_FILE:-$HERE/compose.candidate.yml}"
OBSERVABILITY_COMPOSE_FILE="${OBSERVABILITY_COMPOSE_FILE:-$HERE/compose.observability.yml}"
BRIDGE_ENV_FILE="${BRIDGE_ENV_FILE:-/home/estourpm/hermes-mcp-bridge/.env}"
CANDIDATE_IMAGE="${CANDIDATE_IMAGE:-hermes-mcp-bridge:1.0.0-candidate}"
BACKUP_DIR="${BACKUP_DIR:-/home/estourpm/hermes-mcp-bridge-deploy/1.0.0/backups}"
EVIDENCE_DIR="${EVIDENCE_DIR:-/home/estourpm/hermes-mcp-bridge-deploy/1.0.0/evidence}"
STATE_DB="${STATE_DB:-/home/estourpm/hermes-mcp-bridge/data/state.sqlite3}"
HEALTH_SETTLE_SECONDS="${HEALTH_SETTLE_SECONDS:-}"
SBOM_FILE="${SBOM_FILE:-}"
SBOM_SHA256="${SBOM_SHA256:-}"

require_cmd docker python3 date sha256sum
require_file "$COMPOSE_FILE"
require_file "$OBSERVABILITY_COMPOSE_FILE"
require_file "$BRIDGE_ENV_FILE"
[ -n "$REQUIRED_SHA" ] || fail "REQUIRED_SHA obrigatorio, inclusive em dry-run"

# Keep the canonical compose call contract used by the 1.0 rollout while making
# this deploy command configuration-preserving. The additive overlay is always
# applied for the candidate; rollback.sh continues to use the base helper from
# lib.sh and its separately pinned rollback compose.
compose() {
  local file="$1"; shift
  docker compose --env-file "$BRIDGE_ENV_FILE" -p "$COMPOSE_PROJECT" \
    -f "$file" -f "$OBSERVABILITY_COMPOSE_FILE" "$@"
}

EXPECTED_SHA_1_0_0="$REQUIRED_SHA" \
CANDIDATE_IMAGE="$CANDIDATE_IMAGE" \
BRIDGE_ENV_FILE="$BRIDGE_ENV_FILE" \
OBSERVABILITY_COMPOSE_FILE="$OBSERVABILITY_COMPOSE_FILE" \
SBOM_FILE="$SBOM_FILE" \
SBOM_SHA256="$SBOM_SHA256" \
  bash "$HERE/preflight.sh" || fail "preflight NO-GO"

if ! is_execute_mode "$REQUIRED_SHA"; then
  log "DRY_RUN: nenhuma accao mutavel executada."
  log "DRY_RUN: projeto compose fixo = $COMPOSE_PROJECT"
  log "DRY_RUN: passos previstos:"
  log "  1. backup SQLite verificado -> $BACKUP_DIR"
  log "  2. restore real em destino isolado e cleanup"
  log "  3. docker compose candidate + observability up -d"
  log "  4. aguardar health com budget derivado"
  log "  5. validar contrato 1.x, seguranca e feature gates"
  compose "$COMPOSE_FILE" config >/dev/null || fail "compose candidate + observability invalido"
  log "DEPLOY_1_0_0: DRY_RUN OK"
  exit 0
fi

candidate_image_id="$(docker image inspect "$CANDIDATE_IMAGE" --format '{{.Id}}')"
current_image_id=""
if docker inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
  current_image_id="$(docker inspect "$CONTAINER_NAME" --format '{{.Image}}')"
fi

mkdir -p "$BACKUP_DIR" "$EVIDENCE_DIR"
chmod 700 "$BACKUP_DIR" "$EVIDENCE_DIR"

if [ "$current_image_id" = "$candidate_image_id" ]; then
  ok "container ja no ID imutavel da candidata; apenas revalidar"
else
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  backup_path="$BACKUP_DIR/state-$ts.sqlite3"
  require_file "$STATE_DB"
  create_verified_backup "$STATE_DB" "$backup_path"
  verify_backup_restore_isolated "$backup_path" "$EVIDENCE_DIR"
  backup_sha="$(sha256sum "$backup_path" | awk '{print $1}')"
  printf 'BACKUP_SHA256=%s\n' "$backup_sha" > "$EVIDENCE_DIR/backup-$ts.sha256"
  chmod 600 "$EVIDENCE_DIR/backup-$ts.sha256"
  ok "backup/restore evidence retida sem conteudo da base"

  export CANDIDATE_REVISION="$REQUIRED_SHA"
  compose "$COMPOSE_FILE" up -d --force-recreate
  export HEALTH_REQUIRE_HEALTHCHECK=1
  wait_for_health "$CONTAINER_NAME" "$HEALTH_SETTLE_SECONDS" \
    || fail "health nao estabilizou; executar rollback.sh"
fi

running_image_id="$(docker inspect "$CONTAINER_NAME" --format '{{.Image}}')"
[ "$running_image_id" = "$candidate_image_id" ] \
  || fail "contentor nao esta no ID imutavel da candidata"
ok "ID imutavel da candidata confirmado no contentor"

expect_metrics=0
if env_is_truthy "$BRIDGE_ENV_FILE" "BRIDGE_METRICS_ENABLED"; then
  expect_metrics=1
fi

MCP_PORT="${MCP_PORT:-8765}" \
EXPECT_BRIDGE_VERSION="$BRIDGE_VERSION" \
EXPECT_TOOL_COUNT="$EXPECTED_TOOL_COUNT" \
EXPECT_SCHEMA_VERSION="$SCHEMA_VERSION" \
EXPECT_METRICS_ENABLED="$expect_metrics" \
REQUIRE_1_0_SECURITY=1 \
  bash "$HERE/validate.sh" || fail "validacao falhou; executar rollback.sh"

validate_state_db_read_only "$STATE_DB"
log "HERMES_BRIDGE_1_0_0_PRODUCTION_PASS"
