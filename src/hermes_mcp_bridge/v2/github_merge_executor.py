"""Phase 3 lane L6 — the governed merge executor.

Separate from :mod:`.github_mutations` (the create-only executor, lane L5) on
purpose: merge is the highest-risk operation in Phase 3 and carries its own
capability, policy rule and gate chain. This module adds exactly one write
verb (``PUT``) against exactly one path shape, and only after
:func:`~.github_governed_merge.evaluate_merge_gates` has passed in full.

Ordering (fixed, asserted):

``SCOPE -> REGISTRY -> POLICY -> CREDENTIAL -> APPROVAL -> IDEMPOTENCY ->
PRECONDITION_REVALIDATION -> WRITE_AHEAD_AUDIT -> PROVIDER_CALL -> READ_BACK ->
RESULT_SHAPING``

Fail-closed rules:

* one provider write per execution, never retried;
* ambiguity (transport failure, unenumerated status, unverifiable read-back)
  is ``AMBIGUOUS`` + ``RECONCILIATION_REQUIRED``, never a retry and never an
  automatic revert — a merged commit dead-letters to manual intervention;
* ``COMMITTED`` requires a read-back proving ``merged is True`` and the merged
  SHA matching the pinned head;
* no credential material, path, body or argument reaches a result or an error.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Protocol, runtime_checkable

from .enums import (
    MUTATION_STAGE_ORDER,
    ApprovalState,
    CapabilityState,
    IdempotencyStatus,
    MutationOutcome,
    MutationReasonCode,
    MutationStage,
    PolicyDecision,
)
from .errors import (
    MergeGovernanceError,
    MutationDeniedError,
    MutationIndeterminateError,
    MutationScopeError,
    WriteCapabilityError,
)
from .github_auth import GitHubAuthorization
from .github_direct import GitHubRepositoryScope
from .github_governed_merge import (
    MERGE_TOOL_ID,
    MERGE_WRITE_CAPABILITY,
    MergeGateReport,
    MergeObservation,
    MergePolicyRegistry,
    MergeRequest,
    classify_merge_status,
    evaluate_merge_gates,
    merge_endpoint,
    merge_request_body,
)
from .github_mutations import ProviderResponse, ProviderTransportError
from .github_write_credentials import WriteCapabilityBroker
from .mutation_audit import (
    ApprovalReference,
    CapabilitySnapshot,
    EvidenceClass,
    MutationAuditLedger,
    MutationIntent,
    ProviderObservation,
    VerificationState,
)
from .mutation_digest import (
    ApprovalStore,
    OperationDescriptor,
    OperationPreconditions,
    compute_operation_digest,
)
from .mutation_idempotency import IdempotencyStore, compute_idempotency_key

#: Result envelope version for a governed merge.
MERGE_RESULT_SCHEMA: Final[str] = "v2.phase3.merge-result.1"

DEFAULT_POLICY_VERSION: Final[str] = "phase3.merge.v1"

_STAGE_ORDER: Final[tuple[MutationStage, ...]] = tuple(MUTATION_STAGE_ORDER)


@runtime_checkable
class MergeTransport(Protocol):
    """The only provider surface L6 may use: one read verb and ``PUT``."""

    async def get_json(
        self,
        path: str,
        *,
        authorization: GitHubAuthorization,
        timeout_seconds: int,
    ) -> ProviderResponse: ...

    async def put_json(
        self,
        path: str,
        *,
        body: Mapping[str, Any],
        authorization: GitHubAuthorization,
        timeout_seconds: int,
    ) -> ProviderResponse: ...


@runtime_checkable
class MergeStateReader(Protocol):
    """Reads the live PR/checks/protection state used by the gate chain."""

    async def observe(
        self,
        request: MergeRequest,
        *,
        authorization: GitHubAuthorization,
    ) -> MergeObservation | None: ...


@dataclass(frozen=True, slots=True)
class MergeResult:
    """Bounded, typed merge result. No provider body, no secrets."""

    schema: str
    tool_id: str
    repository: str
    number: int
    outcome: MutationOutcome
    verification: VerificationState
    evidence_class: EvidenceClass
    idempotency_status: IdempotencyStatus
    operation_digest: str
    idempotency_key: str
    merged_sha: str | None
    audit_id: str | None
    evidence_digest: str | None
    gates: MergeGateReport | None
    stages: tuple[MutationStage, ...]
    provider_writes: int

    def as_canonical(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": self.schema,
            "tool_id": self.tool_id,
            "repository": self.repository,
            "number": self.number,
            "outcome": self.outcome.value,
            "verification": self.verification.value,
            "evidence_class": self.evidence_class.value,
            "idempotency_status": self.idempotency_status.value,
            "operation_digest": self.operation_digest,
            "idempotency_key": self.idempotency_key,
            "stages": [stage.value for stage in self.stages],
            "provider_writes": self.provider_writes,
        }
        if self.merged_sha is not None:
            payload["merged_sha"] = self.merged_sha
        if self.audit_id is not None:
            payload["audit_id"] = self.audit_id
        if self.evidence_digest is not None:
            payload["evidence_digest"] = self.evidence_digest
        if self.gates is not None:
            payload["gates"] = self.gates.canonical()
        return payload


def _now() -> datetime:
    return datetime.now(UTC)


def _indeterminate(stage: MutationStage) -> MutationIndeterminateError:
    return MutationIndeterminateError(MutationReasonCode.RECONCILIATION_REQUIRED, stage)


class _SingleWriteGuard:
    """Proves the one-provider-write invariant for a single merge execution."""

    __slots__ = ("writes",)

    def __init__(self) -> None:
        self.writes = 0

    def claim(self) -> None:
        if self.writes:
            raise MutationDeniedError(
                MutationReasonCode.RECONCILIATION_REQUIRED,
                MutationStage.PROVIDER_CALL,
                detail="duplicate_provider_write",
            )
        self.writes += 1


class GovernedMergeExecutor:
    """Execute ``github.merge_pr`` under the full Phase 3 + L6 gate chain."""

    __slots__ = (
        "_approvals",
        "_broker",
        "_clock",
        "_idempotency",
        "_ledger",
        "_merge_policies",
        "_policy_version",
        "_reader",
        "_registry_snapshot_hash",
        "_scope",
        "_transport",
    )

    def __init__(
        self,
        *,
        broker: WriteCapabilityBroker,
        scope: GitHubRepositoryScope,
        merge_policies: MergePolicyRegistry,
        approvals: ApprovalStore,
        idempotency: IdempotencyStore,
        ledger: MutationAuditLedger,
        transport: MergeTransport,
        state_reader: MergeStateReader,
        registry_snapshot_hash: str,
        policy_version: str = DEFAULT_POLICY_VERSION,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(broker, WriteCapabilityBroker):
            raise ValueError("broker must be a WriteCapabilityBroker")
        if not isinstance(transport, MergeTransport):
            raise ValueError("transport must implement MergeTransport")
        if not isinstance(state_reader, MergeStateReader):
            raise ValueError("state_reader must implement MergeStateReader")
        if not isinstance(merge_policies, MergePolicyRegistry):
            raise ValueError("merge_policies must be a MergePolicyRegistry")
        self._broker = broker
        self._scope = scope
        self._merge_policies = merge_policies
        self._approvals = approvals
        self._idempotency = idempotency
        self._ledger = ledger
        self._transport = transport
        self._reader = state_reader
        self._registry_snapshot_hash = registry_snapshot_hash
        self._policy_version = policy_version
        self._clock: Callable[[], datetime] = clock if callable(clock) else _now

    # -- helpers -----------------------------------------------------------

    def _descriptor(self, request: MergeRequest) -> OperationDescriptor:
        return OperationDescriptor(
            operation=MERGE_TOOL_ID,
            capability=MERGE_WRITE_CAPABILITY,
            repository=request.repository,
            arguments={"base": request.base, "number": request.number},
            preconditions=OperationPreconditions(
                expected_head_sha=request.expected_head_sha,
                required_checks_policy=self._policy_version,
            ),
            policy_version=self._policy_version,
            registry_snapshot_hash=self._registry_snapshot_hash,
        )

    @staticmethod
    def _assert_stage_order(stages: Sequence[MutationStage]) -> None:
        index = -1
        for stage in stages:
            position = _STAGE_ORDER.index(stage)
            if position <= index:
                raise MutationDeniedError(
                    MutationReasonCode.RECONCILIATION_REQUIRED,
                    MutationStage.RESULT_SHAPING,
                    detail="stage_order",
                )
            index = position

    # -- main --------------------------------------------------------------

    async def execute(self, request: MergeRequest) -> MergeResult:
        """Run the governed merge. At most one ``PUT``, never retried."""
        if not isinstance(request, MergeRequest):
            raise MutationDeniedError(MutationReasonCode.INVALID_ARGUMENTS, MutationStage.REGISTRY)
        stages: list[MutationStage] = []
        guard = _SingleWriteGuard()

        # 1. SCOPE — allow-list before any credential or HTTP work.
        stages.append(MutationStage.SCOPE)
        owner, _, repo = request.repository.partition("/")
        if not self._scope.allows(owner, repo):
            raise MutationScopeError(
                MutationReasonCode.REPOSITORY_OUT_OF_SCOPE, MutationStage.SCOPE
            )

        # 2. REGISTRY — the merge policy entry is the contract for this repo.
        stages.append(MutationStage.REGISTRY)
        policy = self._merge_policies.require(request.repository)

        # 3. POLICY — merge is APPROVAL_REQUIRED by construction.
        stages.append(MutationStage.POLICY)
        if not self._merge_policies.is_merge_enabled(request.repository):
            raise MergeGovernanceError(MutationReasonCode.MERGE_NOT_PERMITTED, MutationStage.POLICY)

        # 4. CREDENTIAL — the merge capability only, and only when READY.
        stages.append(MutationStage.CREDENTIAL)
        readiness = self._broker.readiness(MERGE_WRITE_CAPABILITY.value)
        if readiness is None or not readiness.is_ready:
            raise WriteCapabilityError(
                (readiness.reason if readiness and readiness.reason else None)
                or MutationReasonCode.WRITE_CAPABILITY_NOT_READY,
                MutationStage.CREDENTIAL,
            )
        authorization = self._broker.authorize(MERGE_WRITE_CAPABILITY.value, request.repository)

        # 5. APPROVAL — single-use, bound to the digest including the head SHA.
        stages.append(MutationStage.APPROVAL)
        descriptor = self._descriptor(request)
        operation_digest = compute_operation_digest(descriptor)
        approval = self._approvals.verify_and_consume(
            request.approval_id,
            descriptor,
            principal=request.principal,
            now=self._clock(),
        )

        # 6. IDEMPOTENCY — claim the key and the exclusive merge lease.
        stages.append(MutationStage.IDEMPOTENCY)
        idempotency_key = compute_idempotency_key(
            principal=request.principal,
            capability=MERGE_WRITE_CAPABILITY,
            repository=request.repository,
            operation=MERGE_TOOL_ID,
            operation_digest=operation_digest,
        )
        decision = self._idempotency.begin(
            idempotency_key=idempotency_key,
            principal=request.principal,
            repository=request.repository,
            operation=MERGE_TOOL_ID,
            operation_digest=operation_digest,
            target=str(request.number),
            now=self._clock(),
        )
        if not decision.executes_provider_call:
            return MergeResult(
                schema=MERGE_RESULT_SCHEMA,
                tool_id=MERGE_TOOL_ID,
                repository=request.repository,
                number=request.number,
                outcome=decision.record.outcome,
                verification=VerificationState.NOT_ATTEMPTED,
                evidence_class=EvidenceClass.SUCCESS
                if decision.record.outcome is MutationOutcome.COMMITTED
                else EvidenceClass.BLOCKED,
                idempotency_status=decision.status,
                operation_digest=operation_digest,
                idempotency_key=idempotency_key,
                merged_sha=None,
                audit_id=None,
                evidence_digest=None,
                gates=None,
                stages=tuple(stages),
                provider_writes=0,
            )

        # 7. PRECONDITION_REVALIDATION — live state read, then the gate chain.
        stages.append(MutationStage.PRECONDITION_REVALIDATION)
        try:
            observation = await self._reader.observe(request, authorization=authorization)
        except ProviderTransportError as exc:
            self._idempotency.mark_ambiguous(idempotency_key, now=self._clock())
            raise _indeterminate(MutationStage.PRECONDITION_REVALIDATION) from exc
        if observation is None:
            self._idempotency.fail_clean(
                idempotency_key,
                reason=MutationReasonCode.PROTECTION_STATE_UNVERIFIABLE,
                now=self._clock(),
            )
            raise MergeGovernanceError(
                MutationReasonCode.PROTECTION_STATE_UNVERIFIABLE,
                MutationStage.PRECONDITION_REVALIDATION,
            )
        try:
            gates = evaluate_merge_gates(request, observation, policy)
        except MutationDeniedError as exc:
            self._idempotency.fail_clean(idempotency_key, reason=exc.reason, now=self._clock())
            raise
        return await self._write_and_verify(
            request=request,
            approval_id=approval.approval_id,
            operation_digest=operation_digest,
            idempotency_key=idempotency_key,
            idempotency_status=decision.status,
            policy=policy,
            gates=gates,
            stages=stages,
            guard=guard,
            authorization=authorization,
        )

    async def _write_and_verify(
        self,
        *,
        request: MergeRequest,
        approval_id: str,
        operation_digest: str,
        idempotency_key: str,
        idempotency_status: IdempotencyStatus,
        policy: Any,
        gates: MergeGateReport,
        stages: list[MutationStage],
        guard: _SingleWriteGuard,
        authorization: GitHubAuthorization,
    ) -> MergeResult:
        """Write-ahead audit, the single ``PUT``, then mandatory read-back."""
        # 8. WRITE_AHEAD_AUDIT — durable before the merge is attempted.
        stages.append(MutationStage.WRITE_AHEAD_AUDIT)
        intent = MutationIntent(
            principal=request.principal,
            operation=MERGE_TOOL_ID,
            repository=request.repository,
            operation_digest=operation_digest,
            policy_decision=PolicyDecision.APPROVAL_REQUIRED,
            capability=CapabilitySnapshot(
                capability_id=MERGE_WRITE_CAPABILITY,
                state=CapabilityState.READY,
                snapshot_hash=self._registry_snapshot_hash,
                policy_version=self._policy_version,
            ),
            idempotency_key=idempotency_key,
            idempotency_status=idempotency_status,
            approval=ApprovalReference(
                approval_id=approval_id,
                state=ApprovalState.PENDING,
                bound_digest=operation_digest,
            ),
            preconditions_observed={"expected_head_sha": request.expected_head_sha},
            registry_snapshot_hash=self._registry_snapshot_hash,
        )
        try:
            handle = self._ledger.begin(intent)
        except MutationDeniedError as exc:
            self._idempotency.fail_clean(idempotency_key, reason=exc.reason, now=self._clock())
            raise

        # 9. PROVIDER_CALL — exactly one PUT, sha-pinned, never retried.
        stages.append(MutationStage.PROVIDER_CALL)
        started_at = self._clock()
        guard.claim()
        try:
            response = await self._transport.put_json(
                merge_endpoint(request),
                body=merge_request_body(request, policy),
                authorization=authorization,
                timeout_seconds=30,
            )
        except ProviderTransportError as exc:
            raise self._finish_ambiguous(
                handle=handle,
                idempotency_key=idempotency_key,
                started_at=started_at,
                status_class=None,
                stage=MutationStage.PROVIDER_CALL,
                guard=guard,
            ) from exc

        reason = classify_merge_status(response.status_code)
        if reason is MutationReasonCode.RECONCILIATION_REQUIRED:
            raise self._finish_ambiguous(
                handle=handle,
                idempotency_key=idempotency_key,
                started_at=started_at,
                status_class=response.status_class,
                stage=MutationStage.PROVIDER_CALL,
                guard=guard,
            )
        if reason is not None:
            self._ledger.finalize(
                handle,
                ProviderObservation(
                    outcome=MutationOutcome.FAILED_CLEAN,
                    verification=VerificationState.NOT_ATTEMPTED,
                    attempts=guard.writes,
                    started_at=started_at,
                    finished_at=self._clock(),
                    status_class=response.status_class,
                    reason=reason,
                ),
            )
            self._idempotency.fail_clean(idempotency_key, reason=reason, now=self._clock())
            raise MergeGovernanceError(reason, MutationStage.PROVIDER_CALL)

        # 10. READ_BACK — merged must be proven, never inferred.
        stages.append(MutationStage.READ_BACK)
        verification, merged_sha = await self._read_back(request, authorization)
        if verification is not VerificationState.VERIFIED:
            raise self._finish_ambiguous(
                handle=handle,
                idempotency_key=idempotency_key,
                started_at=started_at,
                status_class=response.status_class,
                stage=MutationStage.READ_BACK,
                guard=guard,
                verification=verification,
            )

        # 11. RESULT_SHAPING — bounded result, audit and lease closed.
        stages.append(MutationStage.RESULT_SHAPING)
        evidence = self._ledger.finalize(
            handle,
            ProviderObservation(
                outcome=MutationOutcome.COMMITTED,
                verification=VerificationState.VERIFIED,
                attempts=guard.writes,
                started_at=started_at,
                finished_at=self._clock(),
                status_class=response.status_class,
            ),
        )
        data = {"merged_sha": merged_sha, "number": request.number}
        self._idempotency.commit(idempotency_key, result=data, now=self._clock())
        self._assert_stage_order(stages)
        return MergeResult(
            schema=MERGE_RESULT_SCHEMA,
            tool_id=MERGE_TOOL_ID,
            repository=request.repository,
            number=request.number,
            outcome=MutationOutcome.COMMITTED,
            verification=VerificationState.VERIFIED,
            evidence_class=EvidenceClass.SUCCESS,
            idempotency_status=idempotency_status,
            operation_digest=operation_digest,
            idempotency_key=idempotency_key,
            merged_sha=merged_sha,
            audit_id=evidence.audit_id,
            evidence_digest=evidence.evidence_digest,
            gates=gates,
            stages=tuple(stages),
            provider_writes=guard.writes,
        )

    async def _read_back(
        self,
        request: MergeRequest,
        authorization: GitHubAuthorization,
    ) -> tuple[VerificationState, str | None]:
        """Prove the PR is merged and the merged head is the pinned SHA."""
        owner, _, repo = request.repository.partition("/")
        path = f"/repos/{owner}/{repo}/pulls/{request.number}"
        try:
            response = await self._transport.get_json(
                path, authorization=authorization, timeout_seconds=30
            )
        except ProviderTransportError:
            return VerificationState.UNVERIFIABLE, None
        if response.status_code != 200 or not response.payload:
            return VerificationState.UNVERIFIABLE, None
        payload = response.payload
        if payload.get("merged") is not True:
            return VerificationState.MISMATCH, None
        head = payload.get("head")
        head_sha = head.get("sha") if isinstance(head, dict) else None
        if head_sha != request.expected_head_sha:
            return VerificationState.MISMATCH, None
        merge_sha = payload.get("merge_commit_sha")
        if not isinstance(merge_sha, str) or not merge_sha:
            return VerificationState.UNVERIFIABLE, None
        return VerificationState.VERIFIED, merge_sha

    def _finish_ambiguous(
        self,
        *,
        handle: Any,
        idempotency_key: str,
        started_at: datetime,
        status_class: str | None,
        stage: MutationStage,
        guard: _SingleWriteGuard,
        verification: VerificationState = VerificationState.UNVERIFIABLE,
    ) -> MutationIndeterminateError:
        """Close an ambiguous merge. Reconciliation is manual; never a revert."""
        self._ledger.finalize(
            handle,
            ProviderObservation(
                outcome=MutationOutcome.AMBIGUOUS,
                verification=verification,
                attempts=guard.writes,
                started_at=started_at,
                finished_at=self._clock(),
                status_class=status_class,
                reason=MutationReasonCode.RECONCILIATION_REQUIRED,
            ),
        )
        self._idempotency.mark_ambiguous(idempotency_key, now=self._clock())
        return _indeterminate(stage)


def merge_reconciliation_handle(
    *,
    repository: str,
    number: int,
    idempotency_key: str,
    operation_digest: str,
) -> dict[str, Any]:
    """Operator-facing dead-letter handle. A merge is never auto-reverted."""
    return {
        "action": "MANUAL_RECONCILIATION_REQUIRED",
        "compensation": "none_automatic",
        "idempotency_key": idempotency_key,
        "number": number,
        "operation": MERGE_TOOL_ID,
        "operation_digest": operation_digest,
        "repository": repository,
    }


__all__ = [
    "DEFAULT_POLICY_VERSION",
    "MERGE_RESULT_SCHEMA",
    "GovernedMergeExecutor",
    "MergeResult",
    "MergeStateReader",
    "MergeTransport",
    "merge_reconciliation_handle",
]
