#!/usr/bin/env bash
# Shared helpers for the 0.8.1 rollout scripts.
#
# Sourced by preflight.sh, deploy.sh, rollback.sh and validate.sh. Contains no
# secrets and never prints environment file contents.

set -Eeuo pipefail

# Fixed Docker Compose project. Every compose invocation MUST use this so the
# rollout cannot accidentally create or mutate a differently-named project
# derived from the current directory.
COMPOSE_PROJECT="hermes-mcp-bridge"

# Contract expected by this rollout.
BRIDGE_VERSION="0.8.1"
SCHEMA_VERSION="0.6.1"
EXPECTED_TOOL_COUNT="27"
CONTAINER_NAME="hermes-mcp-bridge"

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
