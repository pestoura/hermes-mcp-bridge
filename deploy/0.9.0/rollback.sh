#!/usr/bin/env bash
# Controlled rollback from 0.9.0 to the exact known-good 0.8.2 image.
# DRY-RUN BY DEFAULT. SQLite is not reverted: schema remains 0.6.1.

set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$HERE/lib.sh"

REQUIRED_SHA="${REQUIRED_SHA:-}"
COMPOSE_FILE="${COMPOSE_FILE:-$HERE/compose.rollback.yml}"
ROLLBACK_IMAGE="${ROLLBACK_IMAGE:-}"
ROLLBACK_IMAGE_ID="${ROLLBACK_IMAGE_ID:-}"
ROLLBACK_BRIDGE_VERSION="${ROLLBACK_BRIDGE_VERSION:-0.8.2}"
ROLLBACK_TOOL_COUNT="${ROLLBACK_TOOL_COUNT:-27}"
HEALTH_SETTLE_SECONDS="${HEALTH_SETTLE_SECONDS:-}"

require_cmd docker
require_file "$COMPOSE_FILE"
[ -n "$REQUIRED_SHA" ] || fail "REQUIRED_SHA obrigatorio, inclusive em dry-run"
[ -n "$ROLLBACK_IMAGE" ] || fail "ROLLBACK_IMAGE obrigatorio"
[ -n "$ROLLBACK_IMAGE_ID" ] || fail "ROLLBACK_IMAGE_ID obrigatorio"
assert_image_id "$ROLLBACK_IMAGE" "$ROLLBACK_IMAGE_ID"

if ! is_execute_mode "$REQUIRED_SHA"; then
  log "DRY_RUN: nenhuma accao mutavel executada."
  log "DRY_RUN: projeto compose fixo = $COMPOSE_PROJECT"
  log "DRY_RUN: executaria docker compose -p $COMPOSE_PROJECT -f $COMPOSE_FILE up -d"
  log "DRY_RUN: validaria bridge=$ROLLBACK_BRIDGE_VERSION tools=$ROLLBACK_TOOL_COUNT"
  compose_config_check "$COMPOSE_FILE"
  log "ROLLBACK_0_9_0: DRY_RUN OK"
  exit 0
fi

current_image=""
if docker inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
  current_image="$(docker inspect "$CONTAINER_NAME" --format '{{.Config.Image}}')"
fi

if [ "$current_image" = "$ROLLBACK_IMAGE" ]; then
  ok "container ja na imagem de rollback; apenas revalidar"
else
  compose "$COMPOSE_FILE" up -d
  export HEALTH_REQUIRE_HEALTHCHECK=1
  wait_for_health "$CONTAINER_NAME" "$HEALTH_SETTLE_SECONDS" \
    || fail "health nao estabilizou apos rollback"
fi

MCP_PORT="${MCP_PORT:-8765}" \
EXPECT_BRIDGE_VERSION="$ROLLBACK_BRIDGE_VERSION" \
EXPECT_TOOL_COUNT="$ROLLBACK_TOOL_COUNT" \
EXPECT_SCHEMA_VERSION="$SCHEMA_VERSION" \
REQUIRE_0_9_SECURITY=0 \
  bash "$HERE/validate.sh" || fail "validacao pos-rollback falhou"

log "ROLLBACK_0_9_0: PASS"
