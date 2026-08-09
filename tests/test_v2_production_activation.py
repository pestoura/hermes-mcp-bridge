"""V2 production activation acceptance suite — one real test per PA-01..PA-24.

Executed by ``scripts/validate_v2_production_activation_gate.py``.

The property under test is *activation*, not code presence. Release 2.0.0 shipped
every V2 lane behind a hardcoded ``False``; this suite fails if a required
capability is disabled or structurally unreachable through the intended internal
composition root. Every reachability claim is proven by constructing the real
engine through :class:`V2Composition` and running it — no mocks of the module
under test, no document inspection standing in for behaviour.

Hermetic: no network, no provider, no subprocess. Every mutation target is a
disposable in-memory or tmp_path artefact.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from dag_fixtures import AllowGovernance, budget, catalog, plan, read_node

from hermes_mcp_bridge import contracts
from hermes_mcp_bridge.v2.batch_contract import (
    BatchFailurePolicy,
    BatchRequest,
    BatchStatus,
    BatchStep,
)
from hermes_mcp_bridge.v2.composition import CapabilityDisabled, V2Composition
from hermes_mcp_bridge.v2.dag_contract import Binding, NodeKind, PlanStatus
from hermes_mcp_bridge.v2.dag_contract import Node as DagNode
from hermes_mcp_bridge.v2.dag_store import SqliteCheckpointStore
from hermes_mcp_bridge.v2.dag_validation import validate_plan
from hermes_mcp_bridge.v2.enums import CapabilityState, ExecutionMode, PolicyDecision
from hermes_mcp_bridge.v2.hybrid_execution import HybridCoordinator
from hermes_mcp_bridge.v2.production_profile import (
    DISABLED_PROFILE,
    ENV_AGENTIC_TOKEN_BUDGET,
    ENV_ENABLED,
    ENV_FOR_CAPABILITY,
    MAX_AGENTIC_TOKEN_BUDGET,
    REQUIRED_PRODUCTION_CAPABILITIES,
    ProfileConfigError,
    V2Capability,
    V2ProductionProfile,
)
from hermes_mcp_bridge.v2.provider_audit import (
    IntegrationAuditLedger,
    MemoryAuditSink,
    OutcomeClass,
)
from hermes_mcp_bridge.v2.provider_credentials import CredentialRecord, ProviderCredentialBroker
from hermes_mcp_bridge.v2.provider_gateway import (
    PolicyPort,
    ProviderCallResult,
    ProviderRequest,
    ScopeResolver,
)
from hermes_mcp_bridge.v2.provider_manifests import PROVIDER_ALLOW_LIST, github_manifest
from hermes_mcp_bridge.v2.provider_registry import HealthReport, build_registry
from hermes_mcp_bridge.v2.resolver_contract import (
    MODE_PREFERENCE,
    IntentOperation,
    ResolverBudget,
    ResolverIntent,
    ResolverReason,
)
from hermes_mcp_bridge.v2.runbook_contract import RunbookState
from hermes_mcp_bridge.v2.runbook_digest import runbook_digest
from hermes_mcp_bridge.v2.runbook_engine import InvocationRequest
from hermes_mcp_bridge.v2.runbook_loader import load_manifest
from hermes_mcp_bridge.v2.runbook_registry import RunbookRegistry

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src" / "hermes_mcp_bridge"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "v2_phase6"

ACTIVATION_MODULES = (
    "v2/production_profile.py",
    "v2/composition.py",
)

READ_TOOL = "github.get_repo"
READ_CAPABILITY = "github.repo_read"
WRITE_CAPABILITY = "github.pr_create"
TARGET = "owner/disposable"


def production() -> V2Composition:
    return V2Composition(profile=V2ProductionProfile.production())


# --------------------------------------------------------------------------
# PA-01..PA-06 — the profile itself
# --------------------------------------------------------------------------


def test_pa_01_default_profile_is_fully_disabled() -> None:
    """The 2.0.0 posture is the default: nothing is activated implicitly."""
    profile = V2ProductionProfile()
    assert profile.enabled is False
    assert profile.fully_active is False
    assert profile.active_capabilities == ()
    assert set(profile.disabled_capabilities) == set(V2Capability)
    assert profile == DISABLED_PROFILE


def test_pa_02_master_switch_dominates_every_capability() -> None:
    """Per-capability flags cannot activate anything while the master is off."""
    profile = V2ProductionProfile(
        enabled=False,
        direct=True,
        batch=True,
        dag=True,
        runbook=True,
        integrations=True,
        hybrid=True,
    )
    for capability in V2Capability:
        assert profile.is_enabled(capability) is False
    assert profile.fully_active is False


def test_pa_03_production_profile_activates_every_required_capability() -> None:
    profile = V2ProductionProfile.production()
    for capability in REQUIRED_PRODUCTION_CAPABILITIES:
        assert profile.is_enabled(capability) is True, capability
    assert profile.fully_active is True


def test_pa_04_env_parsing_is_fail_closed() -> None:
    """Absent, malformed and unknown settings all refuse; none default to on."""
    assert V2ProductionProfile.from_env({}).fully_active is False

    with pytest.raises(ProfileConfigError):
        V2ProductionProfile.from_env({ENV_ENABLED: "maybe"})

    with pytest.raises(ProfileConfigError):
        V2ProductionProfile.from_env({"BRIDGE_V2_BACTH": "1"})

    with pytest.raises(ProfileConfigError):
        V2ProductionProfile.from_env(
            {ENV_ENABLED: "1", ENV_AGENTIC_TOKEN_BUDGET: "-1"},
        )

    with pytest.raises(ProfileConfigError):
        V2ProductionProfile.from_env(
            {
                ENV_ENABLED: "1",
                ENV_AGENTIC_TOKEN_BUDGET: str(MAX_AGENTIC_TOKEN_BUDGET + 1),
            },
        )


def test_pa_05_env_round_trip_produces_the_production_profile() -> None:
    env = {ENV_ENABLED: "1"}
    for name in ENV_FOR_CAPABILITY.values():
        env[name] = "true"
    profile = V2ProductionProfile.from_env(env)
    assert profile.fully_active is True
    assert profile.agentic_token_budget == 0
    assert profile.allows_agentic is False
    assert profile.digest() == V2ProductionProfile.production().digest()


def test_pa_06_rollback_switch_returns_the_2_0_0_posture() -> None:
    """One call, or one env var, restores the released disabled behaviour."""
    active = V2ProductionProfile.production()
    assert active.disabled() == DISABLED_PROFILE

    env = {ENV_ENABLED: "0"}
    for name in ENV_FOR_CAPABILITY.values():
        env[name] = "1"
    assert V2ProductionProfile.from_env(env).fully_active is False


# --------------------------------------------------------------------------
# PA-07..PA-09 — public compatibility is untouched by activation
# --------------------------------------------------------------------------


def test_pa_07_public_contract_is_unchanged_by_activation() -> None:
    production()  # activation is constructed, then the contract is re-read
    assert contracts.CURRENT_CONTRACT_VERSION == "1.0.0"
    assert contracts.SCHEMA_VERSION == "0.6.1"
    assert contracts.expected_tool_count() == 27


def test_pa_08_no_v1_module_imports_the_v2_runtime() -> None:
    """Activation must not have wired V2 into V1; the boundary stays one-way."""
    for path in sorted(SRC.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "from .v2" not in text, path.name
        assert "from hermes_mcp_bridge.v2" not in text, path.name


def test_pa_09_activation_modules_add_no_generic_shell_or_http_surface() -> None:
    forbidden = (
        "subprocess",
        "socket",
        "requests",
        "urllib",
        "http.client",
        "ev" + "al(",
        "ex" + "ec(",
        "__imp" + "ort__(",
    )
    for name in ACTIVATION_MODULES:
        text = (SRC / name).read_text(encoding="utf-8")
        for marker in forbidden:
            assert marker not in text, f"{name}:{marker}"


# --------------------------------------------------------------------------
# PA-10..PA-11 — DIRECT
# --------------------------------------------------------------------------


def test_pa_10_direct_is_active_under_the_production_profile() -> None:
    assert production().direct_enabled() is True


def test_pa_11_direct_is_off_under_the_disabled_profile() -> None:
    assert V2Composition().direct_enabled() is False


# --------------------------------------------------------------------------
# PA-12..PA-13 — BATCH reachable through the composition root
# --------------------------------------------------------------------------


def test_pa_12_batch_executes_through_the_composition_root() -> None:
    calls: list[str] = []

    async def executor(item: BatchStep) -> dict[str, Any]:
        calls.append(item.step_id)
        return {"step": item.step_id}

    scheduler = production().batch_scheduler(executor)
    request = BatchRequest(
        batch_id="pa-batch-1",
        steps=(
            BatchStep(step_id="s1", tool=READ_TOOL, step_timeout_s=30),
            BatchStep(step_id="s2", tool=READ_TOOL, step_timeout_s=30),
        ),
        failure_policy=BatchFailurePolicy.CONTINUE_ON_ERROR,
        max_parallelism=2,
        batch_timeout_s=60,
    )
    result = asyncio.run(scheduler.run(request))
    assert result.aggregate_status is BatchStatus.SUCCESS
    assert sorted(calls) == ["s1", "s2"]


def test_pa_13_batch_is_refused_when_the_capability_is_disabled() -> None:
    async def executor(item: BatchStep) -> dict[str, Any]:  # pragma: no cover - never called
        raise AssertionError("must not execute")

    composition = V2Composition(
        profile=V2ProductionProfile.production().without(V2Capability.BATCH)
    )
    with pytest.raises(CapabilityDisabled) as excinfo:
        composition.batch_scheduler(executor)
    assert excinfo.value.capability is V2Capability.BATCH


# --------------------------------------------------------------------------
# PA-14..PA-15 — DAG reachable through the composition root
# --------------------------------------------------------------------------


def _linear_plan() -> Any:
    nodes = (
        read_node("alpha"),
        DagNode(
            id="beta",
            kind=NodeKind.TRANSFORM,
            op="count",
            bindings={"args.value": Binding(source="alpha.result.topics", type="list")},
            depends_on=("alpha",),
        ),
    )
    return plan(nodes, budget=budget(max_parallelism=1))


def test_pa_14_dag_executes_through_the_composition_root(tmp_path: Path) -> None:
    executed: list[str] = []

    async def executor(node: DagNode, args: Any) -> dict[str, Any]:
        executed.append(node.id)
        return {"name": node.id, "topics": ["a"], "effect_ref": f"ref-{node.id}"}

    engine = production().dag_engine(
        executor,
        catalog=catalog(),
        store=SqliteCheckpointStore(tmp_path / "dag.db"),
        governance=AllowGovernance(),
    )
    validated = validate_plan(_linear_plan(), catalog())
    checkpoint = engine.admit(
        validated,
        execution_id="pa-dag-1",
        principal_ref="operator",
        projection_digest="pd",
        policy_digest="pol",
    )
    report = asyncio.run(engine.run(validated, checkpoint))
    assert report.status is PlanStatus.COMPLETED
    assert executed == ["alpha"]


def test_pa_15_dag_is_refused_when_the_capability_is_disabled(tmp_path: Path) -> None:
    async def executor(node: DagNode, args: Any) -> dict[str, Any]:  # pragma: no cover
        raise AssertionError("must not execute")

    composition = V2Composition(profile=V2ProductionProfile.production().without(V2Capability.DAG))
    with pytest.raises(CapabilityDisabled):
        composition.dag_engine(
            executor,
            catalog=catalog(),
            store=SqliteCheckpointStore(tmp_path / "dag.db"),
            governance=AllowGovernance(),
        )


# --------------------------------------------------------------------------
# PA-16..PA-17 — RUNBOOK reachable through the composition root
# --------------------------------------------------------------------------


class _CountingStore(SqliteCheckpointStore):
    """Phase 5 store plus the tiny execution ledger the runbook engine needs."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self._exec: dict[str, dict[str, Any]] = {}

    def load_execution(self, key: str) -> dict[str, Any] | None:
        return self._exec.get(key)

    def save_execution(self, key: str, payload: dict[str, Any]) -> None:
        self._exec[key] = dict(payload)


class _RecordingApproval:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.consumed: set[tuple[str, str]] = set()

    def approve(self, digest: str, plan_digest_value: str, approver: str) -> None:
        key = (digest, plan_digest_value)
        assert key not in self.consumed
        self.consumed.add(key)
        self.calls.append((digest, plan_digest_value, approver))


def _runbook_catalog() -> Any:
    """Catalog matching the Phase 6 exemplar runbook's declared compensations."""
    from hermes_mcp_bridge.v2.dag_validation import StaticToolCatalog, ToolContract

    tools = {
        READ_TOOL: ToolContract(
            tool_id=READ_TOOL,
            arg_types={"repository": "string"},
            result_types={"name": "string", "topics": "list"},
        ),
        "github.create_branch": ToolContract(
            tool_id="github.create_branch",
            arg_types={"repository": "string", "branch": "string", "sha": "string"},
            result_types={"ref": "string", "effect_ref": "string"},
            mutating=True,
            credential_capability_id="github.write",
            compensations=("github.delete_branch",),
        ),
    }
    return StaticToolCatalog(
        contracts=tools,
        projected=frozenset(tools),
        scope=frozenset({TARGET}),
    )


def test_pa_16_runbook_executes_through_the_composition_root(tmp_path: Path) -> None:
    raw = json.loads((FIXTURES / "RB-GITHUB-PR-LIFECYCLE-001.json").read_text(encoding="utf-8"))
    manifest = load_manifest(raw)
    registry = RunbookRegistry(tmp_path / "registry.db")
    digest = runbook_digest(manifest)
    registry.admit(manifest, digest)
    registry.transition(manifest.runbook_id, manifest.version, RunbookState.ACTIVE)

    executed: list[str] = []

    async def executor(node: DagNode, args: Any) -> dict[str, Any]:
        executed.append(node.id)
        return {"name": "disposable", "topics": [], "ref": "b", "effect_ref": f"ref-{node.id}"}

    engine = production().runbook_engine(
        executor,
        registry,
        _runbook_catalog(),
        _CountingStore(tmp_path / "state.db"),
        AllowGovernance(),
        approval=_RecordingApproval(),
        authorized_runbooks={(manifest.runbook_id, manifest.version): {"operator"}},
    )
    result = engine.invoke(
        InvocationRequest(
            runbook_id=manifest.runbook_id,
            version=manifest.version,
            expected_runbook_digest=digest,
            arguments={"repository": TARGET},
            idempotency_key="pa-rb-1",
            principal_ref="operator",
            approval_ref="ap-1",
        )
    )
    assert result.status is PlanStatus.COMPLETED
    assert executed


def test_pa_17_runbook_is_refused_when_the_capability_is_disabled(tmp_path: Path) -> None:
    composition = V2Composition(
        profile=V2ProductionProfile.production().without(V2Capability.RUNBOOK)
    )
    with pytest.raises(CapabilityDisabled):
        composition.runbook_engine(
            lambda node, args: None,
            RunbookRegistry(tmp_path / "registry.db"),
            catalog(),
            SqliteCheckpointStore(tmp_path / "state.db"),
            AllowGovernance(),
        )


# --------------------------------------------------------------------------
# PA-18..PA-19 — INTEGRATIONS reachable through the composition root
# --------------------------------------------------------------------------


def _integration_parts() -> dict[str, Any]:
    manifest = github_manifest(include_write=False)
    registry = build_registry(
        allow_list=PROVIDER_ALLOW_LIST,
        tool_ids=[capability.tool_id for capability in manifest.capabilities],
        manifests=[manifest],
    )
    registry.promote_configured(
        HealthReport(capability_id=capability.capability_id, state=CapabilityState.READY)
        for capability in manifest.capabilities
    )
    broker = ProviderCredentialBroker({manifest.provider_id: manifest.credential_domain})
    for capability in manifest.credential_domain.capability_ids:
        broker.register(
            CredentialRecord(
                provider_id=manifest.provider_id,
                credential_capability_id=capability,
                ready=True,
                apply=lambda headers: {**headers, "Authorization": "Bearer [REDACTED]"},
            )
        )
    scopes = ScopeResolver()
    for capability in manifest.capabilities:
        scopes.allow(capability.capability_id, (TARGET,))
    policy = PolicyPort({capability.capability_id: "ALLOW" for capability in manifest.capabilities})
    return {
        "manifest": manifest,
        "registry": registry,
        "broker": broker,
        "scopes": scopes,
        "policy": policy,
        "audit": IntegrationAuditLedger(MemoryAuditSink()),
    }


def test_pa_18_integrations_execute_through_the_composition_root() -> None:
    parts = _integration_parts()
    manifest = parts["manifest"]
    read_capability = next(c for c in manifest.capabilities if not c.is_write)

    def adapter(request: ProviderRequest, headers: Any, budget_ms: int) -> ProviderCallResult:
        assert "Authorization" in headers
        return ProviderCallResult(payload={"ok": True}, byte_count=8)

    gateway = production().provider_gateway(
        registry=parts["registry"],
        policy=parts["policy"],
        scopes=parts["scopes"],
        broker=parts["broker"],
        audit=parts["audit"],
        adapters={manifest.provider_id: adapter},
    )
    outcome = gateway.invoke(
        ProviderRequest(
            request_id="pa-int-1",
            principal_ref="operator",
            provider_id=manifest.provider_id,
            capability_id=read_capability.capability_id,
            target_scope_ref=TARGET,
            arguments={"repository": TARGET},
        )
    )
    assert outcome.outcome is OutcomeClass.SUCCESS
    assert gateway.provider_calls == 1


def test_pa_19_integrations_are_refused_when_the_capability_is_disabled() -> None:
    parts = _integration_parts()
    composition = V2Composition(
        profile=V2ProductionProfile.production().without(V2Capability.INTEGRATIONS)
    )
    with pytest.raises(CapabilityDisabled):
        composition.provider_gateway(
            registry=parts["registry"],
            policy=parts["policy"],
            scopes=parts["scopes"],
            broker=parts["broker"],
            audit=parts["audit"],
            adapters={},
        )


# --------------------------------------------------------------------------
# PA-20..PA-24 — HYBRID, preference order and agentic budget
# --------------------------------------------------------------------------


def _intent(**overrides: Any) -> ResolverIntent:
    operations = overrides.pop(
        "operations",
        (
            IntentOperation(
                capability_id=READ_CAPABILITY,
                target_scope_ref=TARGET,
                operation_ref=READ_CAPABILITY,
            ),
        ),
    )
    return ResolverIntent(
        request_id=overrides.pop("request_id", "pa-hy-1"),
        principal_ref="operator",
        operations=tuple(operations),
        **overrides,
    )


def _resolver(composition: V2Composition, **overrides: Any):
    return composition.mode_resolver(
        snapshot=overrides.pop(
            "snapshot",
            {
                READ_CAPABILITY: CapabilityState.READY,
                WRITE_CAPABILITY: CapabilityState.READY,
            },
        ),
        snapshot_digest="b" * 64,
        budget=overrides.pop("budget", None),
        runbooks=overrides.pop("runbooks", {}),
        write_capabilities=frozenset({WRITE_CAPABILITY}),
    )


def test_pa_20_hybrid_resolves_and_executes_through_the_composition_root() -> None:
    composition = production()
    resolver = _resolver(composition)
    executed: list[int] = []

    def executor(decision: Any, operations: Any) -> int:
        executed.append(len(operations))
        return len(operations)

    coordinator = composition.hybrid_coordinator(resolver=resolver, executor=executor)
    assert isinstance(coordinator, HybridCoordinator)
    outcome = coordinator.run(_intent(), policy=PolicyDecision.ALLOW)
    assert outcome.final_decision.mode is ExecutionMode.DIRECT
    assert outcome.deterministic_nodes_executed == 1
    assert executed == [1]


def test_pa_21_deterministic_preference_order_is_preserved() -> None:
    assert MODE_PREFERENCE == (
        ExecutionMode.DIRECT,
        ExecutionMode.BATCH,
        ExecutionMode.DAG,
        ExecutionMode.RUNBOOK,
        ExecutionMode.AGENTIC,
    )


def test_pa_22_agentic_budget_defaults_to_zero_in_production() -> None:
    """Activation must not widen the accepted Hybrid contract."""
    composition = production()
    assert composition.profile.allows_agentic is False
    assert composition.resolver_budget().agentic_token_budget == 0
    assert composition.resolver_budget(ResolverBudget()).allows_agentic is False


def test_pa_23_agentic_step_is_refused_without_a_declared_budget() -> None:
    composition = production()
    resolver = _resolver(composition)

    def executor(decision: Any, operations: Any) -> int:  # pragma: no cover - never called
        return 0

    def agentic_step(context: Any) -> Any:  # pragma: no cover - never called
        raise AssertionError("must not run")

    with pytest.raises(CapabilityDisabled):
        composition.hybrid_coordinator(
            resolver=resolver, executor=executor, agentic_step=agentic_step
        )


def test_pa_24_hybrid_requires_the_deterministic_lanes() -> None:
    """A profile enabling HYBRID over missing lanes is rejected at construction."""
    with pytest.raises(ProfileConfigError):
        V2ProductionProfile(enabled=True, direct=True, hybrid=True)

    degraded = V2ProductionProfile.production().without(V2Capability.DAG)
    assert degraded.is_enabled(V2Capability.HYBRID) is False
    assert degraded.fully_active is False

    composition = V2Composition(profile=degraded)
    with pytest.raises(CapabilityDisabled):
        composition.mode_resolver(
            snapshot={READ_CAPABILITY: CapabilityState.READY},
            snapshot_digest="c" * 64,
        )
    assert ResolverReason.R_DIRECT_EXACT.is_mode_selection
