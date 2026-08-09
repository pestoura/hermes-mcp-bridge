"""Phase 3 lane L5 — the only DIRECT GitHub write executor.

This module executes exactly two mutations, ``github.create_branch`` and
``github.create_pr``, and nothing else. It owns the *ordering* of the Phase 3
preflight and the provider interaction; it owns none of the semantics those
stages implement. Every stage delegates to the lane that published it:

* registry / typed input      -> :mod:`.github_mutation_registry` (L2)
* capability readiness        -> :mod:`.github_write_credentials` (L1)
* policy decision             -> :mod:`.policy`
* approval / operation digest -> :mod:`.mutation_digest` (L3)
* idempotency / lease         -> :mod:`.mutation_idempotency` (L3)
* write-ahead audit/evidence  -> :mod:`.mutation_audit` (L4)
* enums / error taxonomy      -> :mod:`.enums`, :mod:`.errors` (C-1)

Fixed ordering
--------------

The stage order is :data:`hermes_mcp_bridge.v2.enums.MUTATION_STAGE_ORDER`,
which is the single source of truth published by the Controller lane:
``SCOPE -> REGISTRY -> POLICY -> CREDENTIAL -> APPROVAL -> IDEMPOTENCY ->
PRECONDITION_REVALIDATION -> WRITE_AHEAD_AUDIT -> PROVIDER_CALL -> READ_BACK ->
RESULT_SHAPING``. The executor asserts the order it walked against that
sequence, so a reordering is a test failure rather than a silent behaviour
change. The idempotency claim necessarily precedes the write-ahead audit
because :class:`~hermes_mcp_bridge.v2.mutation_audit.MutationIntent` requires
the idempotency key and status as inputs; the audit record is still written and
proven durable *before* any provider mutation.

Fail-closed rules
-----------------

* Exactly one provider mutation call per execution, enforced by a guard that
  raises if a second write is attempted.
* No retry after an ambiguous outcome. Timeout, connection reset, an
  unenumerated status, a rate-limited response and an unverifiable or
  mismatching read-back all produce ``AMBIGUOUS`` /
  :class:`~hermes_mcp_bridge.v2.errors.MutationIndeterminateError` with
  ``RECONCILIATION_REQUIRED``. Only ``FAILED_CLEAN`` permits a new attempt, and
  only through a fresh :meth:`GitHubMutationExecutor.execute` call.
* Success is never inferred: ``COMMITTED`` requires a positive read-back.
* No credential material, installation id, auth header, path, prompt or raw
  provider body ever reaches a result, an error or an evidence record. Errors
  are the fixed ``"<STAGE>:<REASON>"`` token pair from C-1.
* HTTP is confined to the bounded adapter in this module: two methods, an
  allow-listed path shape, no redirects, no environment proxies, a byte cap and
  the contract timeout. There is no shell, subprocess, filesystem or generic
  request surface.

V1 is untouched: nothing here is imported by the V1 server, no MCP tool is
registered, and the V1 contract (bridge ``1.0.0``, schema ``0.6.1``, 27 tools)
is unchanged.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Protocol, runtime_checkable

import httpx

from .enums import (
    MUTATION_STAGE_ORDER,
    ApprovalState,
    CapabilityState,
    IdempotencyStatus,
    MutationOutcome,
    MutationReasonCode,
    MutationStage,
    PolicyDecision,
    WriteCapabilityId,
)
from .errors import (
    ApprovalError,
    ConcurrencyDriftError,
    MutationDeniedError,
    MutationIndeterminateError,
    MutationScopeError,
    WriteCapabilityError,
)
from .github_auth import GitHubAuthorization
from .github_direct import GitHubRepositoryScope
from .github_mutation_registry import (
    CREATE_BRANCH_TOOL_ID,
    CREATE_PR_TOOL_ID,
    MutationContract,
    get_mutation_contract,
    github_mutation_policy_rules,
    normalize_arguments,
)
from .github_write_credentials import WriteCapabilityBroker
from .mutation_audit import (
    ApprovalReference,
    CapabilitySnapshot,
    EvidenceClass,
    MutationAuditLedger,
    MutationIntent,
    ProviderObservation,
    VerificationState,
    looks_secret_bearing,
)
from .mutation_digest import (
    ApprovalStore,
    OperationDescriptor,
    OperationPreconditions,
    compute_operation_digest,
)
from .mutation_idempotency import IdempotencyStore, compute_idempotency_key
from .policy import PolicyEngine, PolicyRuleSet
from .registry import ToolRegistry

#: Base URL of the only host this executor may contact.
GITHUB_API_BASE_URL: Final[str] = "https://api.github.com"
GITHUB_API_VERSION: Final[str] = "2026-03-10"
GITHUB_ACCEPT: Final[str] = "application/vnd.github+json"
GITHUB_USER_AGENT: Final[str] = "hermes-mcp-bridge-v2-mutation"

#: Hard cap on a provider response body. A larger body is not parsed.
MAX_RESPONSE_BYTES: Final[int] = 256 * 1024

#: Default, non-secret policy version recorded in the operation digest.
DEFAULT_POLICY_VERSION: Final[str] = "phase3.mutations.v1"

#: Result envelope version.
MUTATION_RESULT_SCHEMA: Final[str] = "v2.phase3.mutation-result.1"

_STAGE_ORDER: Final[tuple[MutationStage, ...]] = tuple(MUTATION_STAGE_ORDER)


# ---------------------------------------------------------------------------
# Bounded provider adapter
# ---------------------------------------------------------------------------


class ProviderTransportError(Exception):
    """Transport-level uncertainty. Never carries a provider body."""


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    """The bounded, sanitized view of one provider response.

    ``payload`` is a parsed JSON object with no headers, no raw text and no
    request context. Anything unparseable yields an empty payload; the caller
    then treats the outcome as unverified rather than guessing.
    """

    status_code: int
    payload: Mapping[str, Any] = field(default_factory=dict)

    @property
    def status_class(self) -> str:
        return f"{max(1, min(5, self.status_code // 100))}xx"


@runtime_checkable
class MutationTransport(Protocol):
    """The only provider surface L5 may use: one read and one write verb."""

    async def get_json(
        self,
        path: str,
        *,
        authorization: GitHubAuthorization,
        timeout_seconds: int,
    ) -> ProviderResponse: ...

    async def post_json(
        self,
        path: str,
        *,
        body: Mapping[str, Any],
        authorization: GitHubAuthorization,
        timeout_seconds: int,
    ) -> ProviderResponse: ...


def _assert_relative_path(path: str) -> str:
    """Reject anything that is not a bounded, relative GitHub API path."""
    invalid = (
        not isinstance(path, str)
        or not path.startswith("/repos/")
        or "://" in path
        or ".." in path
        or "\\" in path
        or len(path) > 512
        or any(ord(char) < 0x20 or ord(char) > 0x7E for char in path)
    )
    if invalid:
        raise ProviderTransportError("invalid provider path")
    return path


class HttpxMutationTransport:
    """Bounded httpx adapter. No redirects, no env proxies, no shell.

    A transport failure raises :class:`ProviderTransportError`, which the
    executor classifies as ``AMBIGUOUS``; it never becomes a clean failure.
    """

    __slots__ = ("_max_bytes", "_transport")

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        if isinstance(max_response_bytes, bool) or not isinstance(max_response_bytes, int):
            raise ValueError("max_response_bytes must be an integer")
        if not 1024 <= max_response_bytes <= MAX_RESPONSE_BYTES:
            raise ValueError("max_response_bytes outside allowed bounds")
        self._transport = transport
        self._max_bytes = max_response_bytes

    def _headers(self, authorization: GitHubAuthorization) -> dict[str, str]:
        return {
            "Accept": GITHUB_ACCEPT,
            "Authorization": authorization.header_value(),
            "User-Agent": GITHUB_USER_AGENT,
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None,
        authorization: GitHubAuthorization,
        timeout_seconds: int,
    ) -> ProviderResponse:
        _assert_relative_path(path)
        if method not in {"GET", "POST"}:
            raise ProviderTransportError("unsupported method")
        try:
            async with httpx.AsyncClient(
                base_url=GITHUB_API_BASE_URL,
                transport=self._transport,
                follow_redirects=False,
                timeout=httpx.Timeout(float(timeout_seconds)),
                trust_env=False,
            ) as client:
                response = await client.request(
                    method,
                    path,
                    json=dict(body) if body is not None else None,
                    headers=self._headers(authorization),
                )
        except httpx.HTTPError as exc:
            raise ProviderTransportError("upstream transport failure") from exc

        content = response.content or b""
        if len(content) > self._max_bytes:
            return ProviderResponse(status_code=response.status_code, payload={})
        try:
            parsed = json.loads(content) if content else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}
        return ProviderResponse(status_code=response.status_code, payload=parsed)

    async def get_json(
        self,
        path: str,
        *,
        authorization: GitHubAuthorization,
        timeout_seconds: int,
    ) -> ProviderResponse:
        return await self._request(
            "GET",
            path,
            body=None,
            authorization=authorization,
            timeout_seconds=timeout_seconds,
        )

    async def post_json(
        self,
        path: str,
        *,
        body: Mapping[str, Any],
        authorization: GitHubAuthorization,
        timeout_seconds: int,
    ) -> ProviderResponse:
        return await self._request(
            "POST",
            path,
            body=body,
            authorization=authorization,
            timeout_seconds=timeout_seconds,
        )


class _SingleWriteGuard:
    """Proves the one-provider-mutation invariant for a single execution."""

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


# ---------------------------------------------------------------------------
# Request / result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MutationRequest:
    """A single, fully-specified mutation attempt.

    ``arguments`` is the untrusted caller map; it is normalized by the L2
    registry and never used raw. ``attempt_token`` is the explicit new-attempt
    marker: a retry after a clean failure must supply a different token, so no
    retry can ever be implicit.
    """

    principal: str
    tool_id: str
    arguments: Mapping[str, Any]
    approval_id: str
    attempt_token: str | None = None


@dataclass(frozen=True, slots=True)
class MutationResult:
    """Bounded, typed result. Contains no provider body and no secrets."""

    schema: str
    tool_id: str
    repository: str
    outcome: MutationOutcome
    verification: VerificationState
    evidence_class: EvidenceClass
    idempotency_status: IdempotencyStatus
    operation_digest: str
    idempotency_key: str
    audit_id: str | None
    evidence_digest: str | None
    data: Mapping[str, Any]
    stages: tuple[MutationStage, ...]
    provider_writes: int

    def as_canonical(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": self.schema,
            "tool_id": self.tool_id,
            "repository": self.repository,
            "outcome": self.outcome.value,
            "verification": self.verification.value,
            "evidence_class": self.evidence_class.value,
            "idempotency_status": self.idempotency_status.value,
            "operation_digest": self.operation_digest,
            "idempotency_key": self.idempotency_key,
            "data": dict(self.data),
            "stages": [stage.value for stage in self.stages],
            "provider_writes": self.provider_writes,
        }
        if self.audit_id is not None:
            payload["audit_id"] = self.audit_id
        if self.evidence_digest is not None:
            payload["evidence_digest"] = self.evidence_digest
        return payload


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _indeterminate(stage: MutationStage) -> MutationIndeterminateError:
    return MutationIndeterminateError(MutationReasonCode.RECONCILIATION_REQUIRED, stage)


def _shape_value(value: Any) -> Any:
    """Bound a single result value; anything unsafe is dropped, never echoed."""
    if isinstance(value, bool | int):
        return value
    if isinstance(value, str):
        if len(value) > 256 or looks_secret_bearing(value):
            return None
        return value
    return None


class GitHubMutationExecutor:
    """Execute ``create_branch`` / ``create_pr`` under the Phase 3 preflight."""

    __slots__ = (
        "_approvals",
        "_broker",
        "_clock",
        "_idempotency",
        "_ledger",
        "_policy_version",
        "_registry",
        "_rules",
        "_scope",
        "_transport",
    )

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        broker: WriteCapabilityBroker,
        scope: GitHubRepositoryScope,
        approvals: ApprovalStore,
        idempotency: IdempotencyStore,
        ledger: MutationAuditLedger,
        transport: MutationTransport,
        rules: PolicyRuleSet | None = None,
        policy_version: str = DEFAULT_POLICY_VERSION,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not registry.frozen:
            raise ValueError("mutation registry must be frozen")
        if not isinstance(broker, WriteCapabilityBroker):
            raise ValueError("broker must be a WriteCapabilityBroker")
        if not isinstance(transport, MutationTransport):
            raise ValueError("transport must implement MutationTransport")
        self._registry = registry
        self._broker = broker
        self._scope = scope
        self._approvals = approvals
        self._idempotency = idempotency
        self._ledger = ledger
        self._transport = transport
        self._rules = rules if rules is not None else github_mutation_policy_rules()
        self._policy_version = policy_version
        self._clock: Callable[[], datetime] = clock if callable(clock) else _now

    # -- stage helpers -----------------------------------------------------

    def _descriptor(
        self,
        contract: MutationContract,
        repository: str,
        arguments: Mapping[str, Any],
    ) -> OperationDescriptor:
        if contract.tool_id == CREATE_BRANCH_TOOL_ID:
            preconditions = OperationPreconditions(base_sha=arguments["base_sha"])
        else:
            preconditions = OperationPreconditions(expected_head_sha=arguments["expected_head_sha"])
        digest_arguments = {key: value for key, value in arguments.items() if key != "repository"}
        return OperationDescriptor(
            operation=contract.tool_id,
            capability=contract.write_capability,
            repository=repository,
            arguments=digest_arguments,
            preconditions=preconditions,
            policy_version=self._policy_version,
            registry_snapshot_hash=self._registry.capability_snapshot_hash(),
        )

    @staticmethod
    def _target(contract: MutationContract, arguments: Mapping[str, Any]) -> str:
        if contract.tool_id == CREATE_BRANCH_TOOL_ID:
            return str(arguments["branch"])
        return str(arguments["head"])

    @staticmethod
    def _observed_preconditions(
        contract: MutationContract, arguments: Mapping[str, Any]
    ) -> dict[str, str]:
        if contract.tool_id == CREATE_BRANCH_TOOL_ID:
            return {"base_sha": str(arguments["base_sha"])}
        return {"expected_head_sha": str(arguments["expected_head_sha"])}

    def _write_body(
        self, contract: MutationContract, arguments: Mapping[str, Any]
    ) -> dict[str, Any]:
        if contract.tool_id == CREATE_BRANCH_TOOL_ID:
            return {"ref": arguments["ref"], "sha": arguments["base_sha"]}
        body: dict[str, Any] = {
            "title": arguments["title"],
            "head": arguments["head"],
            "base": arguments["base"],
            "draft": arguments["draft"],
        }
        if arguments.get("body"):
            body["body"] = arguments["body"]
        return body

    @staticmethod
    def _endpoint(template: str, repository: str, **extra: Any) -> str:
        owner, _, repo = repository.partition("/")
        return template.format(owner=owner, repo=repo, **extra)

    # -- provider stages ---------------------------------------------------

    async def _revalidate(
        self,
        contract: MutationContract,
        repository: str,
        arguments: Mapping[str, Any],
        authorization: GitHubAuthorization,
    ) -> None:
        """TOCTOU re-read immediately before the write. Drift ⇒ DENY."""
        if contract.tool_id == CREATE_BRANCH_TOOL_ID:
            expected = str(arguments["base_sha"])
            path = self._endpoint(
                "/repos/{owner}/{repo}/git/commits/{sha}", repository, sha=expected
            )
        else:
            expected = str(arguments["expected_head_sha"])
            path = self._endpoint(
                "/repos/{owner}/{repo}/git/ref/heads/{branch}",
                repository,
                branch=arguments["head"],
            )
        try:
            response = await self._transport.get_json(
                path,
                authorization=authorization,
                timeout_seconds=contract.timeout_seconds,
            )
        except ProviderTransportError as exc:
            raise _indeterminate(MutationStage.PRECONDITION_REVALIDATION) from exc
        if response.status_code != 200:
            raise ConcurrencyDriftError(
                MutationReasonCode.PRECONDITION_DRIFT,
                MutationStage.PRECONDITION_REVALIDATION,
            )
        observed = _extract_sha(contract.tool_id, response.payload)
        if observed != expected:
            raise ConcurrencyDriftError(
                MutationReasonCode.PRECONDITION_DRIFT,
                MutationStage.PRECONDITION_REVALIDATION,
            )

    async def _read_back(
        self,
        contract: MutationContract,
        repository: str,
        arguments: Mapping[str, Any],
        created: Mapping[str, Any],
        authorization: GitHubAuthorization,
    ) -> tuple[VerificationState, dict[str, Any]]:
        """Mandatory read-back. Unverifiable or mismatching ⇒ never COMMITTED."""
        if contract.tool_id == CREATE_BRANCH_TOOL_ID:
            path = self._endpoint(
                contract.read_back.endpoint_template,
                repository,
                branch=arguments["branch"],
            )
        else:
            number = created.get("number")
            if not isinstance(number, int) or isinstance(number, bool) or number < 1:
                return VerificationState.UNVERIFIABLE, {}
            path = self._endpoint(contract.read_back.endpoint_template, repository, number=number)
        try:
            response = await self._transport.get_json(
                path,
                authorization=authorization,
                timeout_seconds=contract.timeout_seconds,
            )
        except ProviderTransportError:
            return VerificationState.UNVERIFIABLE, {}
        if response.status_code != 200 or not response.payload:
            return VerificationState.UNVERIFIABLE, {}

        if contract.tool_id == CREATE_BRANCH_TOOL_ID:
            observed = {
                "ref": response.payload.get("ref"),
                "sha": _object_sha(response.payload),
            }
            expected = {"ref": arguments["ref"], "sha": arguments["base_sha"]}
        else:
            head = response.payload.get("head")
            observed = {
                "number": response.payload.get("number"),
                "head_sha": head.get("sha") if isinstance(head, dict) else None,
                "state": response.payload.get("state"),
            }
            expected = {
                "number": created.get("number"),
                "head_sha": arguments["expected_head_sha"],
                "state": "open",
            }
        for field_name in contract.read_back.verified_fields:
            if observed.get(field_name) is None:
                return VerificationState.UNVERIFIABLE, {}
            if observed.get(field_name) != expected.get(field_name):
                return VerificationState.MISMATCH, {}
        return VerificationState.VERIFIED, observed

    # -- main --------------------------------------------------------------

    async def execute(self, request: MutationRequest) -> MutationResult:
        """Run the fixed preflight and, at most once, the provider mutation."""
        if not isinstance(request, MutationRequest):
            raise MutationDeniedError(MutationReasonCode.INVALID_ARGUMENTS, MutationStage.REGISTRY)
        stages: list[MutationStage] = []
        guard = _SingleWriteGuard()

        # 1. REGISTRY — typed contract and normalized arguments.
        stages.append(MutationStage.REGISTRY)
        contract = get_mutation_contract(request.tool_id)
        arguments = normalize_arguments(contract.tool_id, dict(request.arguments))
        repository = str(arguments["repository"])

        # 2. SCOPE — exact allow-list, before any credential or HTTP work.
        stages.insert(0, MutationStage.SCOPE)
        owner, _, repo = repository.partition("/")
        if not self._scope.allows(owner, repo):
            raise MutationScopeError(
                MutationReasonCode.REPOSITORY_OUT_OF_SCOPE, MutationStage.SCOPE
            )

        # 3. POLICY — explicit per-operation rule; missing rule is DENY.
        stages.append(MutationStage.POLICY)
        evaluation = PolicyEngine(self._registry, self._rules, self._broker).evaluate(
            contract.tool_id
        )
        if evaluation.decision is not PolicyDecision.APPROVAL_REQUIRED:
            raise MutationDeniedError(
                MutationReasonCode.MUTATION_NOT_REGISTERED
                if evaluation.decision is PolicyDecision.ALLOW
                else MutationReasonCode.DESTRUCTIVE_OPERATION_FORBIDDEN,
                MutationStage.POLICY,
                detail=evaluation.reason_code.value,
            )

        # 4. CREDENTIAL — write capability readiness, then material.
        stages.append(MutationStage.CREDENTIAL)
        capability: WriteCapabilityId = contract.write_capability
        readiness = self._broker.readiness(capability.value)
        if readiness is None or not readiness.is_ready:
            raise WriteCapabilityError(
                (readiness.reason if readiness and readiness.reason else None)
                or MutationReasonCode.WRITE_CAPABILITY_NOT_READY,
                MutationStage.CREDENTIAL,
            )
        authorization = self._broker.authorize(capability.value, repository)

        # 5. APPROVAL — single-use, bound to the operation digest.
        stages.append(MutationStage.APPROVAL)
        descriptor = self._descriptor(contract, repository, arguments)
        operation_digest = compute_operation_digest(descriptor)
        if not isinstance(request.approval_id, str) or not request.approval_id:
            raise ApprovalError(MutationReasonCode.APPROVAL_MISSING, MutationStage.APPROVAL)
        approval = self._approvals.verify_and_consume(
            request.approval_id,
            descriptor,
            principal=request.principal,
            now=self._clock(),
        )

        # 6. IDEMPOTENCY — claim the key and the per-resource lease.
        stages.append(MutationStage.IDEMPOTENCY)
        idempotency_key = compute_idempotency_key(
            principal=request.principal,
            capability=capability,
            repository=repository,
            operation=contract.tool_id,
            operation_digest=operation_digest,
            client_key=request.attempt_token,
        )
        decision = self._idempotency.begin(
            idempotency_key=idempotency_key,
            principal=request.principal,
            repository=repository,
            operation=contract.tool_id,
            operation_digest=operation_digest,
            target=self._target(contract, arguments),
            now=self._clock(),
        )
        if not decision.executes_provider_call:
            return self._replayed_result(
                contract=contract,
                repository=repository,
                decision=decision,
                operation_digest=operation_digest,
                idempotency_key=idempotency_key,
                stages=stages,
            )

        # 7. PRECONDITION_REVALIDATION — TOCTOU re-read against the provider.
        stages.append(MutationStage.PRECONDITION_REVALIDATION)
        try:
            await self._revalidate(contract, repository, arguments, authorization)
        except MutationIndeterminateError:
            self._idempotency.mark_ambiguous(idempotency_key, now=self._clock())
            raise
        except MutationDeniedError as exc:
            self._idempotency.fail_clean(idempotency_key, reason=exc.reason, now=self._clock())
            raise

        # 8. WRITE_AHEAD_AUDIT — durable before any provider mutation.
        stages.append(MutationStage.WRITE_AHEAD_AUDIT)
        intent = MutationIntent(
            principal=request.principal,
            operation=contract.tool_id,
            repository=repository,
            operation_digest=operation_digest,
            policy_decision=PolicyDecision.APPROVAL_REQUIRED,
            capability=CapabilitySnapshot(
                capability_id=capability,
                state=CapabilityState.READY,
                snapshot_hash=self._registry.capability_snapshot_hash(),
                policy_version=self._policy_version,
            ),
            idempotency_key=idempotency_key,
            idempotency_status=decision.status,
            approval=ApprovalReference(
                approval_id=approval.approval_id,
                # L4 records the state that *authorized* the record, which must
                # be a usable one. Consumption already happened atomically at
                # the APPROVAL stage above (single-use is enforced there), so
                # this is the binding state, not a claim that the approval is
                # still spendable.
                state=ApprovalState.PENDING,
                bound_digest=operation_digest,
            ),
            preconditions_observed=self._observed_preconditions(contract, arguments),
            registry_snapshot_hash=self._registry.capability_snapshot_hash(),
        )
        try:
            handle = self._ledger.begin(intent)
        except MutationDeniedError as exc:
            # No provider call may follow an unwritable write-ahead record.
            self._idempotency.fail_clean(idempotency_key, reason=exc.reason, now=self._clock())
            raise
        if not handle.provider_call_permitted:  # pragma: no cover - defensive
            raise MutationDeniedError(
                MutationReasonCode.AUDIT_RECORD_UNWRITABLE, MutationStage.WRITE_AHEAD_AUDIT
            )

        # 9. PROVIDER_CALL — exactly one write, never retried.
        stages.append(MutationStage.PROVIDER_CALL)
        started_at = self._clock()
        path = self._endpoint(contract.endpoint_template, repository)
        guard.claim()
        try:
            response = await self._transport.post_json(
                path,
                body=self._write_body(contract, arguments),
                authorization=authorization,
                timeout_seconds=contract.timeout_seconds,
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

        outcome = contract.classify_status(response.status_code)
        if outcome is MutationOutcome.AMBIGUOUS:
            raise self._finish_ambiguous(
                handle=handle,
                idempotency_key=idempotency_key,
                started_at=started_at,
                status_class=response.status_class,
                stage=MutationStage.PROVIDER_CALL,
                guard=guard,
            )
        if outcome is MutationOutcome.FAILED_CLEAN:
            reason = (
                MutationReasonCode.REF_ALREADY_EXISTS
                if response.status_code == 422
                else MutationReasonCode.WRITE_CAPABILITY_NOT_READY
            )
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
            raise MutationDeniedError(reason, MutationStage.PROVIDER_CALL)

        # 10. READ_BACK — mandatory verification of the created object.
        stages.append(MutationStage.READ_BACK)
        verification, observed = await self._read_back(
            contract, repository, arguments, response.payload, authorization
        )
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

        # 11. RESULT_SHAPING — bounded typed result, audit and lease closed.
        stages.append(MutationStage.RESULT_SHAPING)
        data = self._shape(contract, observed, response.payload)
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
        self._idempotency.commit(idempotency_key, result=data, now=self._clock())
        self._assert_stage_order(stages)
        return MutationResult(
            schema=MUTATION_RESULT_SCHEMA,
            tool_id=contract.tool_id,
            repository=repository,
            outcome=MutationOutcome.COMMITTED,
            verification=VerificationState.VERIFIED,
            evidence_class=EvidenceClass.SUCCESS,
            idempotency_status=decision.status,
            operation_digest=operation_digest,
            idempotency_key=idempotency_key,
            audit_id=evidence.audit_id,
            evidence_digest=evidence.evidence_digest,
            data=data,
            stages=tuple(stages),
            provider_writes=guard.writes,
        )

    # -- terminal helpers --------------------------------------------------

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
        """Close an ambiguous attempt. Reconciliation is mandatory; no retry."""
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

    def _replayed_result(
        self,
        *,
        contract: MutationContract,
        repository: str,
        decision: Any,
        operation_digest: str,
        idempotency_key: str,
        stages: list[MutationStage],
    ) -> MutationResult:
        """A replay returns the recorded result and issues no provider call."""
        record = decision.record
        data = dict(record.result or {})
        return MutationResult(
            schema=MUTATION_RESULT_SCHEMA,
            tool_id=contract.tool_id,
            repository=repository,
            outcome=record.outcome,
            verification=VerificationState.NOT_ATTEMPTED,
            evidence_class=EvidenceClass.SUCCESS
            if record.outcome is MutationOutcome.COMMITTED
            else EvidenceClass.BLOCKED,
            idempotency_status=decision.status,
            operation_digest=operation_digest,
            idempotency_key=idempotency_key,
            audit_id=None,
            evidence_digest=None,
            data=data,
            stages=tuple(stages),
            provider_writes=0,
        )

    @staticmethod
    def _shape(
        contract: MutationContract,
        observed: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Bounded projection over the contract's declared result fields."""
        shaped: dict[str, Any] = {}
        for name in contract.result_fields:
            raw = observed.get(name, payload.get(name))
            if name == "url":
                raw = payload.get("html_url") or _ref_url(payload)
            value = _shape_value(raw)
            if value is not None:
                shaped[name] = value
        return shaped

    @staticmethod
    def _assert_stage_order(stages: Sequence[MutationStage]) -> None:
        """The walked stages must be a prefix-ordered subsequence of C-1."""
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


def _object_sha(payload: Mapping[str, Any]) -> Any:
    obj = payload.get("object")
    return obj.get("sha") if isinstance(obj, dict) else None


def _ref_url(payload: Mapping[str, Any]) -> Any:
    url = payload.get("url")
    return url if isinstance(url, str) else None


def _extract_sha(tool_id: str, payload: Mapping[str, Any]) -> Any:
    """Read the precondition SHA from a revalidation response."""
    if tool_id == CREATE_BRANCH_TOOL_ID:
        return payload.get("sha")
    return _object_sha(payload)


def reconcile_result(
    *,
    executor_repository: str,
    tool_id: str,
    idempotency_key: str,
    operation_digest: str,
) -> dict[str, str]:
    """Operator-facing, non-secret handle for a mandatory reconciliation read."""
    return {
        "action": "RECONCILIATION_REQUIRED",
        "idempotency_key": idempotency_key,
        "operation": tool_id,
        "operation_digest": operation_digest,
        "repository": executor_repository,
    }


__all__ = [
    "CREATE_BRANCH_TOOL_ID",
    "CREATE_PR_TOOL_ID",
    "DEFAULT_POLICY_VERSION",
    "GITHUB_ACCEPT",
    "GITHUB_API_BASE_URL",
    "GITHUB_API_VERSION",
    "GITHUB_USER_AGENT",
    "MAX_RESPONSE_BYTES",
    "MUTATION_RESULT_SCHEMA",
    "GitHubMutationExecutor",
    "HttpxMutationTransport",
    "MutationRequest",
    "MutationResult",
    "MutationTransport",
    "ProviderResponse",
    "ProviderTransportError",
    "reconcile_result",
]
