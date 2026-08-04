#!/usr/bin/env bash
# Shared helpers for the 0.8.2 rollout scripts.
#
# Sourced by preflight.sh, deploy.sh, rollback.sh and validate.sh. Contains no
# secrets and never prints environment file contents or container Env.

set -Eeuo pipefail

# Fixed Docker Compose project. Every compose invocation MUST use this so the
# rollout cannot accidentally create or mutate a differently-named project
# derived from the current directory.
# These are consumed by the scripts that source this file; exported so that
# static analysis does not flag them as unused (SC2034).
export COMPOSE_PROJECT="hermes-mcp-bridge"

# Contract expected by this rollout.
export BRIDGE_VERSION="0.8.2"
export SCHEMA_VERSION="0.6.1"
export EXPECTED_TOOL_COUNT="27"
export CONTAINER_NAME="hermes-mcp-bridge"

# Health-settle policy (0.8.2 defect fix).
#
# 0.8.1 used a fixed `sleep 12` before validating. The container healthcheck is
# configured with start_period=10s, interval=30s, timeout=5s, retries=3, so the
# FIRST health probe can only be recorded at ~10s and the status remains
# "starting" well past 12s. Validation therefore raced the healthcheck and
# produced a FALSE ROLLBACK; the 0.8.1 rollout only succeeded with a manual
# SETTLE_SECONDS=60 override.
#
# 0.8.2 derives the budget from the container's own healthcheck configuration
# and polls the Docker health status until healthy/unhealthy/timeout:
#
#   budget = start_period + (interval + timeout) * retries + HEALTH_SETTLE_MARGIN
#
# with a floor of HEALTH_SETTLE_MIN_SECONDS and a ceiling of
# HEALTH_SETTLE_MAX_SECONDS. Every component stays configurable.
export HEALTH_POLL_INTERVAL_SECONDS="${HEALTH_POLL_INTERVAL_SECONDS:-2}"
export HEALTH_SETTLE_MARGIN_SECONDS="${HEALTH_SETTLE_MARGIN_SECONDS:-15}"
export HEALTH_SETTLE_MIN_SECONDS="${HEALTH_SETTLE_MIN_SECONDS:-30}"
export HEALTH_SETTLE_MAX_SECONDS="${HEALTH_SETTLE_MAX_SECONDS:-300}"
# Fallbacks used only when the container declares no healthcheck fields.
export HEALTH_FALLBACK_START_PERIOD="${HEALTH_FALLBACK_START_PERIOD:-10}"
export HEALTH_FALLBACK_INTERVAL="${HEALTH_FALLBACK_INTERVAL:-30}"
export HEALTH_FALLBACK_TIMEOUT="${HEALTH_FALLBACK_TIMEOUT:-5}"
export HEALTH_FALLBACK_RETRIES="${HEALTH_FALLBACK_RETRIES:-3}"

log()  { printf '%s\n' "$*"; }
ok()   { printf 'OK: %s\n' "$*"; }
warn() { printf 'WARN: %s\n' "$*" >&2; }
fail() { printf 'ABORT: %s\n' "$*" >&2; exit 1; }

# compose <compose-file> <args...>
# Always pins the project name with -p.
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
  [ -f "$1" ] || fail "ficheiro ausente: $1"
}

# is_execute_mode <required-sha>
# Mutating actions require BOTH EXECUTE_DEPLOYMENT=YES and a matching
# EXPECTED_SHA. Anything else stays in dry-run.
is_execute_mode() {
  local required_sha="$1"
  [ "${EXECUTE_DEPLOYMENT:-}" = "YES" ] || return 1
  [ "${EXPECTED_SHA:-}" = "$required_sha" ] || return 1
  return 0
}

# assert_image_revision <image> <expected-sha>
assert_image_revision() {
  local image="$1" expected="$2" rev=""
  docker image inspect "$image" >/dev/null 2>&1 || fail "imagem ausente: $image"
  rev="$(docker image inspect "$image" \
    --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')"
  [ "$rev" = "$expected" ] || fail "revision de $image ($rev) != esperado"
  ok "imagem $image com revision correta"
}

# assert_image_id <image> <expected-image-id>
assert_image_id() {
  local image="$1" expected="$2" id=""
  docker image inspect "$image" >/dev/null 2>&1 || fail "imagem ausente: $image"
  id="$(docker image inspect "$image" --format '{{.Id}}')"
  [ "$id" = "$expected" ] || fail "imagem $image aponta para ID inesperado"
  ok "imagem $image com ID esperado"
}

# compose_config_check <compose-file>
compose_config_check() {
  local file="$1"
  require_file "$file"
  compose "$file" config >/dev/null || fail "compose invalido: $file"
  ok "compose valido: $file (projeto $COMPOSE_PROJECT)"
}

# _ns_to_seconds <nanoseconds>
# Docker reports healthcheck durations in nanoseconds. Rounds up to whole
# seconds; non-numeric or non-positive input yields 0.
_ns_to_seconds() {
  local value="${1:-0}"
  case "$value" in
    ''|*[!0-9]*) printf '0\n'; return 0 ;;
  esac
  printf '%s\n' "$(( (value + 999999999) / 1000000000 ))"
}

# health_settle_budget <container>
# Derives the stabilisation budget in seconds from the container's declared
# healthcheck. Never returns less than HEALTH_SETTLE_MIN_SECONDS nor more than
# HEALTH_SETTLE_MAX_SECONDS. Prints the budget on stdout.
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
      local sp iv to rt
      read -r sp iv to rt <<<"$raw"
      local converted
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

# container_health_status <container>
# Prints one of: healthy | unhealthy | starting | none | missing.
# "none" means the container runs but declares no healthcheck.
container_health_status() {
  local container="$1" status=""
  docker inspect "$container" >/dev/null 2>&1 || { printf 'missing\n'; return 0; }
  status="$(docker inspect "$container" \
    --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
    2>/dev/null || true)"
  [ -n "$status" ] || status="none"
  printf '%s\n' "$status"
}

# wait_for_health <container> [budget-seconds]
# Polls the Docker health status until healthy (0), unhealthy (1) or the budget
# expires (1). "starting" inside the window is NOT a failure. A container with
# no declared healthcheck returns 0 with a warning. Logs are sanitised: only the
# status token and elapsed seconds are printed, never Env or container config.
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
        warn "container sem healthcheck declarado: estabilizacao nao verificavel"
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
