"""Phase 8 resolver contract: closed reason codes, budgets and the decision record.

> **V2 · PHASE 8 · runtime, disabled by default behind ``HYBRID_FEATURE_ENABLED``**

The resolver is a *pure total function*. Everything it can conclude is named
here, so the acceptance suite can assert that the set of reachable outcomes is
closed: there is no "other", no free-text reason and no default branch that
falls through to a mode.

Three vocabularies, deliberately disjoint by prefix:

* ``R-`` — a mode was selected;
* ``R-REJ-`` — a branch was considered and rejected (ordered, recorded);
* ``E-`` — a terminal refusal.

``ResolverReason`` is a single enumeration over all three so that a decision
record's ``primary_reason_code`` is always drawn from one closed set and is
always safe as a bounded metric label.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum, unique
from typing import Any, Final

from .canonical import canonical_hash, canonical_json_text
from .enums import ExecutionMode

#: Phase 8 ships disabled. Nothing in this lane is reachable from the V1
#: surface until an operator opts in, and the acceptance gate asserts the
#: default is ``False``.
HYBRID_FEATURE_ENABLED: Final[bool] = False

#: Contract version of the resolver decision record.
RESOLVER_CONTRACT_VERSION: Final[str] = "1"


@unique
class ResolverReason(StrEnum):
    """The complete, closed set of resolver outcomes."""

    # -- mode selection ---------------------------------------------------
    R_DIRECT_EXACT = "R-DIRECT-EXACT"
    R_RUNBOOK_MATCH = "R-RUNBOOK-MATCH"
    R_BATCH_INDEPENDENT = "R-BATCH-INDEPENDENT"
    R_DAG_TYPED_PLAN = "R-DAG-TYPED-PLAN"
    R_AGENTIC_AMBIGUOUS_INTENT = "R-AGENTIC-AMBIGUOUS-INTENT"
    R_AGENTIC_UNBOUND_ARGUMENT = "R-AGENTIC-UNBOUND-ARGUMENT"
    R_AGENTIC_UNKNOWN_TARGET = "R-AGENTIC-UNKNOWN-TARGET"
    R_AGENTIC_NO_CONTRACT_COVERAGE = "R-AGENTIC-NO-CONTRACT-COVERAGE"
    R_AGENTIC_RESIDUAL_SUBINTENT = "R-AGENTIC-RESIDUAL-SUBINTENT"

    # -- rejected branches ------------------------------------------------
    R_REJ_DIRECT_NOT_READY = "R-REJ-DIRECT-NOT-READY"
    R_REJ_DIRECT_MULTI_TOOL = "R-REJ-DIRECT-MULTI-TOOL"
    R_REJ_DIRECT_UNBOUND = "R-REJ-DIRECT-UNBOUND"
    R_REJ_NO_RUNBOOK = "R-REJ-NO-RUNBOOK"
    R_REJ_RUNBOOK_VERSION_UNPINNED = "R-REJ-RUNBOOK-VERSION-UNPINNED"
    R_REJ_NOT_INDEPENDENT = "R-REJ-NOT-INDEPENDENT"
    R_REJ_NOT_PLANNABLE = "R-REJ-NOT-PLANNABLE"
    R_REJ_CYCLE_DETECTED = "R-REJ-CYCLE-DETECTED"

    # -- refusals ---------------------------------------------------------
    E_REQ_INVALID = "E-REQ-INVALID"
    E_POLICY_DENY = "E-POLICY-DENY"
    E_BUDGET_NODES = "E-BUDGET-NODES"
    E_BUDGET_TOKENS = "E-BUDGET-TOKENS"
    E_BUDGET_DEADLINE = "E-BUDGET-DEADLINE"
    E_AGENTIC_NOT_ALLOWED = "E-AGENTIC-NOT-ALLOWED"
    E_AGENTIC_BUDGET_EXHAUSTED = "E-AGENTIC-BUDGET-EXHAUSTED"
    E_AGENTIC_APPROVAL_MISSING = "E-AGENTIC-APPROVAL-MISSING"
    E_AGENTIC_POLICY_FORBIDDEN = "E-AGENTIC-POLICY-FORBIDDEN"
    E_AGENTIC_CONTEXT_SHAPING_FAILED = "E-AGENTIC-CONTEXT-SHAPING-FAILED"
    E_SAFETY_DOWNGRADE_REFUSED = "E-SAFETY-DOWNGRADE-REFUSED"
    E_RESOLVER_NONDETERMINISM = "E-RESOLVER-NONDETERMINISM"

    @property
    def is_mode_selection(self) -> bool:
        return self.value.startswith("R-") and not self.value.startswith("R-REJ-")

    @property
    def is_rejection(self) -> bool:
        return self.value.startswith("R-REJ-")

    @property
    def is_refusal(self) -> bool:
        return self.value.startswith("E-")

    @property
    def selects_agentic(self) -> bool:
        return self.is_mode_selection and "AGENTIC" in self.value


#: Every code, as a frozen label set. Metric cardinality is bounded by
#: construction: a label value outside this set cannot exist.
REASON_LABEL_SET: Final[frozenset[str]] = frozenset(
    reason.value for reason in ResolverReason
)

#: Mode-selection codes mapped to the mode they select. A resolver result whose
#: primary code is a mode selection must agree with this table — asserted.
MODE_FOR_REASON: Final[Mapping[ResolverReason, ExecutionMode]] = {
    ResolverReason.R_DIRECT_EXACT: ExecutionMode.DIRECT,
    ResolverReason.R_RUNBOOK_MATCH: ExecutionMode.RUNBOOK,
    ResolverReason.R_BATCH_INDEPENDENT: ExecutionMode.BATCH,
    ResolverReason.R_DAG_TYPED_PLAN: ExecutionMode.DAG,
    ResolverReason.R_AGENTIC_AMBIGUOUS_INTENT: ExecutionMode.AGENTIC,
    ResolverReason.R_AGENTIC_UNBOUND_ARGUMENT: ExecutionMode.AGENTIC,
    ResolverReason.R_AGENTIC_UNKNOWN_TARGET: ExecutionMode.AGENTIC,
    ResolverReason.R_AGENTIC_NO_CONTRACT_COVERAGE: ExecutionMode.AGENTIC,
    ResolverReason.R_AGENTIC_RESIDUAL_SUBINTENT: ExecutionMode.AGENTIC,
}

#: Preference order, permanently. The resolver walks this order and the gate
#: asserts the walk matches it, so a future edit that quietly promotes AGENTIC
#: fails the gate rather than shipping.
MODE_PREFERENCE: Final[tuple[ExecutionMode, ...]] = (
    ExecutionMode.DIRECT,
    ExecutionMode.BATCH,
    ExecutionMode.DAG,
    ExecutionMode.RUNBOOK,
    ExecutionMode.AGENTIC,
)


class ResolverContractError(ValueError):
    """A resolver input violated the contract; the caller must fail closed."""

    def __init__(self, reason: ResolverReason, subject: str = "") -> None:
        self.reason = reason
        self.subject = subject
        super().__init__(reason.value if not subject else f"{reason.value}:{subject}")


@dataclass(frozen=True, slots=True)
class ResolverBudget:
    """Declared bounds. Absent allowance is a refusal, never an implicit grant."""

    batch_max_nodes: int = 50
    dag_max_nodes: int = 200
    dag_max_depth: int = 12
    max_escalations_per_request: int = 1
    #: Zero by default. This is the whole zero-default-agentic property.
    agentic_token_budget: int = 0
    agentic_deadline_s: int = 120
    direct_deadline_s: int = 10
    #: Expressed in permille so the canonical form stays float-free: the
    #: canonical encoder rejects floats precisely to keep digests stable.
    min_deterministic_coverage_permille: int = 500

    def __post_init__(self) -> None:
        for name in (
            "batch_max_nodes",
            "dag_max_nodes",
            "dag_max_depth",
            "max_escalations_per_request",
            "agentic_token_budget",
            "agentic_deadline_s",
            "direct_deadline_s",
            "min_deterministic_coverage_permille",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ResolverContractError(ResolverReason.E_REQ_INVALID, name)
        if not 0 <= self.min_deterministic_coverage_permille <= 1000:
            raise ResolverContractError(
                ResolverReason.E_REQ_INVALID, "min_deterministic_coverage_permille"
            )

    @property
    def min_deterministic_coverage(self) -> float:
        """Convenience view. Never enters a canonical form or a digest."""
        return self.min_deterministic_coverage_permille / 1000

    @property
    def allows_agentic(self) -> bool:
        """An allowance exists only when a positive token budget was declared."""
        return self.agentic_token_budget > 0

    def canonical(self) -> dict[str, Any]:
        return {
            "agentic_deadline_s": self.agentic_deadline_s,
            "agentic_token_budget": self.agentic_token_budget,
            "batch_max_nodes": self.batch_max_nodes,
            "dag_max_depth": self.dag_max_depth,
            "dag_max_nodes": self.dag_max_nodes,
            "direct_deadline_s": self.direct_deadline_s,
            "max_escalations_per_request": self.max_escalations_per_request,
            "min_deterministic_coverage_permille": self.min_deterministic_coverage_permille,
        }

    def digest(self) -> str:
        return canonical_hash(self.canonical())


_IDENT = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")


def _identifier(value: str, *, field_name: str) -> str:
    text = str(value).strip().lower()
    if not _IDENT.fullmatch(text):
        raise ResolverContractError(ResolverReason.E_REQ_INVALID, field_name)
    return text


@dataclass(frozen=True, slots=True)
class IntentOperation:
    """One typed, concretely bound operation inside an intent."""

    capability_id: str
    target_scope_ref: str
    #: ``True`` only when every required argument is concretely bound.
    fully_bound: bool = True
    #: Ordered refs of operations this one consumes output from.
    depends_on: tuple[str, ...] = ()
    operation_ref: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "capability_id", _identifier(self.capability_id, field_name="capability_id")
        )
        if not str(self.target_scope_ref).strip():
            raise ResolverContractError(ResolverReason.E_REQ_INVALID, "target_scope_ref")
        if not self.operation_ref:
            object.__setattr__(self, "operation_ref", self.capability_id)

    def canonical(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "depends_on": list(self.depends_on),
            "fully_bound": self.fully_bound,
            "operation_ref": self.operation_ref,
            "target_scope_ref": self.target_scope_ref,
        }


@dataclass(frozen=True, slots=True)
class ResolverIntent:
    """The typed request the resolver decides on.

    ``residual_subintent`` marks the part of the request that no typed contract
    covers. Its presence is what makes a request *hybrid*: the deterministic
    operations still execute deterministically and only the residual is eligible
    for escalation.
    """

    request_id: str
    principal_ref: str
    operations: tuple[IntentOperation, ...] = ()
    residual_subintent: bool = False
    #: Set when the caller could not name a target set concretely.
    unknown_target: bool = False
    #: Set when the intent matches no registered typed contract at all.
    no_contract_coverage: bool = False
    runbook_ref: str = ""
    runbook_version_pinned: bool = True
    approval_ref: str = ""
    #: Opt-in. Combined with a positive token budget to permit escalation.
    agentic_allowance: bool = False
    escalation_count: int = 0

    def __post_init__(self) -> None:
        if not str(self.request_id).strip():
            raise ResolverContractError(ResolverReason.E_REQ_INVALID, "request_id")
        if not str(self.principal_ref).strip():
            raise ResolverContractError(ResolverReason.E_REQ_INVALID, "principal_ref")
        refs = [operation.operation_ref for operation in self.operations]
        if len(refs) != len(set(refs)):
            raise ResolverContractError(ResolverReason.E_REQ_INVALID, "operation_ref")

    @property
    def node_count(self) -> int:
        return len(self.operations)

    @property
    def has_dependencies(self) -> bool:
        return any(operation.depends_on for operation in self.operations)

    @property
    def homogeneous(self) -> bool:
        return len({operation.capability_id for operation in self.operations}) == 1

    @property
    def fully_bound(self) -> bool:
        return all(operation.fully_bound for operation in self.operations)

    def canonical(self) -> dict[str, Any]:
        return {
            "agentic_allowance": self.agentic_allowance,
            "approval_ref": self.approval_ref,
            "escalation_count": self.escalation_count,
            "no_contract_coverage": self.no_contract_coverage,
            "operations": [operation.canonical() for operation in self.operations],
            "principal_ref": self.principal_ref,
            "request_id": self.request_id,
            "residual_subintent": self.residual_subintent,
            "runbook_ref": self.runbook_ref,
            "runbook_version_pinned": self.runbook_version_pinned,
            "unknown_target": self.unknown_target,
        }

    def digest(self) -> str:
        return canonical_hash(self.canonical())


@dataclass(frozen=True, slots=True)
class ResolverDecision:
    """The recorded, replayable outcome of exactly one resolver evaluation."""

    request_id: str
    mode: ExecutionMode | None
    primary_reason_code: ResolverReason
    rejected_branches: tuple[ResolverReason, ...] = ()
    deterministic_nodes: int = 0
    total_nodes: int = 0
    escalation_count: int = 0
    agentic_tokens_authorized: int = 0
    intent_digest: str = ""
    budget_digest: str = ""
    snapshot_digest: str = ""
    contract_version: str = RESOLVER_CONTRACT_VERSION
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.primary_reason_code.is_mode_selection:
            expected = MODE_FOR_REASON[self.primary_reason_code]
            if self.mode is not expected:
                raise ResolverContractError(
                    ResolverReason.E_REQ_INVALID, "mode/reason disagreement"
                )
        elif self.mode is not None:
            raise ResolverContractError(ResolverReason.E_REQ_INVALID, "refusal carries a mode")
        for rejection in self.rejected_branches:
            if not rejection.is_rejection:
                raise ResolverContractError(
                    ResolverReason.E_REQ_INVALID, "non-rejection in rejected_branches"
                )

    @property
    def refused(self) -> bool:
        return self.primary_reason_code.is_refusal

    @property
    def deterministic_coverage_permille(self) -> int:
        """Integer coverage for the canonical record; floats break determinism."""
        if self.total_nodes <= 0:
            return 1000 if not self.refused and self.mode is not ExecutionMode.AGENTIC else 0
        return (self.deterministic_nodes * 1000) // self.total_nodes

    @property
    def deterministic_coverage(self) -> float:
        if self.total_nodes <= 0:
            return 1.0 if not self.refused and self.mode is not ExecutionMode.AGENTIC else 0.0
        return self.deterministic_nodes / self.total_nodes

    def canonical(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "agentic_tokens_authorized": self.agentic_tokens_authorized,
            "budget_digest": self.budget_digest,
            "contract_version": self.contract_version,
            "deterministic_coverage_permille": self.deterministic_coverage_permille,
            "deterministic_nodes": self.deterministic_nodes,
            "escalation_count": self.escalation_count,
            "intent_digest": self.intent_digest,
            "mode": self.mode.value if self.mode is not None else "REFUSED",
            "primary_reason_code": self.primary_reason_code.value,
            "rejected_branches": [reason.value for reason in self.rejected_branches],
            "request_id": self.request_id,
            "snapshot_digest": self.snapshot_digest,
            "total_nodes": self.total_nodes,
        }
        if self.extra:
            payload["extra"] = dict(self.extra)
        return payload

    def canonical_json(self) -> str:
        return canonical_json_text(self.canonical())

    def digest(self) -> str:
        return canonical_hash(self.canonical())


def label_values(reasons: Sequence[ResolverReason]) -> tuple[str, ...]:
    """Bounded metric labels. Membership in the closed set is guaranteed."""
    values = tuple(reason.value for reason in reasons)
    unknown = [value for value in values if value not in REASON_LABEL_SET]
    if unknown:
        raise ResolverContractError(ResolverReason.E_REQ_INVALID, "unknown label")
    return values


__all__ = [
    "HYBRID_FEATURE_ENABLED",
    "MODE_FOR_REASON",
    "MODE_PREFERENCE",
    "REASON_LABEL_SET",
    "RESOLVER_CONTRACT_VERSION",
    "IntentOperation",
    "ResolverBudget",
    "ResolverContractError",
    "ResolverDecision",
    "ResolverIntent",
    "ResolverReason",
    "label_values",
]
