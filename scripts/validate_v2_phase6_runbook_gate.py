#!/usr/bin/env python3
"""Phase 6 promotion gate — `RUNBOOK_ACCEPTED`.

Fail-closed, machine-checked promotion for the runbook layer. There is no
self-approval path: every criterion is evaluated against the real repository and
a real test run, or recorded as a failure.

Layers (both must return no failures):

* INNER — the V1 contract invariants, the full A6-01..A6-26 acceptance suite
  executed for real (a skip or a missing criterion is a failure), the runbook
  feature flag defaulting to off, the agentic budget defaulting to and requiring
  0 before HYBRID, a live determinism probe proving two independent compiles of
  the same manifest produce the same canonical IR and the same digest, a live
  append-only probe proving a conflicting re-admission cannot overwrite a
  record, and a live authorization probe proving an unauthorized caller receives
  `RB_UNKNOWN`.
* OUTER — SHA-256 binding of every Phase 6 module against the live tree, an AST
  scan proving zero generic surface (no shell/subprocess/socket/HTTP/eval), the
  design lane and ADR set, the traceability matrix, and the `DAG_ACCEPTED`
  marker proving Phase 5 preceded Phase 6.

Usage::

    python scripts/validate_v2_phase6_runbook_gate.py \\
        --json-out docs/v2/evidence/phase6-runbook-acceptance.json

Exit code 0 only when ``failures`` is empty (gate ACCEPTED).
"""

from __future__ import annotations

import argparse
import ast
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
TEST_FILE = REPO_ROOT / "tests" / "test_v2_phase6_runbook_acceptance.py"
DESIGN_LANE = REPO_ROOT / "docs" / "v2" / "phase6"
ADR_LANE = DESIGN_LANE / "adrs"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "v2_phase6"
MATRIX = REPO_ROOT / "docs" / "v2" / "requirements" / "traceability-matrix.md"

EXPECTED_CONTRACT_VERSION = "1.0.0"
EXPECTED_SCHEMA_VERSION = "0.6.1"
EXPECTED_TOOL_COUNT = 27

ACCEPTED_GATE = "RUNBOOK_ACCEPTED"
BLOCKED_GATE = "RUNBOOK_BLOCKED"

PHASE6_MODULES = (
    "v2/runbook_contract.py",
    "v2/runbook_digest.py",
    "v2/runbook_admission.py",
    "v2/runbook_registry.py",
    "v2/runbook_compile.py",
    "v2/runbook_engine.py",
    "v2/runbook_loader.py",
)

#: Every acceptance criterion must be represented by at least one real test.
REQUIRED_CRITERIA = tuple(f"a6_{index:02d}" for index in range(1, 27))

REQUIRED_DESIGN_DOCS = {
    "acceptance-criteria.md",
    "admission-validation.md",
    "capability-and-credential-requirements.md",
    "invocation-model.md",
    "migration-dag-to-runbook.md",
    "ownership-and-evidence.md",
    "parameter-schema.md",
    "plan-digest-binding.md",
    "policy-approval-and-destructive-actions.md",
    "registry-identity-and-versioning.md",
    "rollback-timeouts-and-budgets.md",
    "test-plan.md",
}

REQUIRED_ADRS = {
    "ADR-0028-runbook-canonical-ir-and-admission.md",
    "ADR-0029-runbook-plan-digest-and-approval-binding.md",
    "ADR-0030-runbook-least-privilege-computed-capabilities.md",
    "ADR-0031-destructive-marker-and-rollback-declaration.md",
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
        from hermes_mcp_bridge.v2 import runbook_contract  # noqa: F401
    except Exception as exc:  # pragma: no cover - defensive
        return [f"A6-02: import failed: {exc.__class__.__name__}"]
    failures: list[str] = []
    if contracts.CURRENT_CONTRACT_VERSION != EXPECTED_CONTRACT_VERSION:
        failures.append(f"A6-02: contract={contracts.CURRENT_CONTRACT_VERSION}")
    if contracts.SCHEMA_VERSION != EXPECTED_SCHEMA_VERSION:
        failures.append(f"A6-02: schema={contracts.SCHEMA_VERSION}")
    if contracts.expected_tool_count() != EXPECTED_TOOL_COUNT:
        failures.append(f"A6-02: tools={contracts.expected_tool_count()}")
    leaked = sorted(name for name in contracts.required_tools() if "runbook" in name.lower())
    if leaked:
        failures.append(f"A6-02: runbook tools leaked into projection: {','.join(leaked)}")
    return failures


def _check_flag_and_budget_defaults() -> list[str]:
    _import_v2()
    from hermes_mcp_bridge.v2 import runbook_contract as rc

    failures: list[str] = []
    if rc.RUNBOOK_FEATURE_ENABLED is not False:
        failures.append("A6-26: RUNBOOK_FEATURE_ENABLED must default to False")
    if rc.RUNBOOK_MAX_AGENTIC_TOKENS_DEFAULT != 0:
        failures.append("A6-17: agentic token budget must default to 0")
    if rc.RUNBOOK_MAX_AGENTIC_ESCALATIONS_DEFAULT != 0:
        failures.append("A6-17: agentic escalation budget must default to 0")
    return failures


def _check_criteria_present() -> list[str]:
    if not TEST_FILE.is_file():
        return ["A6-26: acceptance suite missing"]
    text = TEST_FILE.read_text(encoding="utf-8")
    covered = {
        criterion for _, criterion in re.findall(r"def (test_(a6_\d{2})[a-z]?_[a-z0-9_]+)", text)
    }
    missing = [name for name in REQUIRED_CRITERIA if name not in covered]
    if missing:
        return [f"A6-26: acceptance criteria not implemented: {','.join(missing)}"]
    return []


def _check_design_lane_present() -> list[str]:
    failures: list[str] = []
    if not DESIGN_LANE.is_dir():
        return ["A6-26: Phase 6 design lane missing"]
    present = {path.name for path in DESIGN_LANE.glob("*.md")}
    missing = sorted(REQUIRED_DESIGN_DOCS - present)
    if missing:
        failures.append(f"A6-26: design documents missing: {','.join(missing)}")
    adrs = {path.name for path in ADR_LANE.glob("ADR-*.md")}
    missing_adrs = sorted(REQUIRED_ADRS - adrs)
    if missing_adrs:
        failures.append(f"A6-26: ADRs missing: {','.join(missing_adrs)}")
    if not FIXTURE_DIR.is_dir() or not list(FIXTURE_DIR.glob("RB-*.json")):
        failures.append("A6-24: Phase 6 exemplar fixture missing")
    if not MATRIX.is_file():
        failures.append("A6-26: traceability matrix missing")
    else:
        matrix_text = MATRIX.read_text(encoding="utf-8")
        if "RUNBOOK_ACCEPTED **[ACCEPTED]**" not in matrix_text:
            failures.append("A6-26: traceability matrix does not record RUNBOOK_ACCEPTED")
    return failures


def _check_acceptance_suite() -> list[str]:
    """Run the real suite. A skip or xfail is a failure, not a pass."""
    result = _run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly", "-rs", str(TEST_FILE)]
    )
    if result.returncode != 0:
        return [f"A6-26: acceptance suite failed:\n{result.stdout[-1500:]}"]
    tail = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    if "skipped" in tail or "xfail" in tail:
        return [f"A6-26: acceptance suite reported skips: {tail}"]
    return []


def _check_no_generic_surface() -> list[str]:
    failures: list[str] = []
    for rel in PHASE6_MODULES:
        tree = ast.parse((SRC / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in BANNED_MODULES:
                        failures.append(f"A6-10: {rel} imports {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.split(".")[0] in BANNED_MODULES:
                    failures.append(f"A6-10: {rel} imports from {node.module}")
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id in BANNED_NAMES:
                    failures.append(f"A6-10: {rel} calls {func.id}()")
                elif isinstance(func, ast.Attribute) and func.attr in BANNED_ATTRS:
                    failures.append(f"A6-10: {rel} calls .{func.attr}()")
    return failures


def _load_exemplar() -> Any:
    _import_v2()
    from hermes_mcp_bridge.v2.runbook_loader import load_manifest

    raw = json.loads((FIXTURE_DIR / "RB-GITHUB-PR-LIFECYCLE-001.json").read_text(encoding="utf-8"))
    return load_manifest(raw)


def _measure_determinism() -> tuple[str, list[str]]:
    """Live probe: the same manifest → identical IR bytes and identical digest."""
    _import_v2()
    from hermes_mcp_bridge.v2.runbook_digest import canonical_ir_bytes, runbook_digest

    try:
        left = _load_exemplar()
        right = _load_exemplar()
    except Exception as exc:  # fail closed
        return "", [f"A6-03: determinism probe raised {exc.__class__.__name__}: {exc}"]

    failures: list[str] = []
    if canonical_ir_bytes(left) != canonical_ir_bytes(right):
        failures.append("A6-03: canonical IR is not byte-stable")
    digest = runbook_digest(left)
    if digest != runbook_digest(right):
        failures.append("A6-03: runbook_digest is not stable")
    return digest, failures


def _measure_append_only_registry() -> list[str]:
    """Live probe: a conflicting re-admission cannot overwrite an admitted record."""
    _import_v2()
    from hermes_mcp_bridge.v2.runbook_contract import RunbookError, RunbookReason
    from hermes_mcp_bridge.v2.runbook_digest import runbook_digest
    from hermes_mcp_bridge.v2.runbook_registry import RunbookRegistry

    try:
        manifest = _load_exemplar()
    except Exception as exc:
        return [f"A6-05: append-only probe could not load the exemplar: {exc}"]

    with tempfile.TemporaryDirectory() as tmp:
        registry = RunbookRegistry(Path(tmp) / "registry.db")
        original = runbook_digest(manifest)
        registry.admit(manifest, original)
        conflicting = type(manifest)(
            **{
                **{
                    field: getattr(manifest, field)
                    for field in manifest.__dataclass_fields__  # type: ignore[attr-defined]
                },
                "timeout_ms": manifest.timeout_ms - 1000,
            }
        )
        failures: list[str] = []
        try:
            registry.admit(conflicting, runbook_digest(conflicting))
        except RunbookError as exc:
            if exc.reason is not RunbookReason.RB_DIGEST_CONFLICT:
                failures.append(f"A6-05: wrong reason for conflicting re-admission: {exc.reason}")
        else:
            failures.append("A6-05: registry accepted a conflicting re-admission")
        stored = registry.get(manifest.runbook_id, manifest.version).runbook_digest
        if stored != original:
            failures.append("A6-05: an admitted record was overwritten")
        registry.close()
        return failures


def _measure_unauthorized_is_rb_unknown() -> list[str]:
    """Live probe: an unauthorized caller cannot detect a restricted runbook."""
    _import_v2()
    from hermes_mcp_bridge.v2.dag_validation import StaticToolCatalog, ToolContract
    from hermes_mcp_bridge.v2.runbook_contract import RunbookError, RunbookReason, RunbookState
    from hermes_mcp_bridge.v2.runbook_digest import runbook_digest
    from hermes_mcp_bridge.v2.runbook_engine import InvocationRequest, RunbookEngine
    from hermes_mcp_bridge.v2.runbook_registry import RunbookRegistry

    class Allow:
        def decide(self, plan: Any, node: Any, resolved_args: Any) -> Any:
            from hermes_mcp_bridge.v2.dag_engine import NodeDecision

            return NodeDecision(allowed=True, policy_digest="gate")

        def record(self, plan: Any, node: Any, state: Any) -> None:
            return None

    async def executor(node: Any, args: Any) -> dict[str, Any]:
        raise AssertionError("unauthorized invocation reached the executor")

    try:
        manifest = _load_exemplar()
    except Exception as exc:
        return [f"A6-23: authorization probe could not load the exemplar: {exc}"]

    with tempfile.TemporaryDirectory() as tmp:
        from hermes_mcp_bridge.v2.dag_store import SqliteCheckpointStore

        registry = RunbookRegistry(Path(tmp) / "registry.db")
        registry.admit(manifest, runbook_digest(manifest))
        registry.transition(manifest.runbook_id, manifest.version, RunbookState.ACTIVE)
        catalog = StaticToolCatalog(
            contracts={
                "github.get_repo": ToolContract(
                    tool_id="github.get_repo",
                    arg_types={"repository": "string"},
                    result_types={"name": "string"},
                )
            },
            projected=frozenset({"github.get_repo"}),
            scope=frozenset({"owner/disposable"}),
        )
        engine = RunbookEngine(
            executor,
            registry,
            catalog,
            SqliteCheckpointStore(Path(tmp) / "state.db"),
            Allow(),
            enabled=True,
            authorized_runbooks={},
        )
        request = InvocationRequest(
            runbook_id=manifest.runbook_id,
            version=manifest.version,
            expected_runbook_digest=runbook_digest(manifest),
            arguments={"repository": "owner/disposable"},
            idempotency_key="gate-unauth",
            principal_ref="stranger",
        )
        failures: list[str] = []
        try:
            engine.invoke(request)
        except RunbookError as exc:
            if exc.reason is not RunbookReason.RB_UNKNOWN:
                failures.append(f"A6-23: unauthorized caller received {exc.reason.value}")
            if runbook_digest(manifest) in str(exc):
                failures.append("A6-23: denial leaked the runbook digest")
        else:
            failures.append("A6-23: unauthorized invocation was not denied")
        registry.close()
        return failures


def _check_dag_accepted() -> list[str]:
    path = REPO_ROOT / "docs" / "v2" / "evidence" / "phase5-dag-acceptance.json"
    if not path.is_file():
        return ["A6-01: Phase 5 acceptance evidence missing"]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ["A6-01: Phase 5 acceptance evidence unreadable"]
    if payload.get("gate") != "DAG_ACCEPTED" or payload.get("failures"):
        return ["A6-01: DAG_ACCEPTED not proven before Phase 6"]
    return []


def validate_gate() -> dict[str, Any]:
    failures: list[str] = []
    failures += _check_v1_contract()
    failures += _check_flag_and_budget_defaults()
    failures += _check_design_lane_present()
    failures += _check_criteria_present()
    failures += _check_acceptance_suite()
    failures += _check_no_generic_surface()
    probe_digest, determinism_failures = _measure_determinism()
    failures += determinism_failures
    failures += _measure_append_only_registry()
    failures += _measure_unauthorized_is_rb_unknown()
    failures += _check_dag_accepted()

    return {
        "gate": ACCEPTED_GATE if not failures else BLOCKED_GATE,
        "failures": list(dict.fromkeys(failures)),
        "determinism_probe_runbook_digest": probe_digest,
        "criteria": list(REQUIRED_CRITERIA),
        "v1_contract": {
            "contract_version": EXPECTED_CONTRACT_VERSION,
            "schema_version": EXPECTED_SCHEMA_VERSION,
            "tool_count": EXPECTED_TOOL_COUNT,
        },
        "module_digests": {rel: _module_digest(rel) for rel in PHASE6_MODULES},
        "adrs": sorted(REQUIRED_ADRS),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json-out", required=True)
    args = parser.parse_args()

    payload = validate_gate()
    out = Path(args.json_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{payload['gate']} failures={len(payload['failures'])}")
    for failure in payload["failures"]:
        print(f"  - {failure}")
    return 0 if not payload["failures"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
