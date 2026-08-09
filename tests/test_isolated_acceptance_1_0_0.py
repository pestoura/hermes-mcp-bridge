"""Static safety contract for the 1.0.0 isolated runtime acceptance."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "scripts" / "isolated_acceptance_1_0_0.py"
MOCK = ROOT / "tests" / "isolated" / "mock_hermes.py"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def test_isolated_acceptance_assets_exist() -> None:
    assert HARNESS.is_file()
    assert MOCK.is_file()
    assert WORKFLOW.is_file()


def test_harness_never_uses_production_names_paths_or_host_network() -> None:
    source = HARNESS.read_text(encoding="utf-8")

    assert "hermes-1-0-accept-" in source
    assert "/home/estourpm" not in source
    assert '"--network",\n            "host"' not in source
    assert '"--network", "host"' not in source
    assert "hermes-mcp-bridge-deploy" not in source
    assert "ritmo_health" not in source
    assert "ritmo_claim" not in source
    assert "ritmo_start" not in source
    assert '"production_touched": False' in source
    assert '"ritmo_used": False' in source


def test_harness_calls_only_declared_read_only_mcp_tools() -> None:
    source = HARNESS.read_text(encoding="utf-8")

    expected = {
        "hermes_health",
        "hermes_readiness",
        "hermes_capabilities",
        "hermes_agent_card",
    }
    for tool in expected:
        assert f'call_tool("{tool}"' in source
    for forbidden in (
        "hermes_submit",
        "hermes_prompt",
        "hermes_stop",
        "hermes_approval_create",
        "hermes_approval_respond",
        "hermes_execute_approved_plan",
        "hermes_checkpoint_create",
        "hermes_continue",
        "hermes_saga_start",
        "hermes_saga_compensate",
        "hermes_lock_acquire",
        "hermes_lock_release",
    ):
        assert f'call_tool("{forbidden}"' not in source


def test_harness_pins_container_hardening_and_loopback_publish() -> None:
    source = HARNESS.read_text(encoding="utf-8")

    for token in (
        '"--read-only"',
        '"--cap-drop"',
        '"ALL"',
        '"no-new-privileges:true"',
        '"127.0.0.1:{port}:8765"',
        '"/var/run/docker.sock"',
        '"candidate root filesystem is not read-only"',
        '"candidate MCP port is not bound to loopback"',
    ):
        assert token in source


def test_harness_pins_file_backed_security_and_features_off() -> None:
    source = HARNESS.read_text(encoding="utf-8")

    for token in (
        "HERMES_API_KEY_FILE=/run/secrets/hermes_api_key",
        "HERMES_BRIDGE_HMAC_SECRET_FILE=/run/secrets/hermes_bridge_hmac_secret",
        "BRIDGE_POLICY_PATH=/etc/hermes-mcp-bridge/policies/production.json",
        "BRIDGE_METRICS_ENABLED=0",
        "BRIDGE_TRACING_ENABLED=0",
        "BRIDGE_TRACING_EXPORT=0",
        "BRIDGE_RETRY_ENABLED=false",
        "BRIDGE_CIRCUIT_ENABLED=false",
        'hmac_posture.get("source_type") != "file"',
        'policy.get("source") != "file"',
    ):
        assert token in source


def test_harness_requires_restart_state_and_json_log_evidence() -> None:
    source = HARNESS.read_text(encoding="utf-8")

    restart_call = '_docker("restart", resources["bridge"])'
    assert restart_call in source
    assert source.count(restart_call) == 1
    assert "authorized restart did not replace the container process exactly once" in source
    assert 'before_state.get("Pid")' in source
    assert 'after_state.get("Pid")' in source
    assert 'before_state.get("StartedAt")' in source
    assert 'after_state.get("StartedAt")' in source
    assert "PRAGMA quick_check" in source
    assert "PRAGMA integrity_check" in source
    assert "emitted non-JSON log line" in source
    assert "sensitive material appeared in container logs" in source
    assert "candidate posture changed after restart" in source
    assert "candidate state integrity changed after restart" in source
    assert '"authorized_restarts": 1' in source
    assert '"restart_evidence": {' in source
    assert "result.stdout.splitlines()" in source
    assert "result.stderr.splitlines()" in source


def test_mock_is_finite_read_only_and_does_not_log_headers_or_bodies() -> None:
    source = MOCK.read_text(encoding="utf-8")

    assert "ALLOWED_GET_PATHS = frozenset({" in source
    assert '"/health"' in source
    assert '"/health/detailed"' in source
    assert '"/v1/capabilities"' in source
    assert "do_POST = _reject_mutation" in source
    assert "do_PUT = _reject_mutation" in source
    assert "do_PATCH = _reject_mutation" in source
    assert "do_DELETE = _reject_mutation" in source
    assert '"mock.mutation_rejected"' in source
    assert "self.headers" not in source.replace('self.headers.get("Authorization")', "")
    assert "self.rfile" not in source


def test_ci_runs_acceptance_after_build_and_before_trivy() -> None:
    payload = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    acceptance_job = payload["jobs"]["acceptance"]
    assert acceptance_job["needs"] == "test"

    steps = acceptance_job["steps"]
    names = [step.get("name") for step in steps]

    build_index = names.index("Build runtime image")
    acceptance_index = names.index("Accept candidate in isolated Docker stack")
    trivy_index = names.index("Scan runtime image (Trivy)")
    assert build_index < acceptance_index < trivy_index

    acceptance = steps[acceptance_index]
    assert "if" not in acceptance
    assert "isolated_acceptance_1_0_0.py" in acceptance["run"]
    assert "--image hermes-mcp-bridge:ci" in acceptance["run"]


def test_ci_retains_sbom_as_blocking_release_evidence() -> None:
    payload = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert payload["permissions"]["contents"] == "write"

    steps = payload["jobs"]["acceptance"]["steps"]
    retention = next(
        step for step in steps if step.get("name") == "Retain SBOM as release evidence"
    )
    assert "if" not in retention
    assert retention.get("continue-on-error") is not True
    assert retention["env"]["EVIDENCE_TAG"] == ("sbom-evidence-${{ github.sha }}")

    command = retention["run"]
    assert "gh release create" in command
    assert "gh release upload" in command
    assert "--draft" in command
    assert '--target "${{ github.sha }}"' in command
    assert "sbom-cyclonedx.json" in command
    assert "SBOM release evidence was not retained" in command
    assert "actions/upload-artifact" not in WORKFLOW.read_text(encoding="utf-8")


def test_pass_marker_is_unique_and_production_safe() -> None:
    source = HARNESS.read_text(encoding="utf-8")

    pass_marker = "HERMES_BRIDGE_1_0_0_ISOLATED_ACCEPTANCE_PASS"
    fail_marker = "HERMES_BRIDGE_1_0_0_ISOLATED_ACCEPTANCE_FAIL"
    assert source.count(pass_marker) == 1
    assert source.count(fail_marker) == 1
    assert '"production_touched": False' in source
    assert '"ritmo_used": False' in source
