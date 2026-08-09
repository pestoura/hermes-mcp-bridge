"""Loader from inert JSON plan documents to :class:`PlanDefinition`.

> **V2 · PHASE 5 · runtime**

The loader is the only place that accepts untrusted plan JSON. It is strict:
unknown fields are rejected (never ignored), review annotations are stripped,
and the design-lane fixture schema token is accepted as an explicit alias so the
frozen `#92` corpus stays byte-identical while the runtime keeps a single
canonical ``schema_version``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .dag_contract import (
    DAG_SCHEMA_VERSION,
    Approval,
    Binding,
    Budget,
    Compensation,
    FailurePolicy,
    Idempotency,
    Node,
    NodeKind,
    OnFailure,
    PlanDefinition,
    PlanReason,
    PlanValidationError,
    RollbackPolicy,
)

#: Design-lane fixture token (PR #92) mapped onto the runtime schema version.
SCHEMA_ALIASES: Mapping[str, str] = {"v2-phase5-draft": DAG_SCHEMA_VERSION}

#: Review-only annotations stripped before construction.
ANNOTATION_FIELDS = ("expected_reason_code",)

_PLAN_FIELDS = frozenset(
    {
        "plan_id",
        "nodes",
        "budget",
        "failure_policy",
        "deadline_ms",
        "rollback_policy",
        "approval",
        "dry_run",
        "schema_version",
        "mode",
        "description",
    }
)
_NODE_FIELDS = frozenset(
    {
        "id",
        "kind",
        "tool",
        "op",
        "args",
        "bindings",
        "depends_on",
        "policy_ref",
        "idempotency",
        "on_failure",
        "timeout_ms",
        "retry_ref",
        "compensation",
        "description",
    }
)
_BINDING_FIELDS = frozenset({"from", "type", "required", "max_bytes"})
_BUDGET_FIELDS = frozenset(
    {
        "max_nodes",
        "max_parallelism",
        "max_external_calls",
        "max_total_wall_ms",
        "max_result_bytes",
        "max_checkpoint_bytes",
        "per_provider_limits",
        "per_credential_limits",
    }
)


def _reject_unknown(payload: Mapping[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise PlanValidationError(PlanReason.PLAN_UNKNOWN_FIELD, f"{where}: {','.join(unknown)}")


def _binding(payload: Mapping[str, Any]) -> Binding:
    _reject_unknown(payload, _BINDING_FIELDS, "binding")
    if "from" not in payload or "type" not in payload:
        raise PlanValidationError(PlanReason.PLAN_SCHEMA_INVALID, "binding: from/type required")
    return Binding(
        source=str(payload["from"]),
        type=str(payload["type"]),
        required=bool(payload.get("required", True)),
        max_bytes=int(payload.get("max_bytes", 262_144)),
    )


def _node(payload: Mapping[str, Any]) -> Node:
    _reject_unknown(payload, _NODE_FIELDS, "node")
    kind = NodeKind(str(payload.get("kind", "")))
    idempotency = payload.get("idempotency")
    compensation = payload.get("compensation")
    return Node(
        id=str(payload.get("id", "")),
        kind=kind,
        tool=payload.get("tool"),
        op=payload.get("op"),
        args=dict(payload.get("args", {})),
        bindings={
            str(target): _binding(spec)
            for target, spec in dict(payload.get("bindings", {})).items()
        },
        depends_on=tuple(payload.get("depends_on", ())),
        policy_ref=payload.get("policy_ref"),
        idempotency=(
            Idempotency(
                enabled=bool(idempotency.get("enabled", True)),
                attempt_epoch=int(idempotency.get("attempt_epoch", 0)),
            )
            if isinstance(idempotency, Mapping)
            else None
        ),
        on_failure=OnFailure(str(payload.get("on_failure", OnFailure.ABORT_PLAN.value))),
        timeout_ms=int(payload.get("timeout_ms", 30_000)),
        retry_ref=payload.get("retry_ref"),
        compensation=(
            Compensation(operation=str(compensation["operation"]))
            if isinstance(compensation, Mapping)
            else None
        ),
        description=str(payload.get("description", "")),
    )


def _budget(payload: Mapping[str, Any]) -> Budget:
    _reject_unknown(payload, _BUDGET_FIELDS, "budget")
    return Budget(
        max_nodes=int(payload.get("max_nodes", 64)),
        max_parallelism=int(payload.get("max_parallelism", 1)),
        max_external_calls=int(payload.get("max_external_calls", 64)),
        max_total_wall_ms=int(payload.get("max_total_wall_ms", 900_000)),
        max_result_bytes=int(payload.get("max_result_bytes", 1_048_576)),
        max_checkpoint_bytes=int(payload.get("max_checkpoint_bytes", 1_048_576)),
        per_provider_limits=dict(payload.get("per_provider_limits", {})),
        per_credential_limits=dict(payload.get("per_credential_limits", {})),
    )


def _approval(payload: Mapping[str, Any]) -> Approval:
    return Approval(
        approval_id=str(payload["approval_id"]),
        digest=str(payload["digest"]),
        nonce=str(payload["nonce"]),
        expires_at_ms=int(payload["expires_at_ms"]),
        scope=frozenset(str(item) for item in payload.get("scope", ())),
        required_for=tuple(str(item) for item in payload.get("required_for", ())),
        runtime_bound=bool(payload.get("runtime_bound", False)),
        operation_digests=frozenset(str(item) for item in payload.get("operation_digests", ())),
    )


def plan_from_mapping(payload: Mapping[str, Any]) -> PlanDefinition:
    """Strict construction. Unknown fields are a validation failure."""
    document = {key: value for key, value in payload.items() if key not in ANNOTATION_FIELDS}
    _reject_unknown(document, _PLAN_FIELDS, "plan")
    schema_version = str(document.get("schema_version", DAG_SCHEMA_VERSION))
    schema_version = SCHEMA_ALIASES.get(schema_version, schema_version)
    approval = document.get("approval")
    return PlanDefinition(
        plan_id=str(document.get("plan_id", "")),
        nodes=tuple(_node(entry) for entry in document.get("nodes", ())),
        budget=_budget(dict(document.get("budget", {}))),
        failure_policy=FailurePolicy(str(document.get("failure_policy", ""))),
        deadline_ms=int(document.get("deadline_ms", 0)),
        rollback_policy=RollbackPolicy(
            str(document.get("rollback_policy", RollbackPolicy.NONE.value))
        ),
        approval=_approval(approval) if isinstance(approval, Mapping) else None,
        dry_run=bool(document.get("dry_run", False)),
        schema_version=schema_version,
        mode=str(document.get("mode", "DAG")),
        description=str(document.get("description", "")),
    )


def load_plan(path: str | Path) -> PlanDefinition:
    """Load a plan document from disk. Read-only; no execution of any kind."""
    text = Path(path).read_text(encoding="utf-8")
    return plan_from_mapping(json.loads(text))


__all__ = ["ANNOTATION_FIELDS", "SCHEMA_ALIASES", "load_plan", "plan_from_mapping"]
