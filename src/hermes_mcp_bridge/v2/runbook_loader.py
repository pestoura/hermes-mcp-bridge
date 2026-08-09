"""Phase 6 loader — strict manifest document to :class:`RunbookManifest`.

> **V2 · PHASE 6 · runtime, disabled by default behind ``RUNBOOK_FEATURE_ENABLED``**

The loader is the only place that accepts untrusted runbook JSON/YAML-ish dicts.
It is strict: unknown fields are rejected (closed DSL, OD-002), every enum is
validated, and node/param objects are reconstructed through the frozen value
types so the validators in ``runbook_contract`` run. No generic surface.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .runbook_contract import (
    ApprovalClass,
    ParamConstraint,
    Parameter,
    ParamSensitivity,
    ParamType,
    PolicyClass,
    RollbackSupport,
    RunbookError,
    RunbookManifest,
    RunbookNode,
    RunbookOwner,
    RunbookReason,
)


def _enum(member: Any, cls: type, field: str) -> Any:
    try:
        return cls(member)
    except ValueError as exc:
        raise RunbookError(RunbookReason.RB_SCHEMA_INVALID, f"{field}={member!r}") from exc


def _param(raw: Mapping[str, Any]) -> Parameter:
    cons = raw.get("constraints", {}) or {}
    return Parameter(
        name=raw["name"],
        type=_enum(raw["type"], ParamType, "type"),
        required=raw.get("required", True),
        default=raw.get("default"),
        constraints=ParamConstraint(
            max_length=cons.get("max_length"),
            pattern=cons.get("pattern"),
            minimum=cons.get("minimum"),
            maximum=cons.get("maximum"),
            enum_values=tuple(cons.get("enum_values", ())),
            max_items=cons.get("max_items"),
            max_depth=cons.get("max_depth"),
        ),
        sensitivity=_enum(raw.get("sensitivity", "public"), ParamSensitivity, "sensitivity"),
        resource_kind=raw.get("resource_kind"),
    )


def _node(raw: Mapping[str, Any]) -> RunbookNode:
    return RunbookNode(
        key=raw["key"],
        tool=raw["tool"],
        tool_version=raw["tool_version"],
        args=dict(raw.get("args", {}) or {}),
        depends_on=tuple(raw.get("depends_on", ()) or ()),
        bindings=tuple(raw.get("bindings", ()) or ()),
        compensation=raw.get("compensation"),
        node_timeout_ms=raw.get("node_timeout_ms", 120_000),
        idempotency_attempt_epoch=raw.get("idempotency_attempt_epoch", 0),
        retry_class=raw.get("retry_class", "NONE"),
    )


_KNOWN = {
    "runbook_id",
    "version",
    "nodes",
    "parameter_schema",
    "output_schema",
    "requires_capabilities",
    "credential_capability_ids",
    "resource_scope",
    "min_capability_state",
    "policy_class",
    "approval_class",
    "destructive_action",
    "accepted_irreversibility",
    "rollback_support",
    "timeout_ms",
    "approval_ttl_ms",
    "lease_ttl_ms",
    "max_agentic_escalations",
    "max_agentic_tokens",
    "owner",
    "requires_signature",
    "title",
    "description",
}


def load_manifest(raw: Mapping[str, Any]) -> RunbookManifest:
    unknown = set(raw) - _KNOWN
    if unknown:
        raise RunbookError(RunbookReason.RB_SCHEMA_INVALID, f"unknown fields {sorted(unknown)}")
    owner_raw = raw.get("owner")
    owner = (
        RunbookOwner(
            id=owner_raw["id"],
            kind=owner_raw["kind"],
            contact=owner_raw["contact"],
            review_cadence_days=owner_raw["review_cadence_days"],
        )
        if owner_raw
        else None
    )
    return RunbookManifest(
        runbook_id=raw["runbook_id"],
        version=raw["version"],
        nodes=[_node(n) for n in raw["nodes"]],
        parameter_schema=[_param(p) for p in raw.get("parameter_schema", ()) or ()],
        output_schema=[_param(p) for p in raw.get("output_schema", ()) or ()],
        requires_capabilities=tuple(raw.get("requires_capabilities", ()) or ()),
        credential_capability_ids=tuple(raw.get("credential_capability_ids", ()) or ()),
        resource_scope=raw.get("resource_scope", ""),
        min_capability_state=raw.get("min_capability_state", "READY"),
        policy_class=_enum(raw.get("policy_class", "READ_ONLY"), PolicyClass, "policy_class"),
        approval_class=_enum(raw.get("approval_class", "NONE"), ApprovalClass, "approval_class"),
        destructive_action=bool(raw.get("destructive_action", False)),
        accepted_irreversibility=bool(raw.get("accepted_irreversibility", False)),
        rollback_support=_enum(
            raw.get("rollback_support", "NOT_APPLICABLE"), RollbackSupport, "rollback_support"
        ),
        timeout_ms=raw.get("timeout_ms", 900_000),
        approval_ttl_ms=raw.get("approval_ttl_ms", 300_000),
        lease_ttl_ms=raw.get("lease_ttl_ms", 60_000),
        max_agentic_escalations=raw.get("max_agentic_escalations", 0),
        max_agentic_tokens=raw.get("max_agentic_tokens", 0),
        owner=owner,
        requires_signature=bool(raw.get("requires_signature", False)),
        title=raw.get("title", ""),
        description=raw.get("description", ""),
    )


__all__ = ["load_manifest"]
