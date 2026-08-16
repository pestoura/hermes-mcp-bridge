"""EPIC-03 regression gate: the epic-03 credential provider file set must stay ruff-clean.

Causal TDD contract: this test is RED while lint findings exist in the
EPIC-03 source/test files and turns GREEN once they are fixed. It mirrors the
CI ``ruff check .`` contract scoped to the files introduced or changed by
EPIC-03 so the regression cannot silently reappear.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TARGETS = [
    "src/hermes_mcp_bridge/v2/provider_credentials.py",
    "src/hermes_mcp_bridge/v2/vault_credentials.py",
    "tests/test_v2_epic03_vault_credentials.py",
    "tests/test_v2_epic03_vault_provider.py",
    "tests/test_v2_epic03_vault_hardening.py",
    "tests/test_v2_epic03_vault_review_hardening.py",
]


@pytest.mark.skipif(shutil.which("ruff") is None, reason="ruff not installed")
def test_epic03_files_ruff_clean() -> None:
    cmd = [sys.executable, "-m", "ruff", "check", *(str(_REPO_ROOT / t) for t in _TARGETS)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 0, (
        "ruff reported lint errors in EPIC-03 files:\n"
        f"{result.stdout}\n{result.stderr}"
    )
