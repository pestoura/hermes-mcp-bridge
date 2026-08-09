#!/usr/bin/env bash
# Controlled rollback from a 1.0.0 candidate to the exact accepted rollback image.
# DRY-RUN BY DEFAULT. SQLite is not reverted: schema remains 0.6.1.

set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$HERE/lib.sh"

REQUIRED_SHA="${REQUIRED_SHA:-}"
COMPOSE_FILE="${COMPOSE_FILE:-$HERE/compose.rollback.yml}"
ROLLBACK_IMAGE="${ROLLBACK_IMAGE:-}"
ROLLBACK_IMAGE_ID="${ROLLBACK_IMAGE_ID:-}"
ROLLBACK_BRIDGE_VERSION="${ROLLBACK_BRIDGE_VERSION:-0.9.0}"
ROLLBACK_TOOL_COUNT="${ROLLBACK_TOOL_COUNT:-27}"
HEALTH_SETTLE_SECONDS="${HEALTH_SETTLE_SECONDS:-}"

require_cmd docker
require_file "$COMPOSE_FILE"
[ -n "$REQUIRED_SHA" ] || fail "REQUIRED_SHA obrigatorio, inclusive em dry-run"
[ -n "$ROLLBACK_IMAGE" ] || fail "ROLLBACK_IMAGE obrigatorio"
[ -n "$ROLLBACK_IMAGE_ID" ] || fail "ROLLBACK_IMAGE_ID obrigatorio"
[ -n "$ROLLBACK_BRIDGE_VERSION" ] || fail "ROLLBACK_BRIDGE_VERSION obrigatorio"
assert_image_id "$ROLLBACK_IMAGE" "$ROLLBACK_IMAGE_ID"
assert_image_version "$ROLLBACK_IMAGE" "$ROLLBACK_BRIDGE_VERSION"

if ! is_execute_mode "$REQUIRED_SHA"; then
  log "DRY_RUN: nenhuma accao mutavel executada."
  log "DRY_RUN: projeto compose fixo = $COMPOSE_PROJECT"
  log "DRY_RUN: executaria docker compose -p $COMPOSE_PROJECT -f $COMPOSE_FILE up -d"
  log "DRY_RUN: rollback usa os mesmos API/HMAC secret files e policy file"
  log "DRY_RUN: validaria bridge=$ROLLBACK_BRIDGE_VERSION tools=$ROLLBACK_TOOL_COUNT"
  compose_config_check "$COMPOSE_FILE"
  log "ROLLBACK_1_0_0: DRY_RUN OK"
  exit 0
fi

current_image_id=""
if docker inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
  current_image_id="$(docker inspect "$CONTAINER_NAME" --format '{{.Image}}')"
fi

if [ "$current_image_id" = "$ROLLBACK_IMAGE_ID" ]; then
  ok "container ja no ID imutavel da baseline $ROLLBACK_BRIDGE_VERSION; apenas revalidar"
else
  compose "$COMPOSE_FILE" up -d --force-recreate
  export HEALTH_REQUIRE_HEALTHCHECK=1
  wait_for_health "$CONTAINER_NAME" "$HEALTH_SETTLE_SECONDS" \
    || fail "health nao estabilizou apos rollback"
fi

running_image_id="$(docker inspect "$CONTAINER_NAME" --format '{{.Image}}')"
[ "$running_image_id" = "$ROLLBACK_IMAGE_ID" ] \
  || fail "contentor nao esta no ID imutavel da baseline de rollback"
ok "ID imutavel da baseline $ROLLBACK_BRIDGE_VERSION confirmado no contentor"

rollback_require_security=0
if [ "$ROLLBACK_BRIDGE_VERSION" = "$BRIDGE_VERSION" ]; then
  rollback_require_security=1
fi

MCP_PORT="${MCP_PORT:-8765}" \
EXPECT_BRIDGE_VERSION="$ROLLBACK_BRIDGE_VERSION" \
EXPECT_TOOL_COUNT="$ROLLBACK_TOOL_COUNT" \
EXPECT_SCHEMA_VERSION="$SCHEMA_VERSION" \
REQUIRE_1_0_SECURITY="$rollback_require_security" \
  bash "$HERE/validate.sh" || fail "validacao pos-rollback falhou"

log "ROLLBACK_1_0_0: PASS"
