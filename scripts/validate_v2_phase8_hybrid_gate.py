#!/usr/bin/env python3
"""Phase 8 promotion gate — `HYBRID_ACCEPTED`.

Fail-closed and machine-checked. Nothing here trusts a document: determinism is
proven by executing 100 real replays per scenario class inside the gate, the
decision-tree order is proven by walking the resolver with constructed intents,
and the zero-default-agentic property is proven by resolving with the default
budget and observing a refusal.

Layers:

* INNER — V1 contract, the full P8-01..P8-20 suite executed for real, feature
  flag default, live 100x replay determinism across six scenario classes, live
  preference-order walk (DIRECT > BATCH > DAG/RUNBOOK > AGENTIC), live
  zero-default-agentic probe, closed reason-code enumeration with zero unknown
  codes, and an economics record with `direct_tokens == 0`.
* OUTER — SHA-256 binding of the Phase 8 modules, an AST purity scan proving the
  resolver performs no I/O, no clock read and no randomness, the design lane and
  ADRs, and the `INTEGRATIONS_ACCEPTED` predecessor with its two accepted
  providers.

Exit code 0 only when ``failures`` is empty.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src" / "hermes_mcp_bridge"
TEST_FILE = REPO_ROOT / "tests" / "test_v2_phase8_hybrid_acceptance.py"
DESIGN_LANE = REPO_ROOT / "docs" / "v2" / "phase8"
ADR_LANE = DESIGN_LANE / "adrs"
EVIDENCE_DIR = REPO_ROOT / "docs" / "v2" / "evidence"

EXPECTED_CONTRACT_VERSION = "1.0.0"
EXPECTED_SCHEMA_VERSION = "0.6.1"
EXPECTED_TOOL_COUNT = 27

ACCEPTED_GATE = "HYBRID_ACCEPTED"
BLOCKED_GATE = "HYBRID_BLOCKED"

PHASE8_MODULES = ("v2/resolver_contract.py", "v2/resolver.py", "v2/hybrid_execution.py")

#: The resolver must be pure: these names may not appear anywhere in it.
PURITY_BANNED_MODULES = {
    "random",
    "secrets",
    "time",
    "datetime",
    "os",
    "socket",
    "subprocess",
    "urllib",
    "http",
    "sqlite3",
    "pathlib",
}
PURITY_BANNED_NAMES = {"eval", "exec", "compile", "__import__", "open", "input"}

REQUIRED_CRITERIA = tuple(f"p8_{index:02d}" for index in range(1, 21))

REQUIRED_DESIGN_DOCS = {
    "README.md",
    "acceptance-criteria.md",
    "evidence-and-economics.md",
    "reason-codes.md",
    "resolver-decision-tree.md",
    "safety-invariants.md",
    "test-matrix.md",
    "thresholds.md",
}

REQUIRED_ADRS = {
    "ADR-0036-deterministic-mode-resolver.md",
    "ADR-0037-no-silent-safety-downgrade.md",
    "ADR-0038-agentic-proposal-not-execution.md",
}

REPLAY_REPETITIONS = 100


def _module_digest(rel: str) -> str:
    return hashlib.sha256((SRC / rel).read_bytes()).hexdigest()


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, check=False)


def _import() -> None:
    path = str(REPO_ROOT / "src")
    if path not in sys.path:
        sys.path.insert(0, path)


def _scenarios():
    """The six scenario classes, built from the real contract types."""
    _import()
    from hermes_mcp_bridge.v2.enums import CapabilityState
    from hermes_mcp_bridge.v2.resolver import ModeResolver
    from hermes_mcp_bridge.v2.resolver_contract import (
        IntentOperation,
        ResolverBudget,
        ResolverIntent,
    )

    read, read2, write = "github.repo_read", "github.pr_read", "github.pr_create"
    snapshot = {
        read: CapabilityState.READY,
        read2: CapabilityState.READY,
        write: CapabilityState.READY,
    }

    def operation(capability_id=read, ref="", **kwargs):
        return IntentOperation(
            capability_id=capability_id,
            target_scope_ref="pestoura/hermes-mcp-bridge",
            operation_ref=ref or capability_id,
            **kwargs,
        )

    def intent(operations=(), **kwargs):
        return ResolverIntent(
            request_id=kwargs.pop("request_id", "gate"),
            principal_ref="gate",
            operations=tuple(operations),
            **kwargs,
        )

    resolver = ModeResolver(
        snapshot=snapshot,
        snapshot_digest="g" * 64,
        budget=ResolverBudget(agentic_token_budget=1_000),
        runbooks={"rb.gate": True},
        write_capabilities=frozenset({write}),
    )
    scenarios = {
        "direct": intent((operation(),)),
        "batch": intent([operation(read, ref=f"op-{index}") for index in range(4)]),
        "dag": intent((operation(read, ref="a"), operation(read2, ref="b", depends_on=("a",)))),
        "runbook": intent(
            (operation(read, ref="a"), operation(read2, ref="b", depends_on=("a",))),
            runbook_ref="rb.gate",
        ),
        "agentic": intent((), no_contract_coverage=True, agentic_allowance=True),
        "refusal": intent((), no_contract_coverage=True),
    }
    return resolver, scenarios


# --------------------------------------------------------------------------
# INNER
# --------------------------------------------------------------------------
def _check_v1_contract() -> list[str]:
    _import()
    from hermes_mcp_bridge import contracts

    failures: list[str] = []
    if contracts.CURRENT_CONTRACT_VERSION != EXPECTED_CONTRACT_VERSION:
        failures.append(f"P8-20: contract={contracts.CURRENT_CONTRACT_VERSION}")
    if contracts.SCHEMA_VERSION != EXPECTED_SCHEMA_VERSION:
        failures.append(f"P8-20: schema={contracts.SCHEMA_VERSION}")
    if contracts.expected_tool_count() != EXPECTED_TOOL_COUNT:
        failures.append(f"P8-20: tools={contracts.expected_tool_count()}")
    return failures


def _check_flag_default() -> list[str]:
    _import()
    from hermes_mcp_bridge.v2 import resolver_contract

    if resolver_contract.HYBRID_FEATURE_ENABLED is not False:
        return ["P8-00: HYBRID_FEATURE_ENABLED must default to False"]
    if resolver_contract.ResolverBudget().agentic_token_budget != 0:
        return ["P8-00: default agentic token budget must be 0"]
    if resolver_contract.ResolverBudget().allows_agentic:
        return ["P8-00: default budget must not allow agentic"]
    return []


def _check_criteria_present() -> list[str]:
    if not TEST_FILE.is_file():
        return ["P8-00: acceptance suite missing"]
    text = TEST_FILE.read_text(encoding="utf-8")
    missing = [name for name in REQUIRED_CRITERIA if f"def test_{name}" not in text]
    if missing:
        return [f"P8-00: criteria without a test: {','.join(missing)}"]
    return []


def _run_acceptance_suite() -> list[str]:
    result = _run([sys.executable, "-m", "pytest", "-q", "-p", "no:randomly", str(TEST_FILE)])
    if result.returncode != 0:
        tail = result.stdout.strip().splitlines()[-1:] or ["no output"]
        return [f"P8-00: acceptance suite failed ({tail[0]})"]
    if "skipped" in result.stdout:
        return ["P8-00: acceptance suite contains skips"]
    return []


def _probe_replay_determinism() -> tuple[list[str], dict[str, Any]]:
    """100 real replays per scenario class, inside the gate."""
    _import()
    from hermes_mcp_bridge.v2.enums import PolicyDecision
    from hermes_mcp_bridge.v2.resolver import replay_decision

    resolver, scenarios = _scenarios()
    failures: list[str] = []
    record: dict[str, Any] = {}
    for name, intent in scenarios.items():
        decision, mismatches = replay_decision(
            resolver, intent, policy=PolicyDecision.ALLOW, repetitions=REPLAY_REPETITIONS
        )
        record[name] = {
            "mode": decision.mode.value if decision.mode else "REFUSED",
            "primary_reason_code": decision.primary_reason_code.value,
            "replays": REPLAY_REPETITIONS,
            "mismatches": mismatches,
            "decision_digest": decision.digest(),
        }
        if mismatches:
            failures.append(f"P8-16: {name} produced {mismatches} replay mismatches")
    return failures, record


def _probe_preference_order() -> list[str]:
    """Live walk: the resolver must honour DIRECT > BATCH > DAG/RUNBOOK > AGENTIC."""
    _import()
    from hermes_mcp_bridge.v2.enums import ExecutionMode, PolicyDecision

    resolver, scenarios = _scenarios()
    expected = {
        "direct": ExecutionMode.DIRECT,
        "batch": ExecutionMode.BATCH,
        "dag": ExecutionMode.DAG,
        "runbook": ExecutionMode.RUNBOOK,
        "agentic": ExecutionMode.AGENTIC,
    }
    failures: list[str] = []
    for name, mode in expected.items():
        decision = resolver.resolve(scenarios[name], policy=PolicyDecision.ALLOW)
        if decision.mode is not mode:
            actual = decision.mode.value if decision.mode else "REFUSED"
            failures.append(f"P8-00: scenario {name} resolved {actual}, expected {mode.value}")
    return failures


def _probe_zero_default_agentic() -> list[str]:
    """With the default budget, an ambiguous intent must refuse, never escalate."""
    _import()
    from hermes_mcp_bridge.v2.enums import PolicyDecision
    from hermes_mcp_bridge.v2.resolver import ModeResolver
    from hermes_mcp_bridge.v2.resolver_contract import (
        ResolverBudget,
        ResolverIntent,
        ResolverReason,
    )

    resolver = ModeResolver(
        snapshot={}, snapshot_digest="z" * 64, budget=ResolverBudget()
    )
    decision = resolver.resolve(
        ResolverIntent(
            request_id="gate-zero-default",
            principal_ref="gate",
            no_contract_coverage=True,
            agentic_allowance=True,
        ),
        policy=PolicyDecision.ALLOW,
    )
    if decision.primary_reason_code is not ResolverReason.E_AGENTIC_NOT_ALLOWED:
        return [f"P8-06: default budget escalated ({decision.primary_reason_code.value})"]
    if decision.agentic_tokens_authorized != 0:
        return ["P8-06: default budget authorized tokens"]
    return []


def _probe_reason_codes() -> tuple[list[str], dict[str, Any]]:
    """Every reachable outcome must carry a code from the closed enumeration."""
    _import()
    from hermes_mcp_bridge.v2.enums import PolicyDecision
    from hermes_mcp_bridge.v2.resolver_contract import REASON_LABEL_SET, ResolverReason

    resolver, scenarios = _scenarios()
    unknown = 0
    observed: set[str] = set()
    for intent in scenarios.values():
        decision = resolver.resolve(intent, policy=PolicyDecision.ALLOW)
        codes = [decision.primary_reason_code, *decision.rejected_branches]
        for code in codes:
            observed.add(code.value)
            if code.value not in REASON_LABEL_SET:
                unknown += 1
    failures = [] if unknown == 0 else [f"P8-19: {unknown} unknown reason codes"]
    # A code outside the enumeration cannot be constructed at all.
    try:
        ResolverReason("R-NOT-A-REAL-CODE")
    except ValueError:
        pass
    else:
        failures.append("P8-19: reason enumeration is not closed")
    return failures, {
        "enumeration_size": len(REASON_LABEL_SET),
        "observed_codes": sorted(observed),
        "unknown_codes": unknown,
    }


def _probe_economics() -> tuple[list[str], dict[str, Any]]:
    """Deterministic paths must authorize zero agentic tokens; coverage recorded."""
    _import()
    from hermes_mcp_bridge.v2.enums import ExecutionMode, PolicyDecision

    resolver, scenarios = _scenarios()
    failures: list[str] = []
    deterministic_nodes = 0
    total_nodes = 0
    direct_tokens = 0
    per_scenario: dict[str, Any] = {}
    for name, intent in scenarios.items():
        decision = resolver.resolve(intent, policy=PolicyDecision.ALLOW)
        per_scenario[name] = {
            "mode": decision.mode.value if decision.mode else "REFUSED",
            "deterministic_nodes": decision.deterministic_nodes,
            "total_nodes": decision.total_nodes,
            "deterministic_coverage_permille": decision.deterministic_coverage_permille,
            "agentic_tokens_authorized": decision.agentic_tokens_authorized,
            "escalation_count": decision.escalation_count,
        }
        if decision.mode is not None and decision.mode is not ExecutionMode.AGENTIC:
            deterministic_nodes += decision.deterministic_nodes
            total_nodes += decision.total_nodes
            direct_tokens += decision.agentic_tokens_authorized
    if direct_tokens != 0:
        failures.append("P8-18: a deterministic path authorized agentic tokens")
    if total_nodes and deterministic_nodes != total_nodes:
        failures.append("P8-18: deterministic paths did not cover all their nodes")
    return failures, {
        "deterministic_nodes": deterministic_nodes,
        "direct_tokens": direct_tokens,
        "per_scenario": per_scenario,
        "total_nodes": total_nodes,
    }


# --------------------------------------------------------------------------
# OUTER
# --------------------------------------------------------------------------
def _purity_scan() -> list[str]:
    failures: list[str] = []
    tree = ast.parse((SRC / "v2/resolver.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in PURITY_BANNED_MODULES:
                    failures.append(f"P8-16: resolver imports {root}")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in PURITY_BANNED_MODULES:
                failures.append(f"P8-16: resolver imports from {root}")
        elif isinstance(node, ast.Name) and node.id in PURITY_BANNED_NAMES:
            failures.append(f"P8-16: resolver references {node.id}")
    return sorted(set(failures))


def _isolation_scan() -> list[str]:
    """The agentic layer must not be able to reach a provider."""
    _import()
    from hermes_mcp_bridge.v2.hybrid_execution import AgenticContext, AgenticProposal

    failures: list[str] = []
    context_fields = set(AgenticContext.__dataclass_fields__)
    forbidden = {"gateway", "broker", "registry", "adapter", "credential", "headers"}
    leaked = context_fields & forbidden
    if leaked:
        failures.append(f"P8-15: agentic context exposes {','.join(sorted(leaked))}")
    proposal_fields = set(AgenticProposal.__dataclass_fields__)
    if proposal_fields != {"operations", "tokens_used", "abandoned"}:
        failures.append("P8-15: agentic proposal shape widened")
    tree = ast.parse((SRC / "v2/hybrid_execution.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith(
            ("provider_gateway", "provider_credentials", "provider_registry")
        ):
            failures.append(f"P8-15: hybrid layer imports {node.module}")
    return sorted(set(failures))


def _check_design_lane() -> list[str]:
    failures: list[str] = []
    if not DESIGN_LANE.is_dir():
        return ["P8-00: design lane missing"]
    present = {path.name for path in DESIGN_LANE.glob("*.md")}
    missing = sorted(REQUIRED_DESIGN_DOCS - present)
    if missing:
        failures.append(f"P8-00: design docs missing: {','.join(missing)}")
    adrs = {path.name for path in ADR_LANE.glob("*.md")} if ADR_LANE.is_dir() else set()
    missing_adrs = sorted(REQUIRED_ADRS - adrs)
    if missing_adrs:
        failures.append(f"P8-00: ADRs missing: {','.join(missing_adrs)}")
    return failures


def _check_predecessor() -> list[str]:
    """Phase 7 accepted with at least two accepted integrations."""
    for path in sorted(EVIDENCE_DIR.glob("phase7*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if payload.get("gate") == "INTEGRATIONS_ACCEPTED" and payload.get("failures") == []:
            accepted = payload.get("accepted_providers") or []
            if len(accepted) < 2:
                return ["P8-01: fewer than two accepted Phase 7 integrations"]
            return []
    return ["P8-01: INTEGRATIONS_ACCEPTED marker not found"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args()

    failures: list[str] = []
    failures += _check_v1_contract()
    failures += _check_flag_default()
    failures += _check_criteria_present()
    failures += _run_acceptance_suite()
    replay_failures, replay_record = _probe_replay_determinism()
    failures += replay_failures
    failures += _probe_preference_order()
    failures += _probe_zero_default_agentic()
    reason_failures, reason_record = _probe_reason_codes()
    failures += reason_failures
    economics_failures, economics_record = _probe_economics()
    failures += economics_failures
    failures += _purity_scan()
    failures += _isolation_scan()
    failures += _check_design_lane()
    failures += _check_predecessor()

    evidence: dict[str, Any] = {
        "criteria": list(REQUIRED_CRITERIA),
        "determinism": replay_record,
        "economics": economics_record,
        "failures": sorted(failures),
        "gate": ACCEPTED_GATE if not failures else BLOCKED_GATE,
        "mode_preference": ["DIRECT", "BATCH", "DAG", "RUNBOOK", "AGENTIC"],
        "module_binding_sha256": {rel: _module_digest(rel) for rel in PHASE8_MODULES},
        "reason_codes": reason_record,
        "source_commit": _run(["git", "rev-parse", "HEAD"]).stdout.strip(),
    }
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    print(
        json.dumps(
            {
                "gate": evidence["gate"],
                "failures": evidence["failures"],
                "determinism": {
                    name: record["mismatches"] for name, record in replay_record.items()
                },
                "direct_tokens": economics_record["direct_tokens"],
                "unknown_reason_codes": reason_record["unknown_codes"],
                "source_commit": evidence["source_commit"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
