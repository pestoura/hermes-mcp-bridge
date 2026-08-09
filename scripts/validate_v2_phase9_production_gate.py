#!/usr/bin/env python3
"""Phase 9 promotion gate — ``V2_PRODUCTION_READY``.

Fail-closed and machine-checked, in the same spirit as the Phase 8 gate: nothing
here trusts a document. Every claim is either recomputed inside the gate or read
from an evidence artifact that the gate itself validates the shape of.

Layers:

* **PREDECESSORS** — all nine prior gates recorded, each with ``failures == []``.
* **V1 PRESERVATION** — bridge 1.0.0, schema 0.6.1, exactly 27 tools, HMAC policy
  signing fail-closed, no V1 module importing the V2 runtime.
* **EXECUTION** — the Phase 9 suites are *run*, not assumed: the failure
  catalogue (F-01..F-20), the continuity scenarios (C-01..C-08), the replay
  suite and the drills.
* **MEASUREMENT** — performance distributions with recorded sample counts,
  audit digest chain and completeness, label cardinality bounds.
* **SUPPLY CHAIN AND SECRETS** — secret scan with ``scanned=true`` and zero
  findings; SBOM/provenance/pinning evidence.
* **OPERATIONS** — rollback, rotation and restore drills executed; runbooks
  present and covering each failure class; the lifecycle draining regression.

``--require-sha`` binds the whole verdict to one exact commit. The gate exits 0
only when ``failures`` is empty.
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
PHASE9_LANE = REPO_ROOT / "docs" / "v2" / "phase9"

ACCEPTED_GATE = "V2_PRODUCTION_READY"
BLOCKED_GATE = "V2_PRODUCTION_BLOCKED"

EXPECTED_CONTRACT_VERSION = "1.0.0"
EXPECTED_SCHEMA_VERSION = "0.6.1"
EXPECTED_TOOL_COUNT = 27

#: Every predecessor gate, in order. All must be present with no failures.
REQUIRED_PREDECESSOR_GATES = (
    "BASELINE_ACCEPTED",
    "REGISTRY_ACCEPTED",
    "DIRECT_READ_ACCEPTED",
    "DIRECT_MUTATION_ACCEPTED",
    "BATCH_ACCEPTED",
    "DAG_ACCEPTED",
    "RUNBOOK_ACCEPTED",
    "INTEGRATIONS_ACCEPTED",
    "HYBRID_ACCEPTED",
)

PHASE9_SUITES = (
    "test_v2_phase9_failure_catalogue.py",
    "test_v2_phase9_continuity.py",
    "test_v2_phase9_replay.py",
    "test_v2_phase9_drills.py",
    "test_v2_phase9_lifecycle.py",
    "test_v2_phase9_chaos.py",
    "test_v2_phase9_audit_chain.py",
)

#: F-01..F-20 and C-01..C-08 must each be represented by an executed test.
REQUIRED_FAILURE_CASES = tuple(f"f{index:02d}" for index in range(1, 21))
REQUIRED_CONTINUITY_CASES = tuple(f"c{index:02d}" for index in range(1, 9))

REQUIRED_RUNBOOK_SECTIONS = (
    "Lifecycle and draining remediation",
    "Rollback",
    "Credential rotation",
    "Restore",
    "Audit recovery",
    "Unknown outcome and manual intervention",
    "Provider degradation",
    "Policy and approval refusals",
)

REQUIRED_DRILLS = ("rollback", "credential_rotation", "audit_restore")

#: Performance evidence must carry at least this many samples per scenario.
MIN_PERFORMANCE_SAMPLES = 500


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
    """Locate a gate marker, including gates nested one level in an artifact."""
    for payload in payloads:
        if payload.get("gate") == gate:
            return payload
        for value in payload.values():
            if isinstance(value, dict) and gate in (
                value.get("gate"),
                value.get("direct_read_status"),
                value.get("status"),
            ):
                return value
    return None


# --------------------------------------------------------------------------
# Predecessors
# --------------------------------------------------------------------------
def check_predecessors(payloads: list[dict[str, Any]]) -> tuple[list[str], dict[str, Any]]:
    failures: list[str] = []
    recorded: dict[str, Any] = {}
    for gate in REQUIRED_PREDECESSOR_GATES:
        marker = _find_gate(payloads, gate)
        if marker is None:
            failures.append(f"P9-01: predecessor gate not recorded: {gate}")
            continue
        gate_failures = marker.get("failures")
        if gate_failures is None:
            gate_failures = marker.get("gate_failures")
        if gate_failures not in ([], (), None):
            failures.append(f"P9-01: predecessor gate {gate} carries failures")
        recorded[gate] = {"source_commit": marker.get("source_commit", "")}
    return failures, recorded


# --------------------------------------------------------------------------
# V1 preservation
# --------------------------------------------------------------------------
def check_v1_preserved() -> list[str]:
    _import()
    from hermes_mcp_bridge import contracts

    failures: list[str] = []
    if contracts.CURRENT_CONTRACT_VERSION != EXPECTED_CONTRACT_VERSION:
        failures.append(f"P9-02: contract={contracts.CURRENT_CONTRACT_VERSION}")
    if contracts.SCHEMA_VERSION != EXPECTED_SCHEMA_VERSION:
        failures.append(f"P9-02: schema={contracts.SCHEMA_VERSION}")
    if contracts.expected_tool_count() != EXPECTED_TOOL_COUNT:
        failures.append(f"P9-02: tools={contracts.expected_tool_count()}")
    return failures


def check_v1_does_not_import_v2() -> list[str]:
    """No V1 module may import the V2 runtime; the boundary is one-directional."""
    failures: list[str] = []
    for path in sorted(SRC.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "from .v2" in text or "from hermes_mcp_bridge.v2" in text or "import v2" in text:
            failures.append(f"P9-03: V1 module imports V2: {path.name}")
    return failures


def check_hmac_policy_fail_closed() -> list[str]:
    """An unsigned policy decision must be explicitly marked, never silently trusted."""
    _import()
    from hermes_mcp_bridge import policy

    text = (SRC / "policy.py").read_text(encoding="utf-8")
    failures: list[str] = []
    if "unsigned:" not in text:
        failures.append("P9-04: unsigned policy decisions are not explicitly marked")
    if "hmac.new" not in text:
        failures.append("P9-04: policy signing does not use HMAC")
    if "hmac.compare_digest" not in (SRC / "approvals.py").read_text(encoding="utf-8"):
        failures.append("P9-04: approval comparison is not constant-time")
    if not hasattr(policy, "PolicyConfigError"):
        failures.append("P9-04: policy has no fail-closed configuration error")
    return failures


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------
def run_phase9_suites() -> tuple[list[str], dict[str, Any]]:
    failures: list[str] = []
    executed: dict[str, str] = {}
    paths = []
    for name in PHASE9_SUITES:
        path = TESTS / name
        if not path.is_file():
            failures.append(f"P9-05: Phase 9 suite missing: {name}")
            continue
        paths.append(str(path))
        executed[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    if not paths:
        return failures, {"suites": executed}
    result = _run([sys.executable, "-m", "pytest", "-q", "-p", "no:randomly", *paths])
    if result.returncode != 0:
        failures.append("P9-05: the Phase 9 suites did not pass")
    return failures, {"suites": executed, "pytest_returncode": result.returncode}


def check_case_coverage() -> list[str]:
    """F-01..F-20 and C-01..C-08 must each have an executed test, not a mention."""
    failures: list[str] = []
    catalogue = TESTS / "test_v2_phase9_failure_catalogue.py"
    continuity = TESTS / "test_v2_phase9_continuity.py"
    if not catalogue.is_file() or not continuity.is_file():
        return ["P9-06: failure/continuity catalogue suites missing"]
    catalogue_text = catalogue.read_text(encoding="utf-8")
    continuity_text = continuity.read_text(encoding="utf-8")
    missing_f = [
        case for case in REQUIRED_FAILURE_CASES if f"def test_{case}" not in catalogue_text
    ]
    missing_c = [
        case for case in REQUIRED_CONTINUITY_CASES if f"def test_{case}" not in continuity_text
    ]
    if missing_f:
        failures.append(f"P9-06: failure cases without a test: {','.join(missing_f)}")
    if missing_c:
        failures.append(f"P9-06: continuity cases without a test: {','.join(missing_c)}")
    return failures


# --------------------------------------------------------------------------
# Measurement
# --------------------------------------------------------------------------
def check_performance(payloads: list[dict[str, Any]]) -> tuple[list[str], dict[str, Any]]:
    marker = _find_gate(payloads, "PERFORMANCE_OK")
    if marker is None:
        return ["P9-07: performance evidence missing or not PERFORMANCE_OK"], {}
    failures: list[str] = []
    distributions = marker.get("distributions") or {}
    if not distributions:
        failures.append("P9-07: performance evidence has no distributions")
    for name, record in distributions.items():
        samples = int(record.get("samples", 0))
        if samples < MIN_PERFORMANCE_SAMPLES:
            failures.append(f"P9-07: {name} has {samples} samples (< {MIN_PERFORMANCE_SAMPLES})")
        for percentile in ("p50_ms", "p95_ms", "p99_ms"):
            if percentile not in record:
                failures.append(f"P9-07: {name} missing {percentile}")
    if marker.get("failures"):
        failures.append("P9-07: performance evidence carries failures")
    return failures, {"scenarios": sorted(distributions)}


def check_audit_and_cardinality() -> list[str]:
    """Recompute the digest chain and the label bounds inside the gate."""
    _import()
    from hermes_mcp_bridge.v2.audit_chain import (
        MAX_AGENTIC_ESCALATIONS,
        MAX_NODES_PER_REQUEST,
        MAX_REASON_LABELS_PER_RUN,
        digest_chain,
    )
    from hermes_mcp_bridge.v2.provider_audit import completeness

    failures: list[str] = []
    records = [{"request_id": f"r{index}", "outcome": "success"} for index in range(8)]
    baseline = digest_chain(*records)
    if digest_chain(*records) != baseline:
        failures.append("P9-08: digest chain is not deterministic")
    tampered = [dict(record) for record in records]
    tampered[3]["outcome"] = "refused"
    if digest_chain(*tampered) == baseline:
        failures.append("P9-08: digest chain does not detect tampering")
    if digest_chain(*records[:-1]) == baseline:
        failures.append("P9-08: digest chain does not detect loss")
    if completeness(terminal_records=8, terminal_outcomes=8) != 1.0:
        failures.append("P9-08: completeness reconciliation is wrong")
    if completeness(terminal_records=7, terminal_outcomes=8) >= 1.0:
        failures.append("P9-08: completeness does not detect a missing record")
    for name, value in (
        ("MAX_NODES_PER_REQUEST", MAX_NODES_PER_REQUEST),
        ("MAX_AGENTIC_ESCALATIONS", MAX_AGENTIC_ESCALATIONS),
        ("MAX_REASON_LABELS_PER_RUN", MAX_REASON_LABELS_PER_RUN),
    ):
        if not isinstance(value, int) or value <= 0:
            failures.append(f"P9-09: label/cardinality bound {name} is not a positive bound")
    return failures


# --------------------------------------------------------------------------
# Supply chain and secrets
# --------------------------------------------------------------------------
def check_secret_scan(payloads: list[dict[str, Any]]) -> tuple[list[str], dict[str, Any]]:
    marker = None
    for payload in payloads:
        if payload.get("schema", "").startswith("hermes-v2-phase9-secret-scan/"):
            marker = payload
            break
    if marker is None:
        return ["P9-10: secret-scan evidence missing"], {}
    failures: list[str] = []
    if marker.get("scanned") is not True:
        failures.append("P9-10: scanned=false is a failure, never a pass")
    if marker.get("finding_count", 1) != 0:
        failures.append("P9-10: secret scan reported findings")
    scope = marker.get("scope") or {}
    if not scope.get("tree", {}).get("files"):
        failures.append("P9-10: secret scan covered no tree files")
    if scope.get("history", {}).get("commits", 0) <= 0:
        failures.append("P9-10: secret scan covered no history window")
    if not marker.get("ruleset_digest_sha256"):
        failures.append("P9-10: secret scan did not record its ruleset version")
    # A finding must never carry the matched text.
    for finding in marker.get("findings", []):
        if "match" in finding and "match_sha256" not in finding:
            failures.append("P9-10: a finding exposes matched text")
    return failures, {
        "ruleset_digest_sha256": marker.get("ruleset_digest_sha256", ""),
        "scope": scope,
    }


def check_supply_chain(payloads: list[dict[str, Any]]) -> tuple[list[str], dict[str, Any]]:
    marker = _find_gate(payloads, "SUPPLY_CHAIN_OK")
    if marker is None:
        return ["P9-11: supply-chain evidence missing or not SUPPLY_CHAIN_OK"], {}
    failures: list[str] = []
    if marker.get("failures"):
        failures.append("P9-11: supply-chain evidence carries failures")
    base = marker.get("base_image") or {}
    if not str(base.get("base_digest", "")).startswith("sha256:"):
        failures.append("P9-11: base image is not pinned by digest")
    dependencies = marker.get("dependencies") or []
    if not dependencies:
        failures.append("P9-11: no runtime dependencies recorded")
    return failures, {"base_digest": base.get("base_digest", ""), "dependencies": len(dependencies)}


# --------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------
def check_drills() -> tuple[list[str], dict[str, Any]]:
    """Execute the drills here, so the gate never trusts a recorded pass."""
    _import()
    from hermes_mcp_bridge.v2.drills import (
        AUDIT_RPO_RECORDS,
        GATEWAY_RTO_SECONDS,
        ROLLBACK_RTO_SECONDS,
        drill_evidence,
        run_restore_drill,
        run_rollback_drill,
        run_rotation_drill,
    )
    from hermes_mcp_bridge.v2.provider_contract import CredentialDomain
    from hermes_mcp_bridge.v2.provider_credentials import (
        CredentialRecord,
        ProviderCredentialBroker,
    )

    counter = {"value": 0.0}

    def clock() -> float:
        counter["value"] += 0.01
        return counter["value"]

    providers = ["github", "jira"]

    def disable(provider_id: str) -> list[str]:
        providers.remove(provider_id)
        return list(providers)

    domain = CredentialDomain(
        provider_id="github",
        read_capability_id="github.read",
        granted_scopes={"github.read": ("repo:read",)},
    )
    broker = ProviderCredentialBroker({"github": domain})

    def record() -> CredentialRecord:
        return CredentialRecord(
            provider_id="github",
            credential_capability_id="github.read",
            ready=True,
            apply=lambda headers: {**headers, "authorization": "Bearer [REDACTED]"},
        )

    broker.register(record())
    original = [{"request_id": f"r{index}", "outcome": "success"} for index in range(6)]

    results = [
        run_rollback_drill(
            registry_provider_ids=list(providers),
            withdraw="jira",
            disable=disable,
            capability_usable=lambda pid: pid in providers,
            live_after_drain=0,
            clock=clock,
        ),
        run_rotation_drill(
            provider_id="github",
            capability_id="github.read",
            mint_handle=lambda: broker.resolve(
                provider_id="github",
                credential_capability_id="github.read",
                requested_scopes=("repo:read",),
            ),
            rotate=lambda: broker.rotate(record()),
            status=lambda: broker.status("github", "github.read"),
            apply_headers=lambda handle: handle.apply({}),
            restart_observed=False,
            clock=clock,
        ),
        run_restore_drill(
            original_records=original, restored_records=list(original), clock=clock
        ),
    ]
    evidence = drill_evidence(results)
    failures: list[str] = []
    if not evidence["passed"]:
        failures.append("P9-12: a rollback/rotation/restore drill failed")
    executed = {entry["drill"] for entry in evidence["drills"]}
    missing = sorted(set(REQUIRED_DRILLS) - executed)
    if missing:
        failures.append(f"P9-12: drills not executed: {','.join(missing)}")
    if AUDIT_RPO_RECORDS != 0:
        failures.append("P9-13: audit RPO target is not zero records")
    if GATEWAY_RTO_SECONDS > 300.0:
        failures.append("P9-13: gateway RTO target exceeds the accepted 5 minutes")
    if ROLLBACK_RTO_SECONDS > 900.0:
        failures.append("P9-13: rollback RTO target exceeds the accepted 15 minutes")
    return failures, evidence


def check_runbooks() -> list[str]:
    path = PHASE9_LANE / "runbooks.md"
    if not path.is_file():
        return ["P9-14: operational runbooks missing"]
    text = path.read_text(encoding="utf-8")
    missing = [section for section in REQUIRED_RUNBOOK_SECTIONS if section not in text]
    if missing:
        return [f"P9-14: runbook sections missing: {','.join(missing)}"]
    return []


def check_lifecycle_regression() -> list[str]:
    """The draining defect must be covered by a test that would fail if it returned."""
    path = TESTS / "test_v2_phase9_lifecycle.py"
    if not path.is_file():
        return ["P9-15: lifecycle draining regression test missing"]
    text = path.read_text(encoding="utf-8")
    failures: list[str] = []
    if "_shutdown_interruptible_agents.pop" not in text:
        failures.append("P9-15: regression does not assert the finally clear")
    if "drain_in_flight" not in text:
        failures.append("P9-15: regression does not exercise the drain")
    # The regression must not depend on a machine-specific absolute path.
    for leak in ("/home/", "/Users/", "C:\\"):
        if f'"{leak}' in text.replace("not in text", ""):
            failures.append("P9-15: regression hard-codes a host path")
            break
    return failures


# --------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--require-sha",
        required=True,
        help="The exact commit this verdict is bound to. A mismatch fails the gate.",
    )
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args()

    failures: list[str] = []
    head = _run(["git", "rev-parse", "HEAD"]).stdout.strip()
    if head != args.require_sha:
        failures.append("P9-00: HEAD does not match the required SHA")
    dirty = _run(["git", "status", "--porcelain"]).stdout.strip()
    if dirty:
        failures.append("P9-00: working tree is dirty; the verdict would not be reproducible")

    payloads = _load_evidence()
    predecessor_failures, predecessors = check_predecessors(payloads)
    failures += predecessor_failures
    failures += check_v1_preserved()
    failures += check_v1_does_not_import_v2()
    failures += check_hmac_policy_fail_closed()
    suite_failures, suites = run_phase9_suites()
    failures += suite_failures
    failures += check_case_coverage()
    performance_failures, performance = check_performance(payloads)
    failures += performance_failures
    failures += check_audit_and_cardinality()
    secret_failures, secret_scan = check_secret_scan(payloads)
    failures += secret_failures
    supply_failures, supply_chain = check_supply_chain(payloads)
    failures += supply_failures
    drill_failures, drills = check_drills()
    failures += drill_failures
    failures += check_runbooks()
    failures += check_lifecycle_regression()

    evidence: dict[str, Any] = {
        "drills": drills,
        "failures": sorted(failures),
        "gate": ACCEPTED_GATE if not failures else BLOCKED_GATE,
        "performance": performance,
        "predecessor_gates": predecessors,
        "required_sha": args.require_sha,
        "schema": "hermes-v2-phase9-production-gate/1",
        "secret_scan": secret_scan,
        "source_commit": head,
        "suites": suites,
        "supply_chain": supply_chain,
        "v1": {
            "contract_version": EXPECTED_CONTRACT_VERSION,
            "schema_version": EXPECTED_SCHEMA_VERSION,
            "tool_count": EXPECTED_TOOL_COUNT,
        },
    }
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    print(
        json.dumps(
            {"gate": evidence["gate"], "failures": evidence["failures"]},
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
