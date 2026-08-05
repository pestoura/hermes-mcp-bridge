"""CI matrix validation for Python 3.11 / 3.12.

Intentionally does NOT use PyYAML: it reads the workflow as plain text and
checks the matrix contract with the stdlib ``re`` module, so the test never
fails merely because the local environment lacks a YAML parser. It guards the
two things the CI reinforcement relies on: the supported interpreter matrix and
the ``${{ matrix.python-version }}`` variable wiring in ``setup-python``.
"""

from __future__ import annotations

import re
from pathlib import Path

CI_YML = Path(".github/workflows/ci.yml")
EXPECTED_VERSIONS = ["3.11", "3.12"]


def _read_ci() -> str:
    assert CI_YML.exists(), f"{CI_YML} not found"
    return CI_YML.read_text(encoding="utf-8")


def test_ci_matrix_python_versions():
    text = _read_ci()
    matrix = re.search(r"matrix:\s*\n((?:[ \t]{8,}[^\n]*\n)+)", text)
    assert matrix, "strategy.matrix.python-version block not found"
    ver = re.search(r"python-version:\s*\[([^\]]+)\]", matrix.group(1))
    assert ver, "matrix.python-version list not found"
    versions = [v.strip().strip('"') for v in ver.group(1).split(",")]
    assert versions == EXPECTED_VERSIONS, versions


def test_ci_matrix_fail_fast_false():
    text = _read_ci()
    assert "fail-fast: false" in text, "strategy.fail-fast: false missing"


def test_ci_setup_python_uses_matrix_variable():
    text = _read_ci()
    assert (
        "python-version: ${{ matrix.python-version }}" in text
    ), "setup-python does not use ${{ matrix.python-version }}"
