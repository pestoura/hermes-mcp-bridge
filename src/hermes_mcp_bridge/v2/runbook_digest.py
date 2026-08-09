"""Phase 6 canonical runbook IR/digest and deterministic compile.

> **V2 · PHASE 6 · runtime, disabled by default behind ``RUNBOOK_FEATURE_ENABLED``**

Resolves OD-018 for runbooks (ADR-0028) by reusing the accepted Phase 1
canonicalizer and the Phase 5 ``digest_version`` discipline. The runbook IR is
the digest-relevant, editorial-free projection of a manifest: identical bytes
for identical semantics, so a documentation edit cannot change execution
identity and a semantic change cannot hide inside a description.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from .canonical import canonical_json_text
from .runbook_contract import (
    RUNBOOK_IR_SCHEMA_VERSION,
    Parameter,
    RunbookManifest,
    RunbookNode,
)

_IR_VERSION_PREFIX = "runbookir/1"


def _canonical_param(param: Parameter) -> dict[str, Any]:
    return {
        "name": param.name,
        "type": param.type.value,
        "required": param.required,
        "default": param.default,
        "constraints": {
            "max_length": param.constraints.max_length,
            "pattern": param.constraints.pattern,
            "minimum": param.constraints.minimum,
            "maximum": param.constraints.maximum,
            "enum_values": list(param.constraints.enum_values),
            "max_items": param.constraints.max_items,
            "max_depth": param.constraints.max_depth,
        },
        "sensitivity": param.sensitivity.value,
        "resource_kind": param.resource_kind,
    }


def _canonical_node(node: RunbookNode) -> dict[str, Any]:
    return {
        "key": node.key,
        "tool": node.tool,
        "tool_version": node.tool_version,
        "args": dict(sorted(node.args.items())),
        "depends_on": sorted(node.depends_on),
        "bindings": [
            {
                "target": b["target"],
                "source": b["source"],
                "type": b.get("type", "string"),
                "required": b.get("required", True),
                "max_bytes": b.get("max_bytes"),
            }
            for b in sorted(node.bindings, key=lambda x: (x["target"], x["source"]))
        ],
        "compensation": node.compensation,
        "node_timeout_ms": node.node_timeout_ms,
        "retry_class": node.retry_class,
    }


def canonical_ir(manifest: RunbookManifest) -> dict[str, Any]:
    """Deterministic, editorial-free intermediate representation (A6-03)."""
    return {
        "ir_schema_version": RUNBOOK_IR_SCHEMA_VERSION,
        "runbook_id": manifest.runbook_id,
        "version": manifest.version,
        "nodes": [_canonical_node(n) for n in manifest.nodes],
        "parameter_schema": [_canonical_param(p) for p in manifest.parameter_schema],
        "output_schema": [_canonical_param(p) for p in manifest.output_schema],
        "requires_capabilities": sorted(manifest.requires_capabilities),
        "credential_capability_ids": sorted(manifest.credential_capability_ids),
        "resource_scope": manifest.resource_scope,
        "min_capability_state": manifest.min_capability_state,
        "policy_class": manifest.policy_class.value,
        "approval_class": manifest.approval_class.value,
        "destructive_action": manifest.destructive_action,
        "accepted_irreversibility": manifest.accepted_irreversibility,
        "rollback_support": manifest.rollback_support.value,
        "timeout_ms": manifest.timeout_ms,
        "approval_ttl_ms": manifest.approval_ttl_ms,
        "lease_ttl_ms": manifest.lease_ttl_ms,
        "max_agentic_escalations": manifest.max_agentic_escalations,
        "max_agentic_tokens": manifest.max_agentic_tokens,
        "owner": {
            "id": manifest.owner.id,
            "kind": manifest.owner.kind,
            "contact": manifest.owner.contact,
            "review_cadence_days": manifest.owner.review_cadence_days,
        }
        if manifest.owner
        else None,
        "requires_signature": manifest.requires_signature,
    }


def canonical_ir_bytes(manifest: RunbookManifest) -> bytes:
    text = canonical_json_text(canonical_ir(manifest))
    return (_IR_VERSION_PREFIX + "\n" + text).encode("utf-8")


def runbook_digest(manifest: RunbookManifest) -> str:
    """SHA-256 over the versioned canonical IR (ADR-0028, OD-018)."""
    return hashlib.sha256(canonical_ir_bytes(manifest)).hexdigest()


def plan_digest_inputs(
    manifest: RunbookManifest,
    *,
    resolved_arguments: Mapping[str, Any],
    resolved_resource_scope: str,
    effective_capabilities: Sequence[str],
    capability_snapshot_hash: str,
    runbook_snapshot_hash: str,
    policy_class: str,
    approval_class: str,
    destructive_action: bool,
    budgets: Mapping[str, Any],
    principal_ref: str,
    expected_preconditions: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The stable inputs to a runbook-derived ``plan_digest`` (OD-018/ADR-0025).

    Sensitive arguments are represented by a salted commitment, never plaintext,
    so the digest is stable without leaking values into approval records.
    """
    committable = {}
    for key in sorted(resolved_arguments):
        value = resolved_arguments[key]
        if _is_sensitive_param(manifest, key):
            committable[key] = {"commitment": _salted_commitment(key, value)}
        else:
            committable[key] = value
    return {
        "digest_schema_version": 1,
        "runbook_id": manifest.runbook_id,
        "runbook_version": manifest.version,
        "runbook_digest": runbook_digest(manifest),
        "resolved_arguments": committable,
        "resolved_resource_scope": resolved_resource_scope,
        "effective_capabilities": sorted(effective_capabilities),
        "capability_snapshot_hash": capability_snapshot_hash,
        "runbook_snapshot_hash": runbook_snapshot_hash,
        "policy_class": policy_class,
        "approval_class": approval_class,
        "destructive_action": destructive_action,
        "resolved_node_plan": [_canonical_node(n) for n in manifest.nodes],
        "budgets": dict(sorted(budgets.items())),
        "principal_ref": principal_ref,
        "expected_preconditions": dict(sorted((expected_preconditions or {}).items())),
    }


def plan_digest(manifest: RunbookManifest, **inputs: Any) -> str:
    """SHA-256 over the versioned canonical plan inputs (binds runbook_digest)."""
    payload = plan_digest_inputs(manifest, **inputs)
    text = canonical_json_text(payload)
    return hashlib.sha256(("plan/1\n" + text).encode("utf-8")).hexdigest()


def _is_sensitive_param(manifest: RunbookManifest, name: str) -> bool:
    for param in manifest.parameter_schema:
        if param.name == name:
            return param.sensitivity is not None and param.sensitivity.value == "sensitive"
    return False


def _salted_commitment(key: str, value: Any) -> str:
    digest = hashlib.sha256(f"{key}:{value}".encode()).hexdigest()
    return f"commitment:{digest[:32]}"


__all__ = [
    "canonical_ir",
    "canonical_ir_bytes",
    "plan_digest",
    "plan_digest_inputs",
    "runbook_digest",
]
