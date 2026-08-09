"""Phase 3 lane L3 — idempotency, replay protection, concurrency and locks.

Hermetic: no network, no credentials, no filesystem writes, no subprocess.
A fake provider counts write attempts so "exactly one mutation" is measured,
not asserted.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest

from hermes_mcp_bridge.v2.enums import (
    IdempotencyStatus,
    MutationOutcome,
    MutationReasonCode,
    MutationStage,
    WriteCapabilityId,
)
from hermes_mcp_bridge.v2.errors import (
    ConcurrencyDriftError,
    IdempotencyConflictError,
    MutationDeniedError,
)
from hermes_mcp_bridge.v2.mutation_digest import (
    OperationDescriptor,
    OperationPreconditions,
    compute_operation_digest,
)
from hermes_mcp_bridge.v2.mutation_idempotency import (
    DEFAULT_LEASE_SECONDS,
    IDEMPOTENCY_KEY_SCHEMA,
    LOCK_TYPE_INTENT_TO_WRITE,
    LOCK_TYPE_WRITE_EXCLUSIVE,
    IdempotencyStore,
    assert_no_precondition_drift,
    compute_idempotency_key,
    lock_type_for,
    operation_family,
)

HEAD = "b" * 40
SNAPSHOT = "c" * 64
REPO = "pestoura/hermes-mcp-bridge"
NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


def descriptor() -> OperationDescriptor:
    return OperationDescriptor(
        operation="github.create_pr",
        capability=WriteCapabilityId.PR,
        repository=REPO,
        arguments={"title": "T", "head": "feat/x", "base": "main"},
        preconditions=OperationPreconditions(expected_head_sha=HEAD),
        policy_version="policy-2026.08.1",
        registry_snapshot_hash=SNAPSHOT,
    )


def key(**overrides: object) -> str:
    kwargs: dict[str, object] = {
        "principal": "svc-jarvas",
        "capability": WriteCapabilityId.PR,
        "repository": REPO,
        "operation": "github.create_pr",
        "operation_digest": compute_operation_digest(descriptor()),
    }
    kwargs.update(overrides)
    return compute_idempotency_key(**kwargs)  # type: ignore[arg-type]


def begin(store: IdempotencyStore, *, idem_key: str | None = None, now: datetime = NOW):
    return store.begin(
        idempotency_key=idem_key or key(),
        principal="svc-jarvas",
        repository=REPO,
        operation="github.create_pr",
        operation_digest=compute_operation_digest(descriptor()),
        target="feat/x",
        now=now,
    )


class FakeProvider:
    """Counts write attempts so duplicate writes are measured, not assumed."""

    def __init__(self) -> None:
        self.calls = 0
        self._lock = threading.Lock()

    def create_pr(self) -> dict[str, object]:
        with self._lock:
            self.calls += 1
        return {"number": 7, "state": "open"}


# --------------------------------------------------------------------------
# key derivation
# --------------------------------------------------------------------------


def test_key_is_lowercase_64_hex_and_deterministic() -> None:
    first, second = key(), key()
    assert first == second
    assert len(first) == 64 and first == first.lower()


@pytest.mark.parametrize(
    "overrides",
    [
        {"principal": "other"},
        {"capability": WriteCapabilityId.BRANCH},
        {"repository": "pestoura/other"},
        {"operation": "github.create_branch"},
        {"operation_digest": "d" * 64},
        {"client_key": "narrow-1"},
    ],
)
def test_key_scope_is_not_collidable(overrides: dict[str, object]) -> None:
    assert key() != key(**overrides)


def test_client_key_only_narrows_never_substitutes() -> None:
    """Two principals with the same client_key still get different records."""
    a = key(client_key="same", principal="p1")
    b = key(client_key="same", principal="p2")
    assert a != b
    assert key(client_key="same") != key()


def test_key_schema_is_versioned() -> None:
    assert IDEMPOTENCY_KEY_SCHEMA == "v2.phase3.idempotency.1"


def test_key_rejects_read_capability_string() -> None:
    with pytest.raises(MutationDeniedError) as exc:
        key(capability="github.read")
    assert exc.value.reason is MutationReasonCode.WRITE_CAPABILITY_MISMATCH


def test_key_rejects_bad_repository_and_digest() -> None:
    with pytest.raises(MutationDeniedError):
        key(repository="../etc")
    with pytest.raises(MutationDeniedError):
        key(operation_digest="short")


def test_key_contains_no_raw_payload() -> None:
    """The key is a digest: caller content is never recoverable from it."""
    value = key(client_key="secret-marker")
    assert "secret-marker" not in value


# --------------------------------------------------------------------------
# write-ahead / replay
# --------------------------------------------------------------------------


def test_first_request_is_new_and_leased() -> None:
    store = IdempotencyStore()
    decision = begin(store)
    assert decision.status is IdempotencyStatus.NEW
    assert decision.executes_provider_call
    assert decision.record.outcome is MutationOutcome.PENDING
    assert decision.lease is not None
    assert decision.lease.lock_key == f"{REPO}#pr#feat/x"


def test_write_ahead_record_exists_before_any_provider_call() -> None:
    store = IdempotencyStore()
    provider = FakeProvider()
    idem = key()
    decision = begin(store, idem_key=idem)
    stored = store.get(idem)
    assert stored is not None and stored.outcome is MutationOutcome.PENDING
    assert provider.calls == 0
    if decision.executes_provider_call:
        provider.create_pr()
    assert provider.calls == 1


def test_repeated_request_single_provider_mutation() -> None:
    store = IdempotencyStore()
    provider = FakeProvider()
    idem = key()

    first = begin(store, idem_key=idem)
    assert first.executes_provider_call
    result = provider.create_pr()
    store.commit(idem, result=result, now=NOW)

    second = begin(store, idem_key=idem, now=NOW + timedelta(seconds=1))
    assert second.status is IdempotencyStatus.REPLAYED
    assert not second.executes_provider_call
    assert provider.calls == 1
    assert second.record.result is not None
    assert second.record.result["number"] == 7


def test_in_progress_lease_blocks_second_write() -> None:
    store = IdempotencyStore()
    provider = FakeProvider()
    idem = key()
    begin(store, idem_key=idem)
    second = begin(store, idem_key=idem, now=NOW + timedelta(seconds=5))
    assert second.status is IdempotencyStatus.IN_PROGRESS
    assert not second.executes_provider_call
    assert provider.calls == 0


def test_expired_lease_becomes_ambiguous_not_reusable() -> None:
    store = IdempotencyStore(lease_seconds=10)
    idem = key()
    begin(store, idem_key=idem)
    with pytest.raises(IdempotencyConflictError) as exc:
        begin(store, idem_key=idem, now=NOW + timedelta(seconds=11))
    assert exc.value.reason is MutationReasonCode.RECONCILIATION_REQUIRED
    record = store.get(idem)
    assert record is not None and record.outcome is MutationOutcome.AMBIGUOUS


def test_ambiguous_outcome_requires_reconciliation_read() -> None:
    store = IdempotencyStore()
    provider = FakeProvider()
    idem = key()
    begin(store, idem_key=idem)
    store.mark_ambiguous(idem, now=NOW)
    with pytest.raises(IdempotencyConflictError) as exc:
        begin(store, idem_key=idem, now=NOW + timedelta(seconds=1))
    assert exc.value.reason is MutationReasonCode.RECONCILIATION_REQUIRED
    assert provider.calls == 0


def test_reconciliation_is_the_only_exit_from_ambiguous() -> None:
    store = IdempotencyStore()
    idem = key()
    begin(store, idem_key=idem)
    store.mark_ambiguous(idem, now=NOW)
    resolved = store.reconcile(
        idem, observed_committed=True, result={"number": 7}, now=NOW + timedelta(seconds=2)
    )
    assert resolved.outcome is MutationOutcome.COMMITTED
    replay = begin(store, idem_key=idem, now=NOW + timedelta(seconds=3))
    assert replay.status is IdempotencyStatus.REPLAYED


def test_reconciliation_not_committed_allows_new_attempt() -> None:
    store = IdempotencyStore()
    idem = key()
    begin(store, idem_key=idem)
    store.mark_ambiguous(idem, now=NOW)
    resolved = store.reconcile(idem, observed_committed=False, now=NOW + timedelta(seconds=2))
    assert resolved.outcome is MutationOutcome.FAILED_CLEAN
    assert resolved.allows_new_attempt
    again = begin(store, idem_key=idem, now=NOW + timedelta(seconds=3))
    assert again.status is IdempotencyStatus.NEW


def test_failed_clean_allows_new_attempt_only() -> None:
    store = IdempotencyStore()
    idem = key()
    begin(store, idem_key=idem)
    store.fail_clean(idem, reason=MutationReasonCode.REF_ALREADY_EXISTS, now=NOW)
    again = begin(store, idem_key=idem, now=NOW + timedelta(seconds=1))
    assert again.status is IdempotencyStatus.NEW


def test_resolving_twice_is_refused() -> None:
    store = IdempotencyStore()
    idem = key()
    begin(store, idem_key=idem)
    store.commit(idem, result={"number": 7}, now=NOW)
    with pytest.raises(IdempotencyConflictError):
        store.commit(idem, result={"number": 7}, now=NOW)


def test_resolving_unknown_key_is_refused() -> None:
    store = IdempotencyStore()
    with pytest.raises(IdempotencyConflictError):
        store.commit("f" * 64, result={}, now=NOW)


def test_concurrent_identical_requests_issue_one_write() -> None:
    store = IdempotencyStore()
    provider = FakeProvider()
    idem = key()
    barrier = threading.Barrier(8)
    statuses: list[IdempotencyStatus] = []
    conflicts: list[MutationReasonCode] = []
    guard = threading.Lock()

    def attempt() -> None:
        barrier.wait()
        try:
            decision = begin(store, idem_key=idem)
        except IdempotencyConflictError as exc:
            with guard:
                conflicts.append(exc.reason)
            return
        with guard:
            statuses.append(decision.status)
        if decision.executes_provider_call:
            provider.create_pr()

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert provider.calls == 1
    assert statuses.count(IdempotencyStatus.NEW) == 1
    assert not conflicts


def test_different_keys_on_same_target_serialize_on_the_lock() -> None:
    """Two principals racing the same ref: the lock refuses the second."""
    store = IdempotencyStore()
    begin(store, idem_key=key())
    other = key(principal="svc-other")
    with pytest.raises(IdempotencyConflictError) as exc:
        store.begin(
            idempotency_key=other,
            principal="svc-other",
            repository=REPO,
            operation="github.create_pr",
            operation_digest=compute_operation_digest(descriptor()),
            target="feat/x",
            now=NOW,
        )
    assert exc.value.reason is MutationReasonCode.OPERATION_IN_PROGRESS


def test_lock_released_after_commit_allows_other_principal() -> None:
    store = IdempotencyStore()
    idem = key()
    begin(store, idem_key=idem)
    store.commit(idem, result={"number": 7}, now=NOW)
    other = store.begin(
        idempotency_key=key(principal="svc-other"),
        principal="svc-other",
        repository=REPO,
        operation="github.create_pr",
        operation_digest=compute_operation_digest(descriptor()),
        target="feat/x",
        now=NOW,
    )
    assert other.status is IdempotencyStatus.NEW


# --------------------------------------------------------------------------
# optimistic concurrency
# --------------------------------------------------------------------------


def test_matching_head_sha_passes() -> None:
    assert_no_precondition_drift(expected_sha=HEAD, observed_sha=HEAD)


def test_head_sha_drift_denies() -> None:
    with pytest.raises(ConcurrencyDriftError) as exc:
        assert_no_precondition_drift(expected_sha=HEAD, observed_sha="d" * 40)
    assert exc.value.reason is MutationReasonCode.PRECONDITION_DRIFT
    assert exc.value.stage is MutationStage.PRECONDITION_REVALIDATION


def test_unobservable_state_is_drift_not_a_pass() -> None:
    with pytest.raises(ConcurrencyDriftError):
        assert_no_precondition_drift(expected_sha=HEAD, observed_sha=None)


def test_expected_sha_grammar_enforced() -> None:
    with pytest.raises(MutationDeniedError):
        assert_no_precondition_drift(expected_sha="HEAD", observed_sha="HEAD")


# --------------------------------------------------------------------------
# typed lock semantics
# --------------------------------------------------------------------------


def test_lock_family_derivation() -> None:
    assert operation_family("github.create_branch") == "branch"
    assert operation_family("github.create_pr") == "pr"
    assert operation_family("github.merge_pr") == "merge"


def test_lock_types_are_typed_per_operation() -> None:
    assert lock_type_for("github.create_branch") == LOCK_TYPE_INTENT_TO_WRITE
    assert lock_type_for("github.create_pr") == LOCK_TYPE_INTENT_TO_WRITE
    assert lock_type_for("github.merge_pr") == LOCK_TYPE_WRITE_EXCLUSIVE


def test_lock_key_is_repository_family_target() -> None:
    store = IdempotencyStore()
    decision = begin(store)
    lease = decision.lease
    assert lease is not None
    assert lease.lock_key == f"{REPO}#pr#feat/x"
    assert lease.lock_type == LOCK_TYPE_INTENT_TO_WRITE
    assert lease.is_expired(NOW + timedelta(seconds=DEFAULT_LEASE_SECONDS))


def test_v2_does_not_import_v1_lock_registry() -> None:
    """Typed lock tokens are reused as literals; V2 stays import-isolated."""
    import ast
    from pathlib import Path

    import hermes_mcp_bridge.v2.mutation_idempotency as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    absolute: set[str] = set()
    relative: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            absolute.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                absolute.add(node.module.split(".")[0])
            elif node.level >= 2:
                relative.add(node.module or "")
    forbidden = {"subprocess", "socket", "httpx", "requests", "sqlite3", "urllib", "os"}
    assert absolute & forbidden == set()
    assert relative == set(), "V2 must not reach into the V1 package"


# --------------------------------------------------------------------------
# evidence and retention
# --------------------------------------------------------------------------


def test_decision_evidence_is_redacted() -> None:
    store = IdempotencyStore()
    decision = begin(store)
    evidence = decision.evidence()
    assert evidence["idempotency_status"] == IdempotencyStatus.NEW.value
    assert evidence["outcome"] == MutationOutcome.PENDING.value
    assert "result" not in evidence
    assert "principal" not in evidence
    assert "T" not in evidence.get("arguments", "")


def test_committed_result_is_not_in_evidence() -> None:
    store = IdempotencyStore()
    idem = key()
    begin(store, idem_key=idem)
    record = store.commit(idem, result={"number": 7, "url": "https://x/y"}, now=NOW)
    evidence = record.evidence()
    assert "https://x/y" not in " ".join(evidence.values())


def test_purge_never_drops_pending_or_ambiguous() -> None:
    store = IdempotencyStore()
    pending = key(client_key="pending")
    ambiguous = key(client_key="ambiguous")
    committed = key(client_key="committed")
    for value, target in ((pending, "a"), (ambiguous, "b"), (committed, "c")):
        store.begin(
            idempotency_key=value,
            principal="svc-jarvas",
            repository=REPO,
            operation="github.create_pr",
            operation_digest=compute_operation_digest(descriptor()),
            target=target,
            now=NOW,
        )
    store.mark_ambiguous(ambiguous, now=NOW)
    store.commit(committed, result={"number": 1}, now=NOW)

    much_later = NOW + timedelta(days=30)
    assert store.purge_terminal(much_later) == 1
    assert store.get(committed) is None
    assert store.get(pending) is not None
    assert store.get(ambiguous) is not None
