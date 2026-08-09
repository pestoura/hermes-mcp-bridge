"""Phase 3 lane L4 — write-ahead mutation audit, provenance and evidence.

Implements ``docs/v2/phase3/audit-and-evidence.md`` for DIRECT mutations. The
module is a *record keeper*: it issues no HTTP, executes no provider call,
resolves no credential and exposes no shell or filesystem surface beyond an
append-only audit directory it is explicitly given.

Contract in one paragraph
-------------------------

A mutation is admissible only after a **write-ahead intent record** has been
made durable. :meth:`MutationAuditLedger.begin` validates and persists that
record and returns an :class:`AuditHandle`; only a handle whose
:attr:`AuditHandle.provider_call_permitted` is ``True`` authorizes lane L5 to
issue the provider call. If the record cannot be validated or made durable the
ledger raises :class:`~hermes_mcp_bridge.v2.errors.AuditWriteError`
(``WRITE_AHEAD_AUDIT:AUDIT_RECORD_UNWRITABLE``) and no mutation may be
attempted. :meth:`MutationAuditLedger.finalize` closes the handle with an
explicit, non-inferred outcome and returns immutable
:class:`MutationEvidence`.

Fail-closed rules enforced here
-------------------------------

* **No write without a prior intent record.** ``finalize`` refuses a handle it
  did not issue, refuses a handle whose record is no longer durable, and
  refuses to be called twice.
* **Never infer success.** :class:`ProviderObservation` carries an explicit
  :class:`VerificationState`; ``SUCCESS`` requires ``VERIFIED`` read-back.
  Anything unverifiable becomes ``INDETERMINATE`` — never ``SUCCESS`` and
  never ``FAILED``.
* **Redaction is fail-closed.** Every string that enters a record is scanned;
  a value that looks credential-bearing, or a field that is not in the closed
  allow-list, is rejected rather than silently omitted, and optional fields
  whose safety cannot be proven are omitted rather than recorded.
* **Low cardinality.** :func:`audit_metric_labels` emits only the four
  allow-listed label names with finite domains; repository, ref, PR number,
  principal and digests are never labels.
"""

from __future__ import annotations

import os
import re
import tempfile
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum, unique
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

from .canonical import canonical_hash, canonical_json_bytes
from .enums import (
    ApprovalState,
    CapabilityState,
    IdempotencyStatus,
    MutationOutcome,
    MutationReasonCode,
    MutationStage,
    PolicyDecision,
    WriteCapabilityId,
)
from .errors import AuditWriteError

#: Envelope version of the audit record shape. A change here changes every
#: record digest and is a breaking change for lane L5.
MUTATION_AUDIT_SCHEMA: Final[str] = "v2.phase3.mutation-audit.1"

#: Envelope version of the finalized evidence shape.
MUTATION_EVIDENCE_SCHEMA: Final[str] = "v2.phase3.mutation-evidence.1"

#: The only metric label names this lane may emit (V2-SEC-024). Repository,
#: ref, PR identifier, principal, digests and approval ids are forbidden.
AUDIT_METRIC_LABELS: Final[frozenset[str]] = frozenset({"operation", "outcome", "stage", "reason"})

#: Lowercase 40-hex git object id.
_SHA1_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{40}$")
#: Lowercase 64-hex SHA-256 digest.
_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
#: ``owner/repo`` with GitHub-legal parts.
_REPOSITORY_RE: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$"
)
#: Canonical dotted tool id, e.g. ``github.create_branch``.
_OPERATION_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9]+(?:[._][a-z0-9]+)+$")
#: Opaque, non-secret identifiers (principal, approval id, audit id).
_OPAQUE_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
#: Precondition keys are a closed set; values are always git object ids.
_PRECONDITION_KEYS: Final[frozenset[str]] = frozenset(
    {"base_sha", "expected_head_sha", "merge_base_sha"}
)

#: Substrings that make a value inadmissible regardless of context. The scan is
#: a *rejection* gate, not a scrubber: nothing is rewritten to look safe.
_SECRET_MARKERS: Final[tuple[str, ...]] = (
    "ghp_",
    "gho_",
    "ghu_",
    "ghs_",
    "ghr_",
    "github_pat_",
    "-----begin",
    "authorization:",
    "bearer ",
    "basic ",
    "private_key",
    "client_secret",
    "installation_id",
    "access_token",
    "refresh_token",
    "password",
    "x-hub-signature",
)
#: A long unbroken high-entropy-looking token is refused unless it matched one
#: of the structured patterns above (sha1/sha256/opaque id) first.
_LONG_OPAQUE_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9+/_=-]{80,}")

_MAX_STRING_LENGTH: Final[int] = 256


@unique
class VerificationState(StrEnum):
    """Result of the post-call read-back of the mutated object.

    ``UNVERIFIABLE`` is the honest state for a read-back that could not be
    performed or could not be trusted; it never collapses into ``VERIFIED``.
    """

    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    VERIFIED = "VERIFIED"
    MISMATCH = "MISMATCH"
    UNVERIFIABLE = "UNVERIFIABLE"

    @property
    def proves_commit(self) -> bool:
        """Only a positive read-back may support a ``SUCCESS`` evidence class."""
        return self is VerificationState.VERIFIED


@unique
class EvidenceClass(StrEnum):
    """The four terminal evidence classes L5 and the gate consume.

    They are deliberately distinct from :class:`MutationOutcome`: the outcome
    describes the provider transaction, the evidence class describes what an
    auditor may conclude from the record.
    """

    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    INDETERMINATE = "INDETERMINATE"

    @property
    def is_terminal(self) -> bool:
        return True

    @property
    def mutation_confirmed(self) -> bool:
        return self is EvidenceClass.SUCCESS

    @property
    def requires_reconciliation(self) -> bool:
        return self is EvidenceClass.INDETERMINATE

    @property
    def allows_new_attempt(self) -> bool:
        """A new attempt is admissible only from a provably clean failure."""
        return self is EvidenceClass.FAILED


#: Outcome -> the only evidence class that outcome may produce. Any other pair
#: is a contract violation, not a warning.
_OUTCOME_TO_EVIDENCE: Final[Mapping[MutationOutcome, EvidenceClass]] = {
    MutationOutcome.COMMITTED: EvidenceClass.SUCCESS,
    MutationOutcome.FAILED_CLEAN: EvidenceClass.FAILED,
    MutationOutcome.DENIED: EvidenceClass.BLOCKED,
    MutationOutcome.AMBIGUOUS: EvidenceClass.INDETERMINATE,
}


def evidence_class_for(outcome: MutationOutcome) -> EvidenceClass:
    """Map a terminal :class:`MutationOutcome` to its evidence class.

    ``PENDING`` is not terminal and raises: an unfinished mutation has no
    evidence class, and defaulting it to anything would be an inference.
    """
    try:
        return _OUTCOME_TO_EVIDENCE[outcome]
    except KeyError:
        raise _audit_error(MutationReasonCode.AUDIT_RECORD_UNWRITABLE) from None


def _audit_error(reason: MutationReasonCode) -> AuditWriteError:
    return AuditWriteError(reason, MutationStage.WRITE_AHEAD_AUDIT)


def _redaction_error() -> AuditWriteError:
    return _audit_error(MutationReasonCode.REDACTION_UNPROVEN)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def looks_secret_bearing(value: str) -> bool:
    """Return ``True`` when ``value`` may not be recorded.

    Conservative by construction: an unknown long opaque blob is treated as
    secret-bearing. Callers must reject, never scrub.
    """
    if not isinstance(value, str):  # pragma: no cover - defensive
        return True
    lowered = value.lower()
    if any(marker in lowered for marker in _SECRET_MARKERS):
        return True
    return bool(_LONG_OPAQUE_RE.search(value))


def _require_safe_text(value: object, *, max_length: int = _MAX_STRING_LENGTH) -> str:
    if not isinstance(value, str) or not value:
        raise _redaction_error()
    if len(value) > max_length:
        raise _redaction_error()
    if any(ch in value for ch in ("\n", "\r", "\x00")):
        raise _redaction_error()
    if looks_secret_bearing(value):
        raise _redaction_error()
    return value


def _require_pattern(value: object, pattern: re.Pattern[str]) -> str:
    text = _require_safe_text(value)
    if not pattern.match(text):
        raise _audit_error(MutationReasonCode.AUDIT_RECORD_UNWRITABLE)
    return text


def _require_optional_pattern(value: object, pattern: re.Pattern[str]) -> str | None:
    """Omit a proven-absent field; reject an unprovable one."""
    if value is None:
        return None
    return _require_pattern(value, pattern)


def _require_preconditions(
    observed: Mapping[str, str] | None,
) -> tuple[tuple[str, str], ...]:
    if not observed:
        return ()
    items: list[tuple[str, str]] = []
    for key, value in observed.items():
        if key not in _PRECONDITION_KEYS:
            raise _redaction_error()
        items.append((key, _require_pattern(value, _SHA1_RE)))
    return tuple(sorted(items))


def _require_artifact_refs(refs: Mapping[str, str] | None) -> tuple[tuple[str, str], ...]:
    """Artifact references are opaque non-secret ids, never URLs or paths."""
    if not refs:
        return ()
    items: list[tuple[str, str]] = []
    for key, value in refs.items():
        safe_key = _require_pattern(key, _OPAQUE_ID_RE)
        text = _require_safe_text(value)
        if "://" in text or text.startswith("/") or text.startswith("~"):
            raise _redaction_error()
        if not _OPAQUE_ID_RE.match(text):
            raise _redaction_error()
        items.append((safe_key, text))
    return tuple(sorted(items))


@dataclass(frozen=True, slots=True)
class CapabilitySnapshot:
    """Non-secret snapshot of the write capability used for this mutation.

    Only the capability identity, its lifecycle state and the hash of the
    registry/capability snapshot are recorded. No permission material, no
    installation identity, no token, no expiry secret.
    """

    capability_id: WriteCapabilityId
    state: CapabilityState
    snapshot_hash: str
    policy_version: str

    def __post_init__(self) -> None:
        if not isinstance(self.capability_id, WriteCapabilityId):
            raise _audit_error(MutationReasonCode.WRITE_CAPABILITY_MISMATCH)
        if not isinstance(self.state, CapabilityState):
            raise _audit_error(MutationReasonCode.AUDIT_RECORD_UNWRITABLE)
        _require_pattern(self.snapshot_hash, _SHA256_RE)
        _require_pattern(self.policy_version, _OPAQUE_ID_RE)

    @property
    def is_ready(self) -> bool:
        return self.state.is_ready

    def as_canonical(self) -> dict[str, object]:
        return {
            "capability_id": self.capability_id.value,
            "state": self.state.value,
            "snapshot_hash": self.snapshot_hash,
            "policy_version": self.policy_version,
        }

    def __repr__(self) -> str:
        return (
            "CapabilitySnapshot("
            f"capability_id={self.capability_id.value!r}, state={self.state.value!r})"
        )


@dataclass(frozen=True, slots=True)
class ApprovalReference:
    """Reference to an approval, by id and state only.

    The approval *material* (nonce, trust context, approver credential) never
    enters the audit record; only the binding facts an auditor needs.
    """

    approval_id: str
    state: ApprovalState
    bound_digest: str

    def __post_init__(self) -> None:
        _require_pattern(self.approval_id, _OPAQUE_ID_RE)
        if not isinstance(self.state, ApprovalState):
            raise _audit_error(MutationReasonCode.AUDIT_RECORD_UNWRITABLE)
        _require_pattern(self.bound_digest, _SHA256_RE)

    def as_canonical(self) -> dict[str, object]:
        return {
            "approval_id": self.approval_id,
            "state": self.state.value,
            "bound_digest": self.bound_digest,
        }


@dataclass(frozen=True, slots=True)
class MutationIntent:
    """Everything the caller must prove *before* a provider call is allowed.

    Construction validates and normalizes; an invalid intent can never become
    a durable record, and a record that is not durable can never authorize a
    provider call.
    """

    principal: str
    operation: str
    repository: str
    operation_digest: str
    policy_decision: PolicyDecision
    capability: CapabilitySnapshot
    idempotency_key: str
    idempotency_status: IdempotencyStatus
    approval: ApprovalReference | None = None
    preconditions_observed: Mapping[str, str] | None = None
    registry_snapshot_hash: str | None = None

    def __post_init__(self) -> None:
        _require_pattern(self.principal, _OPAQUE_ID_RE)
        _require_pattern(self.operation, _OPERATION_RE)
        _require_pattern(self.repository, _REPOSITORY_RE)
        _require_pattern(self.operation_digest, _SHA256_RE)
        _require_pattern(self.idempotency_key, _SHA256_RE)
        if not isinstance(self.policy_decision, PolicyDecision):
            raise _audit_error(MutationReasonCode.AUDIT_RECORD_UNWRITABLE)
        if not isinstance(self.idempotency_status, IdempotencyStatus):
            raise _audit_error(MutationReasonCode.AUDIT_RECORD_UNWRITABLE)
        if not isinstance(self.capability, CapabilitySnapshot):
            raise _audit_error(MutationReasonCode.AUDIT_RECORD_UNWRITABLE)
        if self.approval is not None and not isinstance(self.approval, ApprovalReference):
            raise _audit_error(MutationReasonCode.AUDIT_RECORD_UNWRITABLE)
        _require_optional_pattern(self.registry_snapshot_hash, _SHA256_RE)
        _require_preconditions(self.preconditions_observed)

    @property
    def approval_required(self) -> bool:
        return self.policy_decision is PolicyDecision.APPROVAL_REQUIRED

    def as_canonical(self) -> dict[str, object]:
        """Canonical, redaction-checked body of the write-ahead record."""
        payload: dict[str, object] = {
            "schema": MUTATION_AUDIT_SCHEMA,
            "principal": self.principal,
            "operation": self.operation,
            "repository": self.repository,
            "operation_digest": self.operation_digest,
            "policy_decision": self.policy_decision.value,
            "capability": self.capability.as_canonical(),
            "idempotency_key": self.idempotency_key,
            "idempotency_status": self.idempotency_status.value,
            "preconditions_observed": [
                {"key": key, "sha": value}
                for key, value in _require_preconditions(self.preconditions_observed)
            ],
        }
        # Optional fields are *omitted* when absent — never emitted as null,
        # so an auditor cannot confuse "not applicable" with "unrecorded".
        if self.approval is not None:
            payload["approval"] = self.approval.as_canonical()
        if self.registry_snapshot_hash is not None:
            payload["registry_snapshot_hash"] = self.registry_snapshot_hash
        return payload

    def __repr__(self) -> str:
        return (
            "MutationIntent("
            f"operation={self.operation!r}, "
            f"policy_decision={self.policy_decision.value!r}, "
            f"digest={self.operation_digest[:12]!r}…)"
        )


@dataclass(frozen=True, slots=True)
class ProviderObservation:
    """What the executor actually observed, stated explicitly.

    The outcome is supplied by the caller and cross-checked here; this module
    refuses to *derive* ``COMMITTED`` from a status code, a truthy response or
    an absent exception.
    """

    outcome: MutationOutcome
    verification: VerificationState
    attempts: int
    started_at: datetime
    finished_at: datetime
    status_class: str | None = None
    reason: MutationReasonCode | None = None
    result_digest: str | None = None
    artifact_refs: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, MutationOutcome):
            raise _audit_error(MutationReasonCode.AUDIT_RECORD_UNWRITABLE)
        if not isinstance(self.verification, VerificationState):
            raise _audit_error(MutationReasonCode.AUDIT_RECORD_UNWRITABLE)
        if self.outcome is MutationOutcome.PENDING:
            raise _audit_error(MutationReasonCode.AUDIT_RECORD_UNWRITABLE)
        if isinstance(self.attempts, bool) or not isinstance(self.attempts, int):
            raise _audit_error(MutationReasonCode.AUDIT_RECORD_UNWRITABLE)
        if self.attempts < 0:
            raise _audit_error(MutationReasonCode.AUDIT_RECORD_UNWRITABLE)
        for moment in (self.started_at, self.finished_at):
            if not isinstance(moment, datetime) or moment.tzinfo is None:
                raise _audit_error(MutationReasonCode.AUDIT_RECORD_UNWRITABLE)
        if self.finished_at < self.started_at:
            raise _audit_error(MutationReasonCode.AUDIT_RECORD_UNWRITABLE)
        if self.reason is not None and not isinstance(self.reason, MutationReasonCode):
            raise _audit_error(MutationReasonCode.AUDIT_RECORD_UNWRITABLE)
        if self.status_class is not None:
            status = _require_safe_text(self.status_class, max_length=8)
            if not re.fullmatch(r"[1-5]xx", status):
                raise _audit_error(MutationReasonCode.AUDIT_RECORD_UNWRITABLE)
        _require_optional_pattern(self.result_digest, _SHA256_RE)
        _require_artifact_refs(self.artifact_refs)
        # A commit claim without a positive read-back is exactly the ambiguity
        # this lane exists to prevent.
        if self.outcome is MutationOutcome.COMMITTED and not self.verification.proves_commit:
            raise _audit_error(MutationReasonCode.RECONCILIATION_REQUIRED)
        if self.outcome is MutationOutcome.DENIED and self.attempts != 0:
            raise _audit_error(MutationReasonCode.AUDIT_RECORD_UNWRITABLE)

    @property
    def evidence_class(self) -> EvidenceClass:
        return evidence_class_for(self.outcome)

    @property
    def duration_ms(self) -> int:
        delta = self.finished_at - self.started_at
        return int(delta.total_seconds() * 1000)

    def as_canonical(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "outcome": self.outcome.value,
            "evidence_class": self.evidence_class.value,
            "verification": self.verification.value,
            "attempts": self.attempts,
            "started_at": self.started_at.astimezone(UTC).isoformat(),
            "finished_at": self.finished_at.astimezone(UTC).isoformat(),
            "duration_ms": self.duration_ms,
        }
        if self.status_class is not None:
            payload["status_class"] = self.status_class
        if self.reason is not None:
            payload["reason"] = self.reason.value
        if self.result_digest is not None:
            payload["result_digest"] = self.result_digest
        refs = _require_artifact_refs(self.artifact_refs)
        if refs:
            payload["artifact_refs"] = [{"key": key, "ref": value} for key, value in refs]
        return payload

    def __repr__(self) -> str:
        return (
            "ProviderObservation("
            f"outcome={self.outcome.value!r}, verification={self.verification.value!r}, "
            f"attempts={self.attempts})"
        )


@runtime_checkable
class AuditSink(Protocol):
    """Durable append-only sink for audit records.

    ``append`` must either make the record durable or raise. A sink that
    returns without durability is a contract violation the ledger cannot
    detect, so the only sanctioned implementations are the ones in this
    module plus test doubles that raise honestly.
    """

    def append(self, audit_id: str, payload: Mapping[str, object]) -> None: ...

    def exists(self, audit_id: str) -> bool: ...

    def read(self, audit_id: str) -> Mapping[str, object] | None: ...


class InMemoryAuditSink:
    """Non-durable sink for tests and dry-runs.

    Deliberately *not* usable as a production sink: :attr:`durable` is False
    and :class:`MutationAuditLedger` refuses a non-durable sink unless the
    caller explicitly opts in via ``allow_non_durable_sink=True``.
    """

    durable: Final[bool] = False

    def __init__(self) -> None:
        self._records: dict[str, dict[str, object]] = {}

    def append(self, audit_id: str, payload: Mapping[str, object]) -> None:
        if audit_id in self._records:
            raise _audit_error(MutationReasonCode.AUDIT_RECORD_UNWRITABLE)
        self._records[audit_id] = dict(payload)

    def exists(self, audit_id: str) -> bool:
        return audit_id in self._records

    def read(self, audit_id: str) -> Mapping[str, object] | None:
        record = self._records.get(audit_id)
        return dict(record) if record is not None else None

    def __len__(self) -> int:
        return len(self._records)

    def __iter__(self) -> Iterator[str]:
        return iter(tuple(self._records))


class FileAuditSink:
    """Append-only, fsync'd, one-file-per-record durable sink.

    The directory is the *only* filesystem surface this lane touches: file
    names are derived from a validated audit id, never from caller input, and
    no path is ever echoed back into a record, an error or a metric.
    """

    durable: Final[bool] = True

    def __init__(self, directory: Path | str, *, dir_mode: int = 0o700) -> None:
        path = Path(directory)
        try:
            path.mkdir(parents=True, exist_ok=True, mode=dir_mode)
        except OSError:
            raise _audit_error(MutationReasonCode.AUDIT_RECORD_UNWRITABLE) from None
        if not path.is_dir():
            raise _audit_error(MutationReasonCode.AUDIT_RECORD_UNWRITABLE)
        self._directory = path

    def _path_for(self, audit_id: str) -> Path:
        safe = _require_pattern(audit_id, _OPAQUE_ID_RE)
        if "/" in safe or safe.startswith("."):
            raise _audit_error(MutationReasonCode.AUDIT_RECORD_UNWRITABLE)
        return self._directory / f"{safe}.json"

    def append(self, audit_id: str, payload: Mapping[str, object]) -> None:
        target = self._path_for(audit_id)
        if target.exists():
            raise _audit_error(MutationReasonCode.AUDIT_RECORD_UNWRITABLE)
        data = canonical_json_bytes(dict(payload))
        tmp_fd, tmp_name = tempfile.mkstemp(dir=str(self._directory), suffix=".tmp")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(tmp_fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, target)
            dir_fd = os.open(str(self._directory), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except (OSError, TypeError, ValueError):
            tmp_path.unlink(missing_ok=True)
            raise _audit_error(MutationReasonCode.AUDIT_RECORD_UNWRITABLE) from None

    def exists(self, audit_id: str) -> bool:
        try:
            return self._path_for(audit_id).is_file()
        except AuditWriteError:
            return False

    def read(self, audit_id: str) -> Mapping[str, object] | None:
        import json

        try:
            target = self._path_for(audit_id)
            if not target.is_file():
                return None
            return json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError, AuditWriteError):
            raise _audit_error(MutationReasonCode.AUDIT_RECORD_UNWRITABLE) from None

    def __repr__(self) -> str:
        return "FileAuditSink(<directory redacted>)"


@dataclass(slots=True)
class AuditHandle:
    """Proof that a write-ahead record for this attempt is durable.

    Lane L5 must treat this as the sole authorization token for stage
    ``PROVIDER_CALL``: no handle, no call.
    """

    audit_id: str
    record_digest: str
    intent: MutationIntent
    recorded_at: datetime
    _ledger_token: object = field(repr=False)
    _finalized: bool = field(default=False, repr=False)

    @property
    def provider_call_permitted(self) -> bool:
        """True while the record is durable and the attempt is unfinalized."""
        return not self._finalized

    @property
    def finalized(self) -> bool:
        return self._finalized

    def __repr__(self) -> str:
        return (
            "AuditHandle("
            f"audit_id={self.audit_id!r}, digest={self.record_digest[:12]!r}…, "
            f"finalized={self._finalized})"
        )


@dataclass(frozen=True, slots=True)
class MutationEvidence:
    """Immutable, publishable evidence for exactly one mutation attempt.

    Reconstructability (audit-and-evidence.md invariant 4): from this record
    alone an auditor answers who, what, where, under which policy/registry
    version, with which approval, with what observed preconditions, and what
    the provider did — with no secret and no raw payload.
    """

    schema: str
    audit_id: str
    intent_digest: str
    evidence_class: EvidenceClass
    outcome: MutationOutcome
    verification: VerificationState
    attempts: int
    recorded_at: datetime
    finalized_at: datetime
    body: Mapping[str, object]
    evidence_digest: str

    @property
    def mutation_confirmed(self) -> bool:
        return self.evidence_class.mutation_confirmed

    @property
    def requires_reconciliation(self) -> bool:
        return self.evidence_class.requires_reconciliation

    def as_canonical(self) -> dict[str, object]:
        return dict(self.body)

    def __repr__(self) -> str:
        return (
            "MutationEvidence("
            f"audit_id={self.audit_id!r}, class={self.evidence_class.value!r}, "
            f"digest={self.evidence_digest[:12]!r}…)"
        )


def audit_metric_labels(
    *,
    operation: str,
    evidence_class: EvidenceClass,
    stage: MutationStage,
    reason: MutationReasonCode | None = None,
) -> dict[str, str]:
    """Return the only label set this lane may emit.

    Repository, ref, PR number, principal, approval id, digests and any
    caller string other than the canonical operation id are forbidden as
    labels (V2-SEC-024). ``operation`` is bounded by the registry.
    """
    labels = {
        "operation": _require_pattern(operation, _OPERATION_RE),
        "outcome": evidence_class.value,
        "stage": stage.value,
        "reason": reason.value if reason is not None else "NONE",
    }
    if set(labels) != AUDIT_METRIC_LABELS:  # pragma: no cover - defensive
        raise _audit_error(MutationReasonCode.AUDIT_RECORD_UNWRITABLE)
    return labels


class MutationAuditLedger:
    """Write-ahead audit ledger: the gate between preflight and provider call.

    Usage from lane L5, exactly in this order::

        handle = ledger.begin(intent)          # stage WRITE_AHEAD_AUDIT
        if not handle.provider_call_permitted: # never reachable on success
            raise ...
        observation = _call_provider(...)      # stage PROVIDER_CALL
        evidence = ledger.finalize(handle, observation)

    Any exception from :meth:`begin` forbids the provider call. There is no
    "best effort" mode and no bypass flag.
    """

    def __init__(
        self,
        sink: AuditSink,
        *,
        allow_non_durable_sink: bool = False,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(sink, AuditSink):
            raise _audit_error(MutationReasonCode.AUDIT_RECORD_UNWRITABLE)
        durable = bool(getattr(sink, "durable", False))
        if not durable and not allow_non_durable_sink:
            raise _audit_error(MutationReasonCode.AUDIT_RECORD_UNWRITABLE)
        self._sink = sink
        self._clock: Callable[[], datetime] = clock if callable(clock) else _utc_now
        self._token = object()
        self._open: dict[str, AuditHandle] = {}

    @property
    def open_attempts(self) -> int:
        return len(self._open)

    def _new_audit_id(self) -> str:
        return f"mut-{uuid.uuid4().hex}"

    def begin(self, intent: MutationIntent) -> AuditHandle:
        """Persist the write-ahead record. Fail-closed on any doubt."""
        if not isinstance(intent, MutationIntent):
            raise _audit_error(MutationReasonCode.AUDIT_RECORD_UNWRITABLE)
        # An approval-required decision without a usable approval reference is
        # not auditable, therefore not executable.
        if intent.approval_required:
            if intent.approval is None or not intent.approval.state.is_usable:
                raise _audit_error(MutationReasonCode.APPROVAL_MISSING)
            if intent.approval.bound_digest != intent.operation_digest:
                raise _audit_error(MutationReasonCode.APPROVAL_DIGEST_MISMATCH)
        if intent.policy_decision is PolicyDecision.DENY:
            raise _audit_error(MutationReasonCode.AUDIT_RECORD_UNWRITABLE)
        if not intent.capability.is_ready:
            raise _audit_error(MutationReasonCode.WRITE_CAPABILITY_NOT_READY)
        if not intent.idempotency_status.executes_provider_call:
            raise _audit_error(MutationReasonCode.IDEMPOTENT_REPLAY)

        audit_id = self._new_audit_id()
        recorded_at = self._clock()
        if not isinstance(recorded_at, datetime) or recorded_at.tzinfo is None:
            raise _audit_error(MutationReasonCode.AUDIT_RECORD_UNWRITABLE)

        body = intent.as_canonical()
        body["audit_id"] = audit_id
        body["recorded_at"] = recorded_at.astimezone(UTC).isoformat()
        body["stage"] = MutationStage.WRITE_AHEAD_AUDIT.value
        body["outcome"] = MutationOutcome.PENDING.value
        record_digest = canonical_hash(body)
        body["record_digest"] = record_digest

        try:
            self._sink.append(audit_id, body)
        except AuditWriteError:
            raise
        except Exception:
            raise _audit_error(MutationReasonCode.AUDIT_RECORD_UNWRITABLE) from None

        # Durability is *verified*, not assumed: a sink that silently dropped
        # the record must not authorize a mutation.
        try:
            durable = self._sink.exists(audit_id)
        except Exception:
            durable = False
        if not durable:
            raise _audit_error(MutationReasonCode.AUDIT_RECORD_UNWRITABLE)

        handle = AuditHandle(
            audit_id=audit_id,
            record_digest=record_digest,
            intent=intent,
            recorded_at=recorded_at,
            _ledger_token=self._token,
        )
        self._open[audit_id] = handle
        return handle

    def finalize(self, handle: AuditHandle, observation: ProviderObservation) -> MutationEvidence:
        """Close an attempt with an explicit outcome and emit evidence."""
        if not isinstance(handle, AuditHandle) or handle._ledger_token is not self._token:
            raise _audit_error(MutationReasonCode.AUDIT_RECORD_UNWRITABLE)
        if handle.finalized or self._open.get(handle.audit_id) is not handle:
            raise _audit_error(MutationReasonCode.AUDIT_RECORD_UNWRITABLE)
        if not isinstance(observation, ProviderObservation):
            raise _audit_error(MutationReasonCode.AUDIT_RECORD_UNWRITABLE)
        if observation.started_at < handle.recorded_at:
            # The provider call must not predate the write-ahead record.
            raise _audit_error(MutationReasonCode.AUDIT_RECORD_UNWRITABLE)
        try:
            stored = self._sink.read(handle.audit_id)
        except AuditWriteError:
            raise
        except Exception:
            raise _audit_error(MutationReasonCode.AUDIT_RECORD_UNWRITABLE) from None
        if not stored or stored.get("record_digest") != handle.record_digest:
            raise _audit_error(MutationReasonCode.AUDIT_RECORD_UNWRITABLE)

        finalized_at = self._clock()
        body: dict[str, object] = {
            "schema": MUTATION_EVIDENCE_SCHEMA,
            "audit_id": handle.audit_id,
            "intent_digest": handle.record_digest,
            "intent": handle.intent.as_canonical(),
            "observation": observation.as_canonical(),
            "recorded_at": handle.recorded_at.astimezone(UTC).isoformat(),
            "finalized_at": finalized_at.astimezone(UTC).isoformat(),
        }
        evidence_digest = canonical_hash(body)
        body["evidence_digest"] = evidence_digest

        handle._finalized = True
        del self._open[handle.audit_id]

        return MutationEvidence(
            schema=MUTATION_EVIDENCE_SCHEMA,
            audit_id=handle.audit_id,
            intent_digest=handle.record_digest,
            evidence_class=observation.evidence_class,
            outcome=observation.outcome,
            verification=observation.verification,
            attempts=observation.attempts,
            recorded_at=handle.recorded_at,
            finalized_at=finalized_at,
            body=body,
            evidence_digest=evidence_digest,
        )

    def has_write_ahead_record(self, audit_id: str) -> bool:
        """Gate helper: a committed mutation without a record is a failure."""
        try:
            return self._sink.exists(audit_id)
        except Exception:
            return False

    def __repr__(self) -> str:
        return f"MutationAuditLedger(open_attempts={len(self._open)})"


__all__ = [
    "AUDIT_METRIC_LABELS",
    "MUTATION_AUDIT_SCHEMA",
    "MUTATION_EVIDENCE_SCHEMA",
    "ApprovalReference",
    "AuditHandle",
    "AuditSink",
    "CapabilitySnapshot",
    "EvidenceClass",
    "FileAuditSink",
    "InMemoryAuditSink",
    "MutationAuditLedger",
    "MutationEvidence",
    "MutationIntent",
    "ProviderObservation",
    "VerificationState",
    "audit_metric_labels",
    "evidence_class_for",
    "looks_secret_bearing",
]
