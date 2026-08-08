from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "v2_phase2_connected_jarvas.sh"


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_jarvas_launcher_is_valid_bash() -> None:
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_jarvas_launcher_fails_closed_without_shadow_mutation_basis() -> None:
    text = _text()
    assert 'HERMES_V2_SHADOW_MUTATION_BASIS:-none' in text
    assert 'github_audit_log_reviewed|read_only_credential_enforced' in text
    assert 'blocked "SHADOW_MUTATION_BASIS_UNPROVEN"' in text


def test_jarvas_launcher_uses_only_file_backed_direct_secret() -> None:
    text = _text()
    assert "unset BRIDGE_V2_GITHUB_DIRECT_READ_TOKEN" in text
    assert 'BRIDGE_V2_GITHUB_DIRECT_READ_TOKEN_FILE="$TOKEN"' in text
    assert 'chmod 600 "$TARGETS"' in text
    assert "set -x" not in text
    assert "printenv" not in text
    assert " env " not in text
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


def test_jarvas_launcher_runs_collector_then_canonical_validator() -> None:
    text = _text()
    collector = text.index("v2_phase2_direct_read_acceptance.py")
    validator = text.index("validate_v2_phase2_direct_read_evidence.py")
    assert collector < validator
    assert '--provider-type github_app' in text
    assert '--shadow-mutation-basis "$SHADOW_BASIS"' in text
    assert '--hermes-state-db "$STATE_DB"' in text


def test_jarvas_launcher_only_reports_sanitized_gate_summary() -> None:
    text = _text()
    assert '"privacy_pass": all(value is False for value in privacy.values())' in text
    assert '"direct_hermes_llm_tokens"' in text
    assert '"v1_shadow_hermes_llm_tokens"' in text
    assert '"mutations_observed"' in text
    assert '"contaminated_windows"' in text
