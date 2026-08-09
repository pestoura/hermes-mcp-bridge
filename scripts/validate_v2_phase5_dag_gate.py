#!/usr/bin/env python3
"""Phase 5 promotion gate — `DAG_ACCEPTED`.

Fail-closed, machine-checked promotion for the DAG engine. There is no
self-approval path: every criterion is evaluated against the real repository and
a real test run, or recorded as a failure.

Layers (both must return no failures):

* INNER — V1 contract invariants, the full A5-01..A5-22 acceptance suite executed
  for real (a skip or a missing criterion is a failure), the canonical limit
  constants, the feature flag defaulting to off, a live determinism probe
  proving two independent validations of the same plan produce the same digest
  and the same topological order, and a live durability probe proving a
  write-ahead record exists before a mutation and survives reload.
* OUTER — SHA-256 binding of every Phase 5 module against the live tree, an AST
  scan proving zero generic surface (no shell/subprocess/socket/HTTP/eval), the
  closed TRANSFORM operation set, proof that no DAG tool leaked into the V1
  projection, and the `BATCH_ACCEPTED` marker proving Phase 4 preceded Phase 5.

Usage::

    python scripts/validate_v2_phase5_dag_gate.py \\
        --json-out docs/v2/evidence/phase5-dag-acceptance.json

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
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src" / "hermes_mcp_bridge"
TEST_FILE = REPO_ROOT / "tests" / "test_v2_phase5_dag_acceptance.py"
DESIGN_LANE = REPO_ROOT / "docs" / "v2" / "phase5"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "v2_phase5"

EXPECTED_CONTRACT_VERSION = "1.0.0"
EXPECTED_SCHEMA_VERSION = "0.6.1"
EXPECTED_TOOL_COUNT = 27

ACCEPTED_GATE = "DAG_ACCEPTED"
BLOCKED_GATE = "DAG_BLOCKED"

PHASE5_MODULES = (
    "v2/dag_contract.py",
    "v2/dag_transform.py",
    "v2/dag_digest.py",
    "v2/dag_validation.py",
    "v2/dag_store.py",
    "v2/dag_engine.py",
    "v2/dag_loader.py",
)

EXPECTED_LIMITS = {
    "DAG_MAX_NODES": 64,
    "DAG_MAX_DEPTH": 16,
    "DAG_MAX_FANOUT": 16,
    "DAG_MAX_PARALLELISM": 4,
    "DAG_MAX_PARALLELISM_MUTATION": 1,
    "DAG_MAX_TOTAL_WALL_MS": 900_000,
    "DAG_MAX_NODE_TIMEOUT_MS": 120_000,
}

EXPECTED_TRANSFORM_OPS = (
    "count",
    "filter_eq",
    "filter_in",
    "first",
    "map_field",
    "merge_objects",
    "project",
    "require_non_empty",
    "select",
    "sort_by",
    "to_list",
    "unique",
)

#: Every acceptance criterion must be represented by at least one real test.
REQUIRED_CRITERIA = tuple(f"a5_{index:02d}" for index in range(1, 23))

REQUIRED_DESIGN_DOCS = {
    "plan-definition.md",
    "dag-validation.md",
    "plan-digest.md",
    "scheduling.md",
    "checkpoint-and-resume.md",
    "failure-semantics.md",
    "compensation-and-saga.md",
    "per-node-governance.md",
    "acceptance-criteria.md",
}

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
BANNED_NAMES = {"eval", "exec", "compile", "__import__"}
BANNED_ATTRS = {"system", "popen", "spawn", "fork", "execv"}


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
        from hermes_mcp_bridge.v2 import dag_contract  # noqa: F401
    except Exception as exc:  # pragma: no cover - defensive
        return [f"A5-01: import failed: {exc.__class__.__name__}"]
    failures: list[str] = []
    if contracts.CURRENT_CONTRACT_VERSION != EXPECTED_CONTRACT_VERSION:
        failures.append(f"A5-01: contract={contracts.CURRENT_CONTRACT_VERSION}")
    if contracts.SCHEMA_VERSION != EXPECTED_SCHEMA_VERSION:
        failures.append(f"A5-01: schema={contracts.SCHEMA_VERSION}")
    if contracts.expected_tool_count() != EXPECTED_TOOL_COUNT:
        failures.append(f"A5-01: tools={contracts.expected_tool_count()}")
    leaked = sorted(name for name in contracts.required_tools() if "dag" in name.lower())
    if leaked:
        failures.append(f"A5-01: DAG tools leaked into projection: {','.join(leaked)}")
    return failures


def _check_limits_and_flag() -> list[str]:
    _import_v2()
    from hermes_mcp_bridge.v2 import dag_contract

    failures: list[str] = []
    for name, expected in EXPECTED_LIMITS.items():
        actual = getattr(dag_contract, name, None)
        if actual != expected:
            failures.append(f"A5-02: {name}={actual!r} expected {expected!r}")
    if dag_contract.DAG_FEATURE_ENABLED is not False:
        failures.append("A5-03: DAG_FEATURE_ENABLED must default to False")
    return failures


def _check_transform_closed_set() -> list[str]:
    _import_v2()
    from hermes_mcp_bridge.v2.dag_transform import TRANSFORM_OP_NAMES

    if TRANSFORM_OP_NAMES != EXPECTED_TRANSFORM_OPS:
        return [f"A5-04: transform op set drifted: {TRANSFORM_OP_NAMES}"]
    return []


def _check_criteria_present() -> list[str]:
    if not TEST_FILE.is_file():
        return ["A5-05: acceptance suite missing"]
    text = TEST_FILE.read_text(encoding="utf-8")
    covered = {
        criterion for _, criterion in re.findall(r"def (test_(a5_\d{2})[a-z]?_[a-z0-9_]+)", text)
    }
    missing = [name for name in REQUIRED_CRITERIA if name not in covered]
    if missing:
        return [f"A5-05: acceptance criteria not implemented: {','.join(missing)}"]
    return []


def _check_design_lane_present() -> list[str]:
    if not DESIGN_LANE.is_dir():
        return ["A5-06: Phase 5 design lane missing"]
    present = {path.name for path in DESIGN_LANE.glob("*.md")}
    missing = sorted(REQUIRED_DESIGN_DOCS - present)
    if missing:
        return [f"A5-06: design documents missing: {','.join(missing)}"]
    if not FIXTURE_DIR.is_dir() or not list(FIXTURE_DIR.glob("plan_*.json")):
        return ["A5-06: Phase 5 fixture corpus missing"]
    return []


def _check_acceptance_suite() -> list[str]:
    """Run the real suite. A skip or xfail is a failure, not a pass."""
    result = _run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly", "-rs", str(TEST_FILE)]
    )
    if result.returncode != 0:
        return [f"A5-07: acceptance suite failed:\n{result.stdout[-1500:]}"]
    tail = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    if "skipped" in tail or "xfail" in tail:
        return [f"A5-07: acceptance suite reported skips: {tail}"]
    return []


def _check_no_generic_surface() -> list[str]:
    failures: list[str] = []
    for rel in PHASE5_MODULES:
        tree = ast.parse((SRC / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in BANNED_MODULES:
                        failures.append(f"A5-08: {rel} imports {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.split(".")[0] in BANNED_MODULES:
                    failures.append(f"A5-08: {rel} imports from {node.module}")
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id in BANNED_NAMES:
                    failures.append(f"A5-08: {rel} calls {func.id}()")
                elif isinstance(func, ast.Attribute) and func.attr in BANNED_ATTRS:
                    failures.append(f"A5-08: {rel} calls .{func.attr}()")
    return failures


def _measure_determinism() -> tuple[str, list[str]]:
    """Live probe: identical plan → identical digest and identical order."""
    _import_v2()
    from hermes_mcp_bridge.v2.dag_loader import load_plan
    from hermes_mcp_bridge.v2.dag_validation import (
        StaticToolCatalog,
        ToolContract,
        validate_plan,
    )

    contracts = {
        "github.get_repo": ToolContract(
            tool_id="github.get_repo",
            arg_types={"repository": "string"},
            result_types={"name": "string", "topics": "list"},
        )
    }
    catalog = StaticToolCatalog(
        contracts=contracts,
        projected=frozenset(contracts),
        scope=frozenset({"owner/disposable"}),
    )
    try:
        left = validate_plan(load_plan(FIXTURE_DIR / "plan_digest_reorder_a.json"), catalog)
        right = validate_plan(load_plan(FIXTURE_DIR / "plan_digest_reorder_b.json"), catalog)
    except Exception as exc:  # fail closed
        return "", [f"A5-09: determinism probe raised {exc.__class__.__name__}: {exc}"]

    failures: list[str] = []
    if left.digest != right.digest:
        failures.append("A5-09: digest not stable under node reordering")
    if left.order != right.order:
        failures.append(f"A5-09: order unstable {left.order} != {right.order}")
    return left.digest, failures


def _measure_write_ahead_durability() -> list[str]:
    """Live probe: the intent is durable before the provider call happens."""
    _import_v2()
    from hermes_mcp_bridge.v2.dag_contract import (
        Budget,
        FailurePolicy,
        Idempotency,
        Node,
        NodeKind,
        NodeStatus,
        PlanDefinition,
    )
    from hermes_mcp_bridge.v2.dag_engine import DagEngine, NodeDecision
    from hermes_mcp_bridge.v2.dag_store import SqliteCheckpointStore
    from hermes_mcp_bridge.v2.dag_validation import (
        StaticToolCatalog,
        ToolContract,
        validate_plan,
    )

    contract = ToolContract(
        tool_id="github.create_branch",
        arg_types={"repository": "string", "branch": "string", "sha": "string"},
        result_types={"ref": "string", "effect_ref": "string"},
        mutating=True,
        credential_capability_id="github.write",
    )
    catalog = StaticToolCatalog(
        contracts={contract.tool_id: contract},
        projected=frozenset({contract.tool_id}),
        scope=frozenset({"owner/disposable"}),
    )
    plan = PlanDefinition(
        plan_id="gate-durability",
        nodes=(
            Node(
                id="branch",
                kind=NodeKind.TOOL,
                tool="github.create_branch",
                args={"repository": "owner/disposable", "branch": "b", "sha": "s"},
                idempotency=Idempotency(),
            ),
        ),
        budget=Budget(
            max_nodes=4,
            max_parallelism=1,
            max_external_calls=4,
            max_total_wall_ms=60_000,
            max_result_bytes=65_536,
            max_checkpoint_bytes=65_536,
        ),
        failure_policy=FailurePolicy.FAIL_FAST,
        deadline_ms=60_000,
    )

    class Allow:
        def decide(self, plan: Any, node: Any, resolved_args: Any) -> NodeDecision:
            return NodeDecision(allowed=True, policy_digest="gate")

        def record(self, plan: Any, node: Any, state: Any) -> None:
            return None

    with tempfile.TemporaryDirectory() as tmp:
        store = SqliteCheckpointStore(Path(tmp) / "gate.db")
        observed: list[str | None] = []

        async def executor(node: Any, args: Any) -> dict[str, Any]:
            observed.append(store.load("gate-1").node_states[node.id].idempotency_key)
            return {"ref": "b", "effect_ref": "ref-branch"}

        engine = DagEngine(executor, catalog=catalog, store=store, governance=Allow(), enabled=True)
        try:
            validated = validate_plan(plan, catalog)
            checkpoint = engine.admit(
                validated,
                execution_id="gate-1",
                principal_ref="gate",
                projection_digest="pj",
                policy_digest="pd",
            )
            report = asyncio.run(engine.run(validated, checkpoint))
        except Exception as exc:  # fail closed
            return [f"A5-10: durability probe raised {exc.__class__.__name__}: {exc}"]

        failures: list[str] = []
        if not observed or not observed[0]:
            failures.append("A5-10: no write-ahead record before the mutation")
        reloaded = store.load("gate-1")
        if reloaded.node_states["branch"].status is not NodeStatus.SUCCESS:
            failures.append("A5-10: terminal state not durable after reload")
        if report.status.value != "COMPLETED":
            failures.append(f"A5-10: probe status={report.status.value}")
        store.close()
        return failures


def _check_batch_accepted() -> list[str]:
    path = REPO_ROOT / "docs" / "v2" / "evidence" / "phase4-batch-acceptance.json"
    if not path.is_file():
        return ["A5-11: Phase 4 acceptance evidence missing"]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ["A5-11: Phase 4 acceptance evidence unreadable"]
    if payload.get("gate") != "BATCH_ACCEPTED" or payload.get("failures"):
        return ["A5-11: BATCH_ACCEPTED not proven before Phase 5"]
    return []


def validate_gate() -> dict[str, Any]:
    failures: list[str] = []
    failures += _check_v1_contract()
    failures += _check_limits_and_flag()
    failures += _check_transform_closed_set()
    failures += _check_design_lane_present()
    failures += _check_criteria_present()
    failures += _check_acceptance_suite()
    failures += _check_no_generic_surface()
    probe_digest, determinism_failures = _measure_determinism()
    failures += determinism_failures
    failures += _measure_write_ahead_durability()
    failures += _check_batch_accepted()

    return {
        "gate": ACCEPTED_GATE if not failures else BLOCKED_GATE,
        "failures": list(dict.fromkeys(failures)),
        "determinism_probe_digest": probe_digest,
        "limits": EXPECTED_LIMITS,
        "transform_ops": list(EXPECTED_TRANSFORM_OPS),
        "module_binding_sha256": {rel: _module_digest(rel) for rel in PHASE5_MODULES},
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
        if key not in ("module_binding_sha256", "limits", "transform_ops")
    }
    print(json.dumps(summary, sort_keys=True))
    return 0 if not result["failures"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
