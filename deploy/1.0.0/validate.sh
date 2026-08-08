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
EXPECT_METRICS="${EXPECT_METRICS_ENABLED:-0}"
METRICS_URL="${METRICS_URL:-http://127.0.0.1:${BRIDGE_METRICS_PORT:-9464}/metrics}"
REQUIRE_SECURITY="${REQUIRE_1_0_SECURITY:-1}"
REQUIRED_TOOL="${REQUIRED_TOOL:-hermes_readiness}"

require_cmd curl jq docker
case "$EXPECT_METRICS" in
  0|1) ;;
  *) fail "EXPECT_METRICS_ENABLED deve ser 0 ou 1" ;;
esac

wait_for_health "$CONTAINER_NAME" "${HEALTH_SETTLE_SECONDS:-}" \
  || fail "container nao atingiu health=healthy"

call() {
  curl -sS -m 25 -X POST "$URL" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -d "$1" | sed 's/^data: //' | grep '^{' || true
}

init_payload='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"validate-1.0.0","version":"1"}}}'
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
printf '%s' "$readiness" | jq -e '
  .alive == true and
  .ready == true and
  .accepting_new_work == true and
  .components.admission.status == "ready" and
  .components.admission.accepting_new_work == true and
  (.components.admission.gateway_state == "running" or .components.admission.gateway_state == "ready")
' >/dev/null || fail "upstream nao esta a aceitar novo trabalho"
ok "alive/ready/admission confirmados"

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
    .components.security_posture.hmac.previous_configured == false and
    .components.security_posture.hmac.previous_active == false and
    .components.security_posture.hmac.previous_pending == false and
    .components.security_posture.hmac.previous_expired == false and
    (.components.security_posture.failing | length == 0) and
    .components.config.api_key_configured == true and
    .components.tracing.export_enabled == false
  ' >/dev/null || fail "security posture 1.0.0 nao conforme"

  if [ "$EXPECT_METRICS" = "1" ]; then
    printf '%s' "$readiness" | jq -e '
      .components.metrics_registry.exporter_enabled == true and
      .components.metrics_registry.bind_scope == "loopback"
    ' >/dev/null || fail "metrics esperadas mas exporter nao esta loopback/enabled"
    printf '%s' "$health" | jq -e '
      .bridge.observability.metrics.enabled == true and
      .bridge.observability.metrics.running == true and
      .bridge.observability.tracing.export_enabled == false and
      .bridge.observability.retry.enabled == false and
      .bridge.observability.retry.mutations_retryable == false and
      .bridge.observability.retry.sse_retryable == false and
      .bridge.observability.circuit_breaker.enabled == false and
      .bridge.observability.circuit_breaker.mutations_protected == false and
      .bridge.observability.circuit_breaker.sse_protected == false
    ' >/dev/null || fail "postura observability 1.x nao conforme"

    metrics="$(curl -fsS -m 10 "$METRICS_URL")" || fail "exporter metrics indisponivel"
    printf '%s\n' "$metrics" | grep -Eq '^bridge_expected_tools 27(\.0)?$' \
      || fail "bridge_expected_tools != 27"
    printf '%s\n' "$metrics" | grep -Eq '^bridge_instrumented_tools 27(\.0)?$' \
      || fail "bridge_instrumented_tools != 27"
    printf '%s\n' "$metrics" | grep -Eq '^bridge_instrumentation_coverage_ratio 1(\.0)?$' \
      || fail "instrumentation coverage != 1"
    printf '%s\n' "$metrics" | grep -Eq '^bridge_upstream_admission_ready 1(\.0)?$' \
      || fail "admission metric nao confirma accepting_new_work"
    ok "metrics loopback e coverage 27/27 confirmadas"
  else
    printf '%s' "$readiness" | jq -e '
      .components.metrics_registry.exporter_enabled == false
    ' >/dev/null || fail "metrics nao esperadas mas exporter esta enabled"
    printf '%s' "$health" | jq -e '
      .bridge.observability.metrics.enabled == false and
      .bridge.observability.tracing.export_enabled == false and
      .bridge.observability.retry.enabled == false and
      .bridge.observability.retry.mutations_retryable == false and
      .bridge.observability.retry.sse_retryable == false and
      .bridge.observability.circuit_breaker.enabled == false and
      .bridge.observability.circuit_breaker.mutations_protected == false and
      .bridge.observability.circuit_breaker.sse_protected == false
    ' >/dev/null || fail "features opcionais 1.0.0 nao estao conforme"
  fi
  ok "policy/HMAC/file-backed e feature gates confirmadas"
fi

health_status="$(docker inspect "$CONTAINER_NAME" --format '{{.State.Health.Status}}')"
restart_count="$(docker inspect "$CONTAINER_NAME" --format '{{.RestartCount}}')"
[ "$health_status" = "healthy" ] || fail "container nao healthy"
ok "container healthy; restart count observado=$restart_count"

log "VALIDATE_${EXPECT_VERSION//./_}: PASS"
