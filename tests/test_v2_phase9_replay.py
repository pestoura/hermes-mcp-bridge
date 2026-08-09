"""Phase 9: approval and idempotency replay protection under adversarial reuse.

Phase 3 proved the happy-path semantics. Phase 9 asks the production question:
can a *replayed* request — the same approval, the same key, a crash-interrupted
attempt, a concurrent duplicate — ever produce a second side effect?

Every test counts real provider invocations through a fake writer, so "exactly
one mutation" is measured rather than asserted. Hermetic: no network, no
credentials, no filesystem.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest

from hermes_mcp_bridge.v2.enums import (
    ApprovalState,
    IdempotencyStatus,
    MutationOutcome,
    MutationReasonCode,
    WriteCapabilityId,
)
from hermes_mcp_bridge.v2.errors import IdempotencyConflictError, MutationDeniedError
from hermes_mcp_bridge.v2.mutation_digest import (
    ApprovalRecord,
    ApprovalStore,
    DigestMismatchError,
    OperationDescriptor,
    OperationPreconditions,
    compute_operation_digest,
)
from hermes_mcp_bridge.v2.mutation_idempotency import (
    IdempotencyStore,
    compute_idempotency_key,
)

HEAD = "b" * 40
OTHER_HEAD = "d" * 40
SNAPSHOT = "c" * 64
REPO = "pestoura/hermes-mcp-bridge"
NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
PRINCIPAL = "operator"
APPROVER = "reviewer"


class FakeWriter:
    """Counts provider mutations. The count is the evidence."""

    def __init__(self) -> None:
        self.calls = 0
        self._lock = threading.Lock()

    def write(self) -> dict[str, str]:
        with self._lock:
            self.calls += 1
            return {"number": str(self.calls)}


def _descriptor(*, head: str = HEAD, title: str = "T") -> OperationDescriptor:
    return OperationDescriptor(
        operation="github.create_pr",
        capability=WriteCapabilityId.PR,
        repository=REPO,
        arguments={"title": title, "head": "feat/x", "base": "main"},
        preconditions=OperationPreconditions(expected_head_sha=head),
        policy_version="policy-2026.08.1",
        registry_snapshot_hash=SNAPSHOT,
    )


def _approval(descriptor: OperationDescriptor, *, approval_id: str = "ap-1") -> ApprovalRecord:
    return ApprovalRecord(
        approval_id=approval_id,
        principal=PRINCIPAL,
        approver=APPROVER,
        operation_digest=compute_operation_digest(descriptor),
        repository=REPO,
        operation=descriptor.operation,
        nonce="nonce-1",
        expires_at=NOW + timedelta(minutes=10),
        trust_context="trusted",
    )


def _key(descriptor: OperationDescriptor) -> str:
    return compute_idempotency_key(
        principal=PRINCIPAL,
        capability=WriteCapabilityId.PR,
        repository=REPO,
        operation=descriptor.operation,
        operation_digest=compute_operation_digest(descriptor),
    )


def _begin(store: IdempotencyStore, descriptor: OperationDescriptor, *, now: datetime = NOW):
    return store.begin(
        idempotency_key=_key(descriptor),
        principal=PRINCIPAL,
        repository=REPO,
        operation=descriptor.operation,
        operation_digest=compute_operation_digest(descriptor),
        target="feat/x",
        now=now,
    )


# --------------------------------------------------------------------------
# Approval replay
# --------------------------------------------------------------------------
def test_p9_r01_approval_is_consumed_atomically_once() -> None:
    """A single approval survives exactly one consumption, even under threads."""
    descriptor = _descriptor()
    store = ApprovalStore()
    store.issue(_approval(descriptor))

    successes: list[str] = []
    failures: list[BaseException] = []
    barrier = threading.Barrier(8)

    def attempt() -> None:
        barrier.wait()
        try:
            record = store.verify_and_consume(
                "ap-1", descriptor, principal=PRINCIPAL, now=NOW
            )
            successes.append(record.approval_id)
        except BaseException as exc:
            failures.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(successes) == 1, "an approval must be consumable exactly once"
    assert len(failures) == 7
    assert all(isinstance(exc, MutationDeniedError) for exc in failures)


def test_p9_r02_replayed_approval_is_rejected() -> None:
    descriptor = _descriptor()
    store = ApprovalStore()
    store.issue(_approval(descriptor))
    consumed = store.verify_and_consume("ap-1", descriptor, principal=PRINCIPAL, now=NOW)
    assert consumed.state is ApprovalState.CONSUMED

    with pytest.raises(MutationDeniedError) as excinfo:
        store.verify_and_consume("ap-1", descriptor, principal=PRINCIPAL, now=NOW)
    assert excinfo.value.reason is MutationReasonCode.APPROVAL_ALREADY_CONSUMED


def test_p9_r03_approval_cannot_be_reused_against_a_changed_digest() -> None:
    """The classic escalation: approve a small change, execute a bigger one."""
    approved = _descriptor(title="small")
    mutated = _descriptor(title="MUCH LARGER CHANGE")
    assert compute_operation_digest(approved) != compute_operation_digest(mutated)

    store = ApprovalStore()
    store.issue(_approval(approved))
    with pytest.raises(DigestMismatchError):
        store.verify_and_consume("ap-1", mutated, principal=PRINCIPAL, now=NOW)
    # And the approval must still be intact for its own operation — a failed
    # attack does not burn the operator's approval.
    record = store.verify_and_consume("ap-1", approved, principal=PRINCIPAL, now=NOW)
    assert record.state is ApprovalState.CONSUMED


def test_p9_r04_expired_approval_never_executes() -> None:
    descriptor = _descriptor()
    store = ApprovalStore()
    store.issue(_approval(descriptor))
    with pytest.raises(MutationDeniedError) as excinfo:
        store.verify_and_consume(
            "ap-1", descriptor, principal=PRINCIPAL, now=NOW + timedelta(hours=2)
        )
    assert excinfo.value.reason is MutationReasonCode.APPROVAL_EXPIRED


def test_p9_r05_approval_scope_is_bound_to_principal() -> None:
    descriptor = _descriptor()
    store = ApprovalStore()
    store.issue(_approval(descriptor))
    with pytest.raises(MutationDeniedError) as excinfo:
        store.verify_and_consume("ap-1", descriptor, principal="someone-else", now=NOW)
    assert excinfo.value.reason is MutationReasonCode.APPROVAL_SCOPE_MISMATCH


# --------------------------------------------------------------------------
# Idempotency replay
# --------------------------------------------------------------------------
def test_p9_r06_committed_replay_returns_prior_result_without_second_write() -> None:
    descriptor = _descriptor()
    store = IdempotencyStore()
    writer = FakeWriter()

    first = _begin(store, descriptor)
    assert first.status is IdempotencyStatus.NEW
    result = writer.write()
    store.commit(_key(descriptor), result=result, now=NOW)

    replay = _begin(store, descriptor, now=NOW + timedelta(seconds=1))
    assert replay.status is IdempotencyStatus.REPLAYED
    assert replay.executes_provider_call is False
    assert replay.record.result == result
    assert writer.calls == 1, "a replay must never issue a second provider write"


def test_p9_r07_concurrent_duplicates_produce_exactly_one_write() -> None:
    descriptor = _descriptor()
    store = IdempotencyStore()
    writer = FakeWriter()
    barrier = threading.Barrier(6)
    statuses: list[IdempotencyStatus] = []
    conflicts: list[BaseException] = []
    lock = threading.Lock()

    def attempt() -> None:
        barrier.wait()
        try:
            decision = _begin(store, descriptor)
        except IdempotencyConflictError as exc:
            with lock:
                conflicts.append(exc)
            return
        with lock:
            statuses.append(decision.status)
        if decision.executes_provider_call:
            writer.write()
            store.commit(_key(descriptor), result={"number": "1"}, now=NOW)

    threads = [threading.Thread(target=attempt) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert statuses.count(IdempotencyStatus.NEW) == 1
    assert writer.calls == 1, "duplicate concurrent requests must write exactly once"
    assert all(
        status in {IdempotencyStatus.NEW, IdempotencyStatus.REPLAYED, IdempotencyStatus.IN_PROGRESS}
        for status in statuses
    )


def test_p9_r08_in_progress_never_issues_a_second_call() -> None:
    descriptor = _descriptor()
    store = IdempotencyStore()
    _begin(store, descriptor)
    second = _begin(store, descriptor, now=NOW + timedelta(seconds=5))
    assert second.status is IdempotencyStatus.IN_PROGRESS
    assert second.executes_provider_call is False


def test_p9_r09_ambiguous_forbids_blind_retry() -> None:
    """The crash-mid-write case: unknown outcome must demand reconciliation."""
    descriptor = _descriptor()
    store = IdempotencyStore()
    _begin(store, descriptor)
    record = store.mark_ambiguous(_key(descriptor), now=NOW + timedelta(seconds=2))
    assert record.outcome is MutationOutcome.AMBIGUOUS

    with pytest.raises(IdempotencyConflictError) as excinfo:
        _begin(store, descriptor, now=NOW + timedelta(seconds=3))
    assert excinfo.value.reason is MutationReasonCode.RECONCILIATION_REQUIRED


def test_p9_r10_expired_lease_becomes_ambiguous_not_reusable() -> None:
    """A lost lease means the provider state is unknown — never a free retry."""
    descriptor = _descriptor()
    store = IdempotencyStore(lease_seconds=1)
    _begin(store, descriptor)
    with pytest.raises(IdempotencyConflictError) as excinfo:
        _begin(store, descriptor, now=NOW + timedelta(seconds=30))
    assert excinfo.value.reason is MutationReasonCode.RECONCILIATION_REQUIRED
    assert store.get(_key(descriptor)).outcome is MutationOutcome.AMBIGUOUS


def test_p9_r11_failed_clean_allows_exactly_one_new_attempt() -> None:
    descriptor = _descriptor()
    store = IdempotencyStore()
    writer = FakeWriter()
    _begin(store, descriptor)
    store.fail_clean(
        _key(descriptor), reason=MutationReasonCode.INVALID_ARGUMENTS, now=NOW
    )
    retry = _begin(store, descriptor, now=NOW + timedelta(seconds=1))
    assert retry.status is IdempotencyStatus.NEW
    writer.write()
    assert writer.calls == 1


def test_p9_r12_key_scope_cannot_be_widened_across_principals() -> None:
    descriptor = _descriptor()
    mine = _key(descriptor)
    theirs = compute_idempotency_key(
        principal="another-principal",
        capability=WriteCapabilityId.PR,
        repository=REPO,
        operation=descriptor.operation,
        operation_digest=compute_operation_digest(descriptor),
    )
    assert mine != theirs, "two principals must never share an idempotency record"


def test_p9_r13_client_key_only_narrows() -> None:
    descriptor = _descriptor()
    base = _key(descriptor)
    narrowed = compute_idempotency_key(
        principal=PRINCIPAL,
        capability=WriteCapabilityId.PR,
        repository=REPO,
        operation=descriptor.operation,
        operation_digest=compute_operation_digest(descriptor),
        client_key="caller-supplied",
    )
    assert base != narrowed


def test_p9_r14_changed_operation_changes_the_key() -> None:
    assert _key(_descriptor(head=HEAD)) != _key(_descriptor(head=OTHER_HEAD))


def test_p9_r15_replay_evidence_carries_no_result_payload() -> None:
    """Replay evidence must be safe to log: digests and enums only."""
    descriptor = _descriptor()
    store = IdempotencyStore()
    _begin(store, descriptor)
    store.commit(_key(descriptor), result={"secret_ish": "ghp_" + "a" * 36}, now=NOW)
    replay = _begin(store, descriptor, now=NOW + timedelta(seconds=1))
    evidence = replay.evidence()
    serialized = repr(evidence)
    assert "ghp_" not in serialized
    assert "secret_ish" not in serialized
    assert evidence["idempotency_status"] == IdempotencyStatus.REPLAYED.value
