"""Phase 3 canonical operation digest and digest-bound approval consumption.

Lane L3 (ADR-0021, ``docs/v2/phase3/approval-and-digest.md``). This module is
pure, deterministic and offline: it performs no I/O, spawns no process, opens
no socket and reads no file.

What it guarantees
------------------

* **Deterministic canonicalization.** The digest is
  ``SHA-256(canonical_json(payload))`` using the accepted Phase 1
  canonicalization (:mod:`hermes_mcp_bridge.v2.canonical`): sorted keys, fixed
  separators, UTF-8, floats rejected. Two semantically identical descriptors
  produce the same digest on any platform; any semantic change — an edited PR
  body, a moved SHA, a policy bump, a registry snapshot change — produces a
  different digest.
* **State binding, not intent binding.** The expected SHAs are inside the
  digest, so an approval cannot survive drift (ADR-0021 / T3-02, T3-03).
* **Single-use approvals, atomically consumed.** Consumption is one guarded
  transition that both verifies ``consumed_at IS NULL`` and sets it; a losing
  racer receives ``APPROVAL_ALREADY_CONSUMED``.
* **Fail closed.** Unknown, revoked, expired, mis-scoped, mis-bound or
  already-consumed approvals all raise a subclass of
  :class:`~hermes_mcp_bridge.v2.errors.ApprovalError` carrying a stable,
  redacted ``STAGE:REASON`` pair. There is no permissive branch.
* **No secrets, no payloads.** Neither the digest inputs' values nor the
  approval nonce ever appear in a key, an error message or an evidence record.
  Evidence exposes identifiers, digests and enum tokens only.

Time is always injected by the caller (``now``) so behaviour is reproducible.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Final

from .canonical import canonical_hash, canonical_json_text
from .enums import (
    ApprovalState,
    MutationReasonCode,
    MutationStage,
    WriteCapabilityId,
)
from .errors import (
    ApprovalError,
    DigestMismatchError,
    MutationDeniedError,
)

#: Envelope version of the digest payload. Changing it changes every digest.
OPERATION_DIGEST_SCHEMA: Final[str] = "v2.phase3.operation.1"

#: Proposed TTLs (``approval-and-digest.md``). Exposed as data so an operator
#: policy can pick one explicitly; nothing here applies a TTL implicitly.
APPROVAL_TTL_SECONDS_CREATE: Final[int] = 3600
APPROVAL_TTL_SECONDS_MERGE: Final[int] = 900

_SHA_RE: Final[re.Pattern[str]] = re.compile(r"\A[0-9a-f]{40}\Z")
_DIGEST_RE: Final[re.Pattern[str]] = re.compile(r"\A[0-9a-f]{64}\Z")
_REPOSITORY_RE: Final[re.Pattern[str]] = re.compile(
    r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,38}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z"
)
_OPERATION_RE: Final[re.Pattern[str]] = re.compile(r"\A[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+\Z")
_IDENTIFIER_RE: Final[re.Pattern[str]] = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:@=+-]{0,127}\Z")

_MERGE_OPERATION_SUFFIX: Final[str] = ".merge_pr"


def _deny(
    reason: MutationReasonCode,
    stage: MutationStage = MutationStage.APPROVAL,
    *,
    detail: str = "",
) -> MutationDeniedError:
    return MutationDeniedError(reason, stage, detail=detail)


def _require_identifier(value: str, *, detail: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.match(value):
        raise _deny(MutationReasonCode.INVALID_ARGUMENTS, detail=detail)
    return value


def _require_sha(value: str, *, detail: str) -> str:
    if not isinstance(value, str) or not _SHA_RE.match(value):
        raise _deny(MutationReasonCode.INVALID_ARGUMENTS, detail=detail)
    return value


def require_sha(value: str, *, detail: str = "sha") -> str:
    """Validate a 40-hex lowercase commit SHA, failing closed on anything else."""
    return _require_sha(value, detail=detail)


def require_digest(value: str, *, detail: str = "operation_digest") -> str:
    """Validate a 64-hex lowercase digest, failing closed on anything else."""
    if not isinstance(value, str) or not _DIGEST_RE.match(value):
        raise _deny(MutationReasonCode.INVALID_ARGUMENTS, detail=detail)
    return value


def require_repository(value: str) -> str:
    """Validate an ``owner/repo`` identifier. No path traversal, no URL."""
    if not isinstance(value, str) or not _REPOSITORY_RE.match(value):
        raise _deny(
            MutationReasonCode.INVALID_ARGUMENTS,
            MutationStage.SCOPE,
            detail="repository",
        )
    return value


def _require_operation(value: str) -> str:
    if not isinstance(value, str) or not _OPERATION_RE.match(value):
        raise _deny(
            MutationReasonCode.INVALID_ARGUMENTS,
            MutationStage.REGISTRY,
            detail="operation",
        )
    return value


def _canonical_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Return a plain dict of fully-resolved arguments, rejecting non-canonical values.

    The digest covers *resolved* arguments: nothing may be defaulted, templated
    or expanded afterwards, so a non-serializable value is a hard failure
    rather than something to coerce.
    """
    if not isinstance(arguments, Mapping):
        raise _deny(MutationReasonCode.INVALID_ARGUMENTS, detail="arguments")
    resolved = dict(arguments)
    try:
        canonical_json_text(resolved)
    except TypeError as exc:
        raise _deny(MutationReasonCode.INVALID_ARGUMENTS, detail="arguments") from exc
    return resolved


@dataclass(frozen=True, slots=True)
class OperationPreconditions:
    """Expected provider state bound into the digest.

    At least one SHA precondition must be present: a Phase 3 mutation always
    pins the state it was approved against (``IDEMPOTENT_BY_PRECONDITION``).
    """

    base_sha: str | None = None
    expected_head_sha: str | None = None
    required_checks_policy: str | None = None

    def __post_init__(self) -> None:
        if self.base_sha is None and self.expected_head_sha is None:
            raise _deny(
                MutationReasonCode.INVALID_ARGUMENTS,
                MutationStage.PRECONDITION_REVALIDATION,
                detail="preconditions",
            )
        if self.base_sha is not None:
            _require_sha(self.base_sha, detail="base_sha")
        if self.expected_head_sha is not None:
            _require_sha(self.expected_head_sha, detail="expected_head_sha")
        if self.required_checks_policy is not None:
            _require_identifier(self.required_checks_policy, detail="required_checks_policy")

    def canonical_payload(self) -> dict[str, str]:
        payload: dict[str, str] = {}
        if self.base_sha is not None:
            payload["base_sha"] = self.base_sha
        if self.expected_head_sha is not None:
            payload["expected_head_sha"] = self.expected_head_sha
        if self.required_checks_policy is not None:
            payload["required_checks_policy"] = self.required_checks_policy
        return payload


@dataclass(frozen=True, slots=True)
class OperationDescriptor:
    """The fully-resolved single-node mutation an approval may bind to."""

    operation: str
    capability: WriteCapabilityId
    repository: str
    arguments: Mapping[str, Any]
    preconditions: OperationPreconditions
    policy_version: str
    registry_snapshot_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "operation", _require_operation(self.operation))
        if not isinstance(self.capability, WriteCapabilityId):
            raise _deny(
                MutationReasonCode.WRITE_CAPABILITY_MISMATCH,
                MutationStage.CREDENTIAL,
                detail="capability",
            )
        object.__setattr__(self, "repository", require_repository(self.repository))
        object.__setattr__(self, "arguments", _canonical_arguments(self.arguments))
        if not isinstance(self.preconditions, OperationPreconditions):
            raise _deny(
                MutationReasonCode.INVALID_ARGUMENTS,
                MutationStage.PRECONDITION_REVALIDATION,
                detail="preconditions",
            )
        object.__setattr__(
            self, "policy_version", _require_identifier(self.policy_version, detail="policy")
        )
        object.__setattr__(
            self,
            "registry_snapshot_hash",
            require_digest(self.registry_snapshot_hash, detail="registry_snapshot_hash"),
        )

    @property
    def requires_distinct_approver(self) -> bool:
        """Merges always need an approver distinct from the principal."""
        return self.operation.endswith(_MERGE_OPERATION_SUFFIX)

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "schema": OPERATION_DIGEST_SCHEMA,
            "operation": self.operation,
            "capability": self.capability.value,
            "repository": self.repository,
            "arguments": dict(self.arguments),
            "preconditions": self.preconditions.canonical_payload(),
            "policy_version": self.policy_version,
            "registry_snapshot_hash": self.registry_snapshot_hash,
        }


def compute_operation_digest(descriptor: OperationDescriptor) -> str:
    """Return the canonical ``operation_digest`` (lowercase 64-hex SHA-256)."""
    return canonical_hash(descriptor.canonical_payload())


def digest_evidence(descriptor: OperationDescriptor) -> dict[str, str]:
    """Non-secret evidence for a descriptor.

    Deliberately excludes ``arguments``: a PR body or branch name is caller
    content and must never land in an evidence record.
    """
    return {
        "schema": OPERATION_DIGEST_SCHEMA,
        "operation": descriptor.operation,
        "capability": descriptor.capability.value,
        "repository": descriptor.repository,
        "operation_digest": compute_operation_digest(descriptor),
        "policy_version": descriptor.policy_version,
        "registry_snapshot_hash": descriptor.registry_snapshot_hash,
    }


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    """A single-use approval bound to one ``operation_digest``."""

    approval_id: str
    principal: str
    approver: str
    operation_digest: str
    repository: str
    operation: str
    nonce: str
    expires_at: datetime
    trust_context: str
    state: ApprovalState = ApprovalState.PENDING
    consumed_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.approval_id, detail="approval_id")
        _require_identifier(self.principal, detail="principal")
        _require_identifier(self.approver, detail="approver")
        require_digest(self.operation_digest)
        require_repository(self.repository)
        _require_operation(self.operation)
        _require_identifier(self.nonce, detail="nonce")
        _require_identifier(self.trust_context, detail="trust_context")
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise _deny(MutationReasonCode.INVALID_ARGUMENTS, detail="expires_at")

    def is_expired(self, now: datetime) -> bool:
        return now.astimezone(UTC) >= self.expires_at.astimezone(UTC)

    def evidence(self) -> dict[str, str]:
        """Redacted evidence: never exposes ``nonce``."""
        return {
            "approval_id": self.approval_id,
            "operation": self.operation,
            "repository": self.repository,
            "operation_digest": self.operation_digest,
            "state": self.state.value,
            "trust_context": self.trust_context,
        }


class ApprovalStore:
    """In-memory, thread-safe approval store with atomic single-use consumption.

    The store is intentionally minimal and backend-agnostic: the *semantics*
    (atomicity, single use, digest binding, expiry, scope) are what lane L5
    depends on. A durable backend must reproduce exactly these semantics.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, ApprovalRecord] = {}

    def issue(self, record: ApprovalRecord) -> ApprovalRecord:
        """Register a ``PENDING`` approval. Re-issuing an id is rejected."""
        if record.state is not ApprovalState.PENDING or record.consumed_at is not None:
            raise _deny(MutationReasonCode.APPROVAL_ALREADY_CONSUMED, detail="issue")
        with self._lock:
            if record.approval_id in self._records:
                raise _deny(MutationReasonCode.APPROVAL_ALREADY_CONSUMED, detail="duplicate")
            self._records[record.approval_id] = record
        return record

    def get(self, approval_id: str) -> ApprovalRecord | None:
        with self._lock:
            return self._records.get(approval_id)

    def revoke(self, approval_id: str) -> None:
        with self._lock:
            existing = self._records.get(approval_id)
            if existing is None or existing.state is not ApprovalState.PENDING:
                return
            self._records[approval_id] = replace(existing, state=ApprovalState.REVOKED)

    def verify_and_consume(
        self,
        approval_id: str,
        descriptor: OperationDescriptor,
        *,
        principal: str,
        now: datetime,
    ) -> ApprovalRecord:
        """Atomically verify the binding and consume the approval.

        Order of checks is fixed and every failure is a DENY with a stable
        reason code. Consumption happens *before* the provider call; a clean
        provider failure never restores it (``approval-and-digest.md`` §2).
        """
        expected_digest = compute_operation_digest(descriptor)
        with self._lock:
            record = self._records.get(approval_id)
            if record is None:
                raise ApprovalError(MutationReasonCode.APPROVAL_UNKNOWN, MutationStage.APPROVAL)
            if record.state is ApprovalState.CONSUMED:
                raise ApprovalError(
                    MutationReasonCode.APPROVAL_ALREADY_CONSUMED, MutationStage.APPROVAL
                )
            if record.state is not ApprovalState.PENDING:
                # REVOKED/EXPIRED are reported without leaking which one.
                raise ApprovalError(MutationReasonCode.APPROVAL_UNKNOWN, MutationStage.APPROVAL)
            if record.is_expired(now):
                self._records[approval_id] = replace(record, state=ApprovalState.EXPIRED)
                raise ApprovalError(MutationReasonCode.APPROVAL_EXPIRED, MutationStage.APPROVAL)
            if record.operation_digest != expected_digest:
                raise DigestMismatchError(
                    MutationReasonCode.APPROVAL_DIGEST_MISMATCH, MutationStage.APPROVAL
                )
            if (
                record.repository != descriptor.repository
                or record.operation != descriptor.operation
                or record.principal != principal
            ):
                raise ApprovalError(
                    MutationReasonCode.APPROVAL_SCOPE_MISMATCH, MutationStage.APPROVAL
                )
            if descriptor.requires_distinct_approver and record.approver == record.principal:
                raise ApprovalError(
                    MutationReasonCode.APPROVER_NOT_DISTINCT, MutationStage.APPROVAL
                )
            consumed = replace(
                record,
                state=ApprovalState.CONSUMED,
                consumed_at=now.astimezone(UTC),
            )
            self._records[approval_id] = consumed
            return consumed

    def purge_expired(self, now: datetime) -> int:
        """Mark expired ``PENDING`` records. Returns how many transitioned."""
        transitioned = 0
        with self._lock:
            for approval_id, record in list(self._records.items()):
                if record.state is ApprovalState.PENDING and record.is_expired(now):
                    self._records[approval_id] = replace(record, state=ApprovalState.EXPIRED)
                    transitioned += 1
        return transitioned


@dataclass(frozen=True, slots=True)
class DigestBinding:
    """Result of a successful approval verification, for lane L5."""

    descriptor: OperationDescriptor
    approval: ApprovalRecord
    operation_digest: str = field(default="")

    def __post_init__(self) -> None:
        if not self.operation_digest:
            object.__setattr__(
                self, "operation_digest", compute_operation_digest(self.descriptor)
            )

    def evidence(self) -> dict[str, str]:
        merged = digest_evidence(self.descriptor)
        merged.update(self.approval.evidence())
        return merged


__all__ = [
    "APPROVAL_TTL_SECONDS_CREATE",
    "APPROVAL_TTL_SECONDS_MERGE",
    "OPERATION_DIGEST_SCHEMA",
    "ApprovalRecord",
    "ApprovalStore",
    "DigestBinding",
    "OperationDescriptor",
    "OperationPreconditions",
    "compute_operation_digest",
    "digest_evidence",
    "require_digest",
    "require_repository",
    "require_sha",
]
