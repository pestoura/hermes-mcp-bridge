"""Phase 7 execution boundary: the canonical fail-closed invocation pipeline.

> **V2 · PHASE 7 · runtime, disabled by default behind ``PROVIDER_FEATURE_ENABLED``**

Ordering is identical for every provider and is the whole point of the lane::

    1. tool identification + typed schema validation
    2. provider allow-list resolution
    3. exact target scope check
    4. policy evaluation (stable reason code)
    5. capability readiness check
    6. approval + operation-digest binding      [write only]
    7. idempotency key resolution / replay      [write only]
    8. write-ahead audit record                 [write only]
    9. credential resolution (authorization handle)
   10. provider execution within budget
   11. result normalization, field allow-list, byte budget
   12. terminal audit record

Steps 1-8 complete **zero** provider calls, and a scope or policy denial also
completes **zero** credential resolutions. Both facts are counted at runtime by
the gateway itself (``provider_calls``/``credential_resolutions``) so a test can
assert them rather than trusting the prose.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .canonical import canonical_hash
from .provider_audit import (
    AuditError,
    AuditKind,
    IntegrationAuditLedger,
    IntegrationAuditRecord,
    OutcomeClass,
)
from .provider_contract import (
    PROVIDER_FEATURE_ENABLED,
    CapabilityDeclaration,
    ProviderReason,
    audit_safe,
)
from .provider_credentials import CredentialError, ProviderCredentialBroker
from .provider_registry import ProviderRegistry, ProviderRegistryError


class ProviderDenied(RuntimeError):
    """Typed, redacted refusal. Carries a closed reason code and nothing else."""

    def __init__(self, reason: ProviderReason, subject: str = "") -> None:
        self.reason = reason
        self.subject = subject
        super().__init__(reason.value if not subject else f"{reason.value}:{subject}")


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    """A fully typed, fully bound invocation request."""

    request_id: str
    principal_ref: str
    provider_id: str
    capability_id: str
    target_scope_ref: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    approval_ref: str = ""
    approved_operation_digest: str = ""
    idempotency_key: str = ""
    mode: str = "DIRECT"

    def operation_digest(self) -> str:
        return canonical_hash(
            {
                "arguments": dict(self.arguments),
                "capability_id": self.capability_id,
                "provider_id": self.provider_id,
                "target_scope_ref": self.target_scope_ref,
            }
        )


@dataclass(frozen=True, slots=True)
class ProviderCallResult:
    """What a provider adapter returns to the gateway."""

    payload: Mapping[str, Any]
    byte_count: int
    provider_calls: int = 1


@dataclass(frozen=True, slots=True)
class GatewayOutcome:
    """Terminal outcome of one invocation."""

    request_id: str
    outcome: OutcomeClass
    reason_code: ProviderReason
    payload: Mapping[str, Any] = field(default_factory=dict)
    degraded: bool = False
    provider_calls: int = 0
    credential_resolutions: int = 0
    byte_count: int = 0
    duration_ms: int = 0

    @property
    def refused(self) -> bool:
        return self.outcome is OutcomeClass.REFUSED


class PolicyPort:
    """Minimal policy port: a closed decision plus a stable reason code."""

    __slots__ = ("_available", "_decisions")

    def __init__(self, decisions: Mapping[str, str] | None = None) -> None:
        self._decisions = dict(decisions or {})
        self._available = True

    def set_available(self, available: bool) -> None:
        self._available = bool(available)

    def set(self, capability_id: str, decision: str) -> None:
        self._decisions[capability_id] = decision

    def evaluate(self, capability_id: str) -> str:
        if not self._available:
            # A policy engine error is DENY, never default-allow.
            raise ProviderDenied(ProviderReason.E_POLICY_UNAVAILABLE, capability_id)
        return self._decisions.get(capability_id, "DENY")


class IdempotencyStore:
    """Keyed replay store. Unavailability refuses non-idempotent writes."""

    __slots__ = ("_available", "_entries")

    def __init__(self) -> None:
        self._entries: dict[str, GatewayOutcome] = {}
        self._available = True

    def set_available(self, available: bool) -> None:
        self._available = bool(available)

    def lookup(self, key: str) -> GatewayOutcome | None:
        if not self._available:
            raise ProviderDenied(ProviderReason.E_IDEMPOTENCY_UNAVAILABLE, "store")
        return self._entries.get(key)

    def record(self, key: str, outcome: GatewayOutcome) -> None:
        if not self._available:
            raise ProviderDenied(ProviderReason.E_IDEMPOTENCY_UNAVAILABLE, "store")
        self._entries.setdefault(key, outcome)


class ApprovalStore:
    """Approvals bind an immutable operation digest and are single-use."""

    __slots__ = ("_approvals", "_available", "_consumed")

    def __init__(self) -> None:
        self._approvals: dict[str, str] = {}
        self._consumed: set[str] = set()
        self._available = True

    def set_available(self, available: bool) -> None:
        self._available = bool(available)

    def grant(self, approval_ref: str, operation_digest: str) -> None:
        self._approvals[approval_ref] = operation_digest

    def bind(self, approval_ref: str, operation_digest: str) -> None:
        if not self._available:
            raise ProviderDenied(ProviderReason.E_APPROVAL_MISSING, "store")
        if not approval_ref or approval_ref not in self._approvals:
            raise ProviderDenied(ProviderReason.E_APPROVAL_MISSING, approval_ref)
        if approval_ref in self._consumed:
            raise ProviderDenied(ProviderReason.E_APPROVAL_MISSING, approval_ref)
        if self._approvals[approval_ref] != operation_digest:
            raise ProviderDenied(ProviderReason.E_APPROVAL_DIGEST_MISMATCH, approval_ref)
        self._consumed.add(approval_ref)


#: An adapter receives the validated request, the applied headers and the
#: budget. It never sees the registry, the policy engine, the audit sink, the
#: broker, another provider's authorization or the raw principal identity.
ProviderAdapter = Callable[[ProviderRequest, Mapping[str, str], int], ProviderCallResult]


class ScopeResolver:
    """Exact target scope allow-list per capability. No wildcards, ever."""

    __slots__ = ("_allowed",)

    def __init__(self, allowed: Mapping[str, tuple[str, ...]] | None = None) -> None:
        self._allowed = {key: tuple(value) for key, value in dict(allowed or {}).items()}

    def allow(self, capability_id: str, targets: tuple[str, ...]) -> None:
        self._allowed[capability_id] = tuple(targets)

    def require(self, capability_id: str, target: str) -> str:
        allowed = self._allowed.get(capability_id, ())
        if target not in allowed:
            raise ProviderDenied(ProviderReason.E_SCOPE_DENY, capability_id)
        return target


class ProviderGateway:
    """The single execution path shared by every Phase 7 provider."""

    __slots__ = (
        "_adapters",
        "_approvals",
        "_audit",
        "_broker",
        "_credential_resolutions",
        "_idempotency",
        "_policy",
        "_provider_calls",
        "_registry",
        "_scopes",
        "_unknown_outcomes",
    )

    def __init__(
        self,
        *,
        registry: ProviderRegistry,
        policy: PolicyPort,
        scopes: ScopeResolver,
        broker: ProviderCredentialBroker,
        audit: IntegrationAuditLedger,
        adapters: Mapping[str, ProviderAdapter],
        approvals: ApprovalStore | None = None,
        idempotency: IdempotencyStore | None = None,
    ) -> None:
        self._registry = registry
        self._policy = policy
        self._scopes = scopes
        self._broker = broker
        self._audit = audit
        self._adapters = dict(adapters)
        self._approvals = approvals or ApprovalStore()
        self._idempotency = idempotency or IdempotencyStore()
        self._provider_calls = 0
        self._credential_resolutions = 0
        self._unknown_outcomes: list[str] = []

    # -- counters used as runtime proof, not prose ------------------------
    @property
    def provider_calls(self) -> int:
        return self._provider_calls

    @property
    def credential_resolutions(self) -> int:
        return self._credential_resolutions

    @property
    def unknown_outcomes(self) -> tuple[str, ...]:
        return tuple(self._unknown_outcomes)

    # -- audit helpers ----------------------------------------------------
    def _record(
        self,
        request: ProviderRequest,
        *,
        kind: AuditKind,
        declaration: CapabilityDeclaration | None,
        outcome: OutcomeClass,
        reason: ProviderReason,
        policy_decision: str,
        readiness: str,
        provider_calls: int = 0,
        byte_count: int = 0,
        duration_ms: int = 0,
    ) -> None:
        scope_digest = ""
        credential_capability = ""
        if declaration is not None:
            credential_capability = declaration.credential_capability_id
            try:
                scope_digest = self._broker.scope_digest(
                    request.provider_id, credential_capability
                )
            except CredentialError:
                scope_digest = ""
        suffix = "intent" if kind is AuditKind.INTENT else "terminal"
        self._audit.append(
            IntegrationAuditRecord(
                record_id=f"{request.request_id}:{suffix}",
                kind=kind,
                request_id=request.request_id,
                principal_ref=request.principal_ref,
                provider_id=request.provider_id,
                capability_id=request.capability_id,
                tool_id=declaration.tool_id if declaration else "",
                mode=request.mode,
                target_scope_ref=request.target_scope_ref,
                mutation_class=declaration.mutation_class.value if declaration else "",
                security_tier=declaration.security_tier.value if declaration else "",
                policy_decision=policy_decision,
                reason_code=reason,
                readiness_state=readiness,
                credential_capability_id=credential_capability,
                scope_set_digest=scope_digest,
                outcome=outcome,
                provider_call_count=provider_calls,
                byte_count=byte_count,
                duration_ms=duration_ms,
                idempotency_key_digest=(
                    canonical_hash({"k": request.idempotency_key})
                    if request.idempotency_key
                    else ""
                ),
                operation_digest=request.operation_digest(),
                approval_ref=request.approval_ref,
            )
        )

    def _refuse(
        self,
        request: ProviderRequest,
        reason: ProviderReason,
        *,
        declaration: CapabilityDeclaration | None = None,
        policy_decision: str = "",
        readiness: str = "",
        started_ns: int = 0,
    ) -> GatewayOutcome:
        duration = (time.monotonic_ns() - started_ns) // 1_000_000 if started_ns else 0
        self._record(
            request,
            kind=AuditKind.TERMINAL,
            declaration=declaration,
            outcome=OutcomeClass.REFUSED,
            reason=reason,
            policy_decision=policy_decision,
            readiness=readiness,
            duration_ms=duration,
        )
        return GatewayOutcome(
            request_id=request.request_id,
            outcome=OutcomeClass.REFUSED,
            reason_code=reason,
            provider_calls=0,
            credential_resolutions=0,
            duration_ms=duration,
        )

    # -- the pipeline -----------------------------------------------------
    def invoke(self, request: ProviderRequest) -> GatewayOutcome:
        started = time.monotonic_ns()
        calls_before = self._provider_calls
        resolutions_before = self._credential_resolutions

        # 1-2. tool identification, provider allow-list resolution.
        try:
            declaration = self._registry.capability(request.capability_id)
        except (ProviderRegistryError, Exception) as exc:
            reason = getattr(exc, "reason", ProviderReason.E_PROVIDER_UNKNOWN)
            if not isinstance(reason, ProviderReason):
                reason = ProviderReason.E_PROVIDER_UNKNOWN
            return self._refuse(request, reason, started_ns=started)
        if declaration.capability_id.split(".", 1)[0] != request.provider_id:
            return self._refuse(
                request, ProviderReason.E_PROVIDER_UNKNOWN, started_ns=started
            )
        if request.provider_id not in self._adapters:
            return self._refuse(
                request,
                ProviderReason.E_PROVIDER_UNKNOWN,
                declaration=declaration,
                started_ns=started,
            )
        # Typed schema validation: arguments must be canonically serializable
        # and free of secret-shaped identifiers.
        if audit_safe(dict(request.arguments)):
            return self._refuse(
                request,
                ProviderReason.E_REQ_INVALID,
                declaration=declaration,
                started_ns=started,
            )

        # 3. exact target scope — before policy, so an out-of-scope caller
        # cannot learn capability health, and before any credential use.
        try:
            self._scopes.require(request.capability_id, request.target_scope_ref)
        except ProviderDenied as denied:
            return self._refuse(
                request, denied.reason, declaration=declaration, started_ns=started
            )

        # 4. policy.
        try:
            decision = self._policy.evaluate(request.capability_id)
        except ProviderDenied as denied:
            return self._refuse(
                request, denied.reason, declaration=declaration, started_ns=started
            )
        if decision not in ("ALLOW", "APPROVAL_REQUIRED"):
            return self._refuse(
                request,
                ProviderReason.E_POLICY_DENY,
                declaration=declaration,
                policy_decision=decision,
                started_ns=started,
            )

        # 5. readiness.
        readiness = self._registry.state(request.capability_id).value
        if not self._registry.is_usable(request.capability_id):
            return self._refuse(
                request,
                ProviderReason.E_CAP_NOT_READY,
                declaration=declaration,
                policy_decision=decision,
                readiness=readiness,
                started_ns=started,
            )
        degraded = not declaration.is_write and readiness == "DEGRADED"

        if declaration.is_write:
            # 6. approval + operation digest binding.
            if declaration.approval_required or decision == "APPROVAL_REQUIRED":
                try:
                    self._approvals.bind(request.approval_ref, request.operation_digest())
                except ProviderDenied as denied:
                    return self._refuse(
                        request,
                        denied.reason,
                        declaration=declaration,
                        policy_decision=decision,
                        readiness=readiness,
                        started_ns=started,
                    )
            # 7. idempotency replay.
            if declaration.idempotency.requires_idempotency_key and not request.idempotency_key:
                return self._refuse(
                    request,
                    ProviderReason.E_REQ_INVALID,
                    declaration=declaration,
                    policy_decision=decision,
                    readiness=readiness,
                    started_ns=started,
                )
            if request.idempotency_key:
                try:
                    prior = self._idempotency.lookup(request.idempotency_key)
                except ProviderDenied as denied:
                    return self._refuse(
                        request,
                        denied.reason,
                        declaration=declaration,
                        policy_decision=decision,
                        readiness=readiness,
                        started_ns=started,
                    )
                if prior is not None:
                    # Prior outcome returned; zero second side effect.
                    self._record(
                        request,
                        kind=AuditKind.TERMINAL,
                        declaration=declaration,
                        outcome=prior.outcome,
                        reason=ProviderReason.E_IDEMPOTENCY_REPLAY,
                        policy_decision=decision,
                        readiness=readiness,
                    )
                    return GatewayOutcome(
                        request_id=request.request_id,
                        outcome=prior.outcome,
                        reason_code=ProviderReason.E_IDEMPOTENCY_REPLAY,
                        payload=prior.payload,
                        provider_calls=0,
                        credential_resolutions=0,
                    )
            # 8. write-ahead audit BEFORE any side effect.
            try:
                self._record(
                    request,
                    kind=AuditKind.INTENT,
                    declaration=declaration,
                    outcome=OutcomeClass.UNKNOWN,
                    reason=ProviderReason.OK,
                    policy_decision=decision,
                    readiness=readiness,
                )
            except AuditError:
                return GatewayOutcome(
                    request_id=request.request_id,
                    outcome=OutcomeClass.REFUSED,
                    reason_code=ProviderReason.E_AUDIT_UNAVAILABLE,
                )

        # 9. credential resolution.
        try:
            handle = self._broker.resolve(
                provider_id=request.provider_id,
                credential_capability_id=declaration.credential_capability_id,
                requested_scopes=declaration.scopes,
                ttl_ms=declaration.deadline_ms,
            )
        except CredentialError as exc:
            return self._refuse(
                request,
                exc.reason,
                declaration=declaration,
                policy_decision=decision,
                readiness=readiness,
                started_ns=started,
            )
        self._credential_resolutions += 1

        # 10. provider execution within budget.
        adapter = self._adapters[request.provider_id]
        try:
            headers = handle.apply({})
        except CredentialError as exc:
            return self._refuse(
                request,
                exc.reason,
                declaration=declaration,
                policy_decision=decision,
                readiness=readiness,
                started_ns=started,
            )
        outcome_class = OutcomeClass.SUCCESS
        reason = ProviderReason.OK
        payload: Mapping[str, Any] = {}
        byte_count = 0
        try:
            result = adapter(request, headers, declaration.deadline_ms)
            self._provider_calls += max(0, int(result.provider_calls))
            byte_count = int(result.byte_count)
            # 11. result normalization and byte budget.
            if byte_count > declaration.max_result_bytes:
                raise ProviderDenied(ProviderReason.E_PROVIDER_RESULT_TOO_LARGE)
            if not isinstance(result.payload, Mapping):
                raise ProviderDenied(ProviderReason.E_PROVIDER_SHAPE)
            if audit_safe(dict(result.payload)):
                raise ProviderDenied(ProviderReason.E_PROVIDER_SHAPE)
            payload = dict(result.payload)
        except ProviderDenied as denied:
            outcome_class = (
                OutcomeClass.UNKNOWN
                if declaration.is_write
                and denied.reason is ProviderReason.E_PROVIDER_DEADLINE
                else OutcomeClass.REFUSED
            )
            reason = denied.reason
            payload = {}
        except Exception:
            # A provider fault is contained: normalized, redacted, and it never
            # marks another provider or the gateway unhealthy.
            self._provider_calls += 1
            outcome_class = (
                OutcomeClass.UNKNOWN if declaration.is_write else OutcomeClass.ERROR
            )
            reason = ProviderReason.E_PROVIDER_FAULT
            payload = {}
        finally:
            handle.revoke()

        duration = (time.monotonic_ns() - started) // 1_000_000
        if outcome_class is OutcomeClass.UNKNOWN:
            self._unknown_outcomes.append(request.request_id)

        # 12. terminal audit record.
        self._record(
            request,
            kind=AuditKind.TERMINAL,
            declaration=declaration,
            outcome=outcome_class,
            reason=reason,
            policy_decision=decision,
            readiness=readiness,
            provider_calls=self._provider_calls - calls_before,
            byte_count=byte_count,
            duration_ms=duration,
        )
        outcome = GatewayOutcome(
            request_id=request.request_id,
            outcome=outcome_class,
            reason_code=reason,
            payload=payload,
            degraded=degraded,
            provider_calls=self._provider_calls - calls_before,
            credential_resolutions=self._credential_resolutions - resolutions_before,
            byte_count=byte_count,
            duration_ms=duration,
        )
        if (
            declaration.is_write
            and request.idempotency_key
            and outcome_class is OutcomeClass.SUCCESS
        ):
            self._idempotency.record(request.idempotency_key, outcome)
        return outcome


__all__ = [
    "PROVIDER_FEATURE_ENABLED",
    "ApprovalStore",
    "GatewayOutcome",
    "IdempotencyStore",
    "PolicyPort",
    "ProviderAdapter",
    "ProviderCallResult",
    "ProviderDenied",
    "ProviderGateway",
    "ProviderRequest",
    "ScopeResolver",
]
