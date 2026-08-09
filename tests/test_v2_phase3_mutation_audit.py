"""Phase 3 lane L4 — write-ahead mutation audit, provenance and evidence.

Hermetic: no network, no credentials, no provider. Filesystem use is confined
to pytest's ``tmp_path``. Criteria A3-05 (no write without a prior write-ahead
record) and A3-14 (no secret material anywhere, bounded metric labels).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hermes_mcp_bridge.v2.enums import (
    ApprovalState,
    CapabilityState,
    IdempotencyStatus,
    MutationOutcome,
    MutationReasonCode,
    MutationStage,
    PolicyDecision,
    WriteCapabilityId,
)
from hermes_mcp_bridge.v2.errors import AuditWriteError, MutationDeniedError
from hermes_mcp_bridge.v2.mutation_audit import (
    AUDIT_METRIC_LABELS,
    MUTATION_AUDIT_SCHEMA,
    MUTATION_EVIDENCE_SCHEMA,
    ApprovalReference,
    AuditHandle,
    CapabilitySnapshot,
    EvidenceClass,
    FileAuditSink,
    InMemoryAuditSink,
    MutationAuditLedger,
    MutationIntent,
    ProviderObservation,
    VerificationState,
    audit_metric_labels,
    evidence_class_for,
    looks_secret_bearing,
)

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
SNAPSHOT_HASH = "c" * 64
BASE_SHA = "0" * 39 + "1"
T0 = datetime(2026, 8, 9, 12, 0, 0, tzinfo=UTC)


def _capability(state: CapabilityState = CapabilityState.READY) -> CapabilitySnapshot:
    return CapabilitySnapshot(
        capability_id=WriteCapabilityId.BRANCH,
        state=state,
        snapshot_hash=SNAPSHOT_HASH,
        policy_version="policy-2026-08-09",
    )


def _intent(**overrides: object) -> MutationIntent:
    kwargs: dict[str, object] = {
        "principal": "svc-jarvas",
        "operation": "github.create_branch",
        "repository": "pestoura/hermes-mcp-bridge",
        "operation_digest": DIGEST_A,
        "policy_decision": PolicyDecision.ALLOW,
        "capability": _capability(),
        "idempotency_key": DIGEST_B,
        "idempotency_status": IdempotencyStatus.NEW,
        "preconditions_observed": {"base_sha": BASE_SHA},
    }
    kwargs.update(overrides)
    return MutationIntent(**kwargs)  # type: ignore[arg-type]


def _ledger(tmp_path: Path) -> MutationAuditLedger:
    return MutationAuditLedger(
        FileAuditSink(tmp_path / "audit"), clock=lambda: _ledger_clock.pop(0)
    )


_ledger_clock: list[datetime] = []


@pytest.fixture
def ledger(tmp_path: Path) -> MutationAuditLedger:
    _ledger_clock.clear()
    _ledger_clock.extend(T0 + timedelta(seconds=i) for i in range(50))
    return MutationAuditLedger(
        FileAuditSink(tmp_path / "audit"), clock=lambda: _ledger_clock.pop(0)
    )


def _observation(**overrides: object) -> ProviderObservation:
    kwargs: dict[str, object] = {
        "outcome": MutationOutcome.COMMITTED,
        "verification": VerificationState.VERIFIED,
        "attempts": 1,
        "started_at": T0 + timedelta(seconds=5),
        "finished_at": T0 + timedelta(seconds=6),
        "status_class": "2xx",
        "result_digest": DIGEST_A,
    }
    kwargs.update(overrides)
    return ProviderObservation(**kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# A3-05 — write-ahead ordering
# --------------------------------------------------------------------------


def test_write_ahead_record_precedes_provider_call(ledger, tmp_path: Path) -> None:
    handle = ledger.begin(_intent())
    assert isinstance(handle, AuditHandle)
    assert handle.provider_call_permitted is True
    # The record is on disk *before* any caller could have issued a call.
    files = list((tmp_path / "audit").glob("*.json"))
    assert len(files) == 1
    stored = json.loads(files[0].read_text(encoding="utf-8"))
    assert stored["schema"] == MUTATION_AUDIT_SCHEMA
    assert stored["outcome"] == MutationOutcome.PENDING.value
    assert stored["stage"] == MutationStage.WRITE_AHEAD_AUDIT.value
    assert stored["record_digest"] == handle.record_digest


def test_finalize_rejects_observation_started_before_the_record(ledger) -> None:
    handle = ledger.begin(_intent())
    with pytest.raises(AuditWriteError):
        ledger.finalize(
            handle,
            _observation(
                started_at=T0 - timedelta(seconds=1),
                finished_at=T0,
            ),
        )


def test_committed_mutation_without_audit_record_fails_gate(ledger, tmp_path: Path) -> None:
    handle = ledger.begin(_intent())
    for path in (tmp_path / "audit").glob("*.json"):
        path.unlink()
    assert ledger.has_write_ahead_record(handle.audit_id) is False
    with pytest.raises(AuditWriteError):
        ledger.finalize(handle, _observation())


def test_handle_cannot_be_finalized_twice(ledger) -> None:
    handle = ledger.begin(_intent())
    ledger.finalize(handle, _observation())
    assert handle.provider_call_permitted is False
    with pytest.raises(AuditWriteError):
        ledger.finalize(handle, _observation())


def test_foreign_handle_is_rejected(ledger, tmp_path: Path) -> None:
    _ledger_clock.extend(T0 + timedelta(seconds=i) for i in range(50))
    other = MutationAuditLedger(
        FileAuditSink(tmp_path / "other"), clock=lambda: _ledger_clock.pop(0)
    )
    stolen = other.begin(_intent())
    with pytest.raises(AuditWriteError):
        ledger.finalize(stolen, _observation())


def test_unwritable_sink_denies_the_mutation(tmp_path: Path) -> None:
    class BrokenSink:
        durable = True

        def append(self, audit_id, payload):
            raise OSError("disk full")

        def exists(self, audit_id):
            return False

        def read(self, audit_id):
            return None

    broken = MutationAuditLedger(BrokenSink())
    with pytest.raises(AuditWriteError) as excinfo:
        broken.begin(_intent())
    assert excinfo.value.reason is MutationReasonCode.AUDIT_RECORD_UNWRITABLE
    assert excinfo.value.stage is MutationStage.WRITE_AHEAD_AUDIT
    # Fail-closed: an audit failure is a denial, never a warning.
    assert isinstance(excinfo.value, MutationDeniedError)


def test_silently_dropping_sink_denies_the_mutation() -> None:
    class LyingSink:
        durable = True

        def append(self, audit_id, payload):
            return None

        def exists(self, audit_id):
            return False

        def read(self, audit_id):
            return None

    with pytest.raises(AuditWriteError):
        MutationAuditLedger(LyingSink()).begin(_intent())


def test_non_durable_sink_requires_explicit_opt_in() -> None:
    with pytest.raises(AuditWriteError):
        MutationAuditLedger(InMemoryAuditSink())
    ledger = MutationAuditLedger(InMemoryAuditSink(), allow_non_durable_sink=True)
    assert ledger.open_attempts == 0


# --------------------------------------------------------------------------
# Outcome taxonomy — never infer success
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (MutationOutcome.COMMITTED, EvidenceClass.SUCCESS),
        (MutationOutcome.FAILED_CLEAN, EvidenceClass.FAILED),
        (MutationOutcome.DENIED, EvidenceClass.BLOCKED),
        (MutationOutcome.AMBIGUOUS, EvidenceClass.INDETERMINATE),
    ],
)
def test_outcome_maps_to_exactly_one_evidence_class(outcome, expected) -> None:
    assert evidence_class_for(outcome) is expected


def test_pending_outcome_has_no_evidence_class() -> None:
    with pytest.raises(AuditWriteError):
        evidence_class_for(MutationOutcome.PENDING)


def test_evidence_classes_are_exactly_the_four_required() -> None:
    assert {member.value for member in EvidenceClass} == {
        "SUCCESS",
        "FAILED",
        "BLOCKED",
        "INDETERMINATE",
    }


@pytest.mark.parametrize(
    "verification",
    [
        VerificationState.NOT_ATTEMPTED,
        VerificationState.MISMATCH,
        VerificationState.UNVERIFIABLE,
    ],
)
def test_commit_without_verified_read_back_is_refused(verification) -> None:
    with pytest.raises(AuditWriteError) as excinfo:
        _observation(verification=verification)
    assert excinfo.value.reason is MutationReasonCode.RECONCILIATION_REQUIRED


def test_unverifiable_read_back_yields_indeterminate_not_success(ledger) -> None:
    handle = ledger.begin(_intent())
    evidence = ledger.finalize(
        handle,
        _observation(
            outcome=MutationOutcome.AMBIGUOUS,
            verification=VerificationState.UNVERIFIABLE,
            attempts=2,
            status_class=None,
            result_digest=None,
            reason=MutationReasonCode.RECONCILIATION_REQUIRED,
        ),
    )
    assert evidence.evidence_class is EvidenceClass.INDETERMINATE
    assert evidence.mutation_confirmed is False
    assert evidence.requires_reconciliation is True
    assert evidence.evidence_class.allows_new_attempt is False


def test_only_failed_clean_allows_a_new_attempt() -> None:
    assert EvidenceClass.FAILED.allows_new_attempt is True
    for other in (EvidenceClass.SUCCESS, EvidenceClass.BLOCKED, EvidenceClass.INDETERMINATE):
        assert other.allows_new_attempt is False


def test_denied_outcome_must_report_zero_attempts() -> None:
    with pytest.raises(AuditWriteError):
        _observation(
            outcome=MutationOutcome.DENIED,
            verification=VerificationState.NOT_ATTEMPTED,
            attempts=1,
            status_class=None,
            result_digest=None,
        )


def test_retry_count_and_timestamps_are_recorded(ledger) -> None:
    handle = ledger.begin(_intent())
    evidence = ledger.finalize(
        handle,
        _observation(
            attempts=3,
            started_at=T0 + timedelta(seconds=5),
            finished_at=T0 + timedelta(seconds=8),
        ),
    )
    observation = evidence.body["observation"]
    assert observation["attempts"] == 3
    assert observation["duration_ms"] == 3000
    assert observation["started_at"].endswith("+00:00")
    assert observation["finished_at"].endswith("+00:00")
    assert evidence.attempts == 3


def test_finished_before_started_is_refused() -> None:
    with pytest.raises(AuditWriteError):
        _observation(
            started_at=T0 + timedelta(seconds=9),
            finished_at=T0 + timedelta(seconds=8),
        )


def test_naive_timestamps_are_refused() -> None:
    with pytest.raises(AuditWriteError):
        _observation(started_at=datetime(2026, 8, 9, 12, 0, 5))


# --------------------------------------------------------------------------
# A3-14 — redaction fail-closed
# --------------------------------------------------------------------------


SECRET_SAMPLES = (
    "ghp_0123456789abcdef0123456789abcdef0123",
    "github_pat_11ABCDEFG0abcdefghij",
    "Bearer sk-liveTokenValue",
    "-----BEGIN RSA PRIVATE KEY-----",
    "client_secret=abc",
    "installation_id 12345",
    "x-hub-signature-256: sha256=deadbeef",
    "A" * 96,
)


@pytest.mark.parametrize("sample", SECRET_SAMPLES)
def test_secret_like_values_are_detected(sample: str) -> None:
    assert looks_secret_bearing(sample) is True


@pytest.mark.parametrize("sample", ["github.create_branch", "owner/repo", "2xx", DIGEST_A])
def test_legitimate_values_are_not_flagged(sample: str) -> None:
    assert looks_secret_bearing(sample) is False


@pytest.mark.parametrize("sample", SECRET_SAMPLES)
def test_audit_redaction_fail_closed_on_principal(sample: str) -> None:
    with pytest.raises(AuditWriteError):
        _intent(principal=sample)


def test_audit_redaction_fail_closed_on_artifact_refs() -> None:
    with pytest.raises(AuditWriteError) as excinfo:
        _observation(artifact_refs={"token": "ghp_" + "a" * 36})
    assert excinfo.value.reason is MutationReasonCode.REDACTION_UNPROVEN


def test_artifact_refs_reject_urls_and_paths() -> None:
    for value in ("https://api.github.com/repos/o/r", "/etc/passwd", "~/secrets"):
        with pytest.raises(AuditWriteError):
            _observation(artifact_refs={"ref": value})


def test_unknown_precondition_key_is_refused() -> None:
    with pytest.raises(AuditWriteError) as excinfo:
        _intent(preconditions_observed={"authorization": BASE_SHA})
    assert excinfo.value.reason is MutationReasonCode.REDACTION_UNPROVEN


def test_absent_optional_fields_are_omitted_not_nulled(ledger) -> None:
    handle = ledger.begin(_intent(registry_snapshot_hash=None))
    body = handle.intent.as_canonical()
    assert "registry_snapshot_hash" not in body
    assert "approval" not in body
    evidence = ledger.finalize(
        handle,
        _observation(status_class=None, result_digest=None),
    )
    observation = evidence.body["observation"]
    assert "status_class" not in observation
    assert "result_digest" not in observation
    assert "artifact_refs" not in observation


def test_no_secret_marker_appears_in_serialized_evidence(ledger) -> None:
    handle = ledger.begin(
        _intent(
            approval=ApprovalReference(
                approval_id="apr-2026-0001",
                state=ApprovalState.PENDING,
                bound_digest=DIGEST_A,
            ),
            policy_decision=PolicyDecision.APPROVAL_REQUIRED,
        )
    )
    evidence = ledger.finalize(handle, _observation(artifact_refs={"ref": "refs-heads-x"}))
    serialized = json.dumps(evidence.as_canonical()).lower()
    for marker in ("ghp_", "bearer ", "authorization", "private_key", "client_secret"):
        assert marker not in serialized


def test_repr_never_leaks_directory_or_payload(tmp_path: Path) -> None:
    sink = FileAuditSink(tmp_path / "audit")
    assert str(tmp_path) not in repr(sink)
    snapshot = _capability()
    assert SNAPSHOT_HASH not in repr(snapshot)


# --------------------------------------------------------------------------
# A3-14 — bounded metric cardinality
# --------------------------------------------------------------------------


def test_metric_labels_bounded_cardinality() -> None:
    labels = audit_metric_labels(
        operation="github.create_branch",
        evidence_class=EvidenceClass.SUCCESS,
        stage=MutationStage.PROVIDER_CALL,
    )
    assert set(labels) == AUDIT_METRIC_LABELS
    assert labels == {
        "operation": "github.create_branch",
        "outcome": "SUCCESS",
        "stage": "PROVIDER_CALL",
        "reason": "NONE",
    }


def test_metric_labels_reject_high_cardinality_values() -> None:
    for bad in ("pestoura/hermes-mcp-bridge", DIGEST_A, "refs/heads/feature-x", "42"):
        with pytest.raises(AuditWriteError):
            audit_metric_labels(
                operation=bad,
                evidence_class=EvidenceClass.FAILED,
                stage=MutationStage.PROVIDER_CALL,
            )


def test_metric_label_values_come_from_finite_domains() -> None:
    labels = audit_metric_labels(
        operation="github.create_pr",
        evidence_class=EvidenceClass.BLOCKED,
        stage=MutationStage.APPROVAL,
        reason=MutationReasonCode.APPROVAL_EXPIRED,
    )
    assert labels["outcome"] in {member.value for member in EvidenceClass}
    assert labels["stage"] in {member.value for member in MutationStage}
    assert labels["reason"] in {member.value for member in MutationReasonCode}


# --------------------------------------------------------------------------
# Gating of the intent itself
# --------------------------------------------------------------------------


def test_approval_required_without_approval_is_denied(ledger) -> None:
    with pytest.raises(AuditWriteError) as excinfo:
        ledger.begin(_intent(policy_decision=PolicyDecision.APPROVAL_REQUIRED))
    assert excinfo.value.reason is MutationReasonCode.APPROVAL_MISSING


def test_approval_bound_to_a_different_digest_is_denied(ledger) -> None:
    with pytest.raises(AuditWriteError) as excinfo:
        ledger.begin(
            _intent(
                policy_decision=PolicyDecision.APPROVAL_REQUIRED,
                approval=ApprovalReference(
                    approval_id="apr-1",
                    state=ApprovalState.PENDING,
                    bound_digest=DIGEST_B,
                ),
            )
        )
    assert excinfo.value.reason is MutationReasonCode.APPROVAL_DIGEST_MISMATCH


@pytest.mark.parametrize(
    "state", [ApprovalState.CONSUMED, ApprovalState.EXPIRED, ApprovalState.REVOKED]
)
def test_unusable_approval_is_denied(ledger, state) -> None:
    with pytest.raises(AuditWriteError):
        ledger.begin(
            _intent(
                policy_decision=PolicyDecision.APPROVAL_REQUIRED,
                approval=ApprovalReference(approval_id="apr-1", state=state, bound_digest=DIGEST_A),
            )
        )


def test_deny_decision_never_produces_a_write_ahead_record(ledger, tmp_path: Path) -> None:
    with pytest.raises(AuditWriteError):
        ledger.begin(_intent(policy_decision=PolicyDecision.DENY))
    assert list((tmp_path / "audit").glob("*.json")) == []


@pytest.mark.parametrize(
    "state",
    [
        CapabilityState.CONFIGURED,
        CapabilityState.AVAILABLE,
        CapabilityState.HEALTHY,
        CapabilityState.DEGRADED,
        CapabilityState.UNAVAILABLE,
        CapabilityState.DENIED,
    ],
)
def test_capability_not_ready_is_denied(ledger, state) -> None:
    with pytest.raises(AuditWriteError) as excinfo:
        ledger.begin(_intent(capability=_capability(state)))
    assert excinfo.value.reason is MutationReasonCode.WRITE_CAPABILITY_NOT_READY


@pytest.mark.parametrize("status", [IdempotencyStatus.REPLAYED, IdempotencyStatus.IN_PROGRESS])
def test_non_new_idempotency_status_issues_no_record(ledger, status, tmp_path: Path) -> None:
    with pytest.raises(AuditWriteError) as excinfo:
        ledger.begin(_intent(idempotency_status=status))
    assert excinfo.value.reason is MutationReasonCode.IDEMPOTENT_REPLAY
    assert list((tmp_path / "audit").glob("*.json")) == []


def test_read_capability_cannot_be_used_as_a_write_capability() -> None:
    with pytest.raises(AuditWriteError):
        CapabilitySnapshot(
            capability_id="github.read",  # type: ignore[arg-type]
            state=CapabilityState.READY,
            snapshot_hash=SNAPSHOT_HASH,
            policy_version="policy-1",
        )


def test_malformed_repository_is_refused() -> None:
    for bad in ("owner", "owner/repo/extra", "owner/../etc", ""):
        with pytest.raises(AuditWriteError):
            _intent(repository=bad)


# --------------------------------------------------------------------------
# Reconstructability and provenance chain
# --------------------------------------------------------------------------


def test_evidence_reconstructs_the_full_audit_question(ledger) -> None:
    handle = ledger.begin(
        _intent(
            policy_decision=PolicyDecision.APPROVAL_REQUIRED,
            approval=ApprovalReference(
                approval_id="apr-2026-0002",
                state=ApprovalState.PENDING,
                bound_digest=DIGEST_A,
            ),
            registry_snapshot_hash=SNAPSHOT_HASH,
        )
    )
    evidence = ledger.finalize(handle, _observation())

    intent = evidence.body["intent"]
    assert intent["principal"] == "svc-jarvas"  # who
    assert intent["operation"] == "github.create_branch"  # what
    assert intent["repository"] == "pestoura/hermes-mcp-bridge"  # where
    assert intent["policy_decision"] == "APPROVAL_REQUIRED"  # under which policy
    assert intent["capability"]["policy_version"] == "policy-2026-08-09"
    assert intent["registry_snapshot_hash"] == SNAPSHOT_HASH  # registry version
    assert intent["approval"]["approval_id"] == "apr-2026-0002"  # which approval
    assert intent["preconditions_observed"] == [{"key": "base_sha", "sha": BASE_SHA}]
    assert evidence.body["observation"]["outcome"] == "COMMITTED"  # what happened
    assert evidence.schema == MUTATION_EVIDENCE_SCHEMA


def test_evidence_digest_is_deterministic_and_binds_the_intent(ledger, tmp_path: Path) -> None:
    handle = ledger.begin(_intent())
    evidence = ledger.finalize(handle, _observation())
    assert evidence.intent_digest == handle.record_digest
    assert len(evidence.evidence_digest) == 64

    _ledger_clock.extend(T0 + timedelta(seconds=i) for i in range(50))
    twin = MutationAuditLedger(FileAuditSink(tmp_path / "twin"), clock=lambda: _ledger_clock.pop(0))
    twin_handle = twin.begin(_intent())
    twin_evidence = twin.finalize(twin_handle, _observation())
    # Audit ids differ, so digests must differ: the digest binds the attempt.
    assert twin_evidence.evidence_digest != evidence.evidence_digest
    assert twin_handle.audit_id != handle.audit_id


def test_open_attempts_are_tracked_and_released(ledger) -> None:
    assert ledger.open_attempts == 0
    handle = ledger.begin(_intent())
    assert ledger.open_attempts == 1
    ledger.finalize(handle, _observation())
    assert ledger.open_attempts == 0


def test_module_exposes_no_http_or_shell_surface() -> None:
    import inspect

    from hermes_mcp_bridge.v2 import mutation_audit

    source = inspect.getsource(mutation_audit)
    for forbidden in ("httpx", "subprocess", "requests.", "os.system", "shutil.rmtree"):
        assert forbidden not in source
