"""Acceptance harness and fail-closed gate tests for V2 Phase 1."""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from hermes_mcp_bridge.v2 import CapabilityDescriptor, CapabilityState, ToolRegistry

ROOT = Path(__file__).resolve().parents[1]
COLLECTOR = ROOT / "scripts" / "v2_phase1_registry_acceptance.py"
VALIDATOR = ROOT / "scripts" / "validate_v2_phase1_registry_evidence.py"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
TEST_SHA = "0" * 40
SENTINEL = "PHASE1_TEST_EDITORIAL_SENTINEL"


def _load_validator() -> Any:
    spec = importlib.util.spec_from_file_location("phase1_validator", VALIDATOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _collect(tmp_path: Path) -> dict[str, Any]:
    evidence = tmp_path / "evidence.json"
    result = subprocess.run(
        [
            sys.executable,
            str(COLLECTOR),
            "--source-commit",
            TEST_SHA,
            "--json-out",
            str(evidence),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(evidence.read_text(encoding="utf-8"))


def _validate(payload: dict[str, Any]) -> list[str]:
    return _load_validator().validate_evidence(payload)


def test_collector_and_validator_accept_complete_evidence(tmp_path: Path) -> None:
    payload = _collect(tmp_path)
    assert _validate(payload) == []
    assert payload["gate"] == "REGISTRY_EVIDENCE_COLLECTED"
    assert all(payload["checks"].values())


def test_cli_validator_returns_registry_accepted(tmp_path: Path) -> None:
    payload = _collect(tmp_path)
    evidence = tmp_path / "evidence-copy.json"
    gate = tmp_path / "gate.json"
    evidence.write_text(json.dumps(payload), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(VALIDATOR), str(evidence), "--json-out", str(gate)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    gate_payload = json.loads(gate.read_text(encoding="utf-8"))
    assert gate_payload == {
        "failures": [],
        "gate": "REGISTRY_ACCEPTED",
        "source_commit": TEST_SHA,
    }


@pytest.mark.parametrize(
    ("mutator", "expected_failure"),
    [
        (
            lambda p: p.update({"schema": "wrong"}),
            "invalid_schema",
        ),
        (
            lambda p: p["checks"].update({"unknown_tool_denied": False}),
            "check_failed:unknown_tool_denied",
        ),
        (
            lambda p: p["checks"].pop("projection_authorized_subset_only"),
            "checks_missing:projection_authorized_subset_only",
        ),
        (
            lambda p: p["snapshot"].update({"capability_snapshot_hash": "bad"}),
            "snapshot_hash_invalid",
        ),
        (
            lambda p: p["projection"].update({"tool_ids": ["github.get_pr"]}),
            "projection_tool_ids_invalid",
        ),
        (
            lambda p: p["v1"].update({"tool_contract_count": 28}),
            "v1_tool_contract_changed",
        ),
        (
            lambda p: p["privacy"].update({"credential_values_stored": True}),
            "privacy_contract_not_met",
        ),
        (
            lambda p: p.update({"password": "should-never-exist"}),
            "forbidden_evidence_keys:password",
        ),
    ],
)
def test_validator_fails_closed_on_tampering(
    tmp_path: Path,
    mutator: Any,
    expected_failure: str,
) -> None:
    payload = copy.deepcopy(_collect(tmp_path))
    mutator(payload)
    assert expected_failure in _validate(payload)


def test_capability_editorial_description_is_not_in_snapshot() -> None:
    descriptor = CapabilityDescriptor(
        capability_id="example.api",
        provider="example",
        state=CapabilityState.READY,
        description=SENTINEL,
    )
    registry = ToolRegistry()
    registry.register_capability(descriptor)
    snapshot = registry.snapshot()

    assert descriptor.description == SENTINEL
    assert SENTINEL in descriptor.model_dump_json()
    assert SENTINEL not in snapshot.canonical_json
    assert "description" not in descriptor.canonical()


def test_gate_requires_all_phase1_traceability_requirements(tmp_path: Path) -> None:
    payload = _collect(tmp_path)
    payload["requirements"].remove("V2-SEC-013")
    failures = _validate(payload)
    assert "requirements_missing:V2-SEC-013" in failures


def test_ci_retains_phase1_gate_as_blocking_draft_release() -> None:
    payload = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    acceptance_job = payload["jobs"]["acceptance"]
    assert acceptance_job["needs"] == "test"

    steps = acceptance_job["steps"]
    retention = next(
        step for step in steps if step.get("name") == "Retain Phase 1 registry acceptance evidence"
    )

    assert "if" not in retention
    assert retention.get("continue-on-error") is not True
    assert retention["env"]["PHASE1_EVIDENCE_TAG"] == (
        "phase1-registry-evidence-${{ github.sha }}"
    )
    command = retention["run"]
    assert "gh release create" in command
    assert "gh release upload" in command
    assert "--draft" in command
    assert '--target "${{ github.sha }}"' in command
    assert "phase1-registry-acceptance.json" in command
    assert "phase1-registry-gate.json" in command
    assert "Phase 1 registry acceptance evidence was not retained" in command
