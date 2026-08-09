"""Phase 3 lane L6 — governed merge executor tests.

Covers A3-05 (write-ahead audit before every mutation), A3-07 (one provider
mutation per identical request), A3-08 (head drift never silently writes),
A3-12 (unsafe compensation dead-letters instead of writing) and the L6
redaction share of A3-14.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from merge_fixtures import HEAD, OTHER, REPO, observation, policy, request

from hermes_mcp_bridge.v2 import github_governed_merge as gm
from hermes_mcp_bridge.v2.enums import (
    IdempotencyStatus,
    MutationOutcome,
    MutationReasonCode,
    MutationStage,
    WriteCapabilityId,
)
from hermes_mcp_bridge.v2.errors import (
    MergeGovernanceError,
    MutationDeniedError,
    MutationIndeterminateError,
    MutationScopeError,
    WriteCapabilityError,
)
from hermes_mcp_bridge.v2.github_auth import GitHubAuthorization
from hermes_mcp_bridge.v2.github_direct import GitHubRepositoryScope
from hermes_mcp_bridge.v2.github_merge_executor import (
    GovernedMergeExecutor,
    merge_reconciliation_handle,
)
from hermes_mcp_bridge.v2.github_mutations import ProviderResponse, ProviderTransportError
from hermes_mcp_bridge.v2.github_secret_provider import AuthorizationStatus
from hermes_mcp_bridge.v2.github_write_credentials import (
    WriteCapabilityBroker,
)
from hermes_mcp_bridge.v2.mutation_audit import (
    InMemoryAuditSink,
    MutationAuditLedger,
    VerificationState,
)
from hermes_mcp_bridge.v2.mutation_digest import (
    ApprovalRecord,
    ApprovalStore,
    compute_operation_digest,
)
from hermes_mcp_bridge.v2.mutation_idempotency import IdempotencyStore

SNAPSHOT = "c" * 64
NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


class FakeMergeProvider:
    """In-memory write-material provider for the merge capability only."""

    def __init__(self, *, status: AuthorizationStatus = AuthorizationStatus.READY) -> None:
        self._status = status
        self.resolve_calls = 0

    @property
    def capability(self) -> WriteCapabilityId:
        return WriteCapabilityId.MERGE

    def probe(self) -> AuthorizationStatus:
        return self._status

    def resolve(self, capability_id: str, repository: str) -> GitHubAuthorization | None:
        self.resolve_calls += 1
        return GitHubAuthorization("fake-material-value-1234567890")


def build_broker(*, ready: bool = True) -> WriteCapabilityBroker:
    provider = FakeMergeProvider(
        status=AuthorizationStatus.READY if ready else AuthorizationStatus.NOT_CONFIGURED
    )
    return WriteCapabilityBroker(
        [provider],
        attested_permissions={
            WriteCapabilityId.MERGE: {
                "checks": "read",
                "contents": "write",
                "metadata": "read",
                "pull_requests": "write",
            }
        },
        policy_allows={WriteCapabilityId.MERGE: True},
    )


class FakeTransport:
    """Records verbs and paths only; bodies are never retained verbatim."""

    def __init__(
        self,
        *,
        merge_status: int = 200,
        read_back: dict[str, Any] | None = None,
        raise_on_put: bool = False,
    ) -> None:
        self.merge_status = merge_status
        self.raise_on_put = raise_on_put
        self.calls: list[tuple[str, str]] = []
        self.read_back = (
            read_back
            if read_back is not None
            else {"merged": True, "head": {"sha": HEAD}, "merge_commit_sha": "d" * 40}
        )

    async def get_json(self, path: str, **_: Any) -> ProviderResponse:
        self.calls.append(("GET", path))
        return ProviderResponse(status_code=200, payload=self.read_back)

    async def put_json(self, path: str, **_: Any) -> ProviderResponse:
        self.calls.append(("PUT", path))
        if self.raise_on_put:
            raise ProviderTransportError("transport")
        return ProviderResponse(status_code=self.merge_status, payload={})

    @property
    def writes(self) -> int:
        return sum(1 for verb, _ in self.calls if verb == "PUT")


class FakeReader:
    def __init__(self, obs: gm.MergeObservation | None = None, *, raises: bool = False) -> None:
        self._obs = obs if obs is not None else observation()
        self._raises = raises

    async def observe(self, req: gm.MergeRequest, **_: Any) -> gm.MergeObservation | None:
        if self._raises:
            raise ProviderTransportError("read")
        return self._obs


def build(
    *,
    transport: FakeTransport | None = None,
    reader: FakeReader | None = None,
    broker: WriteCapabilityBroker | None = None,
    scope_repo: str = REPO,
    merge_enabled: bool = True,
) -> tuple[GovernedMergeExecutor, FakeTransport, ApprovalStore, MutationAuditLedger]:
    transport = transport or FakeTransport()
    ledger = MutationAuditLedger(
        sink=InMemoryAuditSink(), clock=lambda: NOW, allow_non_durable_sink=True
    )
    approvals = ApprovalStore()
    executor = GovernedMergeExecutor(
        broker=broker or build_broker(),
        scope=GitHubRepositoryScope([scope_repo]),
        merge_policies=gm.MergePolicyRegistry([policy()] if merge_enabled else []),
        approvals=approvals,
        idempotency=IdempotencyStore(),
        ledger=ledger,
        transport=transport,  # type: ignore[arg-type]
        state_reader=reader or FakeReader(),  # type: ignore[arg-type]
        registry_snapshot_hash=SNAPSHOT,
        clock=lambda: NOW,
    )
    return executor, transport, approvals, ledger


def grant(executor: GovernedMergeExecutor, approvals: ApprovalStore, req: gm.MergeRequest) -> None:
    descriptor = executor._descriptor(req)
    approvals.issue(
        ApprovalRecord(
            approval_id=req.approval_id,
            principal=req.principal,
            approver=req.approver or "human",
            operation_digest=compute_operation_digest(descriptor),
            repository=req.repository,
            operation=gm.MERGE_TOOL_ID,
            nonce="nonce-1",
            expires_at=NOW + timedelta(minutes=10),
            trust_context="test-context",
        )
    )


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def execute(executor: GovernedMergeExecutor, approvals: ApprovalStore, **kw: Any) -> Any:
    req = request(**kw)
    grant(executor, approvals, req)
    return run(executor.execute(req))


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------


def test_governed_merge_commits_with_verified_read_back() -> None:
    executor, transport, approvals, _ = build()
    result = execute(executor, approvals)
    assert result.outcome is MutationOutcome.COMMITTED
    assert result.verification is VerificationState.VERIFIED
    assert result.merged_sha == "d" * 40
    assert result.provider_writes == 1
    assert transport.writes == 1


def test_exactly_one_put_against_the_allow_listed_path() -> None:
    executor, transport, approvals, _ = build()
    execute(executor, approvals)
    puts = [path for verb, path in transport.calls if verb == "PUT"]
    assert puts == ["/repos/octo/lab/pulls/12/merge"]


def test_stage_sequence_is_fail_closed_and_ordered() -> None:
    executor, _, approvals, _ = build()
    stages = execute(executor, approvals).stages
    assert stages[0] is MutationStage.SCOPE
    assert stages[-1] is MutationStage.RESULT_SHAPING
    call = stages.index(MutationStage.PROVIDER_CALL)
    for gate in (
        MutationStage.POLICY,
        MutationStage.CREDENTIAL,
        MutationStage.APPROVAL,
        MutationStage.IDEMPOTENCY,
        MutationStage.PRECONDITION_REVALIDATION,
        MutationStage.WRITE_AHEAD_AUDIT,
    ):
        assert stages.index(gate) < call


def test_write_ahead_audit_precedes_the_provider_call(monkeypatch: Any) -> None:
    executor, transport, approvals, ledger = build()
    order: list[str] = []
    original = ledger.begin

    def spy(intent: Any) -> Any:
        order.append("audit")
        return original(intent)

    monkeypatch.setattr(ledger, "begin", spy)
    original_put = transport.put_json

    async def put_spy(path: str, **kw: Any) -> ProviderResponse:
        order.append("put")
        return await original_put(path, **kw)

    monkeypatch.setattr(transport, "put_json", put_spy)
    execute(executor, approvals)
    assert order == ["audit", "put"]


def test_result_canonical_contains_no_secret_material() -> None:
    executor, _, approvals, _ = build()
    payload = execute(executor, approvals).as_canonical()
    text = repr(payload)
    assert "redacted-test-material" not in text
    assert "Bearer" not in text
    assert "/repos/" not in text


# --------------------------------------------------------------------------
# Fail-closed paths — no write may happen
# --------------------------------------------------------------------------


def test_out_of_scope_repository_never_resolves_credentials_or_writes() -> None:
    provider = FakeMergeProvider()
    broker = WriteCapabilityBroker(
        [provider],
        attested_permissions={
            WriteCapabilityId.MERGE: {
                "checks": "read",
                "contents": "write",
                "metadata": "read",
                "pull_requests": "write",
            }
        },
        policy_allows={WriteCapabilityId.MERGE: True},
    )
    executor, transport, _, _ = build(broker=broker, scope_repo="other/repo")
    with pytest.raises(MutationScopeError):
        run(executor.execute(request()))
    assert provider.resolve_calls == 0
    assert transport.calls == []


def test_repository_without_a_merge_policy_is_denied_before_any_call() -> None:
    executor, transport, _, _ = build(merge_enabled=False)
    with pytest.raises(MergeGovernanceError) as excinfo:
        run(executor.execute(request()))
    assert excinfo.value.reason is MutationReasonCode.MERGE_NOT_PERMITTED
    assert transport.calls == []


def test_not_ready_merge_capability_denies_before_approval() -> None:
    executor, transport, _, _ = build(broker=build_broker(ready=False))
    with pytest.raises(WriteCapabilityError):
        run(executor.execute(request()))
    assert transport.calls == []


def test_failed_gate_chain_never_issues_a_write() -> None:
    reader = FakeReader(observation(checks=()))
    executor, transport, approvals, _ = build(reader=reader)
    req = request()
    grant(executor, approvals, req)
    with pytest.raises(MergeGovernanceError) as excinfo:
        run(executor.execute(req))
    assert excinfo.value.reason is MutationReasonCode.REQUIRED_CHECKS_NOT_GREEN
    assert transport.writes == 0


def test_head_drift_denies_without_writing() -> None:
    from merge_fixtures import pull_request

    reader = FakeReader(observation(pull_request=pull_request(head_sha=OTHER)))
    executor, transport, approvals, _ = build(reader=reader)
    req = request()
    grant(executor, approvals, req)
    with pytest.raises(MergeGovernanceError) as excinfo:
        run(executor.execute(req))
    assert excinfo.value.reason is MutationReasonCode.PRECONDITION_DRIFT
    assert transport.writes == 0


def test_unreadable_state_denies_as_unverifiable() -> None:
    class NoneReader:
        async def observe(self, req: Any, **_: Any) -> None:
            return None

    executor, transport, approvals, _ = build(reader=NoneReader())  # type: ignore[arg-type]
    req = request()
    grant(executor, approvals, req)
    with pytest.raises(MergeGovernanceError) as excinfo:
        run(executor.execute(req))
    assert excinfo.value.reason is MutationReasonCode.PROTECTION_STATE_UNVERIFIABLE
    assert transport.writes == 0


def test_provider_409_is_clean_drift_and_is_not_retried() -> None:
    executor, transport, approvals, _ = build(transport=FakeTransport(merge_status=409))
    req = request()
    grant(executor, approvals, req)
    with pytest.raises(MergeGovernanceError) as excinfo:
        run(executor.execute(req))
    assert excinfo.value.reason is MutationReasonCode.PRECONDITION_DRIFT
    assert transport.writes == 1


def test_provider_405_is_a_clean_refusal() -> None:
    executor, transport, approvals, _ = build(transport=FakeTransport(merge_status=405))
    req = request()
    grant(executor, approvals, req)
    with pytest.raises(MergeGovernanceError) as excinfo:
        run(executor.execute(req))
    assert excinfo.value.reason is MutationReasonCode.PULL_REQUEST_NOT_MERGEABLE
    assert transport.writes == 1


# --------------------------------------------------------------------------
# Ambiguity — reconciliation, never retry, never auto-revert
# --------------------------------------------------------------------------


def test_transport_failure_is_indeterminate_and_not_retried() -> None:
    executor, transport, approvals, _ = build(transport=FakeTransport(raise_on_put=True))
    req = request()
    grant(executor, approvals, req)
    with pytest.raises(MutationIndeterminateError) as excinfo:
        run(executor.execute(req))
    assert excinfo.value.reason is MutationReasonCode.RECONCILIATION_REQUIRED
    assert transport.writes == 1


def test_unenumerated_status_is_indeterminate() -> None:
    executor, transport, approvals, _ = build(transport=FakeTransport(merge_status=500))
    req = request()
    grant(executor, approvals, req)
    with pytest.raises(MutationIndeterminateError):
        run(executor.execute(req))
    assert transport.writes == 1


@pytest.mark.parametrize(
    "read_back",
    (
        {"merged": False, "head": {"sha": HEAD}},
        {"merged": True, "head": {"sha": OTHER}, "merge_commit_sha": "d" * 40},
        {"merged": True, "head": {"sha": HEAD}},
        {},
    ),
)
def test_unproven_read_back_never_reports_committed(read_back: dict[str, Any]) -> None:
    executor, transport, approvals, _ = build(transport=FakeTransport(read_back=read_back))
    req = request()
    grant(executor, approvals, req)
    with pytest.raises(MutationIndeterminateError):
        run(executor.execute(req))
    assert transport.writes == 1


def test_reconciliation_handle_is_manual_and_never_compensating() -> None:
    handle = merge_reconciliation_handle(
        repository=REPO, number=12, idempotency_key="k" * 64, operation_digest="e" * 64
    )
    assert handle["action"] == "MANUAL_RECONCILIATION_REQUIRED"
    assert handle["compensation"] == "none_automatic"


# --------------------------------------------------------------------------
# Idempotency (A3-07)
# --------------------------------------------------------------------------


def test_identical_repeated_request_produces_exactly_one_provider_mutation() -> None:
    executor, transport, approvals, _ = build()
    req = request()
    grant(executor, approvals, req)
    first = run(executor.execute(req))
    assert first.outcome is MutationOutcome.COMMITTED
    assert transport.writes == 1

    # The approval is single-use, so a genuine replay must be refused before
    # any second write can be considered.
    with pytest.raises(MutationDeniedError):
        run(executor.execute(req))
    assert transport.writes == 1


def test_idempotency_status_is_new_on_the_first_attempt() -> None:
    executor, _, approvals, _ = build()
    assert execute(executor, approvals).idempotency_status is IdempotencyStatus.NEW
