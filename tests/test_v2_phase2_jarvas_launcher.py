from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "v2_phase2_connected_jarvas.sh"


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_jarvas_launcher_is_valid_bash() -> None:
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_jarvas_launcher_pins_exact_launcher_checkout_commit() -> None:
    text = _text()
    assert 'SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"' in text
    assert 'rev-parse --show-toplevel' in text
    assert 'ACCEPTED_SOURCE_COMMIT="$(git -C "$CHECKOUT_ROOT" rev-parse HEAD' in text
    assert 'diff --quiet -- scripts/v2_phase2_connected_jarvas.sh' in text
    assert 'git clone -q --no-checkout' in text
    assert 'checkout -q --detach "$ACCEPTED_SOURCE_COMMIT"' in text
    assert '[[ "$SOURCE_COMMIT" == "$ACCEPTED_SOURCE_COMMIT" ]]' in text
    assert 'blocked "SOURCE_COMMIT_MISMATCH"' in text
    assert "git clone -q --depth 1" not in text


def test_jarvas_launcher_preserves_only_sanitized_mint_reason() -> None:
    text = _text()
    assert "mint_output_field()" in text
    assert 're.fullmatch(r"[A-Z0-9_]{1,128}", value)' in text
    assert 'payload.get("status") == "GITHUB_APP_INSTALLATION_TOKEN_BLOCKED"' in text
    assert 'value == "GITHUB_APP_INSTALLATION_TOKEN_MINTED"' in text
    assert 'MINT_REASON="$(printf \'%s\' "$MINT_OUTPUT" | mint_output_field reason || true)"' in text
    assert 'blocked "$MINT_REASON"' in text
    assert 'MINT_OUTPUT=\'\'' in text
    assert '--attestation-out "$ATTESTATION" >/dev/null' not in text


def test_jarvas_launcher_derives_read_only_basis_from_live_shadow_probe() -> None:
    text = _text()
    assert "HERMES_V2_SHADOW_MUTATION_BASIS" not in text
    assert "v2_phase2_prepare_shadow_home.py" in text
    assert "v2_phase2_probe_shadow_runtime.py" in text
    assert '--shadow-mutation-basis read_only_credential_enforced' in text
    assert "SHADOW_ISOLATION_NOT_PROVEN" in text


def test_jarvas_launcher_scrubs_shadow_process_environments() -> None:
    text = _text()
    assert text.count("setsid env -i") == 2
    assert 'HOME="$SHADOW_HOME"' in text
    assert 'HERMES_HOME="$SHADOW_HOME"' in text
    assert 'HERMES_API_KEY_FILE="$SHADOW_API_KEY"' in text
    assert 'HERMES_API_BASE_URL="$SHADOW_API_URL"' in text


def test_jarvas_launcher_uses_only_file_backed_direct_secret() -> None:
    text = _text()
    assert "unset BRIDGE_V2_GITHUB_DIRECT_READ_TOKEN" in text
    assert 'BRIDGE_V2_GITHUB_DIRECT_READ_TOKEN_FILE="$TOKEN"' in text
    assert 'chmod 600 "$TARGETS"' in text
    assert "set -x" not in text
    assert "printenv" not in text
    assert 'cat "$TOKEN"' not in text
    assert 'cat "$PEM"' not in text


def test_jarvas_launcher_executes_exact_phase2_topology() -> None:
    text = _text()
    for tool_id in (
        "github.get_checks",
        "github.get_issue",
        "github.get_pr",
        "github.get_repo",
        "github.search",
    ):
        assert text.count(f'"{tool_id}"') == 1
    assert '"arguments": {"number": 51}' in text
    assert '"arguments": {"number": 54}' in text
    assert '"arguments": {"ref": "$SOURCE_COMMIT"}' in text


def test_jarvas_launcher_runs_collector_then_strict_promotion_gate() -> None:
    text = _text()
    collector = text.index("v2_phase2_direct_read_acceptance.py")
    validator = text.index("validate_v2_phase2_connected_gate.py")
    assert collector < validator
    assert '--provider-type github_app' in text
    assert '--hermes-state-db "$SHADOW_HOME/state.db"' in text
    assert '--shadow-isolation "$SHADOW_ISOLATION"' in text


def test_jarvas_launcher_cleanup_targets_only_owned_runtime() -> None:
    text = _text()
    assert 'cleanup_process_group "$SHADOW_BRIDGE_PID"' in text
    assert 'cleanup_process_group "$SHADOW_HERMES_PID"' in text
    assert 'rm -rf -- "$SHADOW_HOME"' in text
    assert "pkill" not in text
    assert "killall" not in text
    assert 'rm -rf -- "$SOURCE_HERMES_HOME"' not in text


def test_jarvas_launcher_only_reports_sanitized_gate_summary() -> None:
    text = _text()
    assert '"shadow_isolation": "PASS"' in text
    assert '"privacy_pass": all(value is False for value in privacy.values())' in text
    assert '"direct_hermes_llm_tokens"' in text
    assert '"v1_shadow_hermes_llm_tokens"' in text
    assert '"mutations_observed"' in text
    assert '"contaminated_windows"' in text
