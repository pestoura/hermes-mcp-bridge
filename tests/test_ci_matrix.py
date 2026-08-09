"""CI topology validation for Python 3.11 / 3.12.

Intentionally does NOT use PyYAML: it reads the workflow as plain text and
checks the matrix contract with the stdlib ``re`` module, so the test never
fails merely because the local environment lacks a YAML parser.
"""

from __future__ import annotations

import re
from pathlib import Path

CI_YML = Path(".github/workflows/ci.yml")
EXPECTED_VERSIONS = ["3.11", "3.12"]
HEAVY_STEPS = [
    "Build runtime image",
    "Accept candidate in isolated Docker stack",
    "Scan runtime image (Trivy)",
    "Generate SBOM (CycloneDX)",
]


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


def test_heavy_acceptance_waits_for_entire_test_matrix():
    text = _read_ci()
    acceptance = re.search(
        r"(?ms)^  acceptance:\n.*?^    needs: test\s*$",
        text,
    )
    assert acceptance, "acceptance job must depend on the complete test matrix"


def test_heavy_release_steps_are_outside_matrix_job():
    text = _read_ci()
    test_start = text.index("  test:\n")
    acceptance_start = text.index("  acceptance:\n")
    test_job = text[test_start:acceptance_start]
    acceptance_job = text[acceptance_start:]

    for step in HEAVY_STEPS:
        assert step not in test_job, f"{step} must not execute inside matrix job"
        assert step in acceptance_job, f"{step} missing from acceptance job"
