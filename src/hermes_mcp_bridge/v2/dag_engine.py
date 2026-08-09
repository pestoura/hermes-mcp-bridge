"""Phase 5 DAG engine — governed, bounded, resumable execution.

> **V2 · PHASE 5 · runtime, disabled by default behind ``DAG_FEATURE_ENABLED``**

The engine owns scheduling, per-node governance, write-ahead durability,
failure/`INDETERMINATE` semantics, compensation and replay. It contains no
provider client: every external effect goes through the injected
:class:`NodeExecutor`, exactly as Phase 4 injects its step executor. There is no
shell, subprocess, socket, HTTP or ``eval`` surface here.

Replay format (OD-021 closed; ADR-0027): a replay is a mapping
``{node_id: shaped_result}`` captured from a prior execution's checkpoint. It is
data, carries no credentials, and executes with providers disabled: zero
external calls, zero approval consumption and zero idempotency writes, with
``replay=true`` recorded on the execution and every audit record.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Protocol, runtime_checkable

from .dag_contract import (
    DAG_FEATURE_ENABLED,
    Approval,
    DagDisabledError,
    FailurePolicy,
    Node,
    NodeKind,
    NodeStatus,
    OnFailure,
    PlanReason,
    PlanStatus,
    PlanValidationError,
    RollbackPolicy,
)
from .dag_digest import (
    compensation_key,
    node_idempotency_key,
    operation_digest,
)
from .dag_store import Checkpoint, CheckpointStore, Lease, NodeState
from .dag_transform import apply_transform
from .dag_validation import ToolCatalog, ValidatedPlan, revalidate_bound_value


class NodeIndeterminate(Exception):
    """Raised by an executor when commitment can neither be proven nor excluded."""

    def __init__(self, detail: str = "") -> None:
        self.detail = detail
        super().__init__(detail or "INDETERMINATE")


class NodeFailed(Exception):
    """Raised when the failure is *provably* pre-commit."""

    def __init__(self, detail: str = "") -> None:
        self.detail = detail
        super().__init__(detail or "FAILED")


@dataclass(frozen=True, slots=True)
class NodeDecision:
    """Per-node governance outcome. Absence of an ALLOW is a denial."""

    allowed: bool
    reason: PlanReason | None = None
    approval_required: bool = False
    policy_digest: str = ""


@runtime_checkable
class NodeGovernance(Protocol):
    """Phase 1 policy engine adapter, evaluated per node — never per plan."""

    def decide(
        self, plan: ValidatedPlan, node: Node, resolved_args: Mapping[str, Any]
    ) -> NodeDecision: ...

    def record(self, plan: ValidatedPlan, node: Node, state: NodeState) -> None: ...


class DenyAllGovernance:
    """Fail-closed default: no rule, no execution."""

    def decide(
        self, plan: ValidatedPlan, node: Node, resolved_args: Mapping[str, Any]
    ) -> NodeDecision:
        return NodeDecision(allowed=False, reason=PlanReason.POLICY_MISSING)

    def record(self, plan: ValidatedPlan, node: Node, state: NodeState) -> None:
        return None


#: One typed node execution. Returns a shaped, allow-listed result mapping.
NodeExecutor = Callable[[Node, Mapping[str, Any]], Awaitable[Mapping[str, Any]]]

#: Read-only reconciliation for a mutating node: returns the effect ref when the
#: effect provably exists, ``False`` when it provably does not, ``None`` when the
#: provider is unreachable or ambiguous.
Reconciler = Callable[[Node, str], Awaitable[str | bool | None]]

#: Compensation: performs the declared inverse and read-back verifies it.
Compensator = Callable[[Node, str], Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class DryRunReport:
    """Result of :meth:`DagEngine.dry_run`. Never an authorization artifact."""

    plan_digest: str
    order: tuple[str, ...]
    nodes: tuple[Mapping[str, Any], ...]
    external_calls: int = 0
    is_approval: bool = False

    def __post_init__(self) -> None:
        if self.external_calls or self.is_approval:
            raise PlanValidationError(
                PlanReason.PLAN_SCHEMA_INVALID, "dry_run must have no effects"
            )


@dataclass(frozen=True, slots=True)
class ExecutionReport:
    status: PlanStatus
    plan_digest: str
    execution_id: str
    node_statuses: Mapping[str, str]
    node_reasons: Mapping[str, str | None]
    committed_effects: tuple[Mapping[str, str], ...]
    compensated_effects: tuple[Mapping[str, str], ...]
    unknown_effects: tuple[Mapping[str, str], ...]
    budget_consumed: Mapping[str, int]
    max_observed_inflight: int = 0
    replay: bool = False
    dispatch_order: tuple[str, ...] = ()
    llm_tokens: int = 0

    def __post_init__(self) -> None:
        if self.unknown_effects and self.status not in (
            PlanStatus.INDETERMINATE,
            PlanStatus.DEAD_LETTER,
        ):
            # A response may never be silent about a possible side effect.
            raise PlanValidationError(
                PlanReason.PLAN_SCHEMA_INVALID, "unknown_effects requires INDETERMINATE"
            )


class _Meter:
    __slots__ = ("current", "max_observed")

    def __init__(self) -> None:
        self.current = 0
        self.max_observed = 0

    def enter(self) -> None:
        self.current += 1
        self.max_observed = max(self.max_observed, self.current)

    def leave(self) -> None:
        self.current -= 1


def plan_status_from(states: Mapping[str, NodeState], *, aborted: bool = False) -> PlanStatus:
    """Deterministic aggregation with the documented precedence."""
    statuses = [state.status for state in states.values()]
    if any(status is NodeStatus.DEAD_LETTER for status in statuses):
        return PlanStatus.DEAD_LETTER
    if any(status is NodeStatus.INDETERMINATE for status in statuses):
        return PlanStatus.INDETERMINATE
    if aborted:
        return PlanStatus.ABORTED
    successes = sum(1 for status in statuses if status is NodeStatus.SUCCESS)
    if statuses and successes == len(statuses):
        return PlanStatus.COMPLETED
    if successes:
        return PlanStatus.PARTIAL
    return PlanStatus.FAILED


class DagEngine:
    """Bounded, deterministic, resumable DAG executor."""

    def __init__(
        self,
        executor: NodeExecutor,
        *,
        catalog: ToolCatalog,
        store: CheckpointStore,
        governance: NodeGovernance | None = None,
        reconciler: Reconciler | None = None,
        compensator: Compensator | None = None,
        enabled: bool = DAG_FEATURE_ENABLED,
        engine_ceiling: int = 4,
        owner_id: str = "engine-0",
    ) -> None:
        self._executor = executor
        self._catalog = catalog
        self._store = store
        self._governance = governance or DenyAllGovernance()
        self._reconciler = reconciler
        self._compensator = compensator
        self._enabled = bool(enabled)
        self._engine_ceiling = max(1, int(engine_ceiling))
        self._owner_id = owner_id
        self.dispatch_order: list[str] = []

    # ---------------------------------------------------------------- helpers

    def _effective_parallelism(self, plan: ValidatedPlan) -> int:
        limits = [plan.plan.budget.max_parallelism, self._engine_ceiling]
        limits.extend(plan.plan.budget.per_provider_limits.values())
        limits.extend(plan.plan.budget.per_credential_limits.values())
        return max(1, min(limits))

    def _resolve_args(
        self, plan: ValidatedPlan, node: Node, states: Mapping[str, NodeState]
    ) -> dict[str, Any]:
        resolved: dict[str, Any] = dict(node.args)
        contract = self._catalog.contract(str(node.tool)) if node.kind is NodeKind.TOOL else None
        for target, binding in sorted(node.bindings.items()):
            slot = target.split(".", 1)[1]
            source = states.get(binding.source_node)
            if source is None or source.status is not NodeStatus.SUCCESS:
                raise PlanValidationError(PlanReason.BINDING_RUNTIME_REJECT, "source not ready")
            value: Any = source.result
            for part in binding.source_path:
                if not isinstance(value, Mapping) or part not in value:
                    raise PlanValidationError(PlanReason.BINDING_RUNTIME_REJECT, "path")
                value = value[part]
            is_resource = bool(
                contract is not None
                and contract.resource_arg is not None
                and slot == contract.resource_arg
            )
            resolved[slot] = revalidate_bound_value(
                value,
                declared_type=binding.type,
                max_bytes=binding.max_bytes,
                catalog=self._catalog,
                is_resource=is_resource,
            )
        return resolved

    def _resource_key(
        self, plan: ValidatedPlan, node: Node, resolved: Mapping[str, Any]
    ) -> str | None:
        if node.kind is not NodeKind.TOOL:
            return None
        contract = self._catalog.contract(str(node.tool))
        if contract is None or contract.resource_arg is None:
            return None
        value = resolved.get(contract.resource_arg)
        return f"{contract.provider}:{value}" if isinstance(value, str) else None

    def _check_approval(self, plan: ValidatedPlan, now_ms: int, execution_id: str) -> str | None:
        approval: Approval | None = plan.plan.approval
        needs = any(
            self._governance.decide(plan, node, dict(node.args)).approval_required
            for node in plan.plan.nodes
        )
        if not needs:
            return None
        if approval is None:
            raise PlanValidationError(PlanReason.APPROVAL_MISSING, "approval required")
        if approval.digest != plan.digest:
            raise PlanValidationError(PlanReason.APPROVAL_DIGEST_MISMATCH, "digest")
        if approval.expires_at_ms <= now_ms:
            raise PlanValidationError(PlanReason.APPROVAL_EXPIRED, "expired")
        required_scope = {key for key in plan.resource_keys.values() if isinstance(key, str)}
        if not required_scope.issubset(approval.scope):
            raise PlanValidationError(PlanReason.APPROVAL_SCOPE_INSUFFICIENT, "scope")
        if not self._store.consume_approval(approval.approval_id, approval.nonce, execution_id):
            raise PlanValidationError(PlanReason.APPROVAL_ALREADY_CONSUMED, "nonce")
        return approval.approval_id

    def _assert_operation_covered(
        self, plan: ValidatedPlan, node: Node, resolved: Mapping[str, Any]
    ) -> None:
        approval = plan.plan.approval
        if approval is None:
            raise PlanValidationError(PlanReason.APPROVAL_MISSING, node.id)
        digest = operation_digest(node.id, resolved)
        if digest in approval.operation_digests:
            return
        if approval.runtime_bound:
            key = self._resource_key(plan, node, resolved)
            if key is not None and key.split(":", 1)[1] in approval.scope:
                return
        raise PlanValidationError(PlanReason.APPROVAL_OPERATION_DIGEST_UNCOVERED, node.id)

    # ------------------------------------------------------------------- admit

    def admit(
        self,
        plan: ValidatedPlan,
        *,
        execution_id: str,
        principal_ref: str,
        projection_digest: str,
        policy_digest: str,
        now_ms: int = 0,
        expires_at_ms: int = 10**12,
        replay: bool = False,
    ) -> Checkpoint:
        """Validate governance preconditions and durably create the checkpoint."""
        if not self._enabled:
            raise DagDisabledError()
        approval_ref = None if replay else self._check_approval(plan, now_ms, execution_id)
        checkpoint = Checkpoint(
            execution_id=execution_id,
            plan_digest=plan.digest,
            principal_ref=principal_ref,
            projection_digest=projection_digest,
            policy_digest=policy_digest,
            approval_ref=approval_ref,
            replay=replay,
            lease=Lease(owner_id=self._owner_id, fence_token=1, expires_at_ms=expires_at_ms),
            node_states={node.id: NodeState(node_id=node.id) for node in plan.plan.nodes},
        )
        self._store.create(checkpoint)
        return checkpoint

    # ------------------------------------------------------------------- run

    async def run(
        self,
        plan: ValidatedPlan,
        checkpoint: Checkpoint,
        *,
        replay_results: Mapping[str, Any] | None = None,
    ) -> ExecutionReport:
        if not self._enabled:
            raise DagDisabledError()
        if checkpoint.plan_digest != plan.digest:
            raise PlanValidationError(PlanReason.PLAN_DIGEST_MISMATCH, "checkpoint")
        states: dict[str, NodeState] = dict(checkpoint.node_states)
        for node in plan.plan.nodes:
            states.setdefault(node.id, NodeState(node_id=node.id))
        replaying = bool(replay_results is not None) or checkpoint.replay
        meter = _Meter()
        fence = checkpoint.lease.fence_token
        parallelism = self._effective_parallelism(plan)
        semaphore = asyncio.Semaphore(parallelism)
        resource_locks: dict[str, asyncio.Lock] = {}
        aborted = False
        external_calls = 0
        self.dispatch_order = []

        def persist() -> None:
            nonlocal checkpoint
            checkpoint = replace(checkpoint, node_states=dict(states))
            self._store.save(checkpoint, fence_token=fence)

        persist()

        def blocked_reason(node: Node) -> PlanReason | None:
            for dep in node.depends_on:
                status = states[dep].status
                if status is NodeStatus.INDETERMINATE:
                    return PlanReason.UPSTREAM_INDETERMINATE
                if status in (
                    NodeStatus.FAILED,
                    NodeStatus.DENIED,
                    NodeStatus.SKIPPED,
                    NodeStatus.DEAD_LETTER,
                ):
                    return PlanReason.UPSTREAM_FAILED
            return None

        def ready_nodes() -> list[Node]:
            out: list[Node] = []
            for node_id in plan.order:
                state = states[node_id]
                if state.status is not NodeStatus.PENDING:
                    continue
                node = plan.plan.node(node_id)
                if all(states[dep].status is NodeStatus.SUCCESS for dep in node.depends_on):
                    out.append(node)
            out.sort(key=lambda n: (plan.ranks[n.id], n.id))
            return out

        def propagate() -> None:
            changed = True
            while changed:
                changed = False
                for node_id in plan.order:
                    state = states[node_id]
                    if state.status is not NodeStatus.PENDING:
                        continue
                    node = plan.plan.node(node_id)
                    reason = blocked_reason(node)
                    if reason is not None:
                        states[node_id] = replace(
                            state, status=NodeStatus.SKIPPED, reason_code=reason.value
                        )
                        changed = True

        async def execute(node: Node) -> None:
            nonlocal aborted, external_calls
            try:
                resolved = self._resolve_args(plan, node, states)
            except PlanValidationError as exc:
                states[node.id] = replace(
                    states[node.id], status=NodeStatus.FAILED, reason_code=exc.reason.value
                )
                return

            decision = self._governance.decide(plan, node, resolved)
            if not decision.allowed:
                states[node.id] = replace(
                    states[node.id],
                    status=NodeStatus.DENIED,
                    reason_code=(decision.reason or PlanReason.POLICY_DENIED).value,
                )
                self._governance.record(plan, node, states[node.id])
                return

            is_mutating = node.id in plan.mutating_nodes
            if is_mutating and decision.approval_required:
                try:
                    self._assert_operation_covered(plan, node, resolved)
                except PlanValidationError as exc:
                    states[node.id] = replace(
                        states[node.id], status=NodeStatus.DENIED, reason_code=exc.reason.value
                    )
                    return

            if node.kind is NodeKind.TRANSFORM:
                try:
                    value = apply_transform(
                        str(node.op), resolved, max_bytes=plan.plan.budget.max_result_bytes
                    )
                except PlanValidationError as exc:
                    states[node.id] = replace(
                        states[node.id], status=NodeStatus.FAILED, reason_code=exc.reason.value
                    )
                    return
                states[node.id] = replace(states[node.id], status=NodeStatus.SUCCESS, result=value)
                self._governance.record(plan, node, states[node.id])
                return

            if replaying:
                recorded = (replay_results or {}).get(node.id)
                if recorded is None:
                    states[node.id] = replace(
                        states[node.id],
                        status=NodeStatus.FAILED,
                        reason_code=PlanReason.BINDING_RUNTIME_REJECT.value,
                    )
                    return
                states[node.id] = replace(
                    states[node.id], status=NodeStatus.SUCCESS, result=recorded
                )
                self._governance.record(plan, node, states[node.id])
                return

            if external_calls >= plan.plan.budget.max_external_calls:
                states[node.id] = replace(
                    states[node.id],
                    status=NodeStatus.SKIPPED,
                    reason_code=PlanReason.BUDGET_EXHAUSTED.value,
                )
                return

            key = None
            if is_mutating:
                epoch = node.idempotency.attempt_epoch if node.idempotency else 0
                key = node_idempotency_key(plan.digest, node.id, epoch, resolved)
                # Write-ahead: the key is durable *before* any provider call.
                states[node.id] = replace(
                    states[node.id],
                    status=NodeStatus.DISPATCHED,
                    idempotency_key=key,
                    operation_digest=operation_digest(node.id, resolved),
                )
                persist()

            self.dispatch_order.append(node.id)
            external_calls += 1
            meter.enter()
            try:
                result = await self._executor(node, resolved)
            except NodeIndeterminate as exc:
                meter.leave()
                states[node.id] = replace(
                    states[node.id],
                    status=NodeStatus.INDETERMINATE,
                    idempotency_key=key,
                    reason_code=exc.detail or "INDETERMINATE",
                )
                persist()  # durable before any recovery attempt
                if node.on_failure is OnFailure.DEAD_LETTER:
                    states[node.id] = replace(states[node.id], status=NodeStatus.DEAD_LETTER)
                    persist()
                return
            except NodeFailed as exc:
                meter.leave()
                states[node.id] = replace(
                    states[node.id], status=NodeStatus.FAILED, reason_code=exc.detail or "FAILED"
                )
                if (
                    plan.plan.failure_policy is FailurePolicy.FAIL_FAST
                    and node.on_failure is not OnFailure.ISOLATE_BRANCH
                ) or node.on_failure is OnFailure.ABORT_PLAN:
                    aborted = True
                return
            meter.leave()
            states[node.id] = replace(
                states[node.id],
                status=NodeStatus.SUCCESS,
                result=dict(result),
                effect_ref=(
                    str(result["effect_ref"]) if is_mutating and "effect_ref" in result else None
                ),
            )
            self._governance.record(plan, node, states[node.id])

        async def guarded(node: Node) -> None:
            async with semaphore:
                resource = None
                if node.id in plan.mutating_nodes:
                    try:
                        resolved = self._resolve_args(plan, node, states)
                        resource = self._resource_key(plan, node, resolved)
                    except PlanValidationError:
                        resource = None
                if resource is None:
                    await execute(node)
                    return
                lock = resource_locks.setdefault(resource, asyncio.Lock())
                async with lock:
                    await execute(node)

        while True:
            propagate()
            batch = [
                node
                for node in ready_nodes()
                if not (aborted and plan.plan.failure_policy is FailurePolicy.FAIL_FAST)
            ]
            if not batch:
                break
            await asyncio.gather(*(guarded(node) for node in batch))
            persist()
            if aborted and plan.plan.failure_policy is FailurePolicy.FAIL_FAST:
                for node_id in plan.order:
                    if states[node_id].status is NodeStatus.PENDING:
                        states[node_id] = replace(
                            states[node_id],
                            status=NodeStatus.SKIPPED,
                            reason_code=PlanReason.UPSTREAM_ABORT.value,
                        )
                persist()
                break

        propagate()
        persist()

        compensated: list[Mapping[str, str]] = []
        if not replaying and plan.plan.rollback_policy is not RollbackPolicy.NONE:
            compensated = await self._compensate(plan, states, fence, persist)

        status = plan_status_from(
            states,
            aborted=aborted
            and not any(state.status is NodeStatus.SUCCESS for state in states.values()),
        )
        committed = tuple(
            {"node_id": node_id, "effect_ref": state.effect_ref or ""}
            for node_id, state in sorted(states.items())
            if state.status is NodeStatus.SUCCESS and state.effect_ref
        )
        unknown = tuple(
            {
                "node_id": node_id,
                "idempotency_key": state.idempotency_key or "",
                "expected_shape": plan.plan.node(node_id).tool or "",
            }
            for node_id, state in sorted(states.items())
            if state.status in (NodeStatus.INDETERMINATE, NodeStatus.DEAD_LETTER)
        )
        checkpoint = replace(checkpoint, node_states=dict(states), status=status)
        self._store.save(checkpoint, fence_token=fence)
        return ExecutionReport(
            status=status,
            plan_digest=plan.digest,
            execution_id=checkpoint.execution_id,
            node_statuses={
                node_id: state.status.value for node_id, state in sorted(states.items())
            },
            node_reasons={node_id: state.reason_code for node_id, state in sorted(states.items())},
            committed_effects=committed,
            compensated_effects=tuple(compensated),
            unknown_effects=unknown,
            budget_consumed={"external_calls": external_calls},
            max_observed_inflight=meter.max_observed,
            replay=replaying,
            dispatch_order=tuple(self.dispatch_order),
            llm_tokens=0,
        )

    # ----------------------------------------------------------- compensation

    async def _compensate(
        self,
        plan: ValidatedPlan,
        states: dict[str, NodeState],
        fence: int,
        persist: Callable[[], None],
    ) -> list[Mapping[str, str]]:
        candidates = [
            node_id
            for node_id in reversed(plan.order)
            if states[node_id].status is NodeStatus.SUCCESS
            and states[node_id].effect_ref
            and plan.plan.node(node_id).compensation is not None
        ]
        if not candidates:
            return []
        if plan_status_from(states) is PlanStatus.COMPLETED:
            return []
        outcomes: list[Mapping[str, str]] = []
        for node_id in candidates:
            node = plan.plan.node(node_id)
            state = states[node_id]
            effect_ref = str(state.effect_ref)
            states[node_id] = replace(state, status=NodeStatus.COMPENSATION_PENDING)
            # comp_key is written write-ahead exactly like a forward mutation.
            states[node_id] = replace(
                states[node_id],
                idempotency_key=compensation_key(plan.digest, node_id, effect_ref),
            )
            persist()
            if self._compensator is None:
                states[node_id] = replace(
                    states[node_id],
                    status=NodeStatus.COMPENSATION_UNSAFE,
                    reason_code=PlanReason.COMPENSATION_UNSAFE.value,
                )
                outcomes.append({"node_id": node_id, "effect_ref": effect_ref, "outcome": "UNSAFE"})
                persist()
                continue
            decision = self._governance.decide(plan, node, dict(node.args))
            if not decision.allowed:
                states[node_id] = replace(
                    states[node_id],
                    status=NodeStatus.COMPENSATION_UNSAFE,
                    reason_code=PlanReason.POLICY_DENIED.value,
                )
                outcomes.append({"node_id": node_id, "effect_ref": effect_ref, "outcome": "DENIED"})
                persist()
                continue
            try:
                verified = await self._compensator(node, effect_ref)
            except NodeIndeterminate:
                states[node_id] = replace(
                    states[node_id], status=NodeStatus.COMPENSATION_INDETERMINATE
                )
                outcomes.append(
                    {"node_id": node_id, "effect_ref": effect_ref, "outcome": "INDETERMINATE"}
                )
                persist()
                continue
            except Exception:
                states[node_id] = replace(states[node_id], status=NodeStatus.COMPENSATION_FAILED)
                outcomes.append({"node_id": node_id, "effect_ref": effect_ref, "outcome": "FAILED"})
                persist()
                continue
            if verified:
                states[node_id] = replace(states[node_id], status=NodeStatus.COMPENSATED)
                outcomes.append(
                    {"node_id": node_id, "effect_ref": effect_ref, "outcome": "COMPENSATED"}
                )
            else:
                states[node_id] = replace(
                    states[node_id],
                    status=NodeStatus.COMPENSATION_UNSAFE,
                    reason_code=PlanReason.COMPENSATION_UNSAFE.value,
                )
                outcomes.append({"node_id": node_id, "effect_ref": effect_ref, "outcome": "UNSAFE"})
            persist()
        return outcomes

    # -------------------------------------------------------------- dry run

    def dry_run(self, plan: ValidatedPlan) -> DryRunReport:
        """Validation-only simulation: zero credential resolution, zero calls.

        The output deliberately reports the idempotency key *shape*, never key
        material, and is not an approval: nothing here can be replayed to
        authorize an execution.
        """
        if not self._enabled:
            raise DagDisabledError()
        nodes: list[Mapping[str, Any]] = []
        for node_id in plan.order:
            node = plan.plan.node(node_id)
            decision = self._governance.decide(plan, node, dict(node.args))
            nodes.append(
                {
                    "node_id": node.id,
                    "kind": node.kind.value,
                    "target": node.tool or node.op or "",
                    "wave": plan.ranks[node.id],
                    "policy_decision": "ALLOW" if decision.allowed else "DENY",
                    "policy_reason": (
                        None
                        if decision.allowed
                        else (decision.reason or PlanReason.POLICY_DENIED).value
                    ),
                    "approval_required": decision.approval_required,
                    "mutating": node.id in plan.mutating_nodes,
                    "resource_key": plan.resource_keys.get(node.id),
                    "idempotency_key_shape": (
                        "sha256/hex64" if node.id in plan.mutating_nodes else None
                    ),
                }
            )
        return DryRunReport(
            plan_digest=plan.digest,
            order=plan.order,
            nodes=tuple(nodes),
            external_calls=0,
            is_approval=False,
        )

    # ---------------------------------------------------------------- resume

    async def resume(
        self,
        plan: ValidatedPlan,
        execution_id: str,
        *,
        projection_digest: str,
        policy_digest: str,
        now_ms: int = 0,
        expires_at_ms: int = 10**12,
    ) -> ExecutionReport:
        """A resume is a new authorization moment; it never re-consumes approval."""
        checkpoint = self._store.load(execution_id)
        if checkpoint.plan_digest != plan.digest:
            raise PlanValidationError(PlanReason.PLAN_DIGEST_MISMATCH, "resume")
        lease = self._store.acquire_lease(execution_id, self._owner_id, expires_at_ms)
        checkpoint = replace(self._store.load(execution_id), lease=lease)

        states = dict(checkpoint.node_states)
        # Step 6: reconcile every DISPATCHED / INDETERMINATE mutating node.
        for node_id, state in sorted(states.items()):
            if state.status not in (NodeStatus.DISPATCHED, NodeStatus.INDETERMINATE):
                continue
            node = plan.plan.node(node_id)
            if self._reconciler is None:
                states[node_id] = replace(state, status=NodeStatus.INDETERMINATE)
                continue
            outcome = await self._reconciler(node, state.idempotency_key or "")
            if outcome is None:
                states[node_id] = replace(state, status=NodeStatus.INDETERMINATE)
            elif outcome is False:
                states[node_id] = replace(state, status=NodeStatus.PENDING)
            else:
                states[node_id] = replace(state, status=NodeStatus.SUCCESS, effect_ref=str(outcome))

        # Step 5: nodes that were only skipped because an upstream node was
        # unresolved become eligible again. A node skipped for budget exhaustion
        # is likewise retryable; DENIED / FAILED outcomes are terminal.
        retryable_skips = {
            PlanReason.UPSTREAM_INDETERMINATE.value,
            PlanReason.UPSTREAM_FAILED.value,
            PlanReason.UPSTREAM_ABORT.value,
            PlanReason.BUDGET_EXHAUSTED.value,
        }
        for node_id, state in sorted(states.items()):
            if state.status is NodeStatus.SKIPPED and state.reason_code in retryable_skips:
                states[node_id] = replace(state, status=NodeStatus.PENDING, reason_code=None)

        # Step 4: policy drift re-evaluation. A now-DENY node is skipped.
        for node_id, state in sorted(states.items()):
            if state.status is not NodeStatus.PENDING:
                continue
            node = plan.plan.node(node_id)
            decision = self._governance.decide(plan, node, dict(node.args))
            if not decision.allowed:
                states[node_id] = replace(
                    state,
                    status=NodeStatus.DENIED,
                    reason_code=(decision.reason or PlanReason.POLICY_DENIED).value,
                )

        checkpoint = replace(
            checkpoint,
            node_states=states,
            projection_digest=projection_digest,
            policy_digest=policy_digest,
        )
        self._store.save(checkpoint, fence_token=lease.fence_token)
        return await self.run(plan, checkpoint)


__all__ = [
    "Compensator",
    "DagEngine",
    "DenyAllGovernance",
    "DryRunReport",
    "ExecutionReport",
    "NodeDecision",
    "NodeExecutor",
    "NodeFailed",
    "NodeGovernance",
    "NodeIndeterminate",
    "Reconciler",
    "plan_status_from",
]
