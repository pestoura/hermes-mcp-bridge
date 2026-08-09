"""Controller-side tests for the Phase 3 preflight bootstrap.

These cover only the preflight harness itself. They implement no Phase 3
feature and must run fully offline.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT_PATH = REPO_ROOT / "scripts" / "v2_phase3_preflight.py"


def _load_preflight():
    spec = importlib.util.spec_from_file_location("v2_phase3_preflight", PREFLIGHT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def preflight():
    return _load_preflight()


def test_preflight_passes_on_current_tree(preflight) -> None:
    result = preflight.run()
    assert result["failures"] == []
    assert result["verdict"] == "PASS"


def test_all_checks_are_evaluated(preflight) -> None:
    result = preflight.run()
    assert set(result["checks_evaluated"]) == set(preflight.CHECKS)


def test_v1_invariants_are_the_expected_constants(preflight) -> None:
    assert preflight.EXPECTED_CONTRACT_VERSION == "1.0.0"
    assert preflight.EXPECTED_SCHEMA_VERSION == "0.6.1"
    assert preflight.EXPECTED_TOOL_COUNT == 27


def test_check_failure_is_reported_not_swallowed(preflight, monkeypatch) -> None:
    def _boom() -> list[str]:
        raise RuntimeError("nope")

    monkeypatch.setitem(preflight.CHECKS, "NO_REPO_DELETE", _boom)
    result = preflight.run()
    assert result["verdict"] == "FAIL"
    assert any("NO_REPO_DELETE" in failure for failure in result["failures"])


def test_repo_delete_pattern_detects_a_deletion_call(preflight) -> None:
    sample = 'self._request("DELETE", "/repos/{owner}/{repo}")'
    assert any(p.search(sample) for p in preflight._REPO_DELETE_PATTERNS)
