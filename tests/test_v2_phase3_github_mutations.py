"""Phase 3 lane L5 — hermetic tests for the DIRECT GitHub write executor.

Zero network: every provider interaction goes through an in-process fake that
records the exact call sequence. The suite proves the invariants of
``docs/v2/phase3/implementation-wave.md`` lane L5:

* fixed preflight ordering and the stages actually walked;
* exactly one provider mutation call per execution;
* clean failure, ambiguous outcome, TOCTOU drift, replay, live lease conflict,
  approval digest mismatch/missing, audit write failure, read-back mismatch,
  credential/capability not ready, policy DENY;
* fail-closed secret redaction in results and errors;
* no retry after an ambiguous outcome.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from hermes_mcp_bridge.v2.enums import (
    MUTATION_STAGE_ORDER,
    ApprovalState,
    CapabilityState,
    IdempotencyStatus,
    MutationOutcome,
    MutationReasonCode,
    MutationStage,
    WriteCapabilityId,
)
from hermes_mcp_bridge.v2.errors import (
    ApprovalError,
    AuditWriteError,
    ConcurrencyDriftError,
    DigestMismatchError,
    IdempotencyConflictError,
    MutationDeniedError,
    MutationIndeterminateError,
    MutationScopeError,
    WriteCapabilityError,
)
from hermes_mcp_bridge.v2.github_auth import GitHubAuthorization
from hermes_mcp_bridge.v2.github_direct import GitHubRepositoryScope
from hermes_mcp_bridge.v2.github_mutation_registry import (
    CREATE_BRANCH_TOOL_ID,
    CREATE_PR_TOOL_ID,
    build_github_mutation_registry,
    get_mutation_contract,
    normalize_arguments,
)
from hermes_mcp_bridge.v2.github_mutations import (
    GITHUB_API_BASE_URL,
    MUTATION_RESULT_SCHEMA,
    GitHubMutationExecutor,
    HttpxMutationTransport,
    MutationRequest,
    MutationTransport,
    ProviderResponse,
    ProviderTransportError,
    reconcile_result,
)
from hermes_mcp_bridge.v2.github_secret_provider import AuthorizationStatus
from hermes_mcp_bridge.v2.github_write_credentials import WriteCapabilityBroker
from hermes_mcp_bridge.v2.mutation_audit import (
    EvidenceClass,
    InMemoryAuditSink,
    MutationAuditLedger,
    VerificationState,
)
from hermes_mcp_bridge.v2.mutation_digest import (
    ApprovalRecord,
    ApprovalStore,
    OperationDescriptor,
    OperationPreconditions,
    compute_operation_digest,
)
from hermes_mcp_bridge.v2.mutation_idempotency import IdempotencyStore
from hermes_mcp_bridge.v2.policy import PolicyRuleSet

REPOSITORY = "pestoura/disposable-lab"
OWNER, REPO = REPOSITORY.split("/")
PRINCIPAL = "agent-l5"
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
BRANCH = "feat/l5-branch"
POLICY_VERSION = "phase3.mutations.v1"

BRANCH_ARGS: dict[str, Any] = {
    "owner": OWNER,
    "repo": REPO,
    "branch": BRANCH,
    "base_sha": BASE_SHA,
}
PR_ARGS: dict[str, Any] = {
    "owner": OWNER,
    "repo": REPO,
    "head": BRANCH,
    "base": "main",
    "title": "L5 executor",
    "expected_head_sha": HEAD_SHA,
}


# ---------------------------------------------------------------------------
# Hermetic doubles
# ---------------------------------------------------------------------------


class FakeWriteProvider:
    """In-memory write-material provider for one capability."""

    def __init__(
        self,
        capability: WriteCapabilityId,
        *,
        status: AuthorizationStatus = AuthorizationStatus.READY,
    ) -> None:
        self._capability = capability
        self._status = status
        self.resolve_calls = 0

    @property
    def capability(self) -> WriteCapabilityId:
        return self._capability

    def probe(self) -> AuthorizationStatus:
        return self._status

    def resolve(self, capability_id: str, repository: str) -> GitHubAuthorization | None:
        self.resolve_calls += 1
        return GitHubAuthorization("fake-material-value-1234567890")


class FakeTransport:
    """Records every call; answers from a scripted plan. No network."""

    def __init__(
        self,
        *,
        write_status: int = 201,
        write_payload: Mapping[str, Any] | None = None,
        write_error: bool = False,
        revalidate_sha: str | None = None,
        revalidate_status: int = 200,
        revalidate_error: bool = False,
        read_back_status: int = 200,
        read_back_payload: Mapping[str, Any] | None = None,
        read_back_error: bool = False,
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self.write_status = write_status
        self.write_payload = dict(write_payload or {})
        self.write_error = write_error
        self.revalidate_sha = revalidate_sha
        self.revalidate_status = revalidate_status
        self.revalidate_error = revalidate_error
        self.read_back_status = read_back_status
        self.read_back_payload = dict(read_back_payload or {})
        self.read_back_error = read_back_error

    @property
    def writes(self) -> int:
        return sum(1 for method, _ in self.calls if method == "POST")

    async def get_json(
        self,
        path: str,
        *,
        authorization: GitHubAuthorization,
        timeout_seconds: int,
    ) -> ProviderResponse:
        self.calls.append(("GET", path))
        is_read_back = any(method == "POST" for method, _ in self.calls)
        if not is_read_back:
            if self.revalidate_error:
                raise ProviderTransportError("boom")
            sha = self.revalidate_sha
            payload: dict[str, Any] = (
                {"sha": sha, "object": {"sha": sha}} if sha is not None else {}
            )
            return ProviderResponse(status_code=self.revalidate_status, payload=payload)
        if self.read_back_error:
            raise ProviderTransportError("boom")
        return ProviderResponse(status_code=self.read_back_status, payload=self.read_back_payload)

    async def post_json(
        self,
        path: str,
        *,
        body: Mapping[str, Any],
        authorization: GitHubAuthorization,
        timeout_seconds: int,
    ) -> ProviderResponse:
        self.calls.append(("POST", path))
        if self.write_error:
            raise ProviderTransportError("boom")
        return ProviderResponse(status_code=self.write_status, payload=self.write_payload)


class BrokenSink(InMemoryAuditSink):
    """A sink that silently drops records: the ledger must fail closed."""

    def append(self, audit_id: str, payload: Mapping[str, object]) -> None:
        return None


def branch_read_back() -> dict[str, Any]:
    return {
        "ref": f"refs/heads/{BRANCH}",
        "object": {"sha": BASE_SHA},
        "url": f"{GITHUB_API_BASE_URL}/repos/{REPOSITORY}/git/ref/heads/{BRANCH}",
    }


def pr_read_back(number: int = 7) -> dict[str, Any]:
    return {
        "number": number,
        "state": "open",
        "draft": True,
        "head": {"sha": HEAD_SHA},
        "html_url": f"https://github.com/{REPOSITORY}/pull/{number}",
    }


def build_broker(
    *,
    branch_ready: bool = True,
    pr_ready: bool = True,
) -> WriteCapabilityBroker:
    providers = [
        FakeWriteProvider(
            WriteCapabilityId.BRANCH,
            status=AuthorizationStatus.READY
            if branch_ready
            else AuthorizationStatus.NOT_CONFIGURED,
        ),
        FakeWriteProvider(
            WriteCapabilityId.PR,
            status=AuthorizationStatus.READY if pr_ready else AuthorizationStatus.NOT_CONFIGURED,
        ),
    ]
    return WriteCapabilityBroker(
        providers,
        attested_permissions={
            WriteCapabilityId.BRANCH: {"contents": "write", "metadata": "read"},
            WriteCapabilityId.PR: {
                "contents": "read",
                "metadata": "read",
                "pull_requests": "write",
            },
        },
        policy_allows={WriteCapabilityId.BRANCH: True, WriteCapabilityId.PR: True},
    )


class Harness:
    """Everything an execution needs, wired hermetically."""

    def __init__(
        self,
        transport: MutationTransport,
        *,
        broker: WriteCapabilityBroker | None = None,
        rules: PolicyRuleSet | None = None,
        sink: InMemoryAuditSink | None = None,
        scope: GitHubRepositoryScope | None = None,
    ) -> None:
        self.registry = build_github_mutation_registry()
        self.broker = broker if broker is not None else build_broker()
        self.approvals = ApprovalStore()
        self.idempotency = IdempotencyStore()
        self.sink = sink if sink is not None else InMemoryAuditSink()
        self.ledger = MutationAuditLedger(self.sink, allow_non_durable_sink=True)
        self.transport = transport
        self.executor = GitHubMutationExecutor(
            registry=self.registry,
            broker=self.broker,
            scope=scope if scope is not None else GitHubRepositoryScope([REPOSITORY]),
            approvals=self.approvals,
            idempotency=self.idempotency,
            ledger=self.ledger,
            transport=transport,
            rules=rules,
            policy_version=POLICY_VERSION,
        )

    def descriptor(self, tool_id: str, arguments: Mapping[str, Any]) -> OperationDescriptor:
        normalized = normalize_arguments(tool_id, dict(arguments))
        contract = get_mutation_contract(tool_id)
        if tool_id == CREATE_BRANCH_TOOL_ID:
            preconditions = OperationPreconditions(base_sha=normalized["base_sha"])
        else:
            preconditions = OperationPreconditions(
                expected_head_sha=normalized["expected_head_sha"]
            )
        return OperationDescriptor(
            operation=tool_id,
            capability=contract.write_capability,
            repository=REPOSITORY,
            arguments={k: v for k, v in normalized.items() if k != "repository"},
            preconditions=preconditions,
            policy_version=POLICY_VERSION,
            registry_snapshot_hash=self.registry.capability_snapshot_hash(),
        )

    def approve(
        self,
        tool_id: str,
        arguments: Mapping[str, Any],
        *,
        approval_id: str = "apr-1",
        digest: str | None = None,
        principal: str = PRINCIPAL,
    ) -> str:
        descriptor = self.descriptor(tool_id, arguments)
        self.approvals.issue(
            ApprovalRecord(
                approval_id=approval_id,
                principal=principal,
                approver="human-1",
                operation_digest=digest or compute_operation_digest(descriptor),
                repository=REPOSITORY,
                operation=tool_id,
                nonce="nonce-1",
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
                trust_context="cli",
            )
        )
        return approval_id

    def request(
        self,
        tool_id: str,
        arguments: Mapping[str, Any],
        *,
        approval_id: str = "apr-1",
        attempt_token: str | None = None,
    ) -> MutationRequest:
        return MutationRequest(
            principal=PRINCIPAL,
            tool_id=tool_id,
            arguments=dict(arguments),
            approval_id=approval_id,
            attempt_token=attempt_token,
        )


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Happy path and ordering
# ---------------------------------------------------------------------------


def test_create_branch_success_walks_the_fixed_stage_order() -> None:
    transport = FakeTransport(
        revalidate_sha=BASE_SHA,
        write_payload=branch_read_back(),
        read_back_payload=branch_read_back(),
    )
    harness = Harness(transport)
    harness.approve(CREATE_BRANCH_TOOL_ID, BRANCH_ARGS)

    result = run(harness.executor.execute(harness.request(CREATE_BRANCH_TOOL_ID, BRANCH_ARGS)))

    assert result.schema == MUTATION_RESULT_SCHEMA
    assert result.outcome is MutationOutcome.COMMITTED
    assert result.verification is VerificationState.VERIFIED
    assert result.evidence_class is EvidenceClass.SUCCESS
    assert result.idempotency_status is IdempotencyStatus.NEW
    assert result.provider_writes == 1
    assert transport.writes == 1
    assert result.data["ref"] == f"refs/heads/{BRANCH}"
    assert result.data["sha"] == BASE_SHA
    # exact ordering, and a subsequence of the C-1 canonical order
    assert result.stages == (
        MutationStage.SCOPE,
        MutationStage.REGISTRY,
        MutationStage.POLICY,
        MutationStage.CREDENTIAL,
        MutationStage.APPROVAL,
        MutationStage.IDEMPOTENCY,
        MutationStage.PRECONDITION_REVALIDATION,
        MutationStage.WRITE_AHEAD_AUDIT,
        MutationStage.PROVIDER_CALL,
        MutationStage.READ_BACK,
        MutationStage.RESULT_SHAPING,
    )
    order = list(MUTATION_STAGE_ORDER)
    assert [order.index(stage) for stage in result.stages] == sorted(
        order.index(stage) for stage in result.stages
    )


def test_create_pr_success_verifies_read_back_fields() -> None:
    transport = FakeTransport(
        revalidate_sha=HEAD_SHA,
        write_payload=pr_read_back(),
        read_back_payload=pr_read_back(),
    )
    harness = Harness(transport)
    harness.approve(CREATE_PR_TOOL_ID, PR_ARGS)

    result = run(harness.executor.execute(harness.request(CREATE_PR_TOOL_ID, PR_ARGS)))

    assert result.outcome is MutationOutcome.COMMITTED
    assert result.data["number"] == 7
    assert result.data["head_sha"] == HEAD_SHA
    assert result.data["state"] == "open"
    assert transport.writes == 1


def test_write_ahead_audit_precedes_the_provider_call() -> None:
    transport = FakeTransport(
        revalidate_sha=BASE_SHA,
        write_payload=branch_read_back(),
        read_back_payload=branch_read_back(),
    )
    harness = Harness(transport)
    harness.approve(CREATE_BRANCH_TOOL_ID, BRANCH_ARGS)
    result = run(harness.executor.execute(harness.request(CREATE_BRANCH_TOOL_ID, BRANCH_ARGS)))

    assert result.audit_id is not None
    assert harness.ledger.has_write_ahead_record(result.audit_id)
    assert harness.ledger.open_attempts == 0


def test_evidence_and_result_contain_no_secret_material() -> None:
    transport = FakeTransport(
        revalidate_sha=BASE_SHA,
        write_payload={**branch_read_back(), "token": "ghp_" + "z" * 36},
        read_back_payload={**branch_read_back(), "token": "ghp_" + "z" * 36},
    )
    harness = Harness(transport)
    harness.approve(CREATE_BRANCH_TOOL_ID, BRANCH_ARGS)
    result = run(harness.executor.execute(harness.request(CREATE_BRANCH_TOOL_ID, BRANCH_ARGS)))

    rendered = repr(result.as_canonical())
    for marker in ("ghp_", "Bearer", "Authorization", "fake-material-value"):
        assert marker not in rendered
    assert "token" not in result.data
    stored = harness.sink.read(str(result.audit_id))
    assert stored is not None
    assert "ghp_" not in repr(stored)


# ---------------------------------------------------------------------------
# Fail-closed preflight
# ---------------------------------------------------------------------------


def test_out_of_scope_repository_denies_before_any_credential_or_http() -> None:
    transport = FakeTransport(revalidate_sha=BASE_SHA)
    harness = Harness(transport, scope=GitHubRepositoryScope(["other/elsewhere"]))
    harness.approve(CREATE_BRANCH_TOOL_ID, BRANCH_ARGS)

    with pytest.raises(MutationScopeError) as excinfo:
        run(harness.executor.execute(harness.request(CREATE_BRANCH_TOOL_ID, BRANCH_ARGS)))

    assert excinfo.value.reason is MutationReasonCode.REPOSITORY_OUT_OF_SCOPE
    assert str(excinfo.value) == "SCOPE:REPOSITORY_OUT_OF_SCOPE"
    assert transport.calls == []


def test_unknown_operation_is_denied_at_registry() -> None:
    harness = Harness(FakeTransport())
    with pytest.raises(MutationDeniedError) as excinfo:
        run(harness.executor.execute(harness.request("github.delete_repository", BRANCH_ARGS)))
    assert excinfo.value.reason is MutationReasonCode.DESTRUCTIVE_OPERATION_FORBIDDEN


def test_missing_policy_rule_denies_and_issues_no_http() -> None:
    transport = FakeTransport(revalidate_sha=BASE_SHA)
    harness = Harness(transport, rules=PolicyRuleSet([]))
    harness.approve(CREATE_BRANCH_TOOL_ID, BRANCH_ARGS)

    with pytest.raises(MutationDeniedError) as excinfo:
        run(harness.executor.execute(harness.request(CREATE_BRANCH_TOOL_ID, BRANCH_ARGS)))

    assert excinfo.value.stage is MutationStage.POLICY
    assert transport.calls == []


def test_capability_not_ready_denies_before_any_http() -> None:
    transport = FakeTransport(revalidate_sha=BASE_SHA)
    harness = Harness(transport, broker=build_broker(branch_ready=False))
    harness.approve(CREATE_BRANCH_TOOL_ID, BRANCH_ARGS)

    with pytest.raises((WriteCapabilityError, MutationDeniedError)) as excinfo:
        run(harness.executor.execute(harness.request(CREATE_BRANCH_TOOL_ID, BRANCH_ARGS)))

    assert excinfo.value.stage in {MutationStage.CREDENTIAL, MutationStage.POLICY}
    assert transport.calls == []


def test_missing_approval_denies() -> None:
    transport = FakeTransport(revalidate_sha=BASE_SHA)
    harness = Harness(transport)
    with pytest.raises(ApprovalError) as excinfo:
        run(harness.executor.execute(harness.request(CREATE_BRANCH_TOOL_ID, BRANCH_ARGS)))
    assert excinfo.value.reason is MutationReasonCode.APPROVAL_UNKNOWN
    assert transport.calls == []


def test_approval_digest_mismatch_denies() -> None:
    transport = FakeTransport(revalidate_sha=BASE_SHA)
    harness = Harness(transport)
    harness.approve(CREATE_BRANCH_TOOL_ID, BRANCH_ARGS, digest="c" * 64)

    with pytest.raises(DigestMismatchError) as excinfo:
        run(harness.executor.execute(harness.request(CREATE_BRANCH_TOOL_ID, BRANCH_ARGS)))

    assert excinfo.value.reason is MutationReasonCode.APPROVAL_DIGEST_MISMATCH
    assert transport.calls == []


def test_approval_is_single_use() -> None:
    transport = FakeTransport(
        revalidate_sha=BASE_SHA,
        write_payload=branch_read_back(),
        read_back_payload=branch_read_back(),
    )
    harness = Harness(transport)
    harness.approve(CREATE_BRANCH_TOOL_ID, BRANCH_ARGS)
    run(harness.executor.execute(harness.request(CREATE_BRANCH_TOOL_ID, BRANCH_ARGS)))

    with pytest.raises(ApprovalError) as excinfo:
        run(harness.executor.execute(harness.request(CREATE_BRANCH_TOOL_ID, BRANCH_ARGS)))
    assert excinfo.value.reason is MutationReasonCode.APPROVAL_ALREADY_CONSUMED
    assert transport.writes == 1


def test_audit_write_failure_blocks_the_provider_call() -> None:
    transport = FakeTransport(revalidate_sha=BASE_SHA)
    harness = Harness(transport, sink=BrokenSink())
    harness.approve(CREATE_BRANCH_TOOL_ID, BRANCH_ARGS)

    with pytest.raises(AuditWriteError) as excinfo:
        run(harness.executor.execute(harness.request(CREATE_BRANCH_TOOL_ID, BRANCH_ARGS)))

    assert excinfo.value.reason is MutationReasonCode.AUDIT_RECORD_UNWRITABLE
    assert transport.writes == 0


# ---------------------------------------------------------------------------
# TOCTOU, concurrency, replay
# ---------------------------------------------------------------------------


def test_toctou_drift_denies_without_a_write() -> None:
    transport = FakeTransport(revalidate_sha="d" * 40)
    harness = Harness(transport)
    harness.approve(CREATE_BRANCH_TOOL_ID, BRANCH_ARGS)

    with pytest.raises(ConcurrencyDriftError) as excinfo:
        run(harness.executor.execute(harness.request(CREATE_BRANCH_TOOL_ID, BRANCH_ARGS)))

    assert excinfo.value.reason is MutationReasonCode.PRECONDITION_DRIFT
    assert transport.writes == 0


def test_revalidation_transport_failure_is_indeterminate_not_a_write() -> None:
    transport = FakeTransport(revalidate_error=True)
    harness = Harness(transport)
    harness.approve(CREATE_BRANCH_TOOL_ID, BRANCH_ARGS)

    with pytest.raises(MutationIndeterminateError) as excinfo:
        run(harness.executor.execute(harness.request(CREATE_BRANCH_TOOL_ID, BRANCH_ARGS)))

    assert excinfo.value.reason is MutationReasonCode.RECONCILIATION_REQUIRED
    assert transport.writes == 0


def test_replay_of_a_committed_key_issues_no_second_write() -> None:
    transport = FakeTransport(
        revalidate_sha=BASE_SHA,
        write_payload=branch_read_back(),
        read_back_payload=branch_read_back(),
    )
    harness = Harness(transport)
    harness.approve(CREATE_BRANCH_TOOL_ID, BRANCH_ARGS, approval_id="apr-1")
    first = run(harness.executor.execute(harness.request(CREATE_BRANCH_TOOL_ID, BRANCH_ARGS)))
    harness.approve(CREATE_BRANCH_TOOL_ID, BRANCH_ARGS, approval_id="apr-2")

    second = run(
        harness.executor.execute(
            harness.request(CREATE_BRANCH_TOOL_ID, BRANCH_ARGS, approval_id="apr-2")
        )
    )

    assert second.idempotency_status is IdempotencyStatus.REPLAYED
    assert second.provider_writes == 0
    assert second.idempotency_key == first.idempotency_key
    assert transport.writes == 1


def test_live_lease_conflict_refuses_a_concurrent_attempt() -> None:
    transport = FakeTransport(
        revalidate_sha=BASE_SHA,
        write_payload=branch_read_back(),
        read_back_payload=branch_read_back(),
    )
    harness = Harness(transport)
    harness.approve(CREATE_BRANCH_TOOL_ID, BRANCH_ARGS, approval_id="apr-1")
    # A live PENDING lease on the same (repository, family, target).
    contract_digest = compute_operation_digest(
        harness.descriptor(CREATE_BRANCH_TOOL_ID, BRANCH_ARGS)
    )
    harness.idempotency.begin(
        idempotency_key="f" * 64,
        principal="other-agent",
        repository=REPOSITORY,
        operation=CREATE_BRANCH_TOOL_ID,
        operation_digest=contract_digest,
        target=BRANCH,
        now=datetime.now(UTC),
    )

    with pytest.raises(IdempotencyConflictError) as excinfo:
        run(harness.executor.execute(harness.request(CREATE_BRANCH_TOOL_ID, BRANCH_ARGS)))

    assert excinfo.value.reason is MutationReasonCode.OPERATION_IN_PROGRESS
    assert transport.writes == 0


# ---------------------------------------------------------------------------
# Provider outcomes
# ---------------------------------------------------------------------------


def test_clean_failure_allows_a_new_attempt_and_writes_once() -> None:
    transport = FakeTransport(revalidate_sha=BASE_SHA, write_status=422)
    harness = Harness(transport)
    harness.approve(CREATE_BRANCH_TOOL_ID, BRANCH_ARGS)

    with pytest.raises(MutationDeniedError) as excinfo:
        run(harness.executor.execute(harness.request(CREATE_BRANCH_TOOL_ID, BRANCH_ARGS)))

    assert excinfo.value.reason is MutationReasonCode.REF_ALREADY_EXISTS
    assert transport.writes == 1
    key = next(iter(harness.idempotency._records))
    record = harness.idempotency.get(key)
    assert record is not None
    assert record.outcome is MutationOutcome.FAILED_CLEAN
    assert record.allows_new_attempt


def test_ambiguous_status_never_becomes_success_and_forbids_retry() -> None:
    transport = FakeTransport(revalidate_sha=BASE_SHA, write_status=502)
    harness = Harness(transport)
    harness.approve(CREATE_BRANCH_TOOL_ID, BRANCH_ARGS, approval_id="apr-1")

    with pytest.raises(MutationIndeterminateError) as excinfo:
        run(harness.executor.execute(harness.request(CREATE_BRANCH_TOOL_ID, BRANCH_ARGS)))
    assert excinfo.value.reason is MutationReasonCode.RECONCILIATION_REQUIRED
    assert transport.writes == 1

    harness.approve(CREATE_BRANCH_TOOL_ID, BRANCH_ARGS, approval_id="apr-2")
    with pytest.raises(IdempotencyConflictError) as retry:
        run(
            harness.executor.execute(
                harness.request(CREATE_BRANCH_TOOL_ID, BRANCH_ARGS, approval_id="apr-2")
            )
        )
    assert retry.value.reason is MutationReasonCode.RECONCILIATION_REQUIRED
    assert transport.writes == 1


def test_write_transport_failure_is_indeterminate_with_one_attempt() -> None:
    transport = FakeTransport(revalidate_sha=BASE_SHA, write_error=True)
    harness = Harness(transport)
    harness.approve(CREATE_BRANCH_TOOL_ID, BRANCH_ARGS)

    with pytest.raises(MutationIndeterminateError):
        run(harness.executor.execute(harness.request(CREATE_BRANCH_TOOL_ID, BRANCH_ARGS)))

    assert transport.writes == 1


def test_rate_limited_write_does_not_duplicate_the_write() -> None:
    transport = FakeTransport(revalidate_sha=BASE_SHA, write_status=429)
    harness = Harness(transport)
    harness.approve(CREATE_BRANCH_TOOL_ID, BRANCH_ARGS)

    with pytest.raises(MutationIndeterminateError):
        run(harness.executor.execute(harness.request(CREATE_BRANCH_TOOL_ID, BRANCH_ARGS)))

    assert transport.writes == 1


def test_read_back_mismatch_is_indeterminate_never_committed() -> None:
    mismatched = {**branch_read_back(), "object": {"sha": "e" * 40}}
    transport = FakeTransport(
        revalidate_sha=BASE_SHA,
        write_payload=branch_read_back(),
        read_back_payload=mismatched,
    )
    harness = Harness(transport)
    harness.approve(CREATE_BRANCH_TOOL_ID, BRANCH_ARGS)

    with pytest.raises(MutationIndeterminateError) as excinfo:
        run(harness.executor.execute(harness.request(CREATE_BRANCH_TOOL_ID, BRANCH_ARGS)))

    assert excinfo.value.stage is MutationStage.READ_BACK
    assert transport.writes == 1


def test_read_back_transport_failure_is_indeterminate() -> None:
    transport = FakeTransport(
        revalidate_sha=BASE_SHA,
        write_payload=branch_read_back(),
        read_back_error=True,
    )
    harness = Harness(transport)
    harness.approve(CREATE_BRANCH_TOOL_ID, BRANCH_ARGS)

    with pytest.raises(MutationIndeterminateError):
        run(harness.executor.execute(harness.request(CREATE_BRANCH_TOOL_ID, BRANCH_ARGS)))

    assert transport.writes == 1


# ---------------------------------------------------------------------------
# Adapter and surface invariants
# ---------------------------------------------------------------------------


def test_transport_rejects_absolute_and_traversal_paths() -> None:
    adapter = HttpxMutationTransport()
    authorization = GitHubAuthorization("fake-material-value-1234567890")
    for bad in ("https://evil.test/repos/a/b", "/repos/a/../../b", "/user", "/repos/a/b\\c"):
        with pytest.raises(ProviderTransportError):
            run(adapter.get_json(bad, authorization=authorization, timeout_seconds=1))


def test_executor_requires_a_frozen_registry_and_a_typed_transport() -> None:
    harness = Harness(FakeTransport())
    with pytest.raises(ValueError):
        GitHubMutationExecutor(
            registry=harness.registry,
            broker=harness.broker,
            scope=GitHubRepositoryScope([REPOSITORY]),
            approvals=harness.approvals,
            idempotency=harness.idempotency,
            ledger=harness.ledger,
            transport=object(),  # type: ignore[arg-type]
        )


def test_module_exposes_only_the_two_phase3_mutations() -> None:
    """Scan executable code only: prose in the module docstring is not a surface."""
    import ast

    from hermes_mcp_bridge.v2 import github_mutations

    source = github_mutations.__file__
    assert source is not None
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    tree = ast.parse(text)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported & {"subprocess", "os", "shutil", "pathlib", "socket"} == set()

    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    # The docstring is documentation, not surface: exclude it from the scan.
    body_docstring = ast.get_docstring(tree) or ""
    code = code.replace(body_docstring, "")
    assert "delete_repository" not in code
    for banned in ('"DELETE"', "'DELETE'", "client.delete", "client.patch", "client.put"):
        assert banned not in code
    assert "CREATE_BRANCH_TOOL_ID" in github_mutations.__all__
    assert "CREATE_PR_TOOL_ID" in github_mutations.__all__
    assert github_mutations.CREATE_BRANCH_TOOL_ID == CREATE_BRANCH_TOOL_ID
    assert github_mutations.CREATE_PR_TOOL_ID == CREATE_PR_TOOL_ID


def test_v1_surface_is_untouched() -> None:
    from hermes_mcp_bridge import contracts

    assert contracts.CURRENT_CONTRACT_VERSION == "1.0.0"
    assert contracts.SCHEMA_VERSION == "0.6.1"
    assert contracts.expected_tool_count() == 27


def test_reconcile_result_is_non_secret_and_bounded() -> None:
    handle = reconcile_result(
        executor_repository=REPOSITORY,
        tool_id=CREATE_BRANCH_TOOL_ID,
        idempotency_key="a" * 64,
        operation_digest="b" * 64,
    )
    assert handle["action"] == "RECONCILIATION_REQUIRED"
    assert set(handle) == {
        "action",
        "idempotency_key",
        "operation",
        "operation_digest",
        "repository",
    }


def test_capability_snapshot_state_is_ready_only_for_write_capabilities() -> None:
    broker = build_broker()
    assert broker.is_ready(WriteCapabilityId.BRANCH.value)
    assert broker.readiness("github.read") is None
    readiness = broker.readiness(WriteCapabilityId.BRANCH.value)
    assert readiness is not None
    assert readiness.state is CapabilityState.READY
    assert ApprovalState.PENDING.is_usable
