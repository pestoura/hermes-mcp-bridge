#!/usr/bin/env bash
# Shared helpers for the controlled 0.9.0 rollout.
#
# Sourced by preflight.sh, deploy.sh, rollback.sh and validate.sh. Contains no
# secrets and never prints environment-file contents or container Env.

set -Eeuo pipefail

export COMPOSE_PROJECT="hermes-mcp-bridge"
export BRIDGE_VERSION="0.9.0"
export SCHEMA_VERSION="0.6.1"
export EXPECTED_TOOL_COUNT="27"
export CONTAINER_NAME="hermes-mcp-bridge"

export HEALTH_POLL_INTERVAL_SECONDS="${HEALTH_POLL_INTERVAL_SECONDS:-2}"
export HEALTH_SETTLE_MARGIN_SECONDS="${HEALTH_SETTLE_MARGIN_SECONDS:-15}"
export HEALTH_SETTLE_MIN_SECONDS="${HEALTH_SETTLE_MIN_SECONDS:-30}"
export HEALTH_SETTLE_MAX_SECONDS="${HEALTH_SETTLE_MAX_SECONDS:-300}"
export HEALTH_FALLBACK_START_PERIOD="${HEALTH_FALLBACK_START_PERIOD:-10}"
export HEALTH_FALLBACK_INTERVAL="${HEALTH_FALLBACK_INTERVAL:-30}"
export HEALTH_FALLBACK_TIMEOUT="${HEALTH_FALLBACK_TIMEOUT:-5}"
export HEALTH_FALLBACK_RETRIES="${HEALTH_FALLBACK_RETRIES:-3}"
export HEALTH_REQUIRE_HEALTHCHECK="${HEALTH_REQUIRE_HEALTHCHECK:-0}"

log()  { printf '%s\n' "$*"; }
ok()   { printf 'OK: %s\n' "$*"; }
warn() { printf 'WARN: %s\n' "$*" >&2; }
fail() { printf 'ABORT: %s\n' "$*" >&2; exit 1; }

compose() {
  local file="$1"; shift
  docker compose -p "$COMPOSE_PROJECT" -f "$file" "$@"
}

require_cmd() {
  local cmd
  for cmd in "$@"; do
    command -v "$cmd" >/dev/null 2>&1 || fail "comando ausente: $cmd"
  done
}

require_file() {
  [ -f "$1" ] || fail "ficheiro obrigatorio ausente"
}

is_execute_mode() {
  local required_sha="$1"
  [ "${EXECUTE_DEPLOYMENT:-}" = "YES" ] || return 1
  [ "${EXPECTED_SHA:-}" = "$required_sha" ] || return 1
  return 0
}

assert_image_revision() {
  local image="$1" expected="$2" revision=""
  docker image inspect "$image" >/dev/null 2>&1 || fail "imagem candidata ausente"
  revision="$(docker image inspect "$image" \
    --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')"
  [ "$revision" = "$expected" ] || fail "revision OCI da imagem nao corresponde ao SHA requerido"
  ok "revision OCI da imagem candidata confirmada"
}

assert_image_version() {
  local image="$1" expected="$2" version=""
  version="$(docker image inspect "$image" \
    --format '{{index .Config.Labels "org.opencontainers.image.version"}}')"
  [ "$version" = "$expected" ] || fail "version OCI da imagem nao corresponde a release"
  ok "version OCI da imagem candidata confirmada"
}

assert_image_id() {
  local image="$1" expected="$2" image_id=""
  docker image inspect "$image" >/dev/null 2>&1 || fail "imagem de rollback ausente"
  image_id="$(docker image inspect "$image" --format '{{.Id}}')"
  [ "$image_id" = "$expected" ] || fail "imagem de rollback aponta para ID inesperado"
  ok "ID imutavel da imagem de rollback confirmado"
}

compose_config_check() {
  local file="$1"
  require_file "$file"
  compose "$file" config >/dev/null || fail "compose invalido"
  ok "compose valido: $(basename "$file") (projeto $COMPOSE_PROJECT)"
}

env_has_nonempty() {
  local file="$1" name="$2"
  awk -v key="$name" '
    /^[[:space:]]*#/ { next }
    {
      line=$0
      sub(/^[[:space:]]*/, "", line)
      pos=index(line, "=")
      if (pos == 0) next
      lhs=substr(line, 1, pos-1)
      gsub(/[[:space:]]/, "", lhs)
      if (lhs != key) next
      rhs=substr(line, pos+1)
      gsub(/^[[:space:]"\047]+|[[:space:]"\047]+$/, "", rhs)
      if (length(rhs) > 0) found=1
    }
    END { exit(found ? 0 : 1) }
  ' "$file"
}

env_is_truthy() {
  local file="$1" name="$2"
  awk -v key="$name" '
    /^[[:space:]]*#/ { next }
    {
      line=$0
      sub(/^[[:space:]]*/, "", line)
      pos=index(line, "=")
      if (pos == 0) next
      lhs=substr(line, 1, pos-1)
      gsub(/[[:space:]]/, "", lhs)
      if (lhs != key) next
      rhs=tolower(substr(line, pos+1))
      gsub(/^[[:space:]"\047]+|[[:space:]"\047]+$/, "", rhs)
      if (rhs ~ /^(1|true|yes|on)$/) found=1
    }
    END { exit(found ? 0 : 1) }
  ' "$file"
}

assert_secret_file() {
  local file="$1" label="$2" expected_uid="$3"
  require_file "$file"
  [ -s "$file" ] || fail "$label vazio"
  local mode owner
  mode="$(stat -c '%a' "$file")"
  owner="$(stat -c '%u' "$file")"
  case "$mode" in
    400|600) ;;
    *) fail "$label deve ter modo 0400 ou 0600" ;;
  esac
  [ "$owner" = "$expected_uid" ] || fail "$label deve pertencer ao UID do bridge"
  ok "$label presente com ownership/permissoes restritas"
}

validate_secret_lengths() {
  local api_file="$1" hmac_file="$2" minimum="$3"
  python3 - "$api_file" "$hmac_file" "$minimum" <<'PY'
from pathlib import Path
import sys

api = Path(sys.argv[1]).read_text(encoding="utf-8").strip()
hmac_secret = Path(sys.argv[2]).read_text(encoding="utf-8").strip()
minimum = int(sys.argv[3])
if not api:
    raise SystemExit("API key file is empty")
if len(hmac_secret) < minimum:
    raise SystemExit("HMAC secret is shorter than the configured minimum")
PY
  ok "comprimentos minimos dos secrets confirmados"
}

validate_policy_file() {
  local policy_file="$1"
  python3 - "$policy_file" <<'PY'
import json
from pathlib import Path
import sys

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if not isinstance(payload, dict):
    raise SystemExit("policy must be an object")
if str(payload.get("unknown_action_decision", "")).upper() != "DENY":
    raise SystemExit("unknown actions must be denied")
if not isinstance(payload.get("read_only_actions"), list):
    raise SystemExit("read_only_actions missing")
if not isinstance(payload.get("mutating_actions"), list):
    raise SystemExit("mutating_actions missing")
PY
  ok "policy de producao validada offline"
}

_ns_to_seconds() {
  local value="${1:-0}"
  case "$value" in
    ''|*[!0-9]*) printf '0\n'; return 0 ;;
  esac
  printf '%s\n' "$(( (value + 999999999) / 1000000000 ))"
}

health_settle_budget() {
  local container="$1"
  local start_period interval timeout retries budget
  start_period="$HEALTH_FALLBACK_START_PERIOD"
  interval="$HEALTH_FALLBACK_INTERVAL"
  timeout="$HEALTH_FALLBACK_TIMEOUT"
  retries="$HEALTH_FALLBACK_RETRIES"

  if docker inspect "$container" >/dev/null 2>&1; then
    local raw
    raw="$(docker inspect "$container" --format \
      '{{with .Config.Healthcheck}}{{.StartPeriod}} {{.Interval}} {{.Timeout}} {{.Retries}}{{end}}' \
      2>/dev/null || true)"
    if [ -n "$raw" ]; then
      local sp iv to rt converted
      read -r sp iv to rt <<<"$raw"
      converted="$(_ns_to_seconds "${sp:-0}")"
      [ "$converted" -gt 0 ] && start_period="$converted"
      converted="$(_ns_to_seconds "${iv:-0}")"
      [ "$converted" -gt 0 ] && interval="$converted"
      converted="$(_ns_to_seconds "${to:-0}")"
      [ "$converted" -gt 0 ] && timeout="$converted"
      case "${rt:-0}" in
        ''|*[!0-9]*) : ;;
        *) [ "$rt" -gt 0 ] && retries="$rt" ;;
      esac
    fi
  fi

  budget=$(( start_period + (interval + timeout) * retries + HEALTH_SETTLE_MARGIN_SECONDS ))
  [ "$budget" -lt "$HEALTH_SETTLE_MIN_SECONDS" ] && budget="$HEALTH_SETTLE_MIN_SECONDS"
  [ "$budget" -gt "$HEALTH_SETTLE_MAX_SECONDS" ] && budget="$HEALTH_SETTLE_MAX_SECONDS"
  printf '%s\n' "$budget"
}

container_health_status() {
  local container="$1" status=""
  docker inspect "$container" >/dev/null 2>&1 || { printf 'missing\n'; return 0; }
  status="$(docker inspect "$container" \
    --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
    2>/dev/null || true)"
  [ -n "$status" ] || status="none"
  printf '%s\n' "$status"
}

wait_for_health() {
  local container="$1"
  local budget="${2:-}"
  [ -n "$budget" ] || budget="$(health_settle_budget "$container")"
  local waited=0 status=""
  local poll="$HEALTH_POLL_INTERVAL_SECONDS"
  [ "$poll" -ge 1 ] 2>/dev/null || poll=1

  log "HEALTH: aguarda estabilizacao de $container (budget ${budget}s, poll ${poll}s)"
  while :; do
    status="$(container_health_status "$container")"
    case "$status" in
      healthy)
        ok "health=healthy apos ${waited}s (budget ${budget}s)"
        return 0
        ;;
      unhealthy)
        warn "health=unhealthy apos ${waited}s"
        return 1
        ;;
      none)
        if [ "$HEALTH_REQUIRE_HEALTHCHECK" = "1" ]; then
          warn "container sem healthcheck declarado"
          return 1
        fi
        warn "container sem healthcheck; estabilizacao nao verificavel"
        return 0
        ;;
      missing)
        warn "container ausente durante espera de health"
        return 1
        ;;
    esac
    if [ "$waited" -ge "$budget" ]; then
      warn "timeout de health apos ${waited}s com status=$status"
      return 1
    fi
    sleep "$poll"
    waited=$(( waited + poll ))
  done
}
