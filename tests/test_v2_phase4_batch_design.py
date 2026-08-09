"""Docs-only guards for the Phase 4 BATCH design lane.

These tests implement no Phase 4 feature. They assert that the design lane is
complete, self-contained, correctly marked as NOT_IMPLEMENTED, and that no
BATCH runtime surface has been introduced before the Phase 3 gate
``DIRECT_MUTATION_ACCEPTED``. They run fully offline.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LANE = REPO_ROOT / "docs" / "v2" / "phase4"
SRC = REPO_ROOT / "src" / "hermes_mcp_bridge"

EXPECTED_DOCS = {
    "README.md",
    "contract.md",
    "limits-and-budgets.md",
    "concurrency-and-scheduling.md",
    "failure-and-cancellation.md",
    "step-governance.md",
    "aggregation-and-evidence.md",
    "non-goals.md",
    "acceptance-scenarios.md",
    "dependency-map.md",
    "promotion.md",
}

# Phase 3 lane and Controller-owned documents this lane must never modify.
OFF_LIMITS = (
    REPO_ROOT / "docs" / "v2" / "roadmap.md",
    REPO_ROOT / "docs" / "v2" / "requirements" / "traceability-matrix.md",
    REPO_ROOT / "docs" / "v2" / "adrs" / "ADR-0008-batch-execution-semantics.md",
    REPO_ROOT / "docs" / "v2" / "contracts" / "batch-example.md",
)


def _lane_docs() -> list[Path]:
    return sorted(LANE.glob("*.md"))


def test_lane_exists_with_the_expected_documents() -> None:
    assert LANE.is_dir()
    assert {p.name for p in _lane_docs()} == EXPECTED_DOCS


@pytest.mark.parametrize("name", sorted(EXPECTED_DOCS))
def test_every_document_is_gated_until_batch_accepted(name: str) -> None:
    """Phase 3 is accepted, so the lane is unblocked but still flag-gated."""
    text = (LANE / name).read_text(encoding="utf-8")
    assert "DIRECT_MUTATION_ACCEPTED" in text
    assert "BATCH_FEATURE_ENABLED" in text
    assert "BATCH_ACCEPTED" in text


def test_contract_defines_the_typed_surface() -> None:
    text = (LANE / "contract.md").read_text(encoding="utf-8")
    for token in (
        "BatchRequest",
        "BatchStep",
        "BatchStepResult",
        "BatchResult",
        "failure_policy",
        "max_parallelism",
        "batch_timeout_s",
        "step_timeout_s",
        "idempotency_key",
        "approval_ref",
        "aggregate_status",
        "depends_on",
    ):
        assert token in text, token


def test_limits_define_max_items_and_parallelism_ceilings() -> None:
    text = (LANE / "limits-and-budgets.md").read_text(encoding="utf-8")
    for token in (
        "BATCH_MAX_ITEMS",
        "BATCH_MAX_PARALLELISM",
        "BATCH_MAX_TIMEOUT_S",
        "BATCH_MAX_INFLIGHT_GLOBAL",
        "backpressure",
    ):
        assert token.lower() in text.lower(), token


def test_failure_lane_covers_partial_and_fail_closed_cancellation() -> None:
    text = (LANE / "failure-and-cancellation.md").read_text(encoding="utf-8")
    assert "fail_fast" in text
    assert "continue_on_error" in text
    assert "Fail-closed cancellation" in text
    assert "NOT_STARTED" in text
    assert "no compensating" in text.lower()


def test_governance_reuses_phase3_components_per_step() -> None:
    text = (LANE / "step-governance.md").read_text(encoding="utf-8")
    assert "mutation_idempotency" in text
    assert "mutation_audit" in text
    assert "mutation_digest" in text
    assert "no batch-level authorization" in text.lower()


def test_non_goals_forbid_shell_and_generic_http() -> None:
    text = (LANE / "non-goals.md").read_text(encoding="utf-8").lower()
    assert "no generic shell surface" in text
    assert "no generic http surface" in text
    assert "no inter-step data flow" in text


def test_acceptance_scenarios_include_non_serial_execution() -> None:
    text = (LANE / "acceptance-scenarios.md").read_text(encoding="utf-8")
    assert "Non-serial execution" in text
    ids = set(re.findall(r"\bS-(\d{2})\b", text))
    # S-01..S-27 must all be present.
    assert {f"{n:02d}" for n in range(1, 28)} <= ids


def test_dependency_map_blocks_on_the_phase3_gate() -> None:
    text = (LANE / "dependency-map.md").read_text(encoding="utf-8")
    assert "DIRECT_MUTATION_ACCEPTED" in text
    assert "roadmap.md" in text
    assert "traceability-matrix.md" in text


def test_batch_runtime_lives_only_in_the_phase4_modules() -> None:
    """The runtime exists now, but it must stay confined and never touch V1."""
    pattern = re.compile(r"\bBatchRequest\b|\bBatchStep\b|\bBatchResult\b")
    allowed = {
        "src/hermes_mcp_bridge/v2/__init__.py",
        "src/hermes_mcp_bridge/v2/batch_contract.py",
        "src/hermes_mcp_bridge/v2/batch_scheduler.py",
    }
    offenders = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in SRC.rglob("*.py")
        if pattern.search(path.read_text(encoding="utf-8"))
        and path.relative_to(REPO_ROOT).as_posix() not in allowed
    ]
    assert offenders == []


@pytest.mark.parametrize("path", OFF_LIMITS, ids=lambda p: p.name)
def test_controller_owned_documents_are_still_present(path: Path) -> None:
    """This lane refines them; it must not remove or relocate them."""
    assert path.is_file()
