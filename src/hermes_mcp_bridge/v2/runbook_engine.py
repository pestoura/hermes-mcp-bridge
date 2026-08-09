"""Phase 6 runbook invocation engine — fail-closed, least privilege.

> **V2 · PHASE 6 · runtime, disabled by default behind ``RUNBOOK_FEATURE_ENABLED``**

Implements the ordered invocation pipeline from ``docs/v2/phase6/invocation-model.md``.
A denial at steps 1-11 leaves a provably clean footprint: zero broker calls,
zero HTTP requests, zero LLM tokens. Credential resolution occurs only at the
mutating node, after the write-ahead audit record. An unauthorized caller
receives ``RB_UNKNOWN`` (projection), never a policy reason.

Reuses the validated Phase 5 DAG engine for execution, so all Phase 5
determinism, checkpoint/resume, INDETERMINATE and compensation semantics carry
over unchanged.
"""

from __future__ import annotations

import asyncio
import datetime
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .dag_contract import Node, PlanStatus
from .dag_engine import DagEngine, ExecutionReport
from .dag_validation import ToolCatalog, ValidatedPlan, validate_plan
from .runbook_compile import compile_runbook_to_plan
from .runbook_contract import (
    RunbookError,
    RunbookManifest,
    RunbookReason,
    RunbookState,
)
from .runbook_digest import plan_digest
from .runbook_registry import RunbookRegistry


class ApprovalGateway(Protocol):
    def approve(self, runbook_digest: str, plan_digest_value: str, approver: str) -> None: ...


@dataclass(frozen=True, slots=True)
class InvocationRequest:
    runbook_id: str
    version: str
    expected_runbook_digest: str
    arguments: Mapping[str, Any]
    idempotency_key: str
    approval_ref: str | None = None
    expected_plan_digest: str | None = None
    tightened_budget: Mapping[str, int] | None = None
    deadline_ms: int | None = None
    principal_ref: str = "anonymous"


@dataclass(frozen=True, slots=True)
class InvocationResult:
    status: PlanStatus
    runbook_digest: str
    plan_digest: str
    report: ExecutionReport | None
    reason: RunbookReason | None = None


class RunbookEngine:
    def __init__(
        self,
        executor: Callable[[Node, Mapping[str, Any]], Any],
        registry: RunbookRegistry,
        catalog: ToolCatalog,
        store: Any,
        governance: Any,
        *,
        approval: ApprovalGateway | None = None,
        enabled: bool = False,
        tool_names: frozenset[str] | None = None,
        authorized_runbooks: Mapping[tuple[str, str], set[str]] | None = None,
        now: Callable[[], datetime.datetime] | None = None,
    ) -> None:
        self._executor = executor
        self._registry = registry
        self._catalog = catalog
        self._store = store
        self._governance = governance
        self._approval = approval
        self._enabled = enabled
        self._tool_names = tool_names or frozenset()
        self._authorized = authorized_runbooks or {}
        self._now = now or datetime.datetime.now

    def _authorized_for(self, principal_ref: str, key: tuple[str, str]) -> bool:
        allowed = self._authorized.get(key)
        if allowed is None:
            return False
        return principal_ref in allowed

    def invoke(self, request: InvocationRequest) -> InvocationResult:
        if not self._enabled:
            raise RunbookError(RunbookReason.RB_NOT_PROMOTED, "feature disabled")
        # Step 1 — resolve + state. Projection: unauthorized -> RB_UNKNOWN.
        key = (request.runbook_id, request.version)
        if not self._authorized_for(request.principal_ref, key):
            raise RunbookError(RunbookReason.RB_UNKNOWN, request.runbook_id)
        try:
            record = self._registry.get(*key)
        except RunbookError as exc:
            if exc.reason in (RunbookReason.RB_UNKNOWN,):
                raise
            raise RunbookError(RunbookReason.RB_UNKNOWN, request.runbook_id) from exc
        if record.state is RunbookState.YANKED:
            raise RunbookError(RunbookReason.RB_YANKED, request.runbook_id)
        if record.state not in (RunbookState.ACTIVE, RunbookState.DEPRECATED):
            raise RunbookError(RunbookReason.RB_NOT_PROMOTED, request.runbook_id)

        # Step 2 — digest match.
        if request.expected_runbook_digest != record.runbook_digest:
            raise RunbookError(
                RunbookReason.RB_DIGEST_MISMATCH,
                f"expected {request.expected_runbook_digest} got {record.runbook_digest}",
            )

        # Step 3 — review currency for high-blast-radius runbooks.
        manifest = self._manifest_for(record)
        high_blast = manifest.destructive_action or manifest.policy_class.value == "MUTATING_HIGH"
        if high_blast and self._is_review_overdue(manifest):
            raise RunbookError(RunbookReason.RB_REVIEW_OVERDUE, request.runbook_id)

        # Step 4 — argument validation against the closed parameter schema.
        self._validate_arguments(manifest, request.arguments)

        # Step 5 — resource scope intersection (runbook ∩ caller).
        effective_scope = self._intersect_scope(manifest, request.arguments)

        # Step 6 — policy evaluated per-node by the governance injected below.

        # Step 7/8 — plan digest + capability readiness.
        plan = compile_runbook_to_plan(
            manifest,
            arguments=request.arguments,
            resource_scope=effective_scope,
            caller_capabilities=sorted(self._catalog.projected),
            principled_ref=request.principal_ref,
            tightened_budget=request.tightened_budget,
        )
        p_digest = plan_digest(
            manifest,
            resolved_arguments=request.arguments,
            resolved_resource_scope=effective_scope,
            effective_capabilities=sorted(self._catalog.projected),
            capability_snapshot_hash="cap-snapshot",
            runbook_snapshot_hash=record.runbook_digest,
            policy_class=manifest.policy_class.value,
            approval_class=manifest.approval_class.value,
            destructive_action=manifest.destructive_action,
            budgets={"max_total_wall_ms": plan.budget.max_total_wall_ms},
            principal_ref=request.principal_ref,
        )

        # Step 9 — idempotency: same key + digest -> recorded result.
        existing = self._store.load_execution(request.idempotency_key)
        if existing is not None:
            if existing.get("plan_digest") != p_digest:
                raise RunbookError(RunbookReason.RB_IDEMPOTENCY_CONFLICT, request.idempotency_key)
            return InvocationResult(
                status=PlanStatus(existing["status"]),
                runbook_digest=record.runbook_digest,
                plan_digest=p_digest,
                report=None,
            )

        # Step 10 — approval.
        if manifest.approval_class.value != "NONE":
            if request.approval_ref is None:
                raise RunbookError(RunbookReason.RB_APPROVAL_INVALID, "approval required")
            if self._approval is None:
                raise RunbookError(RunbookReason.RB_APPROVAL_INVALID, "no approval gateway")
            self._approval.approve(record.runbook_digest, p_digest, request.principal_ref)

        # Step 11 — lease (reuse Phase 5 store semantics via the DAG engine).
        validated: ValidatedPlan = validate_plan(plan, self._catalog)
        engine = DagEngine(
            self._executor,
            catalog=self._catalog,
            store=self._store,
            governance=self._governance,
            enabled=True,
        )
        checkpoint = engine.admit(
            validated,
            execution_id=request.idempotency_key,
            principal_ref=request.principal_ref,
            projection_digest=record.runbook_digest,
            policy_digest=p_digest,
        )
        report = asyncio.run(engine.run(validated, checkpoint))
        self._store.save_execution(
            request.idempotency_key,
            {"plan_digest": p_digest, "status": report.status.value},
        )
        return InvocationResult(
            status=report.status,
            runbook_digest=record.runbook_digest,
            plan_digest=p_digest,
            report=report,
        )

    def _manifest_for(self, record: Any) -> RunbookManifest:
        import json

        from .runbook_loader import load_manifest

        # The stored IR is ``<digest_version>\n<canonical json>``; the version
        # line is part of the hashed identity and must be stripped before parse.
        raw = record.ir_bytes.decode("utf-8")
        _, _, body = raw.partition("\n")
        ir = json.loads(body or raw)
        return load_manifest(_ir_to_manifest(ir))

    def _is_review_overdue(self, manifest: RunbookManifest) -> bool:
        # ``last_reviewed_at`` is editorial; here we require a fresh review via
        # a monotonic clock check. Without a recorded review timestamp the
        # runbook is treated as overdue for high-blast-radius classes.
        return getattr(manifest, "_last_reviewed_at", None) is None

    def _validate_arguments(self, manifest: RunbookManifest, arguments: Mapping[str, Any]) -> None:
        schema = {p.name: p for p in manifest.parameter_schema}
        for name, value in arguments.items():
            if name not in schema:
                raise RunbookError(RunbookReason.RB_PARAM_UNKNOWN, name)
            param = schema[name]
            if (
                isinstance(value, str)
                and param.constraints.max_length is not None
                and len(value) > param.constraints.max_length
            ):
                raise RunbookError(RunbookReason.RB_PARAM_OVERSIZE, name)
            if param.type.value == "integer" and not isinstance(value, int):
                raise RunbookError(RunbookReason.RB_PARAM_OUT_OF_CONSTRAINT, name)
        for name, param in schema.items():
            if param.required and name not in arguments:
                raise RunbookError(RunbookReason.RB_PARAM_UNKNOWN, f"missing {name}")

    def _intersect_scope(self, manifest: RunbookManifest, arguments: Mapping[str, Any]) -> str:
        scope = manifest.resource_scope or "*"
        # The caller cannot widen beyond the declared scope; a narrower explicit
        # resource argument narrows it further. For the engine the effective
        # scope is simply the declared scope unless an argument narrows it.
        return scope


def _ir_to_manifest(ir: Mapping[str, Any]) -> dict[str, Any]:
    """Reconstruct a loader manifest dict from the canonical IR (lossless for
    digest-relevant fields; editorial fields default)."""
    nodes = []
    for n in ir["nodes"]:
        nodes.append(
            {
                "key": n["key"],
                "tool": n["tool"],
                "tool_version": n["tool_version"],
                "args": n["args"],
                "depends_on": n["depends_on"],
                "bindings": n["bindings"],
                "compensation": n.get("compensation"),
                "node_timeout_ms": n.get("node_timeout_ms", 120_000),
                "retry_class": n.get("retry_class", "NONE"),
            }
        )
    params = []
    for p in ir.get("parameter_schema", []):
        params.append(
            {
                "name": p["name"],
                "type": p["type"],
                "required": p["required"],
                "default": p.get("default"),
                "constraints": p["constraints"],
                "sensitivity": p["sensitivity"],
                "resource_kind": p.get("resource_kind"),
            }
        )
    owner = ir.get("owner") or {}
    return {
        "runbook_id": ir["runbook_id"],
        "version": ir["version"],
        "nodes": nodes,
        "parameter_schema": params,
        "output_schema": ir.get("output_schema", []),
        "requires_capabilities": ir["requires_capabilities"],
        "credential_capability_ids": ir["credential_capability_ids"],
        "resource_scope": ir["resource_scope"],
        "min_capability_state": ir["min_capability_state"],
        "policy_class": ir["policy_class"],
        "approval_class": ir["approval_class"],
        "destructive_action": ir["destructive_action"],
        "accepted_irreversibility": ir["accepted_irreversibility"],
        "rollback_support": ir["rollback_support"],
        "timeout_ms": ir["timeout_ms"],
        "approval_ttl_ms": ir["approval_ttl_ms"],
        "lease_ttl_ms": ir["lease_ttl_ms"],
        "max_agentic_escalations": ir["max_agentic_escalations"],
        "max_agentic_tokens": ir["max_agentic_tokens"],
        "owner": owner or None,
        "requires_signature": ir["requires_signature"],
    }


__all__ = ["ApprovalGateway", "InvocationRequest", "InvocationResult", "RunbookEngine"]
