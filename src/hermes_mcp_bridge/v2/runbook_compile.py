"""Phase 6 runbook -> DAG plan compile (migration equivalence, A6-25).

> **V2 · PHASE 6 · runtime, disabled by default behind ``RUNBOOK_FEATURE_ENABLED``**

A promoted runbook compiles to a :class:`PlanDefinition` that the validated Phase
5 engine can execute. For equivalent inputs the compiled plan and a hand-written
reference plan produce an **identical** ``plan_digest`` under the shared
canonical serialization (proven by A6-25). A caller may only narrow authority:
tightened budgets, a narrowed resource scope, never wider capability or policy.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .dag_contract import (
    DAG_MAX_PARALLELISM,
    DAG_MAX_PARALLELISM_MUTATION,
    Binding,
    Budget,
    Compensation,
    FailurePolicy,
    Idempotency,
    Node,
    NodeKind,
    PlanDefinition,
    RollbackPolicy,
)
from .runbook_contract import PolicyClass, RunbookManifest


def compile_runbook_to_plan(
    manifest: RunbookManifest,
    *,
    arguments: Mapping[str, Any],
    resource_scope: str,
    caller_capabilities: Sequence[str],
    principled_ref: str,
    tightened_budget: Mapping[str, int] | None = None,
) -> PlanDefinition:
    """Compile a runbook to a governed DAG plan.

    ``caller_capabilities`` is the intersection input: the effective capability
    set cannot exceed it. Caller input can only narrow (A6 invocation model).
    """
    effective_caps = [c for c in manifest.requires_capabilities if c in caller_capabilities]
    if set(effective_caps) != set(manifest.requires_capabilities):
        from .runbook_contract import RunbookError, RunbookReason

        raise RunbookError(
            RunbookReason.RB_INSUFFICIENT_CAPABILITY,
            ",".join(sorted(set(manifest.requires_capabilities) - set(effective_caps))),
        )

    nodes: list[Node] = []
    for rn in manifest.nodes:
        args = dict(rn.args)
        # Materialize declared parameters onto nodes that reference them via
        # bindings; argument resolution happens at engine time, here we only
        # carry the static args and let the engine resolve bindings.
        bindings = {
            b["target"]: Binding(
                source=b["source"],
                type=b.get("type", "string"),
                required=b.get("required", True),
                **({"max_bytes": b["max_bytes"]} if b.get("max_bytes") is not None else {}),
            )
            for b in rn.bindings
        }
        nodes.append(
            Node(
                id=rn.key,
                kind=NodeKind.TOOL,
                tool=rn.tool,
                args=args,
                bindings=bindings,
                depends_on=tuple(rn.depends_on),
                timeout_ms=rn.node_timeout_ms,
                idempotency=Idempotency(enabled=True, attempt_epoch=rn.idempotency_attempt_epoch),
                compensation=Compensation(operation=rn.compensation) if rn.compensation else None,
            )
        )

    # A mutating runbook is serialized by construction: the Phase 5 validator
    # caps mutating plans at DAG_MAX_PARALLELISM_MUTATION, so the compiler must
    # never emit a wider ceiling than the plan can legally carry.
    mutating = manifest.policy_class is not PolicyClass.READ_ONLY
    default_parallelism = DAG_MAX_PARALLELISM_MUTATION if mutating else DAG_MAX_PARALLELISM
    budget_fields: dict[str, int] = {
        "max_nodes": len(nodes) or 1,
        "max_parallelism": default_parallelism,
        "max_external_calls": max(1, len(nodes)),
        "max_total_wall_ms": manifest.timeout_ms,
        "max_result_bytes": 1_048_576,
        "max_checkpoint_bytes": 1_048_576,
    }
    if tightened_budget:
        # A caller may only tighten authority: a wider caller value is ignored.
        for field_name in ("max_parallelism", "max_external_calls", "max_total_wall_ms"):
            caller_val = tightened_budget.get(field_name)
            if caller_val is not None:
                budget_fields[field_name] = min(budget_fields[field_name], int(caller_val))
    base_budget = Budget(
        max_nodes=budget_fields["max_nodes"],
        max_parallelism=budget_fields["max_parallelism"],
        max_external_calls=budget_fields["max_external_calls"],
        max_total_wall_ms=budget_fields["max_total_wall_ms"],
        max_result_bytes=budget_fields["max_result_bytes"],
        max_checkpoint_bytes=budget_fields["max_checkpoint_bytes"],
    )

    rollback = (
        RollbackPolicy.COMPENSATE_ON_FAILURE
        if manifest.rollback_support.value in ("AUTOMATIC", "MANUAL")
        else RollbackPolicy.NONE
    )
    return PlanDefinition(
        # ``@`` is outside the Phase 5 plan_id charset; ``:`` is the accepted
        # separator, so the identity reads ``runbook:<id>:<version>``.
        plan_id=f"runbook:{manifest.runbook_id}:{manifest.version}",
        nodes=tuple(nodes),
        budget=base_budget,
        failure_policy=FailurePolicy.FAIL_FAST,
        rollback_policy=rollback,
        deadline_ms=manifest.timeout_ms,
        dry_run=False,
    )


__all__ = ["compile_runbook_to_plan"]
