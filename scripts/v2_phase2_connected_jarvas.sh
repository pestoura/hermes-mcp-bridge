#!/usr/bin/env bash
set -Eeuo pipefail

# Canonical one-shot launcher for the V2 Phase 2 connected acceptance gate.
#
# This script is intentionally fail-closed. It rotates the already-provisioned
# GitHub App installation credential, builds a clean accepted-main checkout,
# executes the real 5x3 DIRECT/V1 shadow collector, runs the canonical validator,
# and prints only a sanitized gate summary.
#
# It NEVER invents the V1 shadow non-mutation basis. The caller must supply one
# of the two bases already accepted by the collector, and only after that basis
# has actually been established for the collection window.

CLIENT_ID='Iv23lioR4qNOECla5a43'
REPOSITORY='pestoura/hermes-mcp-bridge'
BRIDGE_URL="${HERMES_V2_BRIDGE_URL:-http://127.0.0.1:8765/mcp}"
BASE="${HERMES_V2_ACCEPTANCE_DIR:-$HOME/.hermes-v2-acceptance}"
HERMES_HOME_EFFECTIVE="${HERMES_HOME:-$HOME/.hermes}"
STATE_DB="$HERMES_HOME_EFFECTIVE/state.db"
PEM="$BASE/github-app.pem"
TOKEN="$BASE/github-direct.token"
ATTESTATION="$BASE/github-direct-attestation.json"
EVIDENCE="$BASE/phase2-connected-evidence.json"
GATE="$BASE/phase2-connected-gate.json"
TARGETS="$BASE/phase2-targets.json"
VENV="$BASE/venv"
SHADOW_BASIS="${HERMES_V2_SHADOW_MUTATION_BASIS:-none}"
SRC=''

cleanup() {
  if [[ -n "${SRC:-}" && -d "${SRC:-}" ]]; then
    rm -rf -- "$SRC"
  fi
}
trap cleanup EXIT INT TERM

blocked() {
  printf '{"gate":"DIRECT_READ_BLOCKED","reason":"%s"}\n' "$1"
  exit 2
}

for cmd in git openssl python3; do
  command -v "$cmd" >/dev/null 2>&1 || blocked "RUNTIME_COMMAND_MISSING"
done

[[ -d "$BASE" ]] || blocked "ACCEPTANCE_RUNTIME_NOT_PROVISIONED"
[[ -f "$PEM" ]] || blocked "GITHUB_APP_PRIVATE_KEY_MISSING"
[[ -f "$STATE_DB" ]] || blocked "HERMES_STATE_DB_MISSING"
[[ -x "$VENV/bin/python" ]] || blocked "ACCEPTANCE_PYTHON_RUNTIME_MISSING"

[[ "$(stat -c '%a' "$BASE")" == '700' ]] || blocked "ACCEPTANCE_DIR_PERMISSIONS_INVALID"
[[ "$(stat -c '%a' "$PEM")" == '600' ]] || blocked "GITHUB_APP_PRIVATE_KEY_PERMISSIONS_INVALID"
openssl pkey -in "$PEM" -noout >/dev/null 2>&1 || blocked "GITHUB_APP_PRIVATE_KEY_INVALID"

case "$SHADOW_BASIS" in
  github_audit_log_reviewed|read_only_credential_enforced)
    ;;
  *)
    blocked "SHADOW_MUTATION_BASIS_UNPROVEN"
    ;;
esac

# Never permit a bare secret value to override the file-backed provider.
unset BRIDGE_V2_GITHUB_DIRECT_READ_TOKEN || true
export BRIDGE_V2_GITHUB_DIRECT_READ_TOKEN_FILE="$TOKEN"

SRC="$(mktemp -d "$BASE/source.XXXXXX")"
git clone -q --depth 1 https://github.com/pestoura/hermes-mcp-bridge.git "$SRC"
SOURCE_COMMIT="$(git -C "$SRC" rev-parse HEAD)"
[[ "$SOURCE_COMMIT" =~ ^[0-9a-f]{40}$ ]] || blocked "SOURCE_COMMIT_INVALID"

# Reinstall the exact source under test into the existing private acceptance venv.
"$VENV/bin/python" -m pip install -q --disable-pip-version-check "$SRC"

# Rotate the short-lived installation token and regenerate attestation from the
# mint response. Any permission/repository drift fails before connected samples.
"$VENV/bin/python" "$SRC/scripts/v2_github_app_mint.py" \
  --issuer "$CLIENT_ID" \
  --private-key "$PEM" \
  --repository "$REPOSITORY" \
  --token-out "$TOKEN" \
  --attestation-out "$ATTESTATION" >/dev/null

[[ -s "$TOKEN" && "$(stat -c '%a' "$TOKEN")" == '600' ]] || blocked "INSTALLATION_TOKEN_INVALID"
[[ -s "$ATTESTATION" && "$(stat -c '%a' "$ATTESTATION")" == '600' ]] || blocked "PROVIDER_ATTESTATION_INVALID"

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
  --url "$BRIDGE_URL" \
  --targets "$TARGETS" \
  --json-out "$EVIDENCE" \
  --source-commit "$SOURCE_COMMIT" \
  --direct-core-commit "$SOURCE_COMMIT" \
  --provider-type github_app \
  --provider-attestation "$ATTESTATION" \
  --shadow-mutation-basis "$SHADOW_BASIS" \
  --hermes-state-db "$STATE_DB" >/dev/null

[[ -s "$EVIDENCE" ]] || blocked "CONNECTED_EVIDENCE_NOT_PRODUCED"

if ! "$VENV/bin/python" "$SRC/scripts/validate_v2_phase2_direct_read_evidence.py" \
  "$EVIDENCE" \
  --json-out "$GATE" >/dev/null; then
  "$VENV/bin/python" - "$GATE" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    payload = {"gate": "DIRECT_READ_BLOCKED", "failures": ["gate_output_unavailable"]}
print(json.dumps({
    "gate": payload.get("gate", "DIRECT_READ_BLOCKED"),
    "failures": payload.get("failures", ["unknown"]),
}, sort_keys=True))
PY
  exit 2
fi

"$VENV/bin/python" - "$EVIDENCE" "$GATE" <<'PY'
import json
import sys
from pathlib import Path

raw = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
gate = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
aggregate = raw.get("aggregate", {})
privacy = raw.get("privacy", {})
print(json.dumps({
    "gate": gate.get("gate"),
    "failures": gate.get("failures", []),
    "source_commit": gate.get("source_commit"),
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
