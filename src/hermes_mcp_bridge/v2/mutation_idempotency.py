"""Phase 3 idempotency, replay protection, optimistic concurrency and locks.

Lane L3 (ADR-0022, ``docs/v2/phase3/idempotency-and-concurrency.md``). Like
:mod:`hermes_mcp_bridge.v2.mutation_digest` this module is pure and offline: no
network, no filesystem, no subprocess. It provides the *semantics* lane L5 must
obey; a durable backend may replace the in-memory store only by reproducing
them exactly.

Guarantees
----------

* **Server-derived key.** ``idempotency_key`` is
  ``SHA-256(canonical_json({principal, capability, repository, operation,
  operation_digest, client_key?}))``. A caller-supplied ``client_key`` can only
  *narrow* a record: it is one more field inside the hash and can never replace
  the derived scope, so two principals or two repositories can never share a
  record.
* **Write-ahead.** :meth:`IdempotencyStore.begin` persists the intent and takes
  the lease **before** any provider call, so a crash leaves an ``AMBIGUOUS``
  record rather than a silent gap.
* **Fail closed on replay.** ``COMMITTED`` replays the stored shaped result and
  never issues a second write; ``IN_PROGRESS`` refuses; ``AMBIGUOUS`` demands a
  reconciliation read (``RECONCILIATION_REQUIRED``) and forbids blind retry.
  Only ``FAILED_CLEAN`` allows a new attempt.
* **Optimistic concurrency.** :func:`assert_no_precondition_drift` compares the
  approval-time pinned SHA against the freshly observed one immediately before
  the write and raises :class:`ConcurrencyDriftError` on any difference. An
  unobservable state is drift, not an implicit pass.
* **Typed locks.** :class:`MutationLease` keys on
  ``(repository, operation_family, target)`` and uses the existing V1
  ``LockType`` taxonomy (``INTENT_TO_WRITE`` for creates, ``WRITE_EXCLUSIVE``
  for merges) without importing or altering the V1 lock registry. The lock is
  advisory coordination inside the bridge and never substitutes the
  provider-side precondition.
* **No secrets, no payloads.** Keys are digests; evidence carries identifiers,
  digests and enum tokens only. The stored result is returned to the same
  principal on replay and is never placed in a key or a log line.

Every expiry decision takes an explicit ``now`` from the caller.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any, Final

from .canonical import canonical_hash, canonical_json_text
from .enums import (
    IdempotencyStatus,
    MutationOutcome,
    MutationReasonCode,
    MutationStage,
    WriteCapabilityId,
)
from .errors import (
    ConcurrencyDriftError,
    IdempotencyConflictError,
    MutationDeniedError,
)
from .mutation_digest import require_digest, require_repository, require_sha

#: Envelope version of the idempotency key payload.
IDEMPOTENCY_KEY_SCHEMA: Final[str] = "v2.phase3.idempotency.1"

#: Proposed retention for terminal records (``idempotency-and-concurrency.md``).
TERMINAL_RETENTION_SECONDS: Final[int] = 7 * 24 * 3600

#: Proposed lease duration for an ``IN_PROGRESS`` record. A lease that expires
#: does **not** become reusable: it becomes ``AMBIGUOUS``.
DEFAULT_LEASE_SECONDS: Final[int] = 120

#: V1 lock-type tokens reused verbatim. Imported as literals rather than from
#: ``hermes_mcp_bridge.models`` so the V2 package stays import-isolated from V1.
LOCK_TYPE_INTENT_TO_WRITE: Final[str] = "INTENT_TO_WRITE"
LOCK_TYPE_WRITE_EXCLUSIVE: Final[str] = "WRITE_EXCLUSIVE"

_MERGE_FAMILY: Final[str] = "merge"


def _deny(
    reason: MutationReasonCode,
    stage: MutationStage = MutationStage.IDEMPOTENCY,
    *,
    detail: str = "",
) -> MutationDeniedError:
    return MutationDeniedError(reason, stage, detail=detail)


def _conflict(reason: MutationReasonCode, *, detail: str = "") -> IdempotencyConflictError:
    return IdempotencyConflictError(reason, MutationStage.IDEMPOTENCY, detail=detail)


def compute_idempotency_key(
    *,
    principal: str,
    capability: WriteCapabilityId,
    repository: str,
    operation: str,
    operation_digest: str,
    client_key: str | None = None,
) -> str:
    """Derive the server-side idempotency key.

    ``client_key`` participates in the hash and therefore only narrows the
    record; omitting it, or supplying a different one, yields a different key.
    It can never widen the derived (principal, capability, repository,
    operation, digest) scope.
    """
    if not isinstance(capability, WriteCapabilityId):
        raise _deny(
            MutationReasonCode.WRITE_CAPABILITY_MISMATCH,
            MutationStage.CREDENTIAL,
            detail="capability",
        )
    payload: dict[str, Any] = {
        "schema": IDEMPOTENCY_KEY_SCHEMA,
        "principal": principal,
        "capability": capability.value,
        "repository": require_repository(repository),
        "operation": operation,
        "operation_digest": require_digest(operation_digest),
    }
    if client_key is not None:
        if not isinstance(client_key, str) or not client_key:
            raise _deny(MutationReasonCode.INVALID_ARGUMENTS, detail="client_key")
        payload["client_key"] = client_key
    try:
        canonical_json_text(payload)
    except TypeError as exc:
        raise _deny(MutationReasonCode.INVALID_ARGUMENTS, detail="key") from exc
    return canonical_hash(payload)


def assert_no_precondition_drift(
    *,
    expected_sha: str,
    observed_sha: str | None,
    stage: MutationStage = MutationStage.PRECONDITION_REVALIDATION,
) -> None:
    """Fail closed unless the freshly observed SHA equals the approved one.

    ``observed_sha=None`` means the state could not be verified; that is drift,
    never an implicit pass.
    """
    require_sha(expected_sha, detail="expected_sha")
    if observed_sha is None or observed_sha != expected_sha:
        raise ConcurrencyDriftError(MutationReasonCode.PRECONDITION_DRIFT, stage)


def operation_family(operation: str) -> str:
    """Last dotted segment of the operation id, used as the lock family."""
    tail = operation.rsplit(".", 1)[-1]
    if tail.startswith("create_"):
        return tail[len("create_") :]
    if tail.startswith("merge_"):
        return _MERGE_FAMILY
    return tail


def lock_type_for(operation: str) -> str:
    """Typed lock semantics: merges are exclusive, creates are intent-to-write."""
    return (
        LOCK_TYPE_WRITE_EXCLUSIVE
        if operation_family(operation) == _MERGE_FAMILY
        else LOCK_TYPE_INTENT_TO_WRITE
    )


@dataclass(frozen=True, slots=True)
class MutationLease:
    """Advisory in-bridge lock over ``(repository, family, target)``."""

    repository: str
    operation: str
    target: str
    owner: str
    acquired_at: datetime
    expires_at: datetime

    @property
    def lock_key(self) -> str:
        return f"{self.repository}#{operation_family(self.operation)}#{self.target}"

    @property
    def lock_type(self) -> str:
        return lock_type_for(self.operation)

    def is_expired(self, now: datetime) -> bool:
        return now.astimezone(UTC) >= self.expires_at.astimezone(UTC)

    def evidence(self) -> dict[str, str]:
        return {
            "lock_key": self.lock_key,
            "lock_type": self.lock_type,
            "owner": self.owner,
        }


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    """Write-ahead intent record for exactly one mutation attempt."""

    idempotency_key: str
    principal: str
    repository: str
    operation: str
    operation_digest: str
    outcome: MutationOutcome
    created_at: datetime
    updated_at: datetime
    lease: MutationLease | None = None
    result: Mapping[str, Any] | None = None
    reason: MutationReasonCode | None = None

    @property
    def allows_new_attempt(self) -> bool:
        return self.outcome.allows_new_attempt

    def evidence(self) -> dict[str, str]:
        """Redacted evidence. Never contains the shaped result or the key inputs."""
        payload = {
            "idempotency_key": self.idempotency_key,
            "operation": self.operation,
            "repository": self.repository,
            "operation_digest": self.operation_digest,
            "outcome": self.outcome.value,
        }
        if self.reason is not None:
            payload["reason"] = self.reason.value
        return payload


@dataclass(frozen=True, slots=True)
class IdempotencyDecision:
    """What the idempotency layer decided for this request."""

    status: IdempotencyStatus
    record: IdempotencyRecord
    lease: MutationLease | None = field(default=None)

    @property
    def executes_provider_call(self) -> bool:
        return self.status.executes_provider_call

    def evidence(self) -> dict[str, str]:
        payload = self.record.evidence()
        payload["idempotency_status"] = self.status.value
        if self.lease is not None:
            payload.update(self.lease.evidence())
        return payload


class IdempotencyStore:
    """Thread-safe write-ahead idempotency and lease store.

    Records are keyed by the derived ``idempotency_key``; a lock key derived
    from ``(repository, family, target)`` is held for the duration of a write
    so two different keys cannot race on the same ref or PR.
    """

    def __init__(self, *, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self._lease_seconds = lease_seconds
        self._lock = threading.Lock()
        self._records: dict[str, IdempotencyRecord] = {}
        self._leases: dict[str, str] = {}

    @property
    def lease_seconds(self) -> int:
        return self._lease_seconds

    def get(self, idempotency_key: str) -> IdempotencyRecord | None:
        with self._lock:
            return self._records.get(idempotency_key)

    def begin(
        self,
        *,
        idempotency_key: str,
        principal: str,
        repository: str,
        operation: str,
        operation_digest: str,
        target: str,
        now: datetime,
    ) -> IdempotencyDecision:
        """Write-ahead: claim the key and take the lease, or refuse.

        Returns ``NEW`` only when a provider call may be issued. ``REPLAYED``
        and ``IN_PROGRESS`` never issue one. ``AMBIGUOUS`` raises, because a
        reconciliation read is mandatory first.
        """
        require_digest(idempotency_key, detail="idempotency_key")
        require_digest(operation_digest)
        require_repository(repository)
        moment = now.astimezone(UTC)

        with self._lock:
            existing = self._records.get(idempotency_key)
            if existing is not None:
                decision = self._decide_existing(existing, moment)
                if decision is not None:
                    return decision

            lease = MutationLease(
                repository=repository,
                operation=operation,
                target=target,
                owner=principal,
                acquired_at=moment,
                expires_at=moment + timedelta(seconds=self._lease_seconds),
            )
            holder = self._leases.get(lease.lock_key)
            if holder is not None and holder != idempotency_key:
                held = self._records.get(holder)
                if held is not None and held.outcome is MutationOutcome.PENDING:
                    if held.lease is not None and not held.lease.is_expired(moment):
                        raise _conflict(
                            MutationReasonCode.OPERATION_IN_PROGRESS, detail="lock_key"
                        )
                    # An expired lease leaves the provider state unknown.
                    self._records[holder] = replace(
                        held,
                        outcome=MutationOutcome.AMBIGUOUS,
                        reason=MutationReasonCode.RECONCILIATION_REQUIRED,
                        updated_at=moment,
                        lease=None,
                    )
                    raise _conflict(
                        MutationReasonCode.RECONCILIATION_REQUIRED, detail="lock_key"
                    )

            record = IdempotencyRecord(
                idempotency_key=idempotency_key,
                principal=principal,
                repository=repository,
                operation=operation,
                operation_digest=operation_digest,
                outcome=MutationOutcome.PENDING,
                created_at=moment,
                updated_at=moment,
                lease=lease,
            )
            self._records[idempotency_key] = record
            self._leases[lease.lock_key] = idempotency_key
            return IdempotencyDecision(IdempotencyStatus.NEW, record, lease)

    def _decide_existing(
        self, existing: IdempotencyRecord, moment: datetime
    ) -> IdempotencyDecision | None:
        """Return a decision for an existing record, or ``None`` to re-attempt."""
        outcome = existing.outcome
        if outcome is MutationOutcome.COMMITTED:
            return IdempotencyDecision(IdempotencyStatus.REPLAYED, existing)
        if outcome is MutationOutcome.DENIED:
            raise _conflict(MutationReasonCode.IDEMPOTENT_REPLAY, detail="denied")
        if outcome is MutationOutcome.AMBIGUOUS:
            raise _conflict(MutationReasonCode.RECONCILIATION_REQUIRED, detail="ambiguous")
        if outcome is MutationOutcome.PENDING:
            lease = existing.lease
            if lease is not None and not lease.is_expired(moment):
                return IdempotencyDecision(IdempotencyStatus.IN_PROGRESS, existing, lease)
            # Lease lost without resolution: provider state unknown.
            self._records[existing.idempotency_key] = replace(
                existing,
                outcome=MutationOutcome.AMBIGUOUS,
                reason=MutationReasonCode.RECONCILIATION_REQUIRED,
                updated_at=moment,
                lease=None,
            )
            if lease is not None:
                self._leases.pop(lease.lock_key, None)
            raise _conflict(MutationReasonCode.RECONCILIATION_REQUIRED, detail="lease_lost")
        # FAILED_CLEAN: a fresh attempt is permitted.
        if existing.lease is not None:
            self._leases.pop(existing.lease.lock_key, None)
        return None

    def commit(
        self,
        idempotency_key: str,
        *,
        result: Mapping[str, Any],
        now: datetime,
    ) -> IdempotencyRecord:
        """Record a provider-confirmed mutation and release the lease."""
        return self._resolve(
            idempotency_key,
            outcome=MutationOutcome.COMMITTED,
            now=now,
            result=MappingProxyType(dict(result)),
        )

    def fail_clean(
        self,
        idempotency_key: str,
        *,
        reason: MutationReasonCode,
        now: datetime,
    ) -> IdempotencyRecord:
        """Record a provably clean provider rejection; a new attempt is allowed."""
        return self._resolve(
            idempotency_key,
            outcome=MutationOutcome.FAILED_CLEAN,
            now=now,
            reason=reason,
        )

    def mark_ambiguous(
        self,
        idempotency_key: str,
        *,
        now: datetime,
    ) -> IdempotencyRecord:
        """Record an unknown provider state. Blind retry is forbidden afterwards."""
        return self._resolve(
            idempotency_key,
            outcome=MutationOutcome.AMBIGUOUS,
            now=now,
            reason=MutationReasonCode.RECONCILIATION_REQUIRED,
        )

    def _resolve(
        self,
        idempotency_key: str,
        *,
        outcome: MutationOutcome,
        now: datetime,
        result: Mapping[str, Any] | None = None,
        reason: MutationReasonCode | None = None,
    ) -> IdempotencyRecord:
        moment = now.astimezone(UTC)
        with self._lock:
            record = self._records.get(idempotency_key)
            if record is None:
                raise _conflict(MutationReasonCode.IDEMPOTENT_REPLAY, detail="unknown_key")
            if record.outcome is not MutationOutcome.PENDING:
                raise _conflict(MutationReasonCode.IDEMPOTENT_REPLAY, detail="already_resolved")
            if record.lease is not None:
                self._leases.pop(record.lease.lock_key, None)
            resolved = replace(
                record,
                outcome=outcome,
                updated_at=moment,
                lease=None,
                result=result,
                reason=reason,
            )
            self._records[idempotency_key] = resolved
            return resolved

    def reconcile(
        self,
        idempotency_key: str,
        *,
        observed_committed: bool,
        result: Mapping[str, Any] | None = None,
        now: datetime,
    ) -> IdempotencyRecord:
        """Close an ``AMBIGUOUS`` record with the outcome of a reconciliation read.

        This is the only sanctioned exit from ``AMBIGUOUS``; it requires an
        actual observation from the caller, never a retry.
        """
        moment = now.astimezone(UTC)
        with self._lock:
            record = self._records.get(idempotency_key)
            if record is None or record.outcome is not MutationOutcome.AMBIGUOUS:
                raise _conflict(MutationReasonCode.IDEMPOTENT_REPLAY, detail="not_ambiguous")
            if observed_committed:
                resolved = replace(
                    record,
                    outcome=MutationOutcome.COMMITTED,
                    updated_at=moment,
                    result=MappingProxyType(dict(result or {})),
                    reason=None,
                )
            else:
                resolved = replace(
                    record,
                    outcome=MutationOutcome.FAILED_CLEAN,
                    updated_at=moment,
                    reason=MutationReasonCode.RECONCILIATION_REQUIRED,
                )
            self._records[idempotency_key] = resolved
            return resolved

    def purge_terminal(
        self, now: datetime, *, retention_seconds: int = TERMINAL_RETENTION_SECONDS
    ) -> int:
        """Drop terminal records older than the retention window.

        ``PENDING`` and ``AMBIGUOUS`` records are never purged: forgetting them
        would silently permit a duplicate write.
        """
        cutoff = now.astimezone(UTC) - timedelta(seconds=retention_seconds)
        removed = 0
        with self._lock:
            for key, record in list(self._records.items()):
                if record.outcome in (MutationOutcome.PENDING, MutationOutcome.AMBIGUOUS):
                    continue
                if record.updated_at.astimezone(UTC) <= cutoff:
                    del self._records[key]
                    removed += 1
        return removed


__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "IDEMPOTENCY_KEY_SCHEMA",
    "LOCK_TYPE_INTENT_TO_WRITE",
    "LOCK_TYPE_WRITE_EXCLUSIVE",
    "TERMINAL_RETENTION_SECONDS",
    "IdempotencyDecision",
    "IdempotencyRecord",
    "IdempotencyStore",
    "MutationLease",
    "assert_no_precondition_drift",
    "compute_idempotency_key",
    "lock_type_for",
    "operation_family",
]
