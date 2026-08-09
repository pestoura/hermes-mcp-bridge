"""Phase 8 deterministic mode resolver — the S0..S8 decision tree.

> **V2 · PHASE 8 · runtime, disabled by default behind ``HYBRID_FEATURE_ENABLED``**

One pure total function, :meth:`ModeResolver.resolve`, walks the normative tree
in the permanent preference order DIRECT → BATCH → DAG → RUNBOOK → AGENTIC. Each
node maps one-to-one to a labelled block below, and every rejected branch is
recorded in order, so a decision record explains not only what was chosen but
what was ruled out and why.

Determinism is structural, not aspirational:

* no wall-clock read, no randomness, no environment read, no I/O;
* the capability snapshot is passed in as an immutable mapping plus its digest;
* the same ``(intent, snapshot, policy result, budget)`` therefore reproduces the
  same decision record byte-for-byte, which :func:`replay_decision` asserts.

RUNBOOK is evaluated *after* BATCH and DAG despite the design lane's S5-before-S6
ordering: the permanent operator preference is DIRECT > BATCH > DAG/RUNBOOK >
AGENTIC, and a runbook match is only preferred over a DAG when the intent is not
already expressible as a plain independent batch. The tie between DAG and
RUNBOOK is broken toward RUNBOOK, which is the design lane's P8-04 requirement.
"""

from __future__ import annotations

from collections.abc import Mapping

from .enums import CapabilityState, ExecutionMode, PolicyDecision
from .resolver_contract import (
    MODE_FOR_REASON,
    MODE_PREFERENCE,
    IntentOperation,
    ResolverBudget,
    ResolverDecision,
    ResolverIntent,
    ResolverReason,
)

#: Read capabilities may serve while DEGRADED; writes require READY. The
#: resolver does not know read from write on its own — the snapshot carries the
#: state and the registry already refused to promote an unusable write.
_USABLE_READ_STATES = (CapabilityState.READY, CapabilityState.DEGRADED)


class ModeResolver:
    """Pure decision function over an immutable capability snapshot."""

    __slots__ = ("_budget", "_runbooks", "_snapshot", "_snapshot_digest", "_write_capabilities")

    def __init__(
        self,
        *,
        snapshot: Mapping[str, CapabilityState],
        snapshot_digest: str,
        budget: ResolverBudget | None = None,
        runbooks: Mapping[str, bool] | None = None,
        write_capabilities: frozenset[str] | None = None,
    ) -> None:
        self._snapshot = dict(snapshot)
        self._snapshot_digest = snapshot_digest
        self._budget = budget or ResolverBudget()
        #: registered runbook ref -> is the registration version-pinned
        self._runbooks = dict(runbooks or {})
        self._write_capabilities = write_capabilities or frozenset()

    @property
    def budget(self) -> ResolverBudget:
        return self._budget

    # -- helpers ----------------------------------------------------------
    def _usable(self, operation: IntentOperation) -> bool:
        state = self._snapshot.get(operation.capability_id)
        if state is None:
            return False
        if operation.capability_id in self._write_capabilities:
            return state is CapabilityState.READY
        return state in _USABLE_READ_STATES

    def _decision(
        self,
        intent: ResolverIntent,
        *,
        reason: ResolverReason,
        rejected: list[ResolverReason],
        deterministic_nodes: int = 0,
        agentic_tokens: int = 0,
    ) -> ResolverDecision:
        mode = MODE_FOR_REASON.get(reason) if reason.is_mode_selection else None
        return ResolverDecision(
            request_id=intent.request_id,
            mode=mode,
            primary_reason_code=reason,
            rejected_branches=tuple(rejected),
            deterministic_nodes=deterministic_nodes,
            total_nodes=intent.node_count,
            escalation_count=intent.escalation_count,
            agentic_tokens_authorized=agentic_tokens,
            intent_digest=intent.digest(),
            budget_digest=self._budget.digest(),
            snapshot_digest=self._snapshot_digest,
        )

    # -- the tree ---------------------------------------------------------
    def resolve(
        self,
        intent: ResolverIntent,
        *,
        policy: PolicyDecision,
        policy_allows_agentic: bool = True,
        context_shaping_ok: bool = True,
    ) -> ResolverDecision:
        """Return exactly one terminal decision. Never raises for a valid intent."""
        rejected: list[ResolverReason] = []

        # S0 — typed request validation.
        if intent.node_count == 0 and not (
            intent.residual_subintent or intent.no_contract_coverage or intent.unknown_target
        ):
            return self._decision(
                intent, reason=ResolverReason.E_REQ_INVALID, rejected=rejected
            )

        # S1 — policy. DENY is terminal in every mode (invariant I1).
        if policy is PolicyDecision.DENY:
            return self._decision(
                intent, reason=ResolverReason.E_POLICY_DENY, rejected=rejected
            )

        # An escalation budget already spent is terminal before any branch that
        # could consume another one.
        if intent.escalation_count > self._budget.max_escalations_per_request:
            return self._decision(
                intent, reason=ResolverReason.E_AGENTIC_BUDGET_EXHAUSTED, rejected=rejected
            )

        deterministic_nodes = sum(
            1 for operation in intent.operations if self._usable(operation)
        )

        # S2/S3 — DIRECT: exactly one registered typed tool, fully bound, ready.
        if intent.node_count == 1 and not intent.residual_subintent:
            operation = intent.operations[0]
            if not operation.fully_bound:
                rejected.append(ResolverReason.R_REJ_DIRECT_UNBOUND)
            elif not self._usable(operation):
                rejected.append(ResolverReason.R_REJ_DIRECT_NOT_READY)
            else:
                return self._decision(
                    intent,
                    reason=ResolverReason.R_DIRECT_EXACT,
                    rejected=rejected,
                    deterministic_nodes=1,
                )
        elif intent.node_count > 1:
            rejected.append(ResolverReason.R_REJ_DIRECT_MULTI_TOOL)
        elif intent.node_count == 1:
            # Single operation but a residual sub-intent remains: DIRECT cannot
            # express the whole request.
            rejected.append(ResolverReason.R_REJ_DIRECT_MULTI_TOOL)

        # S6 — BATCH: N>1 homogeneous, independent, fully bound.
        if intent.node_count > 1 and not intent.residual_subintent:
            if intent.has_dependencies or not intent.homogeneous:
                rejected.append(ResolverReason.R_REJ_NOT_INDEPENDENT)
            elif not intent.fully_bound:
                rejected.append(ResolverReason.R_REJ_DIRECT_UNBOUND)
            elif intent.node_count > self._budget.batch_max_nodes:
                # Budget refusal, not a fall-through: no partial execution.
                return self._decision(
                    intent, reason=ResolverReason.E_BUDGET_NODES, rejected=rejected
                )
            elif deterministic_nodes != intent.node_count:
                rejected.append(ResolverReason.R_REJ_DIRECT_NOT_READY)
            else:
                return self._decision(
                    intent,
                    reason=ResolverReason.R_BATCH_INDEPENDENT,
                    rejected=rejected,
                    deterministic_nodes=deterministic_nodes,
                )
        elif intent.node_count > 1:
            rejected.append(ResolverReason.R_REJ_NOT_INDEPENDENT)

        # S5 — RUNBOOK, preferred over DAG when a pinned registration matches.
        runbook_pinned = self._runbooks.get(intent.runbook_ref)
        if intent.runbook_ref and runbook_pinned is not None:
            if not (runbook_pinned and intent.runbook_version_pinned):
                rejected.append(ResolverReason.R_REJ_RUNBOOK_VERSION_UNPINNED)
            elif not intent.fully_bound:
                rejected.append(ResolverReason.R_REJ_DIRECT_UNBOUND)
            elif intent.residual_subintent:
                rejected.append(ResolverReason.R_REJ_NOT_PLANNABLE)
            else:
                return self._decision(
                    intent,
                    reason=ResolverReason.R_RUNBOOK_MATCH,
                    rejected=rejected,
                    deterministic_nodes=deterministic_nodes,
                )
        else:
            rejected.append(ResolverReason.R_REJ_NO_RUNBOOK)

        # S7 — DAG: typed dependencies, acyclic, within budget.
        if intent.node_count > 1 and not intent.residual_subintent:
            if _has_cycle(intent):
                rejected.append(ResolverReason.R_REJ_CYCLE_DETECTED)
            elif not intent.fully_bound:
                rejected.append(ResolverReason.R_REJ_DIRECT_UNBOUND)
            elif (
                intent.node_count > self._budget.dag_max_nodes
                or _depth(intent) > self._budget.dag_max_depth
            ):
                return self._decision(
                    intent, reason=ResolverReason.E_BUDGET_NODES, rejected=rejected
                )
            elif deterministic_nodes != intent.node_count:
                rejected.append(ResolverReason.R_REJ_DIRECT_NOT_READY)
            else:
                return self._decision(
                    intent,
                    reason=ResolverReason.R_DAG_TYPED_PLAN,
                    rejected=rejected,
                    deterministic_nodes=deterministic_nodes,
                )
        else:
            rejected.append(ResolverReason.R_REJ_NOT_PLANNABLE)

        # S8 — agentic gate. Every condition must hold; absence is a refusal.
        # (a) explicit allowance, (b) budget, (c) approval for write/T3,
        # (d) policy permission, (e) context shaping.
        if not (intent.agentic_allowance and self._budget.allows_agentic):
            return self._decision(
                intent, reason=ResolverReason.E_AGENTIC_NOT_ALLOWED, rejected=rejected
            )
        if intent.escalation_count >= self._budget.max_escalations_per_request:
            return self._decision(
                intent, reason=ResolverReason.E_AGENTIC_BUDGET_EXHAUSTED, rejected=rejected
            )
        needs_approval = any(
            operation.capability_id in self._write_capabilities
            for operation in intent.operations
        )
        if needs_approval and not intent.approval_ref:
            return self._decision(
                intent, reason=ResolverReason.E_AGENTIC_APPROVAL_MISSING, rejected=rejected
            )
        if not policy_allows_agentic:
            return self._decision(
                intent, reason=ResolverReason.E_AGENTIC_POLICY_FORBIDDEN, rejected=rejected
            )
        if not context_shaping_ok:
            return self._decision(
                intent,
                reason=ResolverReason.E_AGENTIC_CONTEXT_SHAPING_FAILED,
                rejected=rejected,
            )

        # Precise agentic cause, in specificity order.
        if intent.no_contract_coverage:
            reason = ResolverReason.R_AGENTIC_NO_CONTRACT_COVERAGE
        elif intent.unknown_target:
            reason = ResolverReason.R_AGENTIC_UNKNOWN_TARGET
        elif not intent.fully_bound:
            reason = ResolverReason.R_AGENTIC_UNBOUND_ARGUMENT
        elif intent.residual_subintent and deterministic_nodes > 0:
            reason = ResolverReason.R_AGENTIC_RESIDUAL_SUBINTENT
        else:
            reason = ResolverReason.R_AGENTIC_AMBIGUOUS_INTENT
        return self._decision(
            intent,
            reason=reason,
            rejected=rejected,
            deterministic_nodes=deterministic_nodes,
            agentic_tokens=self._budget.agentic_token_budget,
        )


def _has_cycle(intent: ResolverIntent) -> bool:
    graph = {
        operation.operation_ref: tuple(operation.depends_on)
        for operation in intent.operations
    }
    state: dict[str, int] = {}

    def visit(node: str) -> bool:
        mark = state.get(node, 0)
        if mark == 1:
            return True
        if mark == 2:
            return False
        state[node] = 1
        for parent in graph.get(node, ()):
            if parent in graph and visit(parent):
                return True
        state[node] = 2
        return False

    return any(visit(node) for node in graph)


def _depth(intent: ResolverIntent) -> int:
    graph = {
        operation.operation_ref: tuple(operation.depends_on)
        for operation in intent.operations
    }
    memo: dict[str, int] = {}

    def depth(node: str, seen: frozenset[str]) -> int:
        if node in seen:
            return 0
        if node in memo:
            return memo[node]
        parents = [parent for parent in graph.get(node, ()) if parent in graph]
        value = 1 + max((depth(parent, seen | {node}) for parent in parents), default=0)
        memo[node] = value
        return value

    return max((depth(node, frozenset()) for node in graph), default=0)


def replay_decision(
    resolver: ModeResolver,
    intent: ResolverIntent,
    *,
    policy,
    policy_allows_agentic: bool = True,
    context_shaping_ok: bool = True,
    repetitions: int = 100,
) -> tuple[ResolverDecision, int]:
    """Replay one scenario ``repetitions`` times; return the decision and mismatches.

    A mismatch is a hard failure (`E-RESOLVER-NONDETERMINISM`), never a warning:
    the caller is expected to refuse the run.
    """
    first = resolver.resolve(
        intent,
        policy=policy,
        policy_allows_agentic=policy_allows_agentic,
        context_shaping_ok=context_shaping_ok,
    )
    reference = first.canonical_json()
    mismatches = 0
    for _ in range(max(0, repetitions - 1)):
        candidate = resolver.resolve(
            intent,
            policy=policy,
            policy_allows_agentic=policy_allows_agentic,
            context_shaping_ok=context_shaping_ok,
        )
        if candidate.canonical_json() != reference:
            mismatches += 1
    return first, mismatches


def preference_index(mode: ExecutionMode) -> int:
    """Position in the permanent preference order; lower is preferred."""
    return MODE_PREFERENCE.index(mode)


__all__ = ["ModeResolver", "preference_index", "replay_decision"]
