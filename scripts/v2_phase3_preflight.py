#!/usr/bin/env python3
"""Phase 3 canonical preflight — static, offline, fail-closed.

Bootstrap housekeeping for the Phase 3 mutation wave. It implements no Phase 3
feature: it only asserts the invariants every Phase 3 lane must preserve, so a
lane PR that breaks one of them is blocked before review.

Checks (all must pass; an unevaluated check is a failure, never "not
applicable"):

* ``V1_CONTRACT``   bridge contract ``1.0.0``, schema ``0.6.1``, exactly 27 tools.
* ``NO_REPO_DELETE`` no source file can emit a repository-deletion request.
* ``CAP_DISJOINT``  the read capability id is not reused as a write capability.
* ``NO_SHELL_SURFACE`` no public tool id exposes a generic shell/exec surface.

Usage::

    python scripts/v2_phase3_preflight.py [--json]

Exit code 0 only when ``failures`` is empty.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src" / "hermes_mcp_bridge"

EXPECTED_CONTRACT_VERSION = "1.0.0"
EXPECTED_SCHEMA_VERSION = "0.6.1"
EXPECTED_TOOL_COUNT = 27

READ_CAPABILITY = "github.read"
WRITE_CAPABILITY = "github.write"

#: Any HTTP DELETE targeting a bare repository resource is forbidden outright.
_REPO_DELETE_PATTERNS = (
    re.compile(r"""["']DELETE["']\s*,\s*["']/repos/\{[^/'"]+\}/\{[^/'"]+\}["']"""),
    re.compile(r"""\.delete\(\s*f?["']/repos/\{[^/'"]+\}/\{[^/'"]+\}["']\s*\)"""),
    re.compile(r"""delete_repository\s*\(\s*\)"""),
)

_SHELL_TOOL_PATTERN = re.compile(
    r"""["'](?:[a-z0-9_.]*)(?:shell|exec_command|run_command|subprocess_run)["']"""
)


def _python_sources() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if p.is_file())


def _check_v1_contract() -> list[str]:
    sys.path.insert(0, str(REPO_ROOT / "src"))
    try:
        from hermes_mcp_bridge import contracts
    except Exception as exc:  # pragma: no cover - defensive
        return [f"V1_CONTRACT: import failed: {exc.__class__.__name__}"]

    failures: list[str] = []
    if contracts.CURRENT_CONTRACT_VERSION != EXPECTED_CONTRACT_VERSION:
        failures.append(
            "V1_CONTRACT: contract_version="
            f"{contracts.CURRENT_CONTRACT_VERSION!r} expected {EXPECTED_CONTRACT_VERSION!r}"
        )
    if contracts.SCHEMA_VERSION != EXPECTED_SCHEMA_VERSION:
        failures.append(
            f"V1_CONTRACT: schema_version={contracts.SCHEMA_VERSION!r} "
            f"expected {EXPECTED_SCHEMA_VERSION!r}"
        )
    count = contracts.expected_tool_count()
    if count != EXPECTED_TOOL_COUNT:
        failures.append(f"V1_CONTRACT: tool_count={count} expected {EXPECTED_TOOL_COUNT}")
    return failures


def _check_no_repo_delete() -> list[str]:
    failures: list[str] = []
    for path in _python_sources():
        text = path.read_text(encoding="utf-8")
        for pattern in _REPO_DELETE_PATTERNS:
            if pattern.search(text):
                rel = path.relative_to(REPO_ROOT)
                failures.append(f"NO_REPO_DELETE: {rel} matches {pattern.pattern!r}")
    return failures


def _check_capability_disjointness() -> list[str]:
    failures: list[str] = []
    for path in _python_sources():
        text = path.read_text(encoding="utf-8")
        if f'"{READ_CAPABILITY}"' not in text and f"'{READ_CAPABILITY}'" not in text:
            continue
        # A module may name both ids, but never declare them equal.
        if re.search(
            rf"""{re.escape(WRITE_CAPABILITY)}["']\s*==\s*["']{re.escape(READ_CAPABILITY)}""",
            text,
        ):
            rel = path.relative_to(REPO_ROOT)
            failures.append(f"CAP_DISJOINT: {rel} equates read/write capability")
    return failures


def _check_no_shell_surface() -> list[str]:
    failures: list[str] = []
    for path in _python_sources():
        if path.name.startswith("test_"):
            continue
        text = path.read_text(encoding="utf-8")
        for match in _SHELL_TOOL_PATTERN.finditer(text):
            token = match.group(0)
            failures.append(f"NO_SHELL_SURFACE: {path.relative_to(REPO_ROOT)} declares {token}")
    return failures


CHECKS = {
    "V1_CONTRACT": _check_v1_contract,
    "NO_REPO_DELETE": _check_no_repo_delete,
    "CAP_DISJOINT": _check_capability_disjointness,
    "NO_SHELL_SURFACE": _check_no_shell_surface,
}


def run() -> dict[str, object]:
    failures: list[str] = []
    evaluated: list[str] = []
    for name, check in CHECKS.items():
        try:
            failures.extend(check())
        except Exception as exc:  # fail closed
            failures.append(f"{name}: check raised {exc.__class__.__name__}")
        evaluated.append(name)
    return {
        "preflight": "v2_phase3",
        "checks_evaluated": evaluated,
        "failures": failures,
        "verdict": "PASS" if not failures else "FAIL",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    args = parser.parse_args()

    result = run()
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"verdict: {result['verdict']}")
        for failure in result["failures"]:  # type: ignore[union-attr]
            print(f"  - {failure}")
    return 0 if result["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
