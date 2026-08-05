#!/usr/bin/env bash
# Read-only post-deploy/post-rollback validation. Creates no runs or approvals.

set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$HERE/lib.sh"

PORT="${MCP_PORT:-8765}"
URL="${MCP_URL:-http://127.0.0.1:${PORT}/mcp}"
EXPECT_VERSION="${EXPECT_BRIDGE_VERSION:-$BRIDGE_VERSION}"
EXPECT_TOOLS="${EXPECT_TOOL_COUNT:-$EXPECTED_TOOL_COUNT}"
EXPECT_SCHEMA="${EXPECT_SCHEMA_VERSION:-$SCHEMA_VERSION}"
REQUIRE_SECURITY="${REQUIRE_0_9_SECURITY:-1}"
REQUIRED_TOOL="${REQUIRED_TOOL:-hermes_readiness}"

require_cmd curl jq docker

wait_for_health "$CONTAINER_NAME" "${HEALTH_SETTLE_SECONDS:-}" \
  || fail "container nao atingiu health=healthy"

call() {
  curl -sS -m 25 -X POST "$URL" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -d "$1" | sed 's/^data: //' | grep '^{' || true
}

init_payload='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"validate-0.9.0","version":"1"}}}'
for i in $(seq 1 10); do
  if call "$init_payload" | grep -q serverInfo; then
    break
  fi
  sleep 3
  [ "$i" -lt 10 ] || fail "initialize sem resposta"
done
ok "initialize"

tools_json="$(call '{"jsonrpc":"2.0","id":2,"method":"tools/list"}')"
tools_count="$(printf '%s' "$tools_json" | jq '.result.tools|length')"
[ "$tools_count" = "$EXPECT_TOOLS" ] || fail "tool count inesperado"
printf '%s' "$tools_json" | jq -e --arg tool "$REQUIRED_TOOL" \
  '.result.tools | map(.name) | index($tool) != null' >/dev/null \
  || fail "ferramenta obrigatoria ausente"
ok "contrato de ferramentas confirmado"

health="$(call '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"hermes_health","arguments":{}}}' \
  | jq -r '.result.content[0].text')"
printf '%s' "$health" | jq -e . >/dev/null || fail "hermes_health sem JSON valido"
bv="$(printf '%s' "$health" | jq -r '.bridge.bridge_version')"
mv="$(printf '%s' "$health" | jq -r '.bridge.manifest_version')"
sv="$(printf '%s' "$health" | jq -r '.bridge.schema_version')"
up="$(printf '%s' "$health" | jq -r '.upstream.status')"
unsupported="$(printf '%s' "$health" | jq -r '.bridge.unsupported_tools|length')"
[ "$bv" = "$EXPECT_VERSION" ] || fail "bridge_version inesperada"
[ "$mv" = "$EXPECT_VERSION" ] || fail "manifest_version inesperada"
[ "$sv" = "$EXPECT_SCHEMA" ] || fail "schema_version inesperada"
[ "$up" = "ok" ] || [ "$up" = "healthy" ] || fail "upstream nao healthy"
[ "$unsupported" = "0" ] || fail "unsupported_tools nao vazio"
ok "health/manifest/schema/upstream confirmados"

readiness="$(call '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"hermes_readiness","arguments":{}}}' \
  | jq -r '.result.content[0].text')"
printf '%s' "$readiness" | jq -e . >/dev/null || fail "hermes_readiness sem JSON valido"
ready_status="$(printf '%s' "$readiness" | jq -r '.status')"
ready_bridge="$(printf '%s' "$readiness" | jq -r '.bridge_version')"
ready_contract="$(printf '%s' "$readiness" | jq -r '.contract_version')"
ready_schema="$(printf '%s' "$readiness" | jq -r '.schema_version')"
ready_count="$(printf '%s' "$readiness" | jq -r '.components.tool_contract.count')"
[ "$ready_status" = "ready" ] || fail "readiness global nao ready"
[ "$ready_bridge" = "$EXPECT_VERSION" ] || fail "readiness bridge_version inesperada"
[ "$ready_contract" = "$EXPECT_VERSION" ] || fail "contract_version inesperada"
[ "$ready_schema" = "$EXPECT_SCHEMA" ] || fail "readiness schema_version inesperada"
[ "$ready_count" = "$EXPECT_TOOLS" ] || fail "readiness tool count inesperado"

if [ "$REQUIRE_SECURITY" = "1" ]; then
  printf '%s' "$readiness" | jq -e '
    .components.security_posture.status == "ready" and
    .components.security_posture.policy.valid == true and
    .components.security_posture.policy.source == "file" and
    (.components.security_posture.policy.policy_hash | type == "string" and length == 64) and
    .components.security_posture.hmac.required == true and
    .components.security_posture.hmac.configured == true and
    .components.security_posture.hmac.source_type == "file" and
    (.components.security_posture.hmac.key_id | type == "string" and length > 0) and
    (.components.security_posture.failing | length == 0) and
    .components.config.api_key_configured == true
  ' >/dev/null || fail "security posture 0.9.0 nao conforme"
  ok "policy/HMAC/file-backed security posture confirmada"
fi

health_status="$(docker inspect "$CONTAINER_NAME" --format '{{.State.Health.Status}}')"
restart_count="$(docker inspect "$CONTAINER_NAME" --format '{{.RestartCount}}')"
[ "$health_status" = "healthy" ] || fail "container nao healthy"
ok "container healthy; restart count observado=$restart_count"

log "VALIDATE_0_9_0: PASS"
