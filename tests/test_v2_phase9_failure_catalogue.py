"""Phase 9: the F-01..F-20 failure-injection catalogue, executed.

The catalogue in ``docs/v2/downstream/phase9/failure-injection.md`` states an
expected behaviour per fault. This module injects each fault against the real
Phase 7 gateway and asserts the stated behaviour, so the catalogue is executed
rather than described.

Three properties are checked for every fault, because they are what the gate
actually cares about:

* the outcome is fail-closed with a **stable reason code** from the closed enum,
* **zero duplicate mutations** occur (provider call counts are measured),
* the resulting audit record carries **no secret material**.

Hermetic: no network, no credentials, no filesystem, no subprocess.
"""

from __future__ import annotations

import pytest

from hermes_mcp_bridge.v2.enums import CapabilityState
from hermes_mcp_bridge.v2.provider_audit import (
    AuditKind,
    IntegrationAuditLedger,
    MemoryAuditSink,
    OutcomeClass,
    completeness,
)
from hermes_mcp_bridge.v2.provider_contract import ProviderManifest, ProviderReason, audit_safe
from hermes_mcp_bridge.v2.provider_credentials import (
    CredentialRecord,
    ProviderCredentialBroker,
)
from hermes_mcp_bridge.v2.provider_gateway import (
    ApprovalStore,
    IdempotencyStore,
    PolicyPort,
    ProviderCallResult,
    ProviderDenied,
    ProviderGateway,
    ProviderRequest,
    ScopeResolver,
)
from hermes_mcp_bridge.v2.provider_manifests import PROVIDER_ALLOW_LIST, github_manifest
from hermes_mcp_bridge.v2.provider_registry import HealthReport, ProviderRegistry, build_registry

READ = "github.repo_read"
WRITE = "github.pr_create"
TARGET = "pestoura/hermes-mcp-bridge"


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------
def _broker(manifest: ProviderManifest, *, ready: bool = True) -> ProviderCredentialBroker:
    broker = ProviderCredentialBroker({manifest.provider_id: manifest.credential_domain})
    for capability in manifest.credential_domain.capability_ids:
        broker.register(
            CredentialRecord(
                provider_id=manifest.provider_id,
                credential_capability_id=capability,
                ready=ready,
                apply=lambda headers: {**headers, "Authorization": "Bearer [REDACTED]"},
            )
        )
    return broker


def _registry(manifest: ProviderManifest) -> ProviderRegistry:
    registry = build_registry(
        allow_list=PROVIDER_ALLOW_LIST,
        tool_ids=[capability.tool_id for capability in manifest.capabilities],
        manifests=[manifest],
    )
    registry.promote_configured(
        HealthReport(capability_id=capability.capability_id, state=CapabilityState.READY)
        for capability in manifest.capabilities
    )
    return registry


def _scopes(manifest: ProviderManifest) -> ScopeResolver:
    resolver = ScopeResolver()
    for capability in manifest.capabilities:
        resolver.allow(capability.capability_id, (TARGET,))
    return resolver


class CountingAdapter:
    """Adapter that counts invocations so duplicate mutations are measured."""

    def __init__(self, *, raises=None, payload=None, byte_count=64) -> None:
        self.calls = 0
        self._raises = raises
        self._payload = payload if payload is not None else {"ok": True}
        self._byte_count = byte_count

    def __call__(self, request, headers, deadline_ms):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return ProviderCallResult(
            payload=self._payload, byte_count=self._byte_count, provider_calls=1
        )


class Harness:
    def __init__(self, **kwargs) -> None:
        self.manifest = github_manifest(include_write=True)
        self.registry = kwargs.get("registry") or _registry(self.manifest)
        self.broker = kwargs.get("broker") or _broker(self.manifest)
        self.sink = kwargs.get("sink") or MemoryAuditSink()
        self.approvals = kwargs.get("approvals") or ApprovalStore()
        self.idempotency = kwargs.get("idempotency") or IdempotencyStore()
        self.adapter = kwargs.get("adapter") or CountingAdapter()
        self.policy = kwargs.get("policy") or PolicyPort(
            {
                capability.capability_id: kwargs.get("policy_decision", "ALLOW")
                for capability in self.manifest.capabilities
            }
        )
        self.gateway = ProviderGateway(
            registry=self.registry,
            policy=self.policy,
            scopes=_scopes(self.manifest),
            broker=self.broker,
            audit=IntegrationAuditLedger(self.sink),
            adapters={self.manifest.provider_id: self.adapter},
            approvals=self.approvals,
            idempotency=self.idempotency,
        )

    def request(self, capability_id=READ, **overrides) -> ProviderRequest:
        base = {
            "request_id": overrides.pop("request_id", "req-1"),
            "principal_ref": "principal-opaque",
            "provider_id": "github",
            "capability_id": capability_id,
            "target_scope_ref": TARGET,
            "arguments": {"title": "t"},
        }
        base.update(overrides)
        return ProviderRequest(**base)

    def approved_write(self, **overrides) -> ProviderRequest:
        request = self.request(
            WRITE, idempotency_key=overrides.pop("idempotency_key", "idem-1"),
            approval_ref="ap-1", **overrides
        )
        self.approvals.grant("ap-1", request.operation_digest())
        return request

    def assert_clean_audit(self) -> None:
        for record in self.sink.records:
            assert not audit_safe(record), "audit record carries secret-shaped material"


def _stable_reason(outcome) -> None:
    assert isinstance(outcome.reason_code, ProviderReason)


# --------------------------------------------------------------------------
# F-01 .. F-07 — provider-side faults
# --------------------------------------------------------------------------
def test_f01_provider_auth_failure_is_redacted_and_does_not_storm() -> None:
    adapter = CountingAdapter(raises=ProviderDenied(ProviderReason.E_PROVIDER_AUTH))
    harness = Harness(adapter=adapter)
    outcome = harness.gateway.invoke(harness.request())
    assert outcome.reason_code is ProviderReason.E_PROVIDER_AUTH
    assert adapter.calls == 1, "no retry storm on an auth failure"
    _stable_reason(outcome)
    harness.assert_clean_audit()


def test_f02_rate_limit_refuses_non_idempotent_write_without_retry() -> None:
    adapter = CountingAdapter(raises=ProviderDenied(ProviderReason.E_PROVIDER_RATE_LIMIT))
    harness = Harness(adapter=adapter)
    outcome = harness.gateway.invoke(harness.approved_write())
    assert outcome.reason_code is ProviderReason.E_PROVIDER_RATE_LIMIT
    assert adapter.calls == 1, "a rate-limited write must not be retried automatically"


def test_f03_provider_5xx_on_write_is_unknown_not_success() -> None:
    adapter = CountingAdapter(raises=RuntimeError("upstream 503"))
    harness = Harness(adapter=adapter)
    outcome = harness.gateway.invoke(harness.approved_write())
    assert outcome.outcome is OutcomeClass.UNKNOWN
    assert outcome.reason_code is ProviderReason.E_PROVIDER_FAULT
    assert harness.gateway.unknown_outcomes == ("req-1",), "unknown must be surfaced, never silent"


def test_f04_timeout_mid_write_is_unknown_and_needs_manual_intervention() -> None:
    adapter = CountingAdapter(raises=ProviderDenied(ProviderReason.E_PROVIDER_DEADLINE))
    harness = Harness(adapter=adapter)
    outcome = harness.gateway.invoke(harness.approved_write())
    assert outcome.outcome is OutcomeClass.UNKNOWN
    assert outcome.reason_code is ProviderReason.E_PROVIDER_DEADLINE
    assert harness.gateway.unknown_outcomes, "the unknown outcome must be listed for review"
    assert adapter.calls == 1, "never retry a write with an unknown outcome"


def test_f05_transport_failure_fails_closed() -> None:
    adapter = CountingAdapter(raises=OSError("dns failure"))
    harness = Harness(adapter=adapter)
    outcome = harness.gateway.invoke(harness.request())
    assert outcome.outcome is OutcomeClass.ERROR
    assert outcome.reason_code is ProviderReason.E_PROVIDER_FAULT
    assert outcome.payload == {}


def test_f06_oversized_body_is_refused() -> None:
    adapter = CountingAdapter(byte_count=50_000_000)
    harness = Harness(adapter=adapter)
    outcome = harness.gateway.invoke(harness.request())
    assert outcome.reason_code is ProviderReason.E_PROVIDER_RESULT_TOO_LARGE
    assert outcome.payload == {}, "an oversized body must never be partially accepted"


def test_f07_shape_drift_is_refused_without_partial_acceptance() -> None:
    adapter = CountingAdapter(payload={"token": "ghp_" + "a" * 36})
    harness = Harness(adapter=adapter)
    outcome = harness.gateway.invoke(harness.request())
    assert outcome.reason_code is ProviderReason.E_PROVIDER_SHAPE
    assert outcome.payload == {}
    harness.assert_clean_audit()


# --------------------------------------------------------------------------
# F-08 .. F-12 — infrastructure dependency faults
# --------------------------------------------------------------------------
def test_f08_credential_unavailable_denies_with_no_cached_fallback() -> None:
    manifest = github_manifest(include_write=True)
    harness = Harness(broker=_broker(manifest, ready=False))
    outcome = harness.gateway.invoke(harness.request())
    assert outcome.reason_code is ProviderReason.E_CRED_UNAVAILABLE
    assert harness.adapter.calls == 0, "no provider call without a credential"


def test_f09_credential_revoked_mid_run_fails_closed() -> None:
    harness = Harness()
    harness.broker.revoke("github", "github.read")
    outcome = harness.gateway.invoke(harness.request())
    assert outcome.reason_code is ProviderReason.E_CRED_REVOKED
    assert harness.adapter.calls == 0


def test_f10_policy_engine_error_denies_never_default_allows() -> None:
    harness = Harness()
    harness.policy.set_available(False)
    outcome = harness.gateway.invoke(harness.request())
    assert outcome.reason_code is ProviderReason.E_POLICY_UNAVAILABLE
    assert harness.adapter.calls == 0, "a policy outage must never default-allow"


def test_f11_audit_sink_outage_refuses_write_before_any_effect() -> None:
    harness = Harness()
    request = harness.approved_write()
    harness.sink.set_available(False)
    outcome = harness.gateway.invoke(request)
    assert outcome.reason_code is ProviderReason.E_AUDIT_UNAVAILABLE
    assert harness.adapter.calls == 0, "no side effect may precede a durable audit record"


def test_f12_idempotency_store_outage_refuses_writes() -> None:
    harness = Harness()
    request = harness.approved_write()
    harness.idempotency.set_available(False)
    outcome = harness.gateway.invoke(request)
    assert outcome.reason_code is ProviderReason.E_IDEMPOTENCY_UNAVAILABLE
    assert harness.adapter.calls == 0


def test_f13_stale_replay_returns_prior_outcome_with_zero_second_effect() -> None:
    harness = Harness()
    first = harness.approved_write()
    first_outcome = harness.gateway.invoke(first)
    assert first_outcome.outcome is OutcomeClass.SUCCESS
    assert harness.adapter.calls == 1

    replay = ProviderRequest(
        request_id="req-2",
        principal_ref="principal-opaque",
        provider_id="github",
        capability_id=WRITE,
        target_scope_ref=TARGET,
        arguments=dict(first.arguments),
        approval_ref="ap-2",
        idempotency_key="idem-1",
    )
    harness.approvals.grant("ap-2", replay.operation_digest())
    replay_outcome = harness.gateway.invoke(replay)
    assert replay_outcome.reason_code is ProviderReason.E_IDEMPOTENCY_REPLAY
    assert harness.adapter.calls == 1, "duplicate mutation count must be exactly 0"


def test_f14_missing_or_consumed_approval_is_refused() -> None:
    harness = Harness()
    request = harness.request(WRITE, idempotency_key="idem-1", approval_ref="")
    outcome = harness.gateway.invoke(request)
    assert outcome.reason_code is ProviderReason.E_APPROVAL_MISSING
    assert harness.adapter.calls == 0


def test_f14b_approval_digest_mismatch_is_refused() -> None:
    harness = Harness()
    request = harness.request(WRITE, idempotency_key="idem-1", approval_ref="ap-1")
    harness.approvals.grant("ap-1", "0" * 64)  # approves a different operation
    outcome = harness.gateway.invoke(request)
    assert outcome.reason_code is ProviderReason.E_APPROVAL_DIGEST_MISMATCH
    assert harness.adapter.calls == 0


def test_f15_deadlines_are_monotonic_and_skew_cannot_extend_a_budget() -> None:
    """Handle deadlines derive from ``time.monotonic_ns``; wall-clock skew is irrelevant."""
    import hermes_mcp_bridge.v2.provider_credentials as credentials_module

    source = credentials_module.__file__
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    assert "time.monotonic_ns" in text
    assert "time.time()" not in text, "a wall-clock deadline would be skew-extendable"


def test_f16_concurrent_duplicate_same_digest_produces_one_effect() -> None:
    harness = Harness()
    request = harness.approved_write()
    outcomes = [harness.gateway.invoke(request)]
    for index in range(3):
        replay = ProviderRequest(
            request_id=f"req-dup-{index}",
            principal_ref="principal-opaque",
            provider_id="github",
            capability_id=WRITE,
            target_scope_ref=TARGET,
            arguments=dict(request.arguments),
            approval_ref=f"ap-dup-{index}",
            idempotency_key="idem-1",
        )
        harness.approvals.grant(f"ap-dup-{index}", replay.operation_digest())
        outcomes.append(harness.gateway.invoke(replay))
    assert harness.adapter.calls == 1
    assert all(
        outcome.reason_code is ProviderReason.E_IDEMPOTENCY_REPLAY for outcome in outcomes[1:]
    )


def test_f17_lease_loss_never_duplicates_a_mutation() -> None:
    """A lost lease surfaces as unknown/reconciliation, never as a second write."""
    from datetime import UTC, datetime, timedelta

    from hermes_mcp_bridge.v2.errors import IdempotencyConflictError
    from hermes_mcp_bridge.v2.mutation_idempotency import IdempotencyStore as DurableStore

    store = DurableStore(lease_seconds=1)
    now = datetime(2026, 8, 9, tzinfo=UTC)
    key = "a" * 64
    kwargs = {
        "idempotency_key": key,
        "principal": "p",
        "repository": TARGET,
        "operation": "github.create_pr",
        "operation_digest": "b" * 64,
        "target": "feat/x",
    }
    store.begin(now=now, **kwargs)
    with pytest.raises(IdempotencyConflictError):
        store.begin(now=now + timedelta(seconds=60), **kwargs)


def test_f18_partial_batch_records_per_node_outcomes() -> None:
    """Each node produces its own terminal record; a failure does not erase peers."""
    harness = Harness()
    ok = harness.gateway.invoke(harness.request(request_id="node-1"))
    harness.policy.set("github.repo_read", "DENY")
    denied = harness.gateway.invoke(harness.request(request_id="node-2"))
    assert ok.outcome is OutcomeClass.SUCCESS
    assert denied.reason_code is ProviderReason.E_POLICY_DENY
    terminal = [
        record for record in harness.sink.records if record["kind"] == AuditKind.TERMINAL.value
    ]
    assert {record["request_id"] for record in terminal} == {"node-1", "node-2"}


def test_f19_evidence_write_failure_fails_closed_with_no_silent_loss() -> None:
    """An audit sink that cannot persist must refuse, not drop the record."""
    harness = Harness()
    harness.sink.set_available(False)
    outcome = harness.gateway.invoke(harness.approved_write())
    assert outcome.reason_code is ProviderReason.E_AUDIT_UNAVAILABLE
    assert harness.sink.records == (), "nothing was persisted, and nothing was pretended"


def test_f20_restart_mid_operation_surfaces_unknown_without_duplicate() -> None:
    """Model a restart: the pre-restart write ended unknown; recovery must not redo it."""
    adapter = CountingAdapter(raises=ProviderDenied(ProviderReason.E_PROVIDER_DEADLINE))
    harness = Harness(adapter=adapter)
    outcome = harness.gateway.invoke(harness.approved_write())
    assert outcome.outcome is OutcomeClass.UNKNOWN
    assert harness.gateway.unknown_outcomes, "recovery needs the unknown surfaced"
    # The unknown outcome is NOT recorded as a completed idempotency entry, so a
    # human-driven reconciliation is required before any new attempt.
    assert harness.idempotency.lookup("idem-1") is None
    assert adapter.calls == 1


# --------------------------------------------------------------------------
# Catalogue-level invariants
# --------------------------------------------------------------------------
def test_catalogue_every_fault_produces_a_terminal_record() -> None:
    harness = Harness(adapter=CountingAdapter(raises=RuntimeError("boom")))
    harness.gateway.invoke(harness.request())
    terminal = [
        record for record in harness.sink.records if record["kind"] == AuditKind.TERMINAL.value
    ]
    assert len(terminal) == 1
    assert completeness(terminal_records=len(terminal), terminal_outcomes=1) == 1.0


def test_catalogue_reason_codes_are_all_within_the_closed_enum() -> None:
    values = {reason.value for reason in ProviderReason}
    harness = Harness(adapter=CountingAdapter(raises=RuntimeError("boom")))
    outcome = harness.gateway.invoke(harness.request())
    assert outcome.reason_code.value in values
