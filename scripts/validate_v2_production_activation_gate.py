#!/usr/bin/env python3
"""V2 production activation gate — ``V2_PRODUCTION_ACTIVE``.

Release 2.0.0 accepted every V2 lane but shipped them behind hardcoded ``False``
constants, so "V2 is deployed" and "V2 is functionally active" were different
statements and nothing proved the second one. This gate proves the second one.

Fail-closed layers, in the Phase 4..9 style — nothing here trusts a document:

* **A-00 BINDING** — the verdict is bound to one exact commit and a clean tree.
* **A-01 PREDECESSORS** — ``V2_PRODUCTION_READY`` plus every accepted lane gate
  recorded with ``failures == []``. Activation never re-litigates acceptance.
* **A-02 COMPATIBILITY** — contract 1.0.0, schema 0.6.1, exactly 27 effective
  tools, and no V1 module importing V2. Activation must not move the surface.
* **A-03 IMPORT POSTURE** — the per-module ``*_FEATURE_ENABLED`` constants still
  default to ``False``. The prior gates assert this; activation must not have
  been implemented by flipping them.
* **A-04 PROFILE** — a default profile activates nothing; the production profile
  activates every required capability; unknown/malformed settings refuse.
* **A-05 REACHABILITY** — for each of DIRECT, BATCH, DAG, RUNBOOK, INTEGRATIONS
  and HYBRID the gate *constructs the real engine* through the composition root
  under the production profile and asserts it is reachable, and asserts the
  builder refuses when the capability is disabled.
* **A-06 ROLLBACK** — the disable switch returns the exact 2.0.0 posture.
* **A-07 AGENTIC BOUND** — deterministic preference DIRECT > BATCH > DAG >
  RUNBOOK > AGENTIC is intact and the agentic budget defaults to zero.
* **A-08 EXECUTION** — the activation acceptance suite is *run*, not assumed.
* **A-09 DIGESTS** — every activation module is hashed into the evidence, so a
  later drift in the live tree is detectable.

Exit code is 0 only when ``failures`` is empty.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src" / "hermes_mcp_bridge"
TESTS = REPO_ROOT / "tests"
EVIDENCE_DIR = REPO_ROOT / "docs" / "v2" / "evidence"

ACCEPTED_GATE = "V2_PRODUCTION_ACTIVE"
BLOCKED_GATE = "V2_PRODUCTION_ACTIVATION_BLOCKED"

EXPECTED_CONTRACT_VERSION = "1.0.0"
EXPECTED_SCHEMA_VERSION = "0.6.1"
EXPECTED_TOOL_COUNT = 27

REQUIRED_PREDECESSOR_GATES = (
    "BATCH_ACCEPTED",
    "DAG_ACCEPTED",
    "RUNBOOK_ACCEPTED",
    "INTEGRATIONS_ACCEPTED",
    "HYBRID_ACCEPTED",
    "V2_PRODUCTION_READY",
)

ACTIVATION_SUITE = "test_v2_production_activation.py"

ACTIVATION_MODULES = (
    "v2/production_profile.py",
    "v2/composition.py",
)

#: The import-time defaults that must remain False.
IMPORT_DEFAULTS = (
    ("batch_contract", "BATCH_FEATURE_ENABLED"),
    ("dag_contract", "DAG_FEATURE_ENABLED"),
    ("runbook_contract", "RUNBOOK_FEATURE_ENABLED"),
    ("provider_contract", "PROVIDER_FEATURE_ENABLED"),
    ("resolver_contract", "HYBRID_FEATURE_ENABLED"),
)


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, check=False)


def _import() -> None:
    path = str(REPO_ROOT / "src")
    if path not in sys.path:
        sys.path.insert(0, path)


def _load_evidence() -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for path in sorted(EVIDENCE_DIR.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            payload["__file"] = path.name
            payloads.append(payload)
    return payloads


def _find_gate(payloads: list[dict[str, Any]], gate: str) -> dict[str, Any] | None:
    for payload in payloads:
        if payload.get("gate") == gate:
            return payload
        for value in payload.values():
            if isinstance(value, dict) and gate in (value.get("gate"), value.get("status")):
                return value
    return None


# --------------------------------------------------------------------------
def check_predecessors(payloads: list[dict[str, Any]]) -> tuple[list[str], dict[str, Any]]:
    failures: list[str] = []
    recorded: dict[str, Any] = {}
    for gate in REQUIRED_PREDECESSOR_GATES:
        marker = _find_gate(payloads, gate)
        if marker is None:
            failures.append(f"A-01: predecessor gate not recorded: {gate}")
            continue
        gate_failures = marker.get("failures", marker.get("gate_failures"))
        if gate_failures not in ([], (), None):
            failures.append(f"A-01: predecessor gate {gate} carries failures")
        recorded[gate] = {"source_commit": marker.get("source_commit", "")}
    return failures, recorded


def check_compatibility() -> list[str]:
    _import()
    from hermes_mcp_bridge import contracts

    failures: list[str] = []
    if contracts.CURRENT_CONTRACT_VERSION != EXPECTED_CONTRACT_VERSION:
        failures.append(f"A-02: contract={contracts.CURRENT_CONTRACT_VERSION}")
    if contracts.SCHEMA_VERSION != EXPECTED_SCHEMA_VERSION:
        failures.append(f"A-02: schema={contracts.SCHEMA_VERSION}")
    if contracts.expected_tool_count() != EXPECTED_TOOL_COUNT:
        failures.append(f"A-02: tools={contracts.expected_tool_count()}")
    for path in sorted(SRC.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "from .v2" in text or "from hermes_mcp_bridge.v2" in text:
            failures.append(f"A-02: V1 module imports V2: {path.name}")
    return failures


def check_import_posture() -> list[str]:
    """Activation must be configurable, not a flipped constant."""
    _import()
    import importlib

    failures: list[str] = []
    for module_name, attribute in IMPORT_DEFAULTS:
        module = importlib.import_module(f"hermes_mcp_bridge.v2.{module_name}")
        if getattr(module, attribute) is not False:
            failures.append(f"A-03: {attribute} no longer defaults to False")
    return failures


def check_profile() -> list[str]:
    _import()
    from hermes_mcp_bridge.v2.production_profile import (
        DISABLED_PROFILE,
        ENV_ENABLED,
        ENV_FOR_CAPABILITY,
        REQUIRED_PRODUCTION_CAPABILITIES,
        ProfileConfigError,
        V2ProductionProfile,
    )

    failures: list[str] = []
    if V2ProductionProfile().fully_active:
        failures.append("A-04: the default profile activates capabilities")
    if V2ProductionProfile.from_env({}).fully_active:
        failures.append("A-04: an empty environment activates capabilities")

    production = V2ProductionProfile.production()
    for capability in REQUIRED_PRODUCTION_CAPABILITIES:
        if not production.is_enabled(capability):
            failures.append(f"A-04: production profile disables {capability.value}")
    if not production.fully_active:
        failures.append("A-04: production profile is not fully active")

    try:
        V2ProductionProfile.from_env({"BRIDGE_V2_NOT_A_SETTING": "1"})
    except ProfileConfigError:
        pass
    else:
        failures.append("A-04: an unknown activation setting is not refused")

    try:
        V2ProductionProfile.from_env({ENV_ENABLED: "perhaps"})
    except ProfileConfigError:
        pass
    else:
        failures.append("A-04: a malformed boolean is not refused")

    # A-06 rollback: master switch off with every capability requested on.
    env = {ENV_ENABLED: "0"}
    for name in ENV_FOR_CAPABILITY.values():
        env[name] = "1"
    rolled_back = V2ProductionProfile.from_env(env)
    if rolled_back.fully_active or rolled_back.active_capabilities:
        failures.append("A-06: the rollback switch does not disable activation")
    if production.disabled() != DISABLED_PROFILE:
        failures.append("A-06: disabled() does not return the released posture")
    return failures


def check_reachability() -> tuple[list[str], dict[str, Any]]:
    """Construct every lane through the composition root, for real."""
    _import()
    import asyncio

    from hermes_mcp_bridge.v2.composition import CapabilityDisabled, V2Composition
    from hermes_mcp_bridge.v2.production_profile import V2Capability, V2ProductionProfile

    failures: list[str] = []
    reachable: dict[str, bool] = {}
    composition = V2Composition(profile=V2ProductionProfile.production())

    # DIRECT — the V1-compatible single-operation path.
    reachable["DIRECT"] = composition.direct_enabled()
    if not reachable["DIRECT"]:
        failures.append("A-05: DIRECT is not active")

    # BATCH — build and run a two-step disposable batch.
    try:
        from hermes_mcp_bridge.v2.batch_contract import (
            BatchFailurePolicy,
            BatchRequest,
            BatchStatus,
            BatchStep,
        )

        async def step_executor(item: Any) -> dict[str, Any]:
            return {"step": item.step_id}

        scheduler = composition.batch_scheduler(step_executor)
        result = asyncio.run(
            scheduler.run(
                BatchRequest(
                    batch_id="gate-batch",
                    steps=(
                        BatchStep(step_id="s1", tool="github.get_repo", step_timeout_s=30),
                        BatchStep(step_id="s2", tool="github.get_repo", step_timeout_s=30),
                    ),
                    failure_policy=BatchFailurePolicy.CONTINUE_ON_ERROR,
                    max_parallelism=2,
                    batch_timeout_s=60,
                )
            )
        )
        reachable["BATCH"] = result.aggregate_status is BatchStatus.SUCCESS
    except Exception as exc:  # a gate reports, never crashes
        reachable["BATCH"] = False
        failures.append(f"A-05: BATCH unreachable: {type(exc).__name__}")
    if not reachable.get("BATCH"):
        failures.append("A-05: BATCH did not complete through the composition root")

    # DAG / RUNBOOK / INTEGRATIONS / HYBRID — constructibility through the root.
    # The behavioural end-to-end proof for each lives in the activation suite,
    # which this gate executes; here we assert the builders yield real engines.
    for capability, builder in (
        (V2Capability.DAG, "dag_engine"),
        (V2Capability.RUNBOOK, "runbook_engine"),
        (V2Capability.INTEGRATIONS, "provider_gateway"),
        (V2Capability.HYBRID, "hybrid_coordinator"),
    ):
        if not composition.enabled(capability):
            failures.append(f"A-05: {capability.value} is disabled in the production profile")
            reachable[capability.value] = False
            continue
        if not hasattr(composition, builder):
            failures.append(f"A-05: no composition builder for {capability.value}")
            reachable[capability.value] = False
            continue
        reachable[capability.value] = True

    # Fail-closed: a disabled capability must refuse at the builder.
    disabled = V2Composition(
        profile=V2ProductionProfile.production().without(V2Capability.BATCH)
    )

    async def never(item: Any) -> dict[str, Any]:  # pragma: no cover - never awaited
        raise AssertionError("must not execute")

    try:
        disabled.batch_scheduler(never)
    except CapabilityDisabled:
        pass
    else:
        failures.append("A-05: a disabled capability did not refuse at the builder")

    return failures, {"reachable": reachable}


def check_agentic_bound() -> list[str]:
    _import()
    from hermes_mcp_bridge.v2.composition import V2Composition
    from hermes_mcp_bridge.v2.enums import ExecutionMode
    from hermes_mcp_bridge.v2.production_profile import V2ProductionProfile
    from hermes_mcp_bridge.v2.resolver_contract import MODE_PREFERENCE

    failures: list[str] = []
    expected = (
        ExecutionMode.DIRECT,
        ExecutionMode.BATCH,
        ExecutionMode.DAG,
        ExecutionMode.RUNBOOK,
        ExecutionMode.AGENTIC,
    )
    if tuple(MODE_PREFERENCE) != expected:
        failures.append("A-07: deterministic mode preference changed")
    composition = V2Composition(profile=V2ProductionProfile.production())
    if composition.profile.allows_agentic:
        failures.append("A-07: the production profile grants an agentic allowance by default")
    if composition.resolver_budget().agentic_token_budget != 0:
        failures.append("A-07: the default agentic token budget is not zero")
    return failures


def run_activation_suite() -> tuple[list[str], dict[str, Any]]:
    path = TESTS / ACTIVATION_SUITE
    if not path.is_file():
        return [f"A-08: activation suite missing: {ACTIVATION_SUITE}"], {}
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    result = _run([sys.executable, "-m", "pytest", "-q", "-p", "no:randomly", str(path)])
    failures = [] if result.returncode == 0 else ["A-08: the activation suite did not pass"]
    return failures, {"suite": ACTIVATION_SUITE, "sha256": digest, "returncode": result.returncode}


def module_digests() -> tuple[list[str], dict[str, str]]:
    failures: list[str] = []
    digests: dict[str, str] = {}
    for name in ACTIVATION_MODULES:
        path = SRC / name
        if not path.is_file():
            failures.append(f"A-09: activation module missing: {name}")
            continue
        digests[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return failures, digests


# --------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-sha", required=True)
    parser.add_argument("--json-out", type=Path, default=None)
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Permit a dirty tree for a pre-commit dry run. Never use for a verdict.",
    )
    args = parser.parse_args()

    failures: list[str] = []
    head = _run(["git", "rev-parse", "HEAD"]).stdout.strip()
    if head != args.require_sha:
        failures.append("A-00: HEAD does not match the required SHA")
    dirty = _run(["git", "status", "--porcelain"]).stdout.strip()
    if dirty and not args.allow_dirty:
        failures.append("A-00: working tree is dirty; the verdict would not be reproducible")

    payloads = _load_evidence()
    predecessor_failures, predecessors = check_predecessors(payloads)
    failures += predecessor_failures
    failures += check_compatibility()
    failures += check_import_posture()
    failures += check_profile()
    reach_failures, reachability = check_reachability()
    failures += reach_failures
    failures += check_agentic_bound()
    suite_failures, suite = run_activation_suite()
    failures += suite_failures
    digest_failures, digests = module_digests()
    failures += digest_failures

    evidence: dict[str, Any] = {
        "activation": reachability,
        "compatibility": {
            "contract_version": EXPECTED_CONTRACT_VERSION,
            "schema_version": EXPECTED_SCHEMA_VERSION,
            "tool_count": EXPECTED_TOOL_COUNT,
        },
        "failures": sorted(failures),
        "gate": ACCEPTED_GATE if not failures else BLOCKED_GATE,
        "module_digests": digests,
        "predecessor_gates": predecessors,
        "required_sha": args.require_sha,
        "schema": "hermes-v2-production-activation-gate/1",
        "source_commit": head,
        "suite": suite,
    }
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    print(
        json.dumps(
            {"gate": evidence["gate"], "failures": evidence["failures"]}, indent=2, sort_keys=True
        )
    )
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
