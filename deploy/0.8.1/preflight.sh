#!/usr/bin/env bash
# Read-only preflight for the 0.8.1 rollout. Never mutates state.
#
# Usage:
#   ./preflight.sh
#
# Environment:
#   EXPECTED_SHA_0_8_1   git SHA expected on the candidate image label
#   CANDIDATE_IMAGE      candidate image tag
#   ROLLBACK_IMAGE       rollback image tag
#   ROLLBACK_IMAGE_ID    expected image ID of ROLLBACK_IMAGE
#   BRIDGE_ENV_FILE      deployment env file (existence checked only)
#   BRIDGE_STATE_DIR     state directory (existence checked only)

set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$HERE/lib.sh"

CANDIDATE_IMAGE="${CANDIDATE_IMAGE:-hermes-mcp-bridge:0.8.1-candidate}"
ROLLBACK_IMAGE="${ROLLBACK_IMAGE:-hermes-mcp-bridge:rollback-0.8.0-9c7fc64}"
ROLLBACK_IMAGE_ID="${ROLLBACK_IMAGE_ID:-}"
EXPECTED_SHA_0_8_1="${EXPECTED_SHA_0_8_1:-}"
BRIDGE_ENV_FILE="${BRIDGE_ENV_FILE:-/home/estourpm/hermes-mcp-bridge/.env}"
BRIDGE_STATE_DIR="${BRIDGE_STATE_DIR:-/home/estourpm/hermes-mcp-bridge/data}"
MIN_FREE_KB="${MIN_FREE_KB:-5242880}"

require_cmd docker df awk

if [ -n "$EXPECTED_SHA_0_8_1" ]; then
  assert_image_revision "$CANDIDATE_IMAGE" "$EXPECTED_SHA_0_8_1"
else
  docker image inspect "$CANDIDATE_IMAGE" >/dev/null 2>&1 \
    || fail "imagem candidata ausente: $CANDIDATE_IMAGE"
  warn "EXPECTED_SHA_0_8_1 nao definido: revision da imagem nao verificada"
fi

if [ -n "$ROLLBACK_IMAGE_ID" ]; then
  assert_image_id "$ROLLBACK_IMAGE" "$ROLLBACK_IMAGE_ID"
else
  docker image inspect "$ROLLBACK_IMAGE" >/dev/null 2>&1 \
    || fail "tag de rollback ausente: $ROLLBACK_IMAGE"
  warn "ROLLBACK_IMAGE_ID nao definido: ID da tag de rollback nao verificado"
fi

if docker inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
  state="$(docker inspect "$CONTAINER_NAME" \
    --format '{{.State.Health.Status}}|{{.RestartCount}}')"
  log "producao: $state"
  [ "${state%%|*}" = "healthy" ] || fail "producao nao healthy"
  ok "producao healthy"
else
  warn "container $CONTAINER_NAME ausente: primeira instalacao?"
fi

require_file "$BRIDGE_ENV_FILE"
[ -d "$BRIDGE_STATE_DIR" ] || fail "state dir ausente: $BRIDGE_STATE_DIR"
ok "env_file e state dir presentes"

avail_kb="$(df -Pk "$BRIDGE_STATE_DIR" | awk 'NR==2{print $4}')"
[ -n "$avail_kb" ] || fail "nao foi possivel medir espaco em disco"
[ "$avail_kb" -gt "$MIN_FREE_KB" ] || fail "espaco em disco insuficiente"
ok "espaco em disco suficiente (${avail_kb}KB livres)"

for f in "$HERE/compose.candidate.yml" "$HERE/compose.rollback.yml"; do
  [ -f "$f" ] && compose_config_check "$f"
done

log "PREFLIGHT: GO"
