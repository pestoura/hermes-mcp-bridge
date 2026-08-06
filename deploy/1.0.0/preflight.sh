#!/usr/bin/env bash
# Read-only GO/NO-GO preflight for the 1.0.0 rollout.

set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$HERE/lib.sh"

CANDIDATE_IMAGE="${CANDIDATE_IMAGE:-hermes-mcp-bridge:1.0.0-candidate}"
ROLLBACK_IMAGE="${ROLLBACK_IMAGE:-}"
ROLLBACK_IMAGE_ID="${ROLLBACK_IMAGE_ID:-}"
EXPECTED_SHA_1_0_0="${EXPECTED_SHA_1_0_0:-}"
BRIDGE_ENV_FILE="${BRIDGE_ENV_FILE:-/home/estourpm/hermes-mcp-bridge/.env}"
BRIDGE_STATE_DIR="${BRIDGE_STATE_DIR:-/home/estourpm/hermes-mcp-bridge/data}"
BRIDGE_POLICY_DIR="${BRIDGE_POLICY_DIR:-/home/estourpm/hermes-mcp-bridge/config/policies}"
BRIDGE_SECRETS_DIR="${BRIDGE_SECRETS_DIR:-/home/estourpm/hermes-mcp-bridge/secrets}"
BRIDGE_UID="${BRIDGE_UID:-1000}"
BRIDGE_MIN_SECRET_LENGTH="${BRIDGE_MIN_SECRET_LENGTH:-32}"
MIN_FREE_KB="${MIN_FREE_KB:-5242880}"
SBOM_FILE="${SBOM_FILE:-}"
SBOM_SHA256="${SBOM_SHA256:-}"
ROLLBACK_BRIDGE_VERSION="${ROLLBACK_BRIDGE_VERSION:-0.9.0}"

require_cmd docker df awk stat python3 curl jq sha256sum

[ -n "$EXPECTED_SHA_1_0_0" ] || fail "EXPECTED_SHA_1_0_0 obrigatorio"
[ -n "$ROLLBACK_IMAGE" ] || fail "ROLLBACK_IMAGE obrigatorio"
[ -n "$ROLLBACK_IMAGE_ID" ] || fail "ROLLBACK_IMAGE_ID obrigatorio"
[ -n "$SBOM_FILE" ] || fail "SBOM_FILE obrigatorio"
[ -n "$SBOM_SHA256" ] || fail "SBOM_SHA256 obrigatorio"

assert_image_revision "$CANDIDATE_IMAGE" "$EXPECTED_SHA_1_0_0"
assert_image_version "$CANDIDATE_IMAGE" "$BRIDGE_VERSION"
assert_image_id "$ROLLBACK_IMAGE" "$ROLLBACK_IMAGE_ID"
assert_image_version "$ROLLBACK_IMAGE" "$ROLLBACK_BRIDGE_VERSION"
validate_sbom_evidence "$SBOM_FILE" "$SBOM_SHA256"

if docker inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
  state="$(docker inspect "$CONTAINER_NAME" --format \
    '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|{{.RestartCount}}')"
  current_image_id="$(docker inspect "$CONTAINER_NAME" --format '{{.Image}}')"
  log "producao: $state"
  [ "${state%%|*}" = "healthy" ] || fail "producao atual nao esta healthy"
  [ "$current_image_id" = "$ROLLBACK_IMAGE_ID" ] \
    || fail "producao atual nao corresponde ao ID imutavel 0.9.0 de rollback"
  ok "producao atual healthy e alinhada com rollback 0.9.0"
else
  fail "container de producao ausente; usar procedimento de primeira instalacao"
fi

require_file "$BRIDGE_ENV_FILE"
[ -d "$BRIDGE_STATE_DIR" ] || fail "state dir ausente"
state_db="$BRIDGE_STATE_DIR/state.sqlite3"
validate_state_db_read_only "$state_db"
[ -d "$BRIDGE_POLICY_DIR" ] || fail "policy dir ausente"
[ -d "$BRIDGE_SECRETS_DIR" ] || fail "secrets dir ausente"

# 1.0.0 removes the transitional raw API-key rollback path. Both the candidate
# and the 0.9.0 rollback compose consume the mounted file only.
if env_has_nonempty "$BRIDGE_ENV_FILE" "HERMES_API_KEY"; then
  fail "HERMES_API_KEY raw deve ser removida do env antes do rollout 1.0.0"
fi
env_has_nonempty "$BRIDGE_ENV_FILE" "HERMES_BRIDGE_HMAC_KEY_ID" \
  || fail "HERMES_BRIDGE_HMAC_KEY_ID obrigatorio para rastreio de rotacao"

for flag in \
  BRIDGE_METRICS_ENABLED \
  BRIDGE_TRACING_ENABLED \
  BRIDGE_TRACING_EXPORT \
  BRIDGE_RETRY_ENABLED \
  BRIDGE_CIRCUIT_ENABLED
do
  if env_is_truthy "$BRIDGE_ENV_FILE" "$flag"; then
    fail "$flag deve permanecer desativado no rollout base 1.0.0"
  fi
done
ok "metrics/tracing/retry/circuit permanecem desativados"

# The base rollout is current-key only. A bounded previous-key rotation is a
# separate operational gate after the base candidate is accepted.
for name in \
  HERMES_BRIDGE_HMAC_SECRET_PREVIOUS \
  HERMES_BRIDGE_HMAC_SECRET_PREVIOUS_FILE \
  HERMES_BRIDGE_HMAC_PREVIOUS_KEY_ID \
  HERMES_BRIDGE_HMAC_PREVIOUS_VALID_FROM \
  HERMES_BRIDGE_HMAC_PREVIOUS_VALID_UNTIL
do
  if env_has_nonempty "$BRIDGE_ENV_FILE" "$name"; then
    fail "$name deve permanecer vazio no rollout base 1.0.0"
  fi
done
previous_secret="$BRIDGE_SECRETS_DIR/hermes_bridge_hmac_secret_previous"
if [ -e "$previous_secret" ]; then
  fail "previous HMAC secret deve ser removido no rollout base 1.0.0"
fi
ok "base rollout sem previous HMAC verifier"

api_secret="$BRIDGE_SECRETS_DIR/hermes_api_key"
hmac_secret="$BRIDGE_SECRETS_DIR/hermes_bridge_hmac_secret"
policy_file="$BRIDGE_POLICY_DIR/production.json"

assert_secret_file "$api_secret" "Hermes API secret" "$BRIDGE_UID"
assert_secret_file "$hmac_secret" "HMAC secret" "$BRIDGE_UID"
validate_secret_lengths "$api_secret" "$hmac_secret" "$BRIDGE_MIN_SECRET_LENGTH"
require_file "$policy_file"
validate_policy_file "$policy_file"

avail_kb="$(df -Pk "$BRIDGE_STATE_DIR" | awk 'NR==2{print $4}')"
[ -n "$avail_kb" ] || fail "nao foi possivel medir espaco em disco"
[ "$avail_kb" -gt "$MIN_FREE_KB" ] || fail "espaco em disco insuficiente"
ok "espaco em disco suficiente (${avail_kb}KB livres)"

export CANDIDATE_REVISION="$EXPECTED_SHA_1_0_0"
compose_config_check "$HERE/compose.candidate.yml"
compose_config_check "$HERE/compose.rollback.yml"

log "HERMES_BRIDGE_1_0_0_PREFLIGHT_GO"
