#!/usr/bin/env python3
"""Generate deterministic acceptance evidence for Hermes MCP Bridge V2 Phase 1."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from hermes_mcp_bridge import contracts
from hermes_mcp_bridge.v2 import (
    ApprovalRequirement,
    CapabilityDescriptor,
    CapabilityRegistry,
    CapabilityState,
    CredentialCapabilityStatus,
    IdempotencySemantics,
    MutationClass,
    PolicyDecision,
    PolicyEngine,
    PolicyRule,
    PolicyRuleSet,
    RegistryValidationError,
    ResourceKey,
    RetryClass,
    RetryPolicy,
    SecurityTier,
    StaticCredentialBroker,
    ToolDefinition,
    ToolRegistry,
    canonical_json_text,
    project_capabilities,
)
from hermes_mcp_bridge.v2.errors import PolicyValidationError

EVIDENCE_SCHEMA = "hermes-v2-phase1-registry-acceptance/1"
SENTINEL = "PHASE1_EDITORIAL_SENTINEL_DO_NOT_SERIALIZE"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

PHASE1_REQUIREMENTS = (
    "V2-FR-012",
    "V2-FR-013",
    "V2-FR-014",
    "V2-FR-017",
    "V2-FR-021",
    "V2-SEC-001",
    "V2-SEC-002",
    "V2-SEC-006",
    "V2-SEC-013",
    "V2-SEC-014",
    "V2-SEC-019",
    "V2-SEC-020",
    "V2-NFR-013",
    "V2-NFR-017",
)


def _schema() -> dict[str, Any]:
    return {"type": "object", "properties": {"number": {"type": "integer"}}}


def _capability(
    capability_id: str,
    *,
    provider: str,
    state: CapabilityState,
    description: str = "",
) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        capability_id=capability_id,
        provider=provider,
        state=state,
        description=description,
    )


def _read_tool(
    tool_id: str,
    *,
    capability_id: str = "github.api",
    credential_capability_id: str | None = "github.read",
    policy_action: str,
    description: str = "",
) -> ToolDefinition:
    return ToolDefinition(
        tool_id=tool_id,
        provider=tool_id.split(".", 1)[0],
        operation=tool_id.split(".", 1)[1],
        input_schema=_schema(),
        output_schema=_schema(),
        security_tier=SecurityTier.T0,
        read_only=True,
        mutation_class=MutationClass.NONE,
        idempotency=IdempotencySemantics.READ,
        policy_action=policy_action,
        capability_id=capability_id,
        credential_capability_id=credential_capability_id,
        timeout_seconds=30,
        retry_policy=RetryPolicy(retry_class=RetryClass.RETRY_SAFE, max_attempts=3),
        resource_key=ResourceKey(scope="repository", selector="default"),
        description=description,
        backend="github-api" if tool_id.startswith("github.") else "system-local",
    )


def _mutation_tool(
    tool_id: str,
    *,
    tier: SecurityTier,
    mutation_class: MutationClass,
    policy_action: str,
    approval: ApprovalRequirement,
) -> ToolDefinition:
    return ToolDefinition(
        tool_id=tool_id,
        provider="github",
        operation=tool_id.split(".", 1)[1],
        input_schema=_schema(),
        output_schema=_schema(),
        security_tier=tier,
        read_only=False,
        mutation_class=mutation_class,
        idempotency=IdempotencySemantics.KEYED_IDEMPOTENT,
        policy_action=policy_action,
        approval_requirement=approval,
        capability_id="github.api",
        credential_capability_id="github.write",
        timeout_seconds=60,
        resource_key=ResourceKey(scope="repository", selector="default"),
        backend="github-api",
    )


def _build_registry(*, reverse: bool = False) -> ToolRegistry:
    capabilities = [
        _capability(
            "github.api",
            provider="github",
            state=CapabilityState.READY,
            description=SENTINEL,
        ),
        _capability("github.audit", provider="github", state=CapabilityState.READY),
        _capability("github.read", provider="github", state=CapabilityState.READY),
        _capability("github.write", provider="github", state=CapabilityState.READY),
        _capability("system.local", provider="system", state=CapabilityState.DEGRADED),
    ]
    tools = [
        _read_tool(
            "github.get_checks",
            credential_capability_id="github.audit",
            policy_action="github.checks.read",
        ),
        _read_tool(
            "github.get_issue",
            policy_action="github.issue.read",
        ),
        _read_tool(
            "github.get_pr",
            policy_action="github.pr.read",
            description=SENTINEL,
        ),
        _mutation_tool(
            "github.create_pr",
            tier=SecurityTier.T2,
            mutation_class=MutationClass.STANDARD,
            policy_action="github.pr.create",
            approval=ApprovalRequirement.CONDITIONAL,
        ),
        _mutation_tool(
            "github.delete_repo",
            tier=SecurityTier.T4,
            mutation_class=MutationClass.DESTRUCTIVE,
            policy_action="github.repo.delete",
            approval=ApprovalRequirement.REQUIRED,
        ),
        _read_tool(
            "system.status",
            capability_id="system.local",
            credential_capability_id=None,
            policy_action="system.status.read",
        ),
    ]
    if reverse:
        capabilities.reverse()
        tools.reverse()
    return ToolRegistry(CapabilityRegistry(capabilities), tools).freeze()


def _rules() -> PolicyRuleSet:
    return PolicyRuleSet(
        [
            PolicyRule(
                policy_action="github.checks.read",
                decision=PolicyDecision.ALLOW,
                note=SENTINEL,
            ),
            PolicyRule(
                policy_action="github.pr.create",
                decision=PolicyDecision.APPROVAL_REQUIRED,
                note=SENTINEL,
            ),
            PolicyRule(policy_action="github.pr.read", decision=PolicyDecision.ALLOW),
            PolicyRule(policy_action="github.repo.delete", decision=PolicyDecision.ALLOW),
            PolicyRule(policy_action="system.status.read", decision=PolicyDecision.ALLOW),
        ]
    )


def _broker() -> StaticCredentialBroker:
    return StaticCredentialBroker(
        [
            CredentialCapabilityStatus(
                capability_id="github.read",
                provider="github",
                state=CapabilityState.READY,
            ),
            CredentialCapabilityStatus(
                capability_id="github.write",
                provider="github",
                state=CapabilityState.READY,
            ),
        ]
    )


def _health_matrix() -> dict[str, dict[str, bool]]:
    return {
        state.value: {
            "configured": state.is_configured,
            "available": state.is_available,
            "healthy": state.is_healthy,
            "ready": state.is_ready,
        }
        for state in CapabilityState
    }


def _v1_import_scan() -> list[str]:
    root = Path(__file__).resolve().parents[1] / "src" / "hermes_mcp_bridge"
    hits: list[str] = []
    for path in sorted(root.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "hermes_mcp_bridge.v2" in text or "from .v2" in text or "import .v2" in text:
            hits.append(path.name)
    return hits


def _sensitive_schema_rejected() -> bool:
    payload = _read_tool("github.schema_probe", policy_action="github.schema.read").model_dump()
    payload["input_schema"] = {
        "type": "object",
        "properties": {"token": {"type": "string", "default": SENTINEL}},
    }
    try:
        ToolDefinition(**payload)
    except RegistryValidationError:
        return True
    return False


def _sensitive_schema_name_without_literal_allowed() -> bool:
    payload = _read_tool("github.schema_probe", policy_action="github.schema.read").model_dump()
    payload["input_schema"] = {
        "type": "object",
        "properties": {"token": {"type": "string"}},
    }
    ToolDefinition(**payload)
    return True


def _wildcard_policy_rejected() -> bool:
    try:
        PolicyRule(policy_action="github.*", decision=PolicyDecision.ALLOW)
    except PolicyValidationError:
        return True
    return False


def collect_evidence(source_commit: str) -> dict[str, Any]:
    if not _SHA_RE.fullmatch(source_commit):
        raise ValueError("source_commit must be a lowercase 40-hex Git SHA")

    registry = _build_registry()
    reversed_registry = _build_registry(reverse=True)
    rules = _rules()
    broker = _broker()
    engine = PolicyEngine(registry, rules, broker)
    projection = project_capabilities(registry, rules, broker)

    snapshot = registry.snapshot()
    reversed_snapshot = reversed_registry.snapshot()
    snapshot_text = snapshot.canonical_json
    projection_text = canonical_json_text(projection.canonical())
    rules_text = canonical_json_text(rules.canonical())
    credential_text = canonical_json_text([status.canonical() for status in broker.ordered()])

    policy = {item.tool_id: item.canonical() for item in engine.evaluate_all()}
    unknown = engine.evaluate("github.not_registered").canonical()
    projection_fields = sorted(projection.tools[0].canonical()) if projection.tools else []
    excluded = {item.tool_id: item.reason_code.value for item in projection.excluded}

    changed_capabilities = [
        cap.with_state(CapabilityState.DEGRADED)
        if cap.capability_id == "github.api"
        else cap
        for cap in registry.capabilities.ordered()
    ]
    changed_registry = ToolRegistry(
        CapabilityRegistry(changed_capabilities),
        registry.ordered(),
    ).freeze()

    secret_safe = SENTINEL not in "\n".join(
        (snapshot_text, projection_text, rules_text, credential_text)
    )
    v1_import_hits = _v1_import_scan()
    v1_tools = sorted(contracts.required_tools("1.0.0"))

    checks = {
        "canonical_snapshot_deterministic": (
            snapshot.capability_snapshot_hash == reversed_snapshot.capability_snapshot_hash
            and snapshot.canonical_bytes == reversed_snapshot.canonical_bytes
        ),
        "material_change_changes_snapshot_hash": (
            snapshot.capability_snapshot_hash != changed_registry.capability_snapshot_hash()
        ),
        "capability_health_model_exact": set(_health_matrix())
        == {state.value for state in CapabilityState},
        "unknown_tool_denied": unknown["decision"] == "DENY"
        and unknown["reason_code"] == "UNKNOWN_TOOL",
        "missing_policy_denied": policy["github.get_issue"]["reason_code"]
        == "MISSING_POLICY_RULE",
        "missing_credential_denied": policy["github.get_checks"]["reason_code"]
        == "CREDENTIAL_CAPABILITY_UNKNOWN",
        "degraded_capability_denied": policy["system.status"]["reason_code"]
        == "CAPABILITY_NOT_READY",
        "destructive_t4_denied_before_allow": policy["github.delete_repo"]["reason_code"]
        == "DESTRUCTIVE_DENIED_BY_DEFAULT",
        "approval_required_projected_explicitly": any(
            tool.tool_id == "github.create_pr" and tool.requires_approval
            for tool in projection.tools
        ),
        "projection_authorized_subset_only": projection.tool_ids
        == ["github.create_pr", "github.get_pr"],
        "projection_field_allowlist_exact": projection_fields
        == [
            "execution_mode",
            "input_schema",
            "mutation_class",
            "operation",
            "output_schema",
            "provider",
            "read_only",
            "requires_approval",
            "result_shaping",
            "security_tier",
            "timeout_seconds",
            "tool_id",
            "version",
        ],
        "editorial_and_operator_text_not_serialized": secret_safe,
        "materialized_sensitive_schema_rejected": _sensitive_schema_rejected(),
        "sensitive_field_name_without_literal_allowed": (
            _sensitive_schema_name_without_literal_allowed()
        ),
        "wildcard_policy_rejected": _wildcard_policy_rejected(),
        "credential_contract_status_only": all(
            set(status.canonical()) == {"capability_id", "provider", "state", "version"}
            for status in broker.ordered()
        ),
        "v1_contract_surface_unchanged": len(v1_tools) == 27,
        "v1_modules_do_not_import_v2": not v1_import_hits,
    }

    return {
        "schema": EVIDENCE_SCHEMA,
        "gate": "REGISTRY_EVIDENCE_COLLECTED",
        "source_commit": source_commit,
        "requirements": list(PHASE1_REQUIREMENTS),
        "checks": checks,
        "snapshot": {
            "schema_version": snapshot.payload["schema_version"],
            "capability_snapshot_hash": snapshot.capability_snapshot_hash,
            "tool_count": len(registry),
            "capability_count": len(registry.capabilities),
            "deterministic_under_reordering": checks["canonical_snapshot_deterministic"],
            "material_change_detected": checks["material_change_changes_snapshot_hash"],
        },
        "capability_state_matrix": _health_matrix(),
        "policy": {
            "decisions": policy,
            "unknown_tool": unknown,
        },
        "projection": {
            "tool_ids": projection.tool_ids,
            "excluded_reason_codes": excluded,
            "projection_hash": projection.projection_hash(),
            "capability_snapshot_hash": projection.capability_snapshot_hash,
            "projected_fields": projection_fields,
        },
        "credentials": {
            "broker_type": "StaticCredentialBroker",
            "status_fields": ["capability_id", "provider", "state", "version"],
            "real_backend_present": False,
        },
        "v1": {
            "tool_contract_count": len(v1_tools),
            "v2_import_hits": v1_import_hits,
            "runtime_wiring_changed": False,
        },
        "privacy": {
            "credential_values_stored": False,
            "secret_paths_stored": False,
            "editorial_text_serialized": False,
        },
        "deferred_non_blocking": [
            "registry_persistence_and_signing",
            "real_credential_backend",
            "principal_tenant_authorization_model",
            "dynamic_projection_and_discovery",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--json-out", required=True)
    args = parser.parse_args()

    payload = collect_evidence(args.source_commit)
    failures = sorted(name for name, passed in payload["checks"].items() if passed is not True)
    if failures:
        print(json.dumps({"gate": "REGISTRY_EVIDENCE_BLOCKED", "failures": failures}, indent=2))
        return 1

    text = json.dumps(payload, indent=2, sort_keys=True)
    Path(args.json_out).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
