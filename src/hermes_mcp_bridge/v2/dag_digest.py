"""Deterministic canonical plan digest (OD-018 closed for plans; ADR-0025).

> **V2 · PHASE 5 · runtime**

Decision: canonical form is **JSON, UTF-8, sorted keys, fixed separators, no
floats**, reusing :mod:`hermes_mcp_bridge.v2.canonical` (the Phase 1 primitive)
rather than introducing CBOR. Rationale: one canonicalizer for the whole V2
surface, already proven by the accepted Phase 1 gate, human-diffable in review,
and free of the float/tag ambiguity that a second encoding would add.

The hashed byte stream is prefixed with ``DAG_DIGEST_VERSION`` so digests from
different canonicalization versions can never compare equal.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .canonical import canonical_json_bytes, sha256_hex
from .dag_contract import (
    DAG_DIGEST_VERSION,
    DAG_MAX_CANONICAL_BYTES,
    Node,
    NodeKind,
    PlanDefinition,
    PlanReason,
    PlanValidationError,
)


def _canonical_binding(target: str, binding: Any) -> dict[str, Any]:
    return {
        "target": target,
        "from": binding.source,
        "type": binding.type,
        "required": binding.required,
        "max_bytes": binding.max_bytes,
    }


def canonical_node(node: Node) -> dict[str, Any]:
    """Semantically significant node fields only; editorial text excluded."""
    payload: dict[str, Any] = {
        "id": node.id,
        "kind": node.kind.value,
        "args": dict(node.args),
        "bindings": [
            _canonical_binding(target, binding) for target, binding in sorted(node.bindings.items())
        ],
        "depends_on": sorted(set(node.depends_on)),
        "on_failure": node.on_failure.value,
        "timeout_ms": node.timeout_ms,
    }
    if node.kind is NodeKind.TOOL:
        payload["tool"] = node.tool
    else:
        payload["op"] = node.op
    if node.policy_ref is not None:
        payload["policy_ref"] = node.policy_ref
    if node.retry_ref is not None:
        payload["retry_ref"] = node.retry_ref
    if node.idempotency is not None:
        payload["idempotency"] = {
            "enabled": node.idempotency.enabled,
            "attempt_epoch": node.idempotency.attempt_epoch,
        }
    if node.compensation is not None:
        payload["compensation"] = {"operation": node.compensation.operation}
    return payload


def canonical_plan(plan: PlanDefinition) -> dict[str, Any]:
    """Canonical projection of a plan.

    Excluded on purpose: ``plan_id`` (caller-supplied and volatile),
    descriptions and any editorial metadata, ``approval`` (an approval cannot
    bind to itself), and everything transport-related.
    """
    return {
        "digest_version": DAG_DIGEST_VERSION,
        "schema_version": plan.schema_version,
        "mode": plan.mode,
        "failure_policy": plan.failure_policy.value,
        "rollback_policy": plan.rollback_policy.value,
        "deadline_ms": plan.deadline_ms,
        "dry_run": plan.dry_run,
        "budget": {
            "max_nodes": plan.budget.max_nodes,
            "max_parallelism": plan.budget.max_parallelism,
            "max_external_calls": plan.budget.max_external_calls,
            "max_total_wall_ms": plan.budget.max_total_wall_ms,
            "max_result_bytes": plan.budget.max_result_bytes,
            "max_checkpoint_bytes": plan.budget.max_checkpoint_bytes,
            "per_provider_limits": dict(sorted(plan.budget.per_provider_limits.items())),
            "per_credential_limits": dict(sorted(plan.budget.per_credential_limits.items())),
        },
        "nodes": [canonical_node(node) for node in sorted(plan.nodes, key=lambda n: n.id)],
    }


def canonical_plan_bytes(plan: PlanDefinition) -> bytes:
    encoded = canonical_json_bytes(canonical_plan(plan))
    if len(encoded) > DAG_MAX_CANONICAL_BYTES:
        raise PlanValidationError(PlanReason.PLAN_BUDGET_EXCEEDED, "canonical plan too large")
    return encoded


def plan_digest(plan: PlanDefinition) -> str:
    """``SHA-256`` over the version-prefixed canonical plan bytes."""
    return sha256_hex(canonical_plan_bytes(plan))


def operation_digest(node_id: str, resolved_args: Mapping[str, Any]) -> str:
    """Phase 3-compatible per-mutation digest over *resolved* arguments."""
    return sha256_hex(
        canonical_json_bytes(
            {
                "digest_version": DAG_DIGEST_VERSION,
                "node_id": node_id,
                "args": dict(resolved_args),
            }
        )
    )


def node_idempotency_key(
    digest: str, node_id: str, attempt_epoch: int, resolved_args: Mapping[str, Any]
) -> str:
    """``H(plan_digest || node_id || attempt_epoch || canonical(resolved_args))``."""
    return sha256_hex(
        canonical_json_bytes(
            {
                "plan_digest": digest,
                "node_id": node_id,
                "attempt_epoch": attempt_epoch,
                "args": dict(resolved_args),
            }
        )
    )


def compensation_key(digest: str, node_id: str, effect_ref: str) -> str:
    """``H(plan_digest || node_id || "compensate" || effect_ref)``."""
    return sha256_hex(
        canonical_json_bytes(
            {
                "plan_digest": digest,
                "node_id": node_id,
                "kind": "compensate",
                "effect_ref": effect_ref,
            }
        )
    )


__all__ = [
    "canonical_node",
    "canonical_plan",
    "canonical_plan_bytes",
    "compensation_key",
    "node_idempotency_key",
    "operation_digest",
    "plan_digest",
]
