#!/usr/bin/env bash
# Rollback from 0.8.1 to the previous known-good image. DRY-RUN BY DEFAULT.
#
# Mutating mode requires BOTH EXECUTE_DEPLOYMENT=YES and a matching
# EXPECTED_SHA. The rollback never touches the SQLite state file: the schema
# (0.6.1) is unchanged across 0.8.0/0.8.1, so no data migration is reversed.

set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$HERE/lib.sh"

REQUIRED_SHA="${REQUIRED_SHA:-}"
COMPOSE_FILE="${COMPOSE_FILE:-$HERE/compose.rollback.yml}"
ROLLBACK_IMAGE="${ROLLBACK_IMAGE:-hermes-mcp-bridge:rollback-0.8.0-9c7fc64}"
ROLLBACK_IMAGE_ID="${ROLLBACK_IMAGE_ID:-}"
ROLLBACK_BRIDGE_VERSION="${ROLLBACK_BRIDGE_VERSION:-0.8.0}"
ROLLBACK_TOOL_COUNT="${ROLLBACK_TOOL_COUNT:-27}"
SETTLE_SECONDS="${SETTLE_SECONDS:-12}"

require_cmd docker
require_file "$COMPOSE_FILE"

docker image inspect "$ROLLBACK_IMAGE" >/dev/null 2>&1 \
  || fail "tag de rollback ausente: $ROLLBACK_IMAGE"
if [ -n "$ROLLBACK_IMAGE_ID" ]; then
  assert_image_id "$ROLLBACK_IMAGE" "$ROLLBACK_IMAGE_ID"
else
  warn "ROLLBACK_IMAGE_ID nao definido: ID da imagem nao verificado"
fi

if [ -z "$REQUIRED_SHA" ] || ! is_execute_mode "$REQUIRED_SHA"; then
  log "DRY_RUN: nenhuma accao mutavel executada."
  log "DRY_RUN: projeto compose fixo = $COMPOSE_PROJECT"
  log "DRY_RUN: executaria docker compose -p $COMPOSE_PROJECT -f $COMPOSE_FILE up -d"
  log "DRY_RUN: validaria bridge_version=$ROLLBACK_BRIDGE_VERSION tools=$ROLLBACK_TOOL_COUNT"
  compose_config_check "$COMPOSE_FILE"
  log "ROLLBACK: DRY_RUN OK"
  exit 0
fi

current_image=""
if docker inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
  current_image="$(docker inspect "$CONTAINER_NAME" --format '{{.Config.Image}}')"
fi

if [ "$current_image" = "$ROLLBACK_IMAGE" ]; then
  ok "container ja na imagem de rollback; nao recria (idempotente)"
else
  compose "$COMPOSE_FILE" up -d
  sleep "$SETTLE_SECONDS"
fi

MCP_PORT="${MCP_PORT:-8765}" \
EXPECT_BRIDGE_VERSION="$ROLLBACK_BRIDGE_VERSION" \
EXPECT_TOOL_COUNT="$ROLLBACK_TOOL_COUNT" \
  bash "$HERE/validate.sh" || fail "validacao pos-rollback falhou"

log "ROLLBACK: PASS"
