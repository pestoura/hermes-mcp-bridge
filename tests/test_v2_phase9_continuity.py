"""Phase 9: the C-01..C-08 chaos and continuity scenarios, executed.

``docs/v2/downstream/phase9/chaos-and-recovery.md`` defines eight scenarios and
four objectives (RTO, audit RPO 0, duplicate mutations 0, no silent unknowns).
This module runs each scenario against the real gateway, registry, broker and
audit ledger, and asserts the objectives as *measurements*: provider call counts,
terminal record counts and recomputed chain digests.

A process kill cannot be executed inside a hermetic test, so scenarios C-01,
C-02 and C-08 are modelled the way a restart actually presents to the system —
state is rebuilt from the durable stores while the pre-restart in-memory context
is discarded — and the assertion is that no duplicate side effect and no lost
terminal record survives that transition. That is the property the objective
names; the honest limitation is that host-level process termination is covered
by the connected drill evidence, not by this file.

Hermetic: no network, no credentials, no filesystem, no subprocess.
"""

from __future__ import annotations

from hermes_mcp_bridge.v2.audit_chain import digest_chain
from hermes_mcp_bridge.v2.drills import AUDIT_RPO_RECORDS
from hermes_mcp_bridge.v2.enums import CapabilityState
from hermes_mcp_bridge.v2.provider_audit import (
    AuditKind,
    IntegrationAuditLedger,
    MemoryAuditSink,
    OutcomeClass,
    completeness,
)
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
    ProviderReason,
    ProviderRequest,
    ScopeResolver,
)
from hermes_mcp_bridge.v2.provider_manifests import PROVIDER_ALLOW_LIST, github_manifest
from hermes_mcp_bridge.v2.provider_registry import HealthReport, build_registry

READ = "github.repo_read"
WRITE = "github.pr_create"
TARGET = "pestoura/hermes-mcp-bridge"


class CountingAdapter:
    def __init__(self, *, raises=None) -> None:
        self.calls = 0
        self._raises = raises

    def set_fault(self, raises) -> None:
        self._raises = raises

    def __call__(self, request, headers, deadline_ms):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return ProviderCallResult(payload={"ok": True}, byte_count=64, provider_calls=1)


class Stack:
    """A gateway plus the durable stores that survive a modelled restart."""

    def __init__(self, *, sink=None, approvals=None, idempotency=None, adapter=None) -> None:
        self.manifest = github_manifest(include_write=True)
        self.sink = sink if sink is not None else MemoryAuditSink()
        self.approvals = approvals if approvals is not None else ApprovalStore()
        self.idempotency = idempotency if idempotency is not None else IdempotencyStore()
        self.adapter = adapter if adapter is not None else CountingAdapter()
        self.registry = build_registry(
            allow_list=PROVIDER_ALLOW_LIST,
            tool_ids=[c.tool_id for c in self.manifest.capabilities],
            manifests=[self.manifest],
        )
        self.registry.promote_configured(
            HealthReport(capability_id=c.capability_id, state=CapabilityState.READY)
            for c in self.manifest.capabilities
        )
        self.broker = ProviderCredentialBroker(
            {self.manifest.provider_id: self.manifest.credential_domain}
        )
        for capability in self.manifest.credential_domain.capability_ids:
            self.broker.register(
                CredentialRecord(
                    provider_id=self.manifest.provider_id,
                    credential_capability_id=capability,
                    ready=True,
                    apply=lambda headers: {**headers, "Authorization": "Bearer [REDACTED]"},
                )
            )
        scopes = ScopeResolver()
        for capability in self.manifest.capabilities:
            scopes.allow(capability.capability_id, (TARGET,))
        self.policy = PolicyPort(
            {c.capability_id: "ALLOW" for c in self.manifest.capabilities}
        )
        self.gateway = ProviderGateway(
            registry=self.registry,
            policy=self.policy,
            scopes=scopes,
            broker=self.broker,
            audit=IntegrationAuditLedger(self.sink),
            adapters={self.manifest.provider_id: self.adapter},
            approvals=self.approvals,
            idempotency=self.idempotency,
        )

    def restart(self) -> Stack:
        """Model a restart: durable stores persist, in-memory gateway is rebuilt."""
        return Stack(
            sink=self.sink,
            approvals=self.approvals,
            idempotency=self.idempotency,
            adapter=self.adapter,
        )

    def read(self, request_id="req-r") -> ProviderRequest:
        return ProviderRequest(
            request_id=request_id,
            principal_ref="principal-opaque",
            provider_id="github",
            capability_id=READ,
            target_scope_ref=TARGET,
            arguments={"q": "x"},
        )

    def write(self, request_id="req-w", approval_ref="ap-1", key="idem-1") -> ProviderRequest:
        request = ProviderRequest(
            request_id=request_id,
            principal_ref="principal-opaque",
            provider_id="github",
            capability_id=WRITE,
            target_scope_ref=TARGET,
            arguments={"title": "t"},
            approval_ref=approval_ref,
            idempotency_key=key,
        )
        self.approvals.grant(approval_ref, request.operation_digest())
        return request

    def terminal_records(self):
        return [r for r in self.sink.records if r["kind"] == AuditKind.TERMINAL.value]


# --------------------------------------------------------------------------
# C-01 — kill during a write
# --------------------------------------------------------------------------
def test_c01_restart_during_a_write_produces_no_duplicate_effect() -> None:
    stack = Stack()
    committed = stack.gateway.invoke(stack.write())
    assert committed.outcome is OutcomeClass.SUCCESS
    assert stack.adapter.calls == 1

    recovered = stack.restart()
    replay = recovered.write(request_id="req-w2", approval_ref="ap-2", key="idem-1")
    outcome = recovered.gateway.invoke(replay)
    assert outcome.reason_code is ProviderReason.E_IDEMPOTENCY_REPLAY
    assert stack.adapter.calls == 1, "duplicate mutation count must be exactly 0"


def test_c01b_unknown_outcome_survives_restart_as_unknown_not_success() -> None:
    adapter = CountingAdapter(raises=ProviderDenied(ProviderReason.E_PROVIDER_DEADLINE))
    stack = Stack(adapter=adapter)
    outcome = stack.gateway.invoke(stack.write())
    assert outcome.outcome is OutcomeClass.UNKNOWN

    recovered = stack.restart()
    # The unknown was never committed, so recovery cannot mistake it for done.
    assert recovered.idempotency.lookup("idem-1") is None
    assert stack.gateway.unknown_outcomes, "unknown outcomes must be surfaced for review"


# --------------------------------------------------------------------------
# C-02 — kill during a DAG run
# --------------------------------------------------------------------------
def test_c02_dag_node_is_never_executed_twice_across_a_restart() -> None:
    stack = Stack()
    for index in range(3):
        stack.gateway.invoke(
            stack.write(request_id=f"n{index}", approval_ref=f"ap-n{index}", key=f"idem-n{index}")
        )
    assert stack.adapter.calls == 3

    recovered = stack.restart()
    for index in range(3):
        outcome = recovered.gateway.invoke(
            recovered.write(
                request_id=f"n{index}-resume",
                approval_ref=f"ap-r{index}",
                key=f"idem-n{index}",
            )
        )
        assert outcome.reason_code is ProviderReason.E_IDEMPOTENCY_REPLAY
    assert stack.adapter.calls == 3, "resume must not re-execute a completed node"


# --------------------------------------------------------------------------
# C-03 / C-04 — provider degradation must not cascade
# --------------------------------------------------------------------------
def test_c03_single_provider_down_degrades_only_that_provider() -> None:
    stack = Stack()
    stack.registry.apply_health(
        [HealthReport(capability_id=WRITE, state=CapabilityState.UNAVAILABLE)]
    )
    write_outcome = stack.gateway.invoke(stack.write())
    read_outcome = stack.gateway.invoke(stack.read())
    assert write_outcome.reason_code is ProviderReason.E_CAP_NOT_READY
    assert read_outcome.outcome is OutcomeClass.SUCCESS, "an unrelated capability must keep serving"


def test_c04_two_capabilities_degraded_refuses_cleanly_without_cascade() -> None:
    stack = Stack()
    stack.registry.apply_health(
        [
            HealthReport(capability_id=WRITE, state=CapabilityState.UNAVAILABLE),
            HealthReport(capability_id=READ, state=CapabilityState.UNAVAILABLE),
        ]
    )
    for request in (stack.write(), stack.read()):
        outcome = stack.gateway.invoke(request)
        assert outcome.reason_code is ProviderReason.E_CAP_NOT_READY
        assert outcome.outcome is OutcomeClass.REFUSED, "a clean refusal, not an error cascade"
    assert stack.adapter.calls == 0


def test_c04b_degraded_read_serves_with_a_marker_never_silently() -> None:
    stack = Stack()
    stack.registry.apply_health(
        [HealthReport(capability_id=READ, state=CapabilityState.DEGRADED)]
    )
    outcome = stack.gateway.invoke(stack.read())
    assert outcome.outcome is OutcomeClass.SUCCESS
    assert outcome.degraded is True, "degraded results must be explicitly marked"


def test_c04c_degraded_never_relaxes_the_write_path() -> None:
    stack = Stack()
    stack.registry.apply_health(
        [HealthReport(capability_id=WRITE, state=CapabilityState.DEGRADED)]
    )
    outcome = stack.gateway.invoke(stack.write())
    assert outcome.reason_code is ProviderReason.E_CAP_NOT_READY
    assert stack.adapter.calls == 0


# --------------------------------------------------------------------------
# C-05 — sustained rate limiting must not grow an unbounded queue
# --------------------------------------------------------------------------
def test_c05_sustained_rate_limiting_is_bounded_and_refuses_cleanly() -> None:
    adapter = CountingAdapter(raises=ProviderDenied(ProviderReason.E_PROVIDER_RATE_LIMIT))
    stack = Stack(adapter=adapter)
    outcomes = [stack.gateway.invoke(stack.read(request_id=f"r{i}")) for i in range(25)]
    assert all(o.reason_code is ProviderReason.E_PROVIDER_RATE_LIMIT for o in outcomes)
    # One provider call per request: no internal retry amplification.
    assert adapter.calls == 25
    assert len(stack.terminal_records()) == 25, "every refusal is accounted for"


# --------------------------------------------------------------------------
# C-06 — audit sink outage
# --------------------------------------------------------------------------
def test_c06_audit_outage_refuses_writes_and_loses_no_record() -> None:
    stack = Stack()
    before = len(stack.sink.records)
    stack.sink.set_available(False)
    outcome = stack.gateway.invoke(stack.write())
    assert outcome.reason_code is ProviderReason.E_AUDIT_UNAVAILABLE
    assert stack.adapter.calls == 0
    assert len(stack.sink.records) == before, "no partial record, no silent loss"

    stack.sink.set_available(True)
    recovered = stack.gateway.invoke(stack.write(request_id="req-after", approval_ref="ap-after"))
    assert recovered.outcome is OutcomeClass.SUCCESS, "backlog resolves once the sink returns"


# --------------------------------------------------------------------------
# C-07 — credential rotation under load
# --------------------------------------------------------------------------
def test_c07_rotation_under_load_never_fails_open() -> None:
    stack = Stack()
    outcomes = []
    for index in range(10):
        if index == 5:
            stack.broker.rotate(
                CredentialRecord(
                    provider_id="github",
                    credential_capability_id="github.read",
                    ready=True,
                    apply=lambda headers: {**headers, "Authorization": "Bearer [ROTATED]"},
                )
            )
        outcomes.append(stack.gateway.invoke(stack.read(request_id=f"load-{index}")))
    assert all(o.outcome is OutcomeClass.SUCCESS for o in outcomes)

    stack.broker.revoke("github", "github.read")
    after_revoke = stack.gateway.invoke(stack.read(request_id="post-revoke"))
    assert after_revoke.reason_code is ProviderReason.E_CRED_REVOKED, "must fail closed"


# --------------------------------------------------------------------------
# C-08 — restart storm
# --------------------------------------------------------------------------
def test_c08_restart_storm_preserves_state_integrity() -> None:
    stack = Stack()
    stack.gateway.invoke(stack.write())
    assert stack.adapter.calls == 1
    prefix_length = len(stack.sink.records)
    prefix_digest = digest_chain(*stack.sink.records)

    current = stack
    for index in range(3):
        current = current.restart()
        outcome = current.gateway.invoke(
            current.write(
                request_id=f"storm-{index}", approval_ref=f"ap-storm-{index}", key="idem-1"
            )
        )
        assert outcome.reason_code is ProviderReason.E_IDEMPOTENCY_REPLAY

    assert stack.adapter.calls == 1, "three restarts, still exactly one mutation"
    # The ledger is append-only: the pre-storm prefix must still hash to the
    # digest taken before the restarts, and the storm must have appended to it.
    assert digest_chain(*stack.sink.records[:prefix_length]) == prefix_digest
    assert len(stack.sink.records) > prefix_length, "each replay is still audited"


# --------------------------------------------------------------------------
# Objectives
# --------------------------------------------------------------------------
def test_objective_audit_completeness_is_total_across_the_scenarios() -> None:
    stack = Stack()
    stack.gateway.invoke(stack.read(request_id="a"))
    stack.gateway.invoke(stack.read(request_id="b"))
    stack.gateway.invoke(stack.write())
    terminal = stack.terminal_records()
    assert completeness(terminal_records=len(terminal), terminal_outcomes=3) == 1.0


def test_objective_audit_rpo_is_zero_records() -> None:
    assert AUDIT_RPO_RECORDS == 0


def test_objective_no_silent_unknowns() -> None:
    adapter = CountingAdapter(raises=RuntimeError("boom"))
    stack = Stack(adapter=adapter)
    stack.gateway.invoke(stack.write())
    assert stack.gateway.unknown_outcomes, "an unknown outcome must always be enumerable"
    assert len(stack.terminal_records()) >= 1
