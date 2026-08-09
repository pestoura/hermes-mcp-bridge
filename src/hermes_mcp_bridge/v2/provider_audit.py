"""Phase 7 integration audit: exactly one terminal record per outcome.

> **V2 · PHASE 7 · runtime, disabled by default behind ``PROVIDER_FEATURE_ENABLED``**

Obligations enforced here:

* **write-ahead ordering** — for a mutating capability the *intent* record is
  appended before any side effect, the *terminal* record after;
* **exactly one terminal record per terminal outcome**, refusals included, so
  completeness is ``terminal_records == terminal_outcomes``;
* **append-only with a chained digest** — each record carries the digest of the
  previous one, so deletion or reordering is detectable;
* **structural redaction** — a record whose canonical form contains a
  secret-shaped identifier is refused before it reaches the sink.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum, unique
from typing import Any, Protocol

from .canonical import canonical_hash, canonical_json_text
from .provider_contract import ProviderReason, audit_safe

#: Genesis link of the per-window digest chain.
CHAIN_GENESIS = "0" * 64


@unique
class AuditKind(StrEnum):
    INTENT = "INTENT"
    TERMINAL = "TERMINAL"


@unique
class OutcomeClass(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    REFUSED = "refused"
    ERROR = "error"
    UNKNOWN = "unknown"

    @property
    def is_terminal(self) -> bool:
        return True

    @property
    def requires_manual_resolution(self) -> bool:
        return self is OutcomeClass.UNKNOWN


class AuditError(RuntimeError):
    """The audit obligation could not be met; the caller must fail closed."""

    def __init__(self, reason: ProviderReason, subject: str) -> None:
        self.reason = reason
        self.subject = subject
        super().__init__(f"{reason.value}:{subject}")


class AuditSink(Protocol):
    """Append-only sink. ``append`` must be durable before it returns."""

    def append(self, record_id: str, payload: Mapping[str, Any]) -> None: ...

    def exists(self, record_id: str) -> bool: ...


class MemoryAuditSink:
    """In-memory append-only sink (tests and hermetic acceptance runs)."""

    __slots__ = ("_available", "_records", "_order")

    def __init__(self) -> None:
        self._records: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        self._available = True

    def set_available(self, available: bool) -> None:
        self._available = bool(available)

    def append(self, record_id: str, payload: Mapping[str, Any]) -> None:
        if not self._available:
            raise AuditError(ProviderReason.E_AUDIT_UNAVAILABLE, record_id)
        if record_id in self._records:
            raise AuditError(ProviderReason.E_AUDIT_UNAVAILABLE, record_id)
        self._records[record_id] = dict(payload)
        self._order.append(record_id)

    def exists(self, record_id: str) -> bool:
        return record_id in self._records

    @property
    def records(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._records[record_id] for record_id in self._order)


@dataclass(frozen=True, slots=True)
class IntegrationAuditRecord:
    """One audit record. Every field is bounded, enumerated or a digest."""

    record_id: str
    kind: AuditKind
    request_id: str
    principal_ref: str
    provider_id: str
    capability_id: str
    tool_id: str
    mode: str
    target_scope_ref: str
    mutation_class: str
    security_tier: str
    policy_decision: str
    reason_code: ProviderReason
    readiness_state: str
    credential_capability_id: str
    scope_set_digest: str
    outcome: OutcomeClass
    provider_call_count: int = 0
    byte_count: int = 0
    duration_ms: int = 0
    idempotency_key_digest: str = ""
    operation_digest: str = ""
    approval_ref: str = ""
    prev_digest: str = CHAIN_GENESIS
    extra: Mapping[str, Any] = field(default_factory=dict)

    def canonical(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "approval_ref": self.approval_ref,
            "byte_count": self.byte_count,
            "capability_id": self.capability_id,
            "credential_capability_id": self.credential_capability_id,
            "duration_ms": self.duration_ms,
            "idempotency_key_digest": self.idempotency_key_digest,
            "kind": self.kind.value,
            "mode": self.mode,
            "mutation_class": self.mutation_class,
            "operation_digest": self.operation_digest,
            "outcome": self.outcome.value,
            "policy_decision": self.policy_decision,
            "prev_digest": self.prev_digest,
            "principal_ref": self.principal_ref,
            "provider_call_count": self.provider_call_count,
            "provider_id": self.provider_id,
            "readiness_state": self.readiness_state,
            "reason_code": self.reason_code.value,
            "record_id": self.record_id,
            "request_id": self.request_id,
            "scope_set_digest": self.scope_set_digest,
            "security_tier": self.security_tier,
            "target_scope_ref": self.target_scope_ref,
            "tool_id": self.tool_id,
        }
        if self.extra:
            payload["extra"] = dict(self.extra)
        return payload

    def digest(self) -> str:
        return canonical_hash(self.canonical())

    def canonical_json(self) -> str:
        return canonical_json_text(self.canonical())


class IntegrationAuditLedger:
    """Chained, append-only ledger with structural redaction enforcement."""

    __slots__ = ("_head", "_sink", "_terminal_ids", "_intent_ids", "_digests")

    def __init__(self, sink: AuditSink) -> None:
        self._sink = sink
        self._head = CHAIN_GENESIS
        self._terminal_ids: set[str] = set()
        self._intent_ids: set[str] = set()
        self._digests: list[str] = []

    @property
    def head(self) -> str:
        return self._head

    @property
    def chain(self) -> tuple[str, ...]:
        return tuple(self._digests)

    @property
    def terminal_count(self) -> int:
        return len(self._terminal_ids)

    def append(self, record: IntegrationAuditRecord) -> IntegrationAuditRecord:
        if record.kind is AuditKind.TERMINAL and record.request_id in self._terminal_ids:
            # Exactly one terminal record per terminal outcome.
            raise AuditError(ProviderReason.E_AUDIT_UNAVAILABLE, record.request_id)
        if record.kind is AuditKind.INTENT and record.request_id in self._intent_ids:
            raise AuditError(ProviderReason.E_AUDIT_UNAVAILABLE, record.request_id)

        linked = IntegrationAuditRecord(
            **{
                **{
                    slot: getattr(record, slot)
                    for slot in record.__dataclass_fields__  # type: ignore[attr-defined]
                },
                "prev_digest": self._head,
            }
        )
        payload = linked.canonical()
        findings = audit_safe(payload)
        if findings:
            raise AuditError(ProviderReason.E_AUDIT_UNAVAILABLE, "redaction")
        self._sink.append(linked.record_id, payload)
        self._head = linked.digest()
        self._digests.append(self._head)
        if linked.kind is AuditKind.TERMINAL:
            self._terminal_ids.add(linked.request_id)
        else:
            self._intent_ids.add(linked.request_id)
        return linked

    def has_intent(self, request_id: str) -> bool:
        return request_id in self._intent_ids

    def has_terminal(self, request_id: str) -> bool:
        return request_id in self._terminal_ids

    def verify_chain(self, records: tuple[Mapping[str, Any], ...]) -> bool:
        """Recompute the chain from a retained corpus; detect deletion/reorder."""
        previous = CHAIN_GENESIS
        for payload in records:
            if payload.get("prev_digest") != previous:
                return False
            previous = canonical_hash(dict(payload))
        return previous == self._head


def completeness(*, terminal_records: int, terminal_outcomes: int) -> float:
    """Independent reconciliation ratio. 1.0 means complete."""
    if terminal_outcomes <= 0:
        return 1.0
    return terminal_records / terminal_outcomes


__all__ = [
    "CHAIN_GENESIS",
    "AuditError",
    "AuditKind",
    "AuditSink",
    "IntegrationAuditLedger",
    "IntegrationAuditRecord",
    "MemoryAuditSink",
    "OutcomeClass",
    "completeness",
]
