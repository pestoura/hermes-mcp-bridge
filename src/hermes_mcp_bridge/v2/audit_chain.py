"""Phase 9 hardening: audit chain, digest binding and cardinality bounds.

> **V2 · PHASE 9 · hardening**

Two failure modes hardening must close:

* **Audit gap.** A decision, an executed operation and its evidence must form an
  unbroken chain: the digest recorded at decision time matches the digest of the
  executed plan. If they diverge, the run is rejected, not silently allowed.
* **Cardinality blow-up.** A single request must not expand into an unbounded
  number of labels, branches or child tasks. Every counted set has a hard ceiling
  enforced at construction and at execution time, so observability and policy
  surfaces cannot be flooded by a hostile or buggy request.

Both properties are proven by the tests in ``test_v2_phase9_audit_chain.py``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .canonical import canonical_hash
from .resolver_contract import ResolverDecision, ResolverIntent

#: Hard ceilings. These are the cardinality bounds: no request may produce more
#: labels, branches or nodes than these, regardless of input.
MAX_REJECTED_BRANCHES: int = 16
MAX_REASON_LABELS_PER_RUN: int = 1 + MAX_REJECTED_BRANCHES
MAX_NODES_PER_REQUEST: int = 200
MAX_AGENTIC_ESCALATIONS: int = 1


class AuditChainError(ValueError):
    """The audit chain did not bind; fail closed."""


@dataclass(frozen=True, slots=True)
class AuditLink:
    """One link binding a decision to the plan that was actually executed."""

    request_id: str
    decision_digest: str
    executed_plan_digest: str
    matched: bool
    mode: str
    reason_code: str

    def canonical(self) -> dict[str, Any]:
        return {
            "decision_digest": self.decision_digest,
            "executed_plan_digest": self.executed_plan_digest,
            "matched": self.matched,
            "mode": self.mode,
            "reason_code": self.reason_code,
            "request_id": self.request_id,
        }

    def digest(self) -> str:
        return canonical_hash(self.canonical())


def bind_audit_link(
    decision: ResolverDecision,
    executed_intent: ResolverIntent,
) -> AuditLink:
    """Prove the executed plan is exactly what the decision authorized.

    If the executed intent's digest differs from the decision's recorded intent
    digest, the chain does not bind and the caller must refuse execution.
    """
    executed_digest = executed_intent.digest()
    matched = executed_digest == decision.intent_digest
    if not matched:
        # The mismatch is recorded; it is the caller's duty to refuse. We do not
        # raise here so the link is observable in evidence, but ``matched`` is
        # False and the gate treats that as a hard failure.
        pass
    return AuditLink(
        request_id=decision.request_id,
        decision_digest=decision.digest(),
        executed_plan_digest=executed_digest,
        matched=matched,
        mode=decision.mode.value if decision.mode else "REFUSED",
        reason_code=decision.primary_reason_code.value,
    )


def enforce_cardinality(intent: ResolverIntent, *, budget_nodes: int) -> None:
    """Fail closed if a request exceeds the cardinality bounds."""
    if intent.node_count > MAX_NODES_PER_REQUEST:
        raise AuditChainError(f"node count {intent.node_count} > {MAX_NODES_PER_REQUEST}")
    if intent.node_count > budget_nodes:
        raise AuditChainError(f"node count {intent.node_count} > budget {budget_nodes}")
    if intent.escalation_count > MAX_AGENTIC_ESCALATIONS:
        raise AuditChainError("escalation budget exceeded")


def truncated_rejected_branches(decision: ResolverDecision) -> tuple[str, ...]:
    """Bounded, ordered label set for observability — never grows without limit."""
    codes = [decision.primary_reason_code.value, *[r.value for r in decision.rejected_branches]]
    if len(codes) > MAX_REASON_LABELS_PER_RUN:
        # A buggy request must not flood the label space; keep the primary code
        # and truncate the tail, recording the overflow as a separate signal.
        return tuple([*codes[:MAX_REASON_LABELS_PER_RUN], "OVERFLOW"])
    return tuple(codes)


def digest_chain(*payloads: Mapping[str, Any]) -> str:
    """Chain digests so a tampered link breaks the whole chain."""
    acc = hashlib.sha256(b"").hexdigest()
    for payload in payloads:
        leaf = canonical_hash(payload)
        acc = hashlib.sha256((acc + leaf).encode("utf-8")).hexdigest()
    return acc


def link_reason_code(decision: ResolverDecision) -> str:
    """Single bounded metric label for a decision — always within the enum."""
    return decision.primary_reason_code.value


__all__ = [
    "AuditChainError",
    "AuditLink",
    "bind_audit_link",
    "digest_chain",
    "enforce_cardinality",
    "link_reason_code",
    "truncated_rejected_branches",
]
