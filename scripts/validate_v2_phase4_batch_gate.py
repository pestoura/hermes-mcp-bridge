#!/usr/bin/env python3
"""Phase 4 promotion gate — `BATCH_ACCEPTED`.

Fail-closed, machine-checked promotion for the BATCH engine. There is no
self-approval path: every criterion is evaluated against the real repository
and a real test run, or recorded as a failure.

Layers (both must return no failures):

* INNER — V1 contract invariants, the full S-01..S-27 acceptance suite executed
  for real (a skip or a missing scenario is a failure), the canonical limit
  constants, the feature flag defaulting to off, and a live concurrency
  measurement proving `max_observed_inflight > 1` for independent read steps.
* OUTER — SHA-256 binding of every Phase 4 module against the live tree, an AST
  scan proving zero generic surface (no shell/subprocess/socket/HTTP/eval), and
  the `DIRECT_MUTATION_ACCEPTED` marker proving Phase 3 preceded Phase 4.

Usage::

    python scripts/validate_v2_phase4_batch_gate.py \
        --json-out docs/v2/evidence/phase4-batch-acceptance.json

Exit code 0 only when ``failures`` is empty (gate ACCEPTED).
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src" / "hermes_mcp_bridge"
TEST_FILE = REPO_ROOT / "tests" / "test_v2_phase4_batch_scheduler.py"
DESIGN_LANE = REPO_ROOT / "docs" / "v2" / "phase4"

EXPECTED_CONTRACT_VERSION = "1.0.0"
EXPECTED_SCHEMA_VERSION = "0.6.1"
EXPECTED_TOOL_COUNT = 27

ACCEPTED_GATE = "BATCH_ACCEPTED"
BLOCKED_GATE = "BATCH_BLOCKED"

PHASE4_MODULES = ("v2/batch_contract.py", "v2/batch_scheduler.py")

EXPECTED_LIMITS = {
    "BATCH_MAX_ITEMS": 10,
    "BATCH_MAX_PARALLELISM": 4,
    "BATCH_MAX_PARALLELISM_MUTATION": 1,
    "BATCH_MAX_TIMEOUT_S": 300,
    "BATCH_MAX_INFLIGHT_GLOBAL": 8,
}

#: Every acceptance scenario must be represented by at least one real test.
REQUIRED_SCENARIOS = tuple(f"s{index:02d}" for index in range(1, 28))

BANNED_MODULES = {
    "subprocess",
    "socket",
    "requests",
    "httpx",
    "urllib",
    "http",
    "shlex",
    "pty",
}
BANNED_CALLS = {"eval", "exec", "compile", "__import__", "system", "popen"}


def _module_digest(rel: str) -> str:
    return hashlib.sha256((SRC / rel).read_bytes()).hexdigest()


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, check=False)


def _import_v2() -> None:
    path = str(REPO_ROOT / "src")
    if path not in sys.path:
        sys.path.insert(0, path)


def _check_v1_contract() -> list[str]:
    _import_v2()
    try:
        from hermes_mcp_bridge import contracts
        from hermes_mcp_bridge.v2 import batch_contract  # noqa: F401
    except Exception as exc:  # pragma: no cover - defensive
        return [f"A4-01: import failed: {exc.__class__.__name__}"]
    failures: list[str] = []
    if contracts.CURRENT_CONTRACT_VERSION != EXPECTED_CONTRACT_VERSION:
        failures.append(f"A4-01: contract={contracts.CURRENT_CONTRACT_VERSION}")
    if contracts.SCHEMA_VERSION != EXPECTED_SCHEMA_VERSION:
        failures.append(f"A4-01: schema={contracts.SCHEMA_VERSION}")
    if contracts.expected_tool_count() != EXPECTED_TOOL_COUNT:
        failures.append(f"A4-01: tools={contracts.expected_tool_count()}")
    return failures


def _check_limits_and_flag() -> list[str]:
    _import_v2()
    from hermes_mcp_bridge.v2 import batch_contract

    failures: list[str] = []
    for name, expected in EXPECTED_LIMITS.items():
        actual = getattr(batch_contract, name, None)
        if actual != expected:
            failures.append(f"A4-02: {name}={actual!r} expected {expected!r}")
    if batch_contract.BATCH_FEATURE_ENABLED is not False:
        failures.append("A4-03: BATCH_FEATURE_ENABLED must default to False")
    return failures


def _check_scenarios_present() -> list[str]:
    if not TEST_FILE.is_file():
        return ["A4-04: acceptance suite missing"]
    text = TEST_FILE.read_text(encoding="utf-8")
    names = set(re.findall(r"def (test_(s\d{2})[a-z]?_[a-z0-9_]+)", text))
    covered = {scenario for _, scenario in names}
    missing = [scenario for scenario in REQUIRED_SCENARIOS if scenario not in covered]
    if missing:
        return [f"A4-04: acceptance scenarios not implemented: {','.join(missing)}"]
    return []


def _check_acceptance_suite() -> list[str]:
    """Run the real suite. A skip or xfail is a failure, not a pass."""
    result = _run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly", "-rs", str(TEST_FILE)]
    )
    if result.returncode != 0:
        return [f"A4-05: acceptance suite failed:\n{result.stdout[-1500:]}"]
    tail = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    if "skipped" in tail or "xfail" in tail:
        return [f"A4-05: acceptance suite reported skips: {tail}"]
    return []


def _check_no_generic_surface() -> list[str]:
    failures: list[str] = []
    for rel in PHASE4_MODULES:
        tree = ast.parse((SRC / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in BANNED_MODULES:
                        failures.append(f"A4-06: {rel} imports {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.split(".")[0] in BANNED_MODULES:
                    failures.append(f"A4-06: {rel} imports from {node.module}")
            elif isinstance(node, ast.Call):
                func = node.func
                name = getattr(func, "id", None) or getattr(func, "attr", None)
                if name in BANNED_CALLS:
                    failures.append(f"A4-06: {rel} calls {name}()")
    return failures


def _measure_real_concurrency() -> tuple[int, list[str]]:
    """Live measurement: independent read steps must genuinely overlap."""
    _import_v2()
    from hermes_mcp_bridge.v2.batch_contract import (
        BatchFailurePolicy,
        BatchRequest,
        BatchStatus,
        BatchStep,
    )
    from hermes_mcp_bridge.v2.batch_scheduler import BatchScheduler

    barrier = asyncio.Barrier(2)

    async def executor(step: BatchStep) -> dict[str, Any]:
        await asyncio.wait_for(barrier.wait(), timeout=5)
        return {"step": step.step_id}

    request = BatchRequest(
        batch_id="gate-concurrency",
        steps=tuple(
            BatchStep(step_id=f"s{index}", tool="github.get_repo", step_timeout_s=10)
            for index in range(4)
        ),
        failure_policy=BatchFailurePolicy.CONTINUE_ON_ERROR,
        max_parallelism=2,
        batch_timeout_s=30,
    )
    scheduler = BatchScheduler(executor, enabled=True)
    try:
        result = asyncio.run(scheduler.run(request))
    except Exception as exc:  # fail closed
        return 0, [f"A4-07: concurrency probe raised {exc.__class__.__name__}"]

    failures: list[str] = []
    if result.aggregate_status is not BatchStatus.SUCCESS:
        failures.append(f"A4-07: probe status={result.aggregate_status.value}")
    if result.max_observed_inflight < 2:
        failures.append(
            f"A4-07: max_observed_inflight={result.max_observed_inflight} (serial execution)"
        )
    return result.max_observed_inflight, failures


def _check_direct_mutation_accepted() -> list[str]:
    path = REPO_ROOT / "docs" / "v2" / "evidence" / "phase3-direct-mutation-acceptance.json"
    if not path.is_file():
        return ["A4-08: Phase 3 acceptance evidence missing"]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ["A4-08: Phase 3 acceptance evidence unreadable"]
    if payload.get("gate") != "DIRECT_MUTATION_ACCEPTED" or payload.get("failures"):
        return ["A4-08: DIRECT_MUTATION_ACCEPTED not proven before Phase 4"]
    return []


def _check_design_lane_present() -> list[str]:
    if not DESIGN_LANE.is_dir():
        return ["A4-09: Phase 4 design lane missing"]
    required = {
        "contract.md",
        "concurrency-and-scheduling.md",
        "limits-and-budgets.md",
        "failure-and-cancellation.md",
        "step-governance.md",
        "acceptance-scenarios.md",
    }
    present = {path.name for path in DESIGN_LANE.glob("*.md")}
    missing = sorted(required - present)
    if missing:
        return [f"A4-09: design documents missing: {','.join(missing)}"]
    return []


def validate_gate() -> dict[str, Any]:
    failures: list[str] = []
    failures += _check_v1_contract()
    failures += _check_limits_and_flag()
    failures += _check_design_lane_present()
    failures += _check_scenarios_present()
    failures += _check_acceptance_suite()
    failures += _check_no_generic_surface()
    max_inflight, concurrency_failures = _measure_real_concurrency()
    failures += concurrency_failures
    failures += _check_direct_mutation_accepted()

    return {
        "gate": ACCEPTED_GATE if not failures else BLOCKED_GATE,
        "failures": list(dict.fromkeys(failures)),
        "max_observed_inflight": max_inflight,
        "limits": EXPECTED_LIMITS,
        "module_binding_sha256": {rel: _module_digest(rel) for rel in PHASE4_MODULES},
        "source_commit": _run(["git", "rev-parse", "HEAD"]).stdout.strip(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-out", required=True)
    args = parser.parse_args()

    result = validate_gate()
    Path(args.json_out).write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    summary = {
        key: value
        for key, value in result.items()
        if key not in ("module_binding_sha256", "limits")
    }
    print(json.dumps(summary, sort_keys=True))
    return 0 if not result["failures"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
