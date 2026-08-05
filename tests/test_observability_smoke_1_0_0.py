"""Execution tests for the 1.0.0 observability smoke."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SMOKE_PATH = REPO_ROOT / "scripts" / "observability_smoke.py"
SPEC = importlib.util.spec_from_file_location("observability_smoke_1_0_0", SMOKE_PATH)
assert SPEC is not None and SPEC.loader is not None
SMOKE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SMOKE
SPEC.loader.exec_module(SMOKE)


def test_alloy_profile_smoke_passes() -> None:
    notes = SMOKE.check_alloy_profile()
    assert len(notes) == 1
    assert "loopback" in notes[0]
    assert "bridge namespace" in notes[0]


def test_full_offline_observability_smoke_passes(capsys) -> None:
    result = SMOKE.main(["--check-config", "--check-logging"])
    captured = capsys.readouterr()

    assert result == 0
    assert "FAIL" not in captured.err
    assert "Alloy profile ok" in captured.out
    assert "logging ok" in captured.out
