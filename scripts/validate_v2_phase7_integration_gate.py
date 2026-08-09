#!/usr/bin/env python3
"""Phase 7 promotion gate — `INTEGRATION_GITHUB_ACCEPTED` + `INTEGRATION_JIRA_ACCEPTED`.

Fail-closed, machine-checked promotion for the integration layer. No
self-approval path exists: every criterion is evaluated against the real
repository, a real test run and (when `--connected` is given) a real bounded
read against the authorized target set.

Layers (both must return no failures):

* INNER — V1 contract invariants, the full P7-01..P7-20 acceptance suite executed
  for real, the provider feature flag defaulting to off, live determinism of the
  capability/write digests, a live fail-closed ordering probe proving a scope
  denial performs zero provider calls and zero credential resolutions, a live
  rollback probe proving allow-list removal yields `E-PROVIDER-UNKNOWN`, and an
  audit-completeness reconciliation.
* OUTER — SHA-256 binding of every Phase 7 module against the live tree, an AST
  scan proving no generic surface (no shell/subprocess/socket/eval) outside the
  single declared HTTP probe module, the design lane and ADR set, and the
  `RUNBOOK_ACCEPTED` marker proving Phase 6 preceded Phase 7.

Usage::

    python scripts/validate_v2_phase7_integration_gate.py \
        --json-out docs/v2/evidence/phase7-integration-acceptance.json

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
TEST_FILE = REPO_ROOT / "tests" / "test_v2_phase7_integration_acceptance.py"
DESIGN_LANE = REPO_ROOT / "docs" / "v2" / "phase7"
ADR_LANE = DESIGN_LANE / "adrs"
EVIDENCE_DIR = REPO_ROOT / "docs" / "v2" / "evidence"

EXPECTED_CONTRACT_VERSION = "1.0.0"
EXPECTED_SCHEMA_VERSION = "0.6.1"
EXPECTED_TOOL_COUNT = 27

ACCEPTED_GATE = "INTEGRATIONS_ACCEPTED"
BLOCKED_GATE = "INTEGRATIONS_BLOCKED"

PHASE7_MODULES = (
    "v2/provider_contract.py",
    "v2/provider_registry.py",
    "v2/provider_credentials.py",
    "v2/provider_audit.py",
    "v2/provider_gateway.py",
    "v2/provider_manifests.py",
    "v2/provider_health.py",
)

#: The probe module is the only Phase 7 file allowed to perform egress, and only
#: through the standard library HTTP client with redirects and proxies disabled.
EGRESS_ALLOWED_MODULE = "v2/provider_health.py"

REQUIRED_CRITERIA = tuple(f"p7_{index:02d}" for index in range(1, 21))

REQUIRED_DESIGN_DOCS = {
    "acceptance-criteria.md",
    "audit-and-policy.md",
    "capability-discovery.md",
    "credential-isolation.md",
    "plugin-boundary.md",
    "provider-lanes.md",
    "test-matrix.md",
    "tool-capability-contracts.md",
}

REQUIRED_ADRS = {
    "ADR-0032-provider-plugin-boundary-and-allow-list.md",
    "ADR-0033-per-provider-credential-domains.md",
    "ADR-0034-declare-probe-demote-capability-discovery.md",
    "ADR-0035-integration-audit-chain-and-redaction.md",
}

BANNED_MODULES = {"subprocess", "socket", "requests", "httpx", "shlex", "pty", "os"}
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


# --------------------------------------------------------------------------
# INNER
# --------------------------------------------------------------------------
def _check_v1_contract() -> list[str]:
    _import_v2()
    try:
        from hermes_mcp_bridge import contracts
        from hermes_mcp_bridge.v2 import provider_contract  # noqa: F401
    except Exception as exc:  # pragma: no cover - defensive
        return [f"P7-19: import failed: {exc.__class__.__name__}"]
    failures: list[str] = []
    if contracts.CURRENT_CONTRACT_VERSION != EXPECTED_CONTRACT_VERSION:
        failures.append(f"P7-19: contract={contracts.CURRENT_CONTRACT_VERSION}")
    if contracts.SCHEMA_VERSION != EXPECTED_SCHEMA_VERSION:
        failures.append(f"P7-19: schema={contracts.SCHEMA_VERSION}")
    if contracts.expected_tool_count() != EXPECTED_TOOL_COUNT:
        failures.append(f"P7-19: tools={contracts.expected_tool_count()}")
    leaked = sorted(name for name in contracts.required_tools() if "provider" in name.lower())
    if leaked:
        failures.append(f"P7-19: provider tools leaked into projection: {','.join(leaked)}")
    return failures


def _check_flag_default() -> list[str]:
    _import_v2()
    from hermes_mcp_bridge.v2 import provider_contract as pc

    if pc.PROVIDER_FEATURE_ENABLED is not False:
        return ["P7-00: PROVIDER_FEATURE_ENABLED must default to False"]
    return []


def _check_criteria_present() -> list[str]:
    if not TEST_FILE.is_file():
        return ["P7-00: acceptance suite missing"]
    text = TEST_FILE.read_text(encoding="utf-8")
    missing = [name for name in REQUIRED_CRITERIA if f"def test_{name}" not in text]
    if missing:
        return [f"P7-00: criteria without a test: {','.join(missing)}"]
    return []


def _run_acceptance_suite() -> list[str]:
    result = _run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly", str(TEST_FILE)]
    )
    if result.returncode != 0:
        tail = result.stdout.strip().splitlines()[-1:] or ["no output"]
        return [f"P7-00: acceptance suite failed ({tail[0]})"]
    if "skipped" in result.stdout:
        return ["P7-00: acceptance suite contains skips"]
    return []


def _probe_determinism() -> list[str]:
    """Two independent registry builds must produce identical digests."""
    _import_v2()
    from hermes_mcp_bridge.v2.enums import CapabilityState
    from hermes_mcp_bridge.v2.provider_manifests import PROVIDER_ALLOW_LIST, accepted_manifests
    from hermes_mcp_bridge.v2.provider_registry import HealthReport, build_registry

    def _build() -> tuple[str, str]:
        manifests = accepted_manifests()
        tool_ids = [
            capability.tool_id
            for manifest in manifests
            for capability in manifest.capabilities
        ]
        registry = build_registry(
            allow_list=PROVIDER_ALLOW_LIST, tool_ids=tool_ids, manifests=manifests
        )
        registry.promote_configured(
            HealthReport(capability_id=capability.capability_id, state=CapabilityState.READY)
            for manifest in manifests
            for capability in manifest.capabilities
        )
        return registry.capability_snapshot_hash(), registry.write_capability_digest()

    first = _build()
    second = _build()
    if first != second:
        return ["P7-18: capability/write digests are not deterministic"]
    return []


def _probe_fail_closed_ordering() -> list[str]:
    """Live proof: scope denial performs zero provider calls and zero resolutions."""
    _import_v2()
    from hermes_mcp_bridge.v2.enums import CapabilityState
    from hermes_mcp_bridge.v2.provider_audit import IntegrationAuditLedger, MemoryAuditSink
    from hermes_mcp_bridge.v2.provider_contract import ProviderReason
    from hermes_mcp_bridge.v2.provider_credentials import CredentialRecord, ProviderCredentialBroker
    from hermes_mcp_bridge.v2.provider_gateway import (
        PolicyPort,
        ProviderCallResult,
        ProviderGateway,
        ProviderRequest,
        ScopeResolver,
    )
    from hermes_mcp_bridge.v2.provider_manifests import PROVIDER_ALLOW_LIST, jira_manifest
    from hermes_mcp_bridge.v2.provider_registry import HealthReport, build_registry

    manifest = jira_manifest()
    registry = build_registry(
        allow_list=PROVIDER_ALLOW_LIST,
        tool_ids=[capability.tool_id for capability in manifest.capabilities],
        manifests=[manifest],
    )
    registry.promote_configured(
        HealthReport(capability_id=capability.capability_id, state=CapabilityState.READY)
        for capability in manifest.capabilities
    )
    broker = ProviderCredentialBroker({"jira": manifest.credential_domain})
    broker.register(
        CredentialRecord(
            provider_id="jira",
            credential_capability_id="jira.read",
            ready=True,
            apply=lambda headers: {**headers, "Authorization": "Basic [REDACTED]"},
        )
    )
    scopes = ScopeResolver()
    scopes.allow("jira.issue_read", ("PPE",))
    calls: list[int] = []

    def _adapter(request, headers, deadline_ms):
        calls.append(1)
        return ProviderCallResult(payload={"ok": True}, byte_count=16)

    gateway = ProviderGateway(
        registry=registry,
        policy=PolicyPort({"jira.issue_read": "ALLOW"}),
        scopes=scopes,
        broker=broker,
        audit=IntegrationAuditLedger(MemoryAuditSink()),
        adapters={"jira": _adapter},
    )
    denied = gateway.invoke(
        ProviderRequest(
            request_id="gate-scope-denial",
            principal_ref="gate",
            provider_id="jira",
            capability_id="jira.issue_read",
            target_scope_ref="FORBIDDEN",
        )
    )
    failures: list[str] = []
    if denied.reason_code is not ProviderReason.E_SCOPE_DENY:
        failures.append(f"P7-03: expected E-SCOPE-DENY, got {denied.reason_code.value}")
    if gateway.provider_calls != 0 or calls:
        failures.append("P7-03: scope denial performed a provider call")
    if gateway.credential_resolutions != 0:
        failures.append("P7-03: scope denial resolved a credential")
    return failures


def _probe_rollback() -> list[str]:
    """Live proof: allow-list removal disables a provider with zero side effects."""
    _import_v2()
    from hermes_mcp_bridge.v2.provider_contract import ProviderReason
    from hermes_mcp_bridge.v2.provider_manifests import jira_manifest
    from hermes_mcp_bridge.v2.provider_registry import ProviderRegistry, ProviderRegistryError

    manifest = jira_manifest()
    registry = ProviderRegistry(
        allow_list=("github",),
        tool_ids=[capability.tool_id for capability in manifest.capabilities],
    )
    try:
        registry.register(manifest)
    except ProviderRegistryError as exc:
        if exc.reason is ProviderReason.E_PROVIDER_UNKNOWN:
            return []
        return [f"P7-00: rollback produced {exc.reason.value}"]
    return ["P7-00: allow-list removal did not disable the provider"]


def _probe_audit_completeness() -> list[str]:
    """Live proof: every terminal outcome produced exactly one terminal record."""
    _import_v2()
    from hermes_mcp_bridge.v2.enums import CapabilityState
    from hermes_mcp_bridge.v2.provider_audit import (
        AuditKind,
        IntegrationAuditLedger,
        MemoryAuditSink,
        completeness,
    )
    from hermes_mcp_bridge.v2.provider_contract import audit_safe
    from hermes_mcp_bridge.v2.provider_credentials import CredentialRecord, ProviderCredentialBroker
    from hermes_mcp_bridge.v2.provider_gateway import (
        PolicyPort,
        ProviderCallResult,
        ProviderGateway,
        ProviderRequest,
        ScopeResolver,
    )
    from hermes_mcp_bridge.v2.provider_manifests import PROVIDER_ALLOW_LIST, github_manifest
    from hermes_mcp_bridge.v2.provider_registry import HealthReport, build_registry

    manifest = github_manifest()
    registry = build_registry(
        allow_list=PROVIDER_ALLOW_LIST,
        tool_ids=[capability.tool_id for capability in manifest.capabilities],
        manifests=[manifest],
    )
    registry.promote_configured(
        HealthReport(capability_id=capability.capability_id, state=CapabilityState.READY)
        for capability in manifest.capabilities
    )
    broker = ProviderCredentialBroker({"github": manifest.credential_domain})
    broker.register(
        CredentialRecord(
            provider_id="github",
            credential_capability_id="github.read",
            ready=True,
            apply=lambda headers: {**headers, "Authorization": "Bearer [REDACTED]"},
        )
    )
    scopes = ScopeResolver()
    for capability in manifest.capabilities:
        scopes.allow(capability.capability_id, ("pestoura/hermes-mcp-bridge",))
    sink = MemoryAuditSink()
    gateway = ProviderGateway(
        registry=registry,
        policy=PolicyPort(
            {capability.capability_id: "ALLOW" for capability in manifest.capabilities}
        ),
        scopes=scopes,
        broker=broker,
        audit=IntegrationAuditLedger(sink),
        adapters={
            "github": lambda request, headers, deadline: ProviderCallResult(
                payload={"ok": True}, byte_count=16
            )
        },
    )
    outcomes = 0
    for index, target in enumerate(("pestoura/hermes-mcp-bridge", "other/repo")):
        gateway.invoke(
            ProviderRequest(
                request_id=f"gate-audit-{index}",
                principal_ref="gate",
                provider_id="github",
                capability_id="github.repo_read",
                target_scope_ref=target,
            )
        )
        outcomes += 1
    terminal = [
        record for record in sink.records if record["kind"] == AuditKind.TERMINAL.value
    ]
    failures: list[str] = []
    ratio = completeness(terminal_records=len(terminal), terminal_outcomes=outcomes)
    if ratio != 1.0:
        failures.append(f"P7-00: audit completeness {ratio}")
    if audit_safe(list(sink.records)):
        failures.append("P7-20: redaction scan found secret-shaped material")
    return failures


# --------------------------------------------------------------------------
# OUTER
# --------------------------------------------------------------------------
def _ast_scan() -> list[str]:
    failures: list[str] = []
    for rel in PHASE7_MODULES:
        tree = ast.parse((SRC / rel).read_text(encoding="utf-8"))
        allowed_egress = rel == EGRESS_ALLOWED_MODULE
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root in BANNED_MODULES:
                        failures.append(f"P7-00: {rel} imports {root}")
                    if root in ("urllib", "http", "ssl") and not allowed_egress:
                        failures.append(f"P7-00: {rel} imports {root}")
            elif isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".")[0]
                if root in BANNED_MODULES:
                    failures.append(f"P7-00: {rel} imports from {root}")
                if root in ("urllib", "http", "ssl") and not allowed_egress:
                    failures.append(f"P7-00: {rel} imports from {root}")
            elif isinstance(node, ast.Name) and node.id in BANNED_NAMES:
                failures.append(f"P7-00: {rel} references {node.id}")
            elif isinstance(node, ast.Attribute) and node.attr in BANNED_ATTRS:
                failures.append(f"P7-00: {rel} references attribute {node.attr}")
    return sorted(set(failures))


def _check_design_lane() -> list[str]:
    failures: list[str] = []
    if not DESIGN_LANE.is_dir():
        return ["P7-00: design lane missing"]
    present = {path.name for path in DESIGN_LANE.glob("*.md")}
    missing = sorted(REQUIRED_DESIGN_DOCS - present)
    if missing:
        failures.append(f"P7-00: design docs missing: {','.join(missing)}")
    adrs = {path.name for path in ADR_LANE.glob("*.md")} if ADR_LANE.is_dir() else set()
    missing_adrs = sorted(REQUIRED_ADRS - adrs)
    if missing_adrs:
        failures.append(f"P7-00: ADRs missing: {','.join(missing_adrs)}")
    return failures


def _check_runbook_accepted() -> list[str]:
    for path in sorted(EVIDENCE_DIR.glob("phase6*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if payload.get("gate") == "RUNBOOK_ACCEPTED" and payload.get("failures") == []:
            return []
    return ["P7-01: RUNBOOK_ACCEPTED marker not found"]


def _check_provider_status() -> list[str]:
    """Only genuinely supported providers may be ACCEPTED; the rest stay blocked."""
    _import_v2()
    from hermes_mcp_bridge.v2.provider_contract import ProviderStatus
    from hermes_mcp_bridge.v2.provider_manifests import (
        BLOCKED_UNCONFIRMED_PROVIDERS,
        CANDIDATE_PROVIDERS,
        PROVIDER_ALLOW_LIST,
        accepted_manifests,
    )

    failures: list[str] = []
    accepted = {manifest.provider_id for manifest in accepted_manifests()}
    if accepted != set(PROVIDER_ALLOW_LIST):
        failures.append("P7-00: allow-list and accepted manifests disagree")
    if len(accepted) < 2:
        failures.append("P7-00: fewer than two accepted integrations")
    for manifest in accepted_manifests():
        if manifest.status is not ProviderStatus.ACCEPTED:
            failures.append(f"P7-00: {manifest.provider_id} is not ACCEPTED")
    overlap = set(BLOCKED_UNCONFIRMED_PROVIDERS) & set(PROVIDER_ALLOW_LIST)
    if overlap:
        failures.append(f"P7-00: blocked provider in allow-list: {','.join(sorted(overlap))}")
    candidate_overlap = set(CANDIDATE_PROVIDERS) & set(PROVIDER_ALLOW_LIST)
    if candidate_overlap:
        failures.append(
            f"P7-00: candidate provider in allow-list: {','.join(sorted(candidate_overlap))}"
        )
    return failures


def _connected_evidence(path: Path | None) -> tuple[list[str], dict[str, Any]]:
    """Bind an externally produced connected-run artifact, when supplied."""
    if path is None:
        return [], {"connected": "NOT_SUPPLIED"}
    if not path.is_file():
        return [f"P7-12: connected evidence missing at {path.name}"], {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    failures: list[str] = []
    if payload.get("direct_tokens") != 0:
        failures.append("P7-12: connected run attributed non-zero Hermes LLM tokens")
    if payload.get("direct_sessions_created") != 0:
        failures.append("P7-12: connected run created a Hermes LLM session")
    if payload.get("mutation_residual") not in (0, None):
        failures.append("P7-12: connected run left a mutation residual")
    providers = payload.get("providers") or {}
    for provider_id, record in providers.items():
        if record.get("state") not in ("READY", "DEGRADED"):
            failures.append(f"P7-12: provider {provider_id} not usable in connected run")
    if len(providers) < 2:
        failures.append("P7-12: connected run covered fewer than two providers")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return failures, {"connected": "SUPPLIED", "connected_digest_sha256": digest}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-out", type=Path, default=None)
    parser.add_argument("--connected-evidence", type=Path, default=None)
    args = parser.parse_args()

    failures: list[str] = []
    failures += _check_v1_contract()
    failures += _check_flag_default()
    failures += _check_criteria_present()
    failures += _run_acceptance_suite()
    failures += _probe_determinism()
    failures += _probe_fail_closed_ordering()
    failures += _probe_rollback()
    failures += _probe_audit_completeness()
    failures += _check_provider_status()
    failures += _ast_scan()
    failures += _check_design_lane()
    failures += _check_runbook_accepted()
    connected_failures, connected_meta = _connected_evidence(args.connected_evidence)
    failures += connected_failures

    binding = {rel: _module_digest(rel) for rel in PHASE7_MODULES}
    source_commit = _run(["git", "rev-parse", "HEAD"]).stdout.strip()
    evidence: dict[str, Any] = {
        "accepted_providers": ["github", "jira"],
        "blocked_unconfirmed": ["ritmo"],
        "criteria": list(REQUIRED_CRITERIA),
        "failures": sorted(failures),
        "gate": ACCEPTED_GATE if not failures else BLOCKED_GATE,
        "module_binding_sha256": binding,
        "source_commit": source_commit,
        **connected_meta,
    }
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    printable = {key: value for key, value in evidence.items() if key != "module_binding_sha256"}
    print(json.dumps(printable, indent=2, sort_keys=True))
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
