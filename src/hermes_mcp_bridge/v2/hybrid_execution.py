"""Phase 8 HYBRID execution: deterministic first, escalate only the residual.

> **V2 · PHASE 8 · runtime, disabled by default behind ``HYBRID_FEATURE_ENABLED``**

The structural guarantee of this module is invariant **I7**: a reasoning step
cannot reach a provider. It is enforced by types, not by discipline — an agentic
step returns an :class:`AgenticProposal`, which is a *typed plan*, and the only
thing the coordinator can do with a proposal is re-enter the resolver at S0 with
a decremented escalation budget. The reasoning callable never receives the
gateway, the credential broker, the registry or a provider adapter, so there is
no object on which a provider call could be made.

Everything already obtained deterministically is kept: escalation covers the
residual sub-intent only, and the recorded ``deterministic_coverage`` is the
ratio actually executed deterministically.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .canonical import canonical_hash
from .enums import ExecutionMode, PolicyDecision
from .resolver import ModeResolver
from .resolver_contract import (
    IntentOperation,
    ResolverBudget,
    ResolverDecision,
    ResolverIntent,
    ResolverReason,
)


@dataclass(frozen=True, slots=True)
class AgenticContext:
    """The *only* thing a reasoning step sees. Shaped to a minimum.

    Carries capability ids, target refs and a free-text intent summary — never a
    credential, never a provider body, never a raw prompt, never a header.
    """

    request_id: str
    intent_summary: str
    available_capability_ids: tuple[str, ...]
    target_scope_refs: tuple[str, ...]
    escalations_remaining: int
    token_budget: int

    def digest(self) -> str:
        return canonical_hash(
            {
                "available_capability_ids": list(self.available_capability_ids),
                "escalations_remaining": self.escalations_remaining,
                "intent_summary": self.intent_summary,
                "request_id": self.request_id,
                "target_scope_refs": list(self.target_scope_refs),
                "token_budget": self.token_budget,
            }
        )


@dataclass(frozen=True, slots=True)
class AgenticProposal:
    """A typed plan proposed by a reasoning step. Not an execution.

    It is deliberately impossible to express a provider call here: the proposal
    is a list of :class:`IntentOperation`, which the resolver must accept before
    anything runs.
    """

    operations: tuple[IntentOperation, ...]
    tokens_used: int = 0
    #: Set when the reasoning step concluded nothing typed is expressible.
    abandoned: bool = False


#: Signature of the reasoning step. It receives a shaped context and returns a
#: typed proposal. No gateway, no broker, no registry, no adapter.
AgenticStep = Callable[[AgenticContext], AgenticProposal]

#: Signature of the deterministic executor the coordinator drives. Returns the
#: number of operations actually executed.
DeterministicExecutor = Callable[[ResolverDecision, Sequence[IntentOperation]], int]


class ContextShapingError(RuntimeError):
    """Minimum-context shaping could not exclude sensitive material."""


def shape_context(
    intent: ResolverIntent,
    *,
    budget: ResolverBudget,
    intent_summary: str,
    forbidden: Mapping[str, Any] | None = None,
) -> AgenticContext:
    """Build the shaped context, refusing rather than best-effort sending.

    ``forbidden`` is any material the caller knows must not travel (secret refs,
    raw bodies). If shaping cannot exclude it — because the summary itself
    contains it — this raises and the coordinator refuses with
    ``E-AGENTIC-CONTEXT-SHAPING-FAILED``.
    """
    from .provider_contract import audit_safe

    summary = str(intent_summary)
    if forbidden:
        for value in forbidden.values():
            if isinstance(value, str) and value and value in summary:
                raise ContextShapingError("summary carries forbidden material")
    context = AgenticContext(
        request_id=intent.request_id,
        intent_summary=summary,
        available_capability_ids=tuple(
            sorted({operation.capability_id for operation in intent.operations})
        ),
        target_scope_refs=tuple(
            sorted({operation.target_scope_ref for operation in intent.operations})
        ),
        escalations_remaining=max(
            0, budget.max_escalations_per_request - intent.escalation_count
        ),
        token_budget=budget.agentic_token_budget,
    )
    if audit_safe(
        {
            "intent_summary": context.intent_summary,
            "capability_ids": list(context.available_capability_ids),
            "targets": list(context.target_scope_refs),
        }
    ):
        raise ContextShapingError("shaped context contains secret-shaped material")
    return context


@dataclass(frozen=True, slots=True)
class HybridOutcome:
    """Terminal result of one coordinated HYBRID request."""

    request_id: str
    decisions: tuple[ResolverDecision, ...]
    final_decision: ResolverDecision
    deterministic_nodes_executed: int
    total_nodes: int
    agentic_tokens_used: int
    escalations: int
    partial: bool = False
    provider_calls_from_agentic_layer: int = 0
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def mode(self) -> ExecutionMode | None:
        return self.final_decision.mode

    @property
    def deterministic_coverage(self) -> float:
        if self.total_nodes <= 0:
            return 0.0
        return self.deterministic_nodes_executed / self.total_nodes

    @property
    def reason_codes(self) -> tuple[ResolverReason, ...]:
        return tuple(decision.primary_reason_code for decision in self.decisions)


class HybridCoordinator:
    """Drives resolve → deterministic execution → bounded escalation → resolve."""

    __slots__ = ("_agentic_step", "_executor", "_resolver")

    def __init__(
        self,
        *,
        resolver: ModeResolver,
        executor: DeterministicExecutor,
        agentic_step: AgenticStep | None = None,
    ) -> None:
        self._resolver = resolver
        self._executor = executor
        self._agentic_step = agentic_step

    def run(
        self,
        intent: ResolverIntent,
        *,
        policy: PolicyDecision,
        policy_allows_agentic: bool = True,
        intent_summary: str = "",
        forbidden: Mapping[str, Any] | None = None,
    ) -> HybridOutcome:
        budget = self._resolver.budget
        decisions: list[ResolverDecision] = []
        executed = 0
        tokens = 0
        escalations = 0
        current = intent
        partial = False

        while True:
            shaping_ok = True
            context: AgenticContext | None = None
            if current.agentic_allowance and budget.allows_agentic:
                try:
                    context = shape_context(
                        current,
                        budget=budget,
                        intent_summary=intent_summary,
                        forbidden=forbidden,
                    )
                except ContextShapingError:
                    shaping_ok = False

            decision = self._resolver.resolve(
                current,
                policy=policy,
                policy_allows_agentic=policy_allows_agentic,
                context_shaping_ok=shaping_ok,
            )
            decisions.append(decision)

            if decision.mode is not None and decision.mode is not ExecutionMode.AGENTIC:
                executed += self._executor(decision, current.operations)
                return HybridOutcome(
                    request_id=intent.request_id,
                    decisions=tuple(decisions),
                    final_decision=decision,
                    deterministic_nodes_executed=executed,
                    total_nodes=intent.node_count,
                    agentic_tokens_used=tokens,
                    escalations=escalations,
                    partial=partial,
                )

            if decision.refused:
                return HybridOutcome(
                    request_id=intent.request_id,
                    decisions=tuple(decisions),
                    final_decision=decision,
                    deterministic_nodes_executed=executed,
                    total_nodes=intent.node_count,
                    agentic_tokens_used=tokens,
                    escalations=escalations,
                    partial=partial or executed > 0,
                )

            # AGENTIC selected. Execute the deterministic segment first, then
            # escalate only the residual.
            deterministic_part = tuple(
                operation for operation in current.operations if operation.fully_bound
            )
            if deterministic_part and decision.deterministic_nodes > 0:
                executed += self._executor(decision, deterministic_part)

            if self._agentic_step is None or context is None:
                return HybridOutcome(
                    request_id=intent.request_id,
                    decisions=tuple(decisions),
                    final_decision=decision,
                    deterministic_nodes_executed=executed,
                    total_nodes=intent.node_count,
                    agentic_tokens_used=tokens,
                    escalations=escalations,
                    partial=True,
                )

            proposal = self._agentic_step(context)
            tokens += max(0, int(proposal.tokens_used))
            escalations += 1

            if tokens > budget.agentic_token_budget:
                exhausted = ResolverDecision(
                    request_id=current.request_id,
                    mode=None,
                    primary_reason_code=ResolverReason.E_AGENTIC_BUDGET_EXHAUSTED,
                    rejected_branches=decision.rejected_branches,
                    deterministic_nodes=executed,
                    total_nodes=intent.node_count,
                    escalation_count=escalations,
                    intent_digest=current.digest(),
                    budget_digest=budget.digest(),
                    snapshot_digest=decision.snapshot_digest,
                )
                decisions.append(exhausted)
                return HybridOutcome(
                    request_id=intent.request_id,
                    decisions=tuple(decisions),
                    final_decision=exhausted,
                    deterministic_nodes_executed=executed,
                    total_nodes=intent.node_count,
                    agentic_tokens_used=tokens,
                    escalations=escalations,
                    partial=True,
                )

            if proposal.abandoned or not proposal.operations:
                return HybridOutcome(
                    request_id=intent.request_id,
                    decisions=tuple(decisions),
                    final_decision=decision,
                    deterministic_nodes_executed=executed,
                    total_nodes=intent.node_count,
                    agentic_tokens_used=tokens,
                    escalations=escalations,
                    partial=True,
                )

            # Re-enter the tree at S0 with a decremented budget. The proposal is
            # a plan, never an execution: safety controls apply again in full.
            partial = True
            current = ResolverIntent(
                request_id=current.request_id,
                principal_ref=current.principal_ref,
                operations=proposal.operations,
                residual_subintent=False,
                runbook_ref=current.runbook_ref,
                runbook_version_pinned=current.runbook_version_pinned,
                approval_ref=current.approval_ref,
                agentic_allowance=current.agentic_allowance,
                escalation_count=current.escalation_count + 1,
            )


__all__ = [
    "AgenticContext",
    "AgenticProposal",
    "AgenticStep",
    "ContextShapingError",
    "DeterministicExecutor",
    "HybridCoordinator",
    "HybridOutcome",
    "shape_context",
]
