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
REQUIRED_TOOL="${REQUIRED_TOOL:-hermes_readiness}"

require_cmd curl jq docker

call() {
  curl -s -m 25 -X POST "$URL" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -d "$1" | sed 's/^data: //' | grep '^{' || true
}

init_payload='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"validate","version":"1"}}}'
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
[ "$tools_count" = "$EXPECT_TOOLS" ] || fail "tool count $tools_count != $EXPECT_TOOLS"
ok "tools=$tools_count"

printf '%s' "$tools_json" | jq -e --arg t "$REQUIRED_TOOL" \
  '.result.tools | map(.name) | index($t) != null' >/dev/null \
  || fail "ferramenta obrigatoria ausente: $REQUIRED_TOOL"
ok "ferramenta obrigatoria presente: $REQUIRED_TOOL"

health="$(call '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"hermes_health","arguments":{}}}' \
  | jq -r '.result.content[0].text')"
bv="$(printf '%s' "$health" | jq -r '.bridge.bridge_version')"
mv="$(printf '%s' "$health" | jq -r '.bridge.manifest_version')"
sv="$(printf '%s' "$health" | jq -r '.bridge.schema_version')"
up="$(printf '%s' "$health" | jq -r '.upstream.status')"
uns="$(printf '%s' "$health" | jq -r '.bridge.unsupported_tools|length')"
[ "$bv" = "$EXPECT_VERSION" ] || fail "bridge_version $bv != $EXPECT_VERSION"
[ "$mv" = "$EXPECT_VERSION" ] || fail "manifest_version $mv != $EXPECT_VERSION"
[ "$sv" = "$EXPECT_SCHEMA" ] || fail "schema_version $sv != $EXPECT_SCHEMA"
[ "$up" = "ok" ] || [ "$up" = "healthy" ] || fail "upstream $up"
[ "$uns" = "0" ] || fail "unsupported_tools nao vazio"
ok "bridge=$bv manifest=$mv schema=$sv upstream=$up unsupported=0"

hs="$(docker inspect "$CONTAINER_NAME" --format '{{.State.Health.Status}}')"
rc="$(docker inspect "$CONTAINER_NAME" --format '{{.RestartCount}}')"
[ "$hs" = "healthy" ] || fail "container $hs"
ok "container healthy restarts=$rc"

log "VALIDATE: PASS"
