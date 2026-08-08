#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# Canonical one-shot launcher for the V2 Phase 2 connected acceptance gate.
#
# The V1 comparison path runs through a disposable Hermes home whose api_server
# tool surface is runtime-probed to contain exactly one dynamic MCP toolset with
# exactly five fixed-repository GitHub GET tools. The launcher therefore derives
# read_only_credential_enforced from a live isolation proof instead of accepting
# it as an operator-supplied string.
#
# Source integrity rule: the connected run is pinned to the exact Git commit of
# the checkout containing this launcher. A clean internal checkout of that same
# commit is used for every executable/validator involved in the evidence run.

CLIENT_ID='Iv23lioR4qNOECla5a43'
REPOSITORY='pestoura/hermes-mcp-bridge'
BASE="${HERMES_V2_ACCEPTANCE_DIR:-$HOME/.hermes-v2-acceptance}"
SOURCE_HERMES_HOME="${HERMES_V2_SOURCE_HERMES_HOME:-${HERMES_HOME:-$HOME/.hermes}}"
PEM="$BASE/github-app.pem"
TOKEN="$BASE/github-direct.token"
ATTESTATION="$BASE/github-direct-attestation.json"
SHADOW_ISOLATION="$BASE/shadow-isolation.json"
EVIDENCE="$BASE/phase2-connected-evidence.json"
GATE="$BASE/phase2-connected-gate.json"
TARGETS="$BASE/phase2-targets.json"
VENV="$BASE/venv"
SHADOW_HOME="$BASE/shadow-hermes-runtime"
SHADOW_API_KEY="$BASE/shadow-api.key"
SHADOW_BRIDGE_STATE="$BASE/shadow-bridge-state.sqlite3"
SHADOW_HERMES_LOG="$BASE/.shadow-hermes.log"
SHADOW_BRIDGE_LOG="$BASE/.shadow-bridge.log"
SRC=''
SHADOW_HERMES_PID=''
SHADOW_BRIDGE_PID=''

cleanup_process_group() {
  local pid="${1:-}"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    for _ in 1 2 3 4 5; do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.2
    done
    kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
  fi
}

cleanup() {
  cleanup_process_group "$SHADOW_BRIDGE_PID"
  cleanup_process_group "$SHADOW_HERMES_PID"
  [[ -n "${SRC:-}" && -d "${SRC:-}" ]] && rm -rf -- "$SRC"
  rm -rf -- "$SHADOW_HOME"
  rm -f -- "$SHADOW_API_KEY" "$SHADOW_BRIDGE_STATE" "$SHADOW_HERMES_LOG" "$SHADOW_BRIDGE_LOG"
}
trap cleanup EXIT INT TERM

blocked() {
  printf '{"gate":"DIRECT_READ_BLOCKED","reason":"%s"}\n' "$1"
  exit 2
}

for cmd in git openssl python3 hermes setsid readlink; do
  command -v "$cmd" >/dev/null 2>&1 || blocked "RUNTIME_COMMAND_MISSING"
done

# Bind the acceptance run to the exact commit containing the launcher itself.
# This eliminates a race in which `main` could advance between launcher review
# and connected execution. A locally modified launcher is also rejected.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
CHECKOUT_ROOT="$(git -C "$SCRIPT_DIR/.." rev-parse --show-toplevel 2>/dev/null)" \
  || blocked "LAUNCHER_CHECKOUT_UNAVAILABLE"
ACCEPTED_SOURCE_COMMIT="$(git -C "$CHECKOUT_ROOT" rev-parse HEAD 2>/dev/null)" \
  || blocked "LAUNCHER_SOURCE_COMMIT_UNAVAILABLE"
[[ "$ACCEPTED_SOURCE_COMMIT" =~ ^[0-9a-f]{40}$ ]] \
  || blocked "LAUNCHER_SOURCE_COMMIT_INVALID"
git -C "$CHECKOUT_ROOT" diff --quiet -- scripts/v2_phase2_connected_jarvas.sh \
  || blocked "LAUNCHER_WORKTREE_MODIFIED"

[[ -d "$BASE" ]] || blocked "ACCEPTANCE_RUNTIME_NOT_PROVISIONED"
[[ -f "$PEM" ]] || blocked "GITHUB_APP_PRIVATE_KEY_MISSING"
[[ -x "$VENV/bin/python" ]] || blocked "ACCEPTANCE_PYTHON_RUNTIME_MISSING"
[[ -d "$SOURCE_HERMES_HOME" ]] || blocked "SOURCE_HERMES_HOME_MISSING"
[[ -f "$SOURCE_HERMES_HOME/config.yaml" ]] || blocked "SOURCE_HERMES_CONFIG_MISSING"

[[ "$(stat -c '%a' "$BASE")" == '700' ]] || blocked "ACCEPTANCE_DIR_PERMISSIONS_INVALID"
[[ "$(stat -c '%a' "$PEM")" == '600' ]] || blocked "GITHUB_APP_PRIVATE_KEY_PERMISSIONS_INVALID"
openssl pkey -in "$PEM" -noout >/dev/null 2>&1 || blocked "GITHUB_APP_PRIVATE_KEY_INVALID"

HERMES_BIN="$(readlink -f "$(command -v hermes)")"
HERMES_PY="$(dirname "$HERMES_BIN")/python"
[[ -x "$HERMES_PY" ]] || blocked "HERMES_PYTHON_RUNTIME_MISSING"

# Never permit a bare DIRECT secret value to override the file-backed provider.
unset BRIDGE_V2_GITHUB_DIRECT_READ_TOKEN || true
export BRIDGE_V2_GITHUB_DIRECT_READ_TOKEN_FILE="$TOKEN"

# Re-fetch the repository into a private disposable directory, but execute only
# the commit already bound above. Do not follow moving refs during acceptance.
SRC="$(mktemp -d "$BASE/source.XXXXXX")"
git clone -q --no-checkout https://github.com/pestoura/hermes-mcp-bridge.git "$SRC" \
  || blocked "SOURCE_CLONE_FAILED"
git -C "$SRC" checkout -q --detach "$ACCEPTED_SOURCE_COMMIT" \
  || blocked "SOURCE_COMMIT_UNAVAILABLE"
SOURCE_COMMIT="$(git -C "$SRC" rev-parse HEAD)"
[[ "$SOURCE_COMMIT" == "$ACCEPTED_SOURCE_COMMIT" ]] \
  || blocked "SOURCE_COMMIT_MISMATCH"

# Reinstall the exact source under test into the private acceptance venv without
# resolving dependencies from the network.
"$VENV/bin/python" -m pip install -q --no-deps --disable-pip-version-check "$SRC" \
  || blocked "ACCEPTANCE_SOURCE_INSTALL_FAILED"

# Rotate the GitHub App installation token and regenerate its provider
# attestation. Permission/repository drift blocks before any connected sample.
"$VENV/bin/python" "$SRC/scripts/v2_github_app_mint.py" \
  --issuer "$CLIENT_ID" \
  --private-key "$PEM" \
  --repository "$REPOSITORY" \
  --token-out "$TOKEN" \
  --attestation-out "$ATTESTATION" >/dev/null \
  || blocked "GITHUB_APP_MINT_FAILED"

[[ -s "$TOKEN" && "$(stat -c '%a' "$TOKEN")" == '600' ]] \
  || blocked "INSTALLATION_TOKEN_INVALID"
[[ -s "$ATTESTATION" && "$(stat -c '%a' "$ATTESTATION")" == '600' ]] \
  || blocked "PROVIDER_ATTESTATION_INVALID"

free_port() {
  "$VENV/bin/python" - <<'PY'
import socket
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
}

SHADOW_API_PORT="$(free_port)"
SHADOW_BRIDGE_PORT="$(free_port)"
while [[ "$SHADOW_BRIDGE_PORT" == "$SHADOW_API_PORT" ]]; do
  SHADOW_BRIDGE_PORT="$(free_port)"
done

# Build a clean Hermes home containing only model inference material plus the
# five-tool shadow MCP. Messaging/integration credentials are never copied.
"$HERMES_PY" "$SRC/scripts/v2_phase2_prepare_shadow_home.py" \
  --source-home "$SOURCE_HERMES_HOME" \
  --shadow-home "$SHADOW_HOME" \
  --mcp-python "$VENV/bin/python" \
  --mcp-script "$SRC/scripts/v2_phase2_shadow_github_mcp.py" \
  --token-file "$TOKEN" \
  --repository "$REPOSITORY" \
  --api-port "$SHADOW_API_PORT" \
  --api-key-out "$SHADOW_API_KEY" >/dev/null \
  || blocked "SHADOW_HOME_PREPARATION_FAILED"

[[ -s "$SHADOW_API_KEY" && "$(stat -c '%a' "$SHADOW_API_KEY")" == '600' ]] \
  || blocked "SHADOW_API_KEY_INVALID"

: >"$SHADOW_HERMES_LOG"
: >"$SHADOW_BRIDGE_LOG"
chmod 600 "$SHADOW_HERMES_LOG" "$SHADOW_BRIDGE_LOG"

# Start Hermes with an empty inherited environment. The only secrets it can
# resolve are the model-provider material intentionally written into the
# disposable home. The MCP subprocess separately receives only the read-only
# GitHub token-file path declared in its private config.
setsid env -i \
  HOME="$SHADOW_HOME" \
  HERMES_HOME="$SHADOW_HOME" \
  PATH="$PATH" \
  USER="${USER:-estourpm}" \
  LANG="${LANG:-C.UTF-8}" \
  "$HERMES_BIN" gateway >"$SHADOW_HERMES_LOG" 2>&1 &
SHADOW_HERMES_PID=$!

SHADOW_API_URL="http://127.0.0.1:$SHADOW_API_PORT"
"$VENV/bin/python" "$SRC/scripts/v2_phase2_probe_shadow_runtime.py" \
  --url "$SHADOW_API_URL" \
  --api-key-file "$SHADOW_API_KEY" \
  --repository "$REPOSITORY" \
  --source-commit "$SOURCE_COMMIT" \
  --json-out "$SHADOW_ISOLATION" >/dev/null \
  || blocked "SHADOW_ISOLATION_NOT_PROVEN"

[[ -s "$SHADOW_ISOLATION" && "$(stat -c '%a' "$SHADOW_ISOLATION")" == '600' ]] \
  || blocked "SHADOW_ISOLATION_EVIDENCE_INVALID"
[[ -f "$SHADOW_HOME/state.db" ]] || blocked "SHADOW_STATE_DB_MISSING"

# Start a disposable instance of the unchanged V1 bridge surface. This is the
# agentic shadow comparator: hermes_prompt still crosses Bridge -> Hermes, while
# Hermes itself is now mechanically constrained to the five read-only MCP tools.
setsid env -i \
  HOME="$SHADOW_HOME" \
  PATH="$PATH" \
  USER="${USER:-estourpm}" \
  LANG="${LANG:-C.UTF-8}" \
  HERMES_API_BASE_URL="$SHADOW_API_URL" \
  HERMES_API_KEY_FILE="$SHADOW_API_KEY" \
  HERMES_MODEL='phase2-shadow' \
  MCP_HOST='127.0.0.1' \
  MCP_PORT="$SHADOW_BRIDGE_PORT" \
  MCP_PATH='/mcp' \
  LOG_LEVEL='WARNING' \
  BRIDGE_STATE_DB_PATH="$SHADOW_BRIDGE_STATE" \
  "$VENV/bin/hermes-mcp-bridge" >"$SHADOW_BRIDGE_LOG" 2>&1 &
SHADOW_BRIDGE_PID=$!

"$VENV/bin/python" - "$SHADOW_BRIDGE_PORT" "$SHADOW_BRIDGE_PID" <<'PY' \
  || blocked "SHADOW_BRIDGE_NOT_READY"
import os
import socket
import sys
import time

port = int(sys.argv[1])
pid = int(sys.argv[2])
for _ in range(60):
    try:
        os.kill(pid, 0)
    except OSError:
        raise SystemExit(2)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            raise SystemExit(0)
    except OSError:
        time.sleep(0.25)
raise SystemExit(2)
PY

SHADOW_BRIDGE_URL="http://127.0.0.1:$SHADOW_BRIDGE_PORT/mcp"

cat >"$TARGETS" <<EOF
{
  "targets": {
    "github.get_checks": {
      "repository": "$REPOSITORY",
      "arguments": {"ref": "$SOURCE_COMMIT"}
    },
    "github.get_issue": {
      "repository": "$REPOSITORY",
      "arguments": {"number": 51}
    },
    "github.get_pr": {
      "repository": "$REPOSITORY",
      "arguments": {"number": 54}
    },
    "github.get_repo": {
      "repository": "$REPOSITORY",
      "arguments": {}
    },
    "github.search": {
      "repository": "$REPOSITORY",
      "arguments": {"text": "DIRECT_READ_ACCEPTED"}
    }
  }
}
EOF
chmod 600 "$TARGETS"
rm -f -- "$EVIDENCE" "$GATE"

"$VENV/bin/python" "$SRC/scripts/v2_phase2_direct_read_acceptance.py" \
  --url "$SHADOW_BRIDGE_URL" \
  --targets "$TARGETS" \
  --json-out "$EVIDENCE" \
  --source-commit "$SOURCE_COMMIT" \
  --direct-core-commit "$SOURCE_COMMIT" \
  --provider-type github_app \
  --provider-attestation "$ATTESTATION" \
  --shadow-mutation-basis read_only_credential_enforced \
  --hermes-state-db "$SHADOW_HOME/state.db" >/dev/null \
  || blocked "CONNECTED_EVIDENCE_COLLECTION_FAILED"

[[ -s "$EVIDENCE" ]] || blocked "CONNECTED_EVIDENCE_NOT_PRODUCED"

# Promotion now requires BOTH the original connected contract and the companion
# live isolation proof bound to the same source commit and repository scope.
if ! "$VENV/bin/python" "$SRC/scripts/validate_v2_phase2_connected_gate.py" \
  "$EVIDENCE" \
  --shadow-isolation "$SHADOW_ISOLATION" \
  --json-out "$GATE" >/dev/null; then
  "$VENV/bin/python" - "$GATE" <<'PY'
import json
import sys
from pathlib import Path

try:
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except Exception:
    payload = {"gate": "DIRECT_READ_BLOCKED", "failures": ["gate_output_unavailable"]}
print(json.dumps({
    "gate": payload.get("gate", "DIRECT_READ_BLOCKED"),
    "failures": payload.get("failures", ["unknown"]),
}, sort_keys=True))
PY
  exit 2
fi

"$VENV/bin/python" - "$EVIDENCE" "$GATE" "$SHADOW_ISOLATION" <<'PY'
import json
import sys
from pathlib import Path

raw = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
gate = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
shadow = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
aggregate = raw.get("aggregate", {})
privacy = raw.get("privacy", {})
print(json.dumps({
    "gate": gate.get("gate"),
    "failures": gate.get("failures", []),
    "source_commit": gate.get("source_commit"),
    "shadow_isolation": "PASS",
    "shadow_effective_toolsets": shadow.get("effective_toolsets"),
    "shadow_effective_tool_count": len(shadow.get("effective_tools", [])),
    "samples": aggregate.get("sample_count"),
    "successful_samples": aggregate.get("successful_samples"),
    "semantic_matches": aggregate.get("semantic_matches"),
    "direct_provider_api_calls": aggregate.get("direct_provider_api_calls"),
    "direct_hermes_upstream_calls": aggregate.get("direct_hermes_upstream_calls"),
    "direct_hermes_llm_tokens": aggregate.get("direct_hermes_llm_tokens"),
    "v1_shadow_hermes_llm_tokens": aggregate.get("v1_shadow_hermes_llm_tokens"),
    "mutations_observed": aggregate.get("mutations_observed"),
    "contaminated_windows": aggregate.get("contaminated_windows"),
    "privacy_pass": all(value is False for value in privacy.values()),
}, sort_keys=True))
PY
