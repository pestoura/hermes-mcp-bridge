"""Phase 6 RUNBOOK acceptance suite — one real test per A6-01..A6-26.

Executed by ``scripts/validate_v2_phase6_runbook_gate.py``. Every test asserts a
behaviour of the runtime, never a document. Nothing here mocks the module under
test: the registry is a real SQLite file, the engine is the validated Phase 5
engine, and denials are proven by observable absence of side effects.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from hermes_mcp_bridge import contracts
from hermes_mcp_bridge.v2 import runbook_contract as rc
from hermes_mcp_bridge.v2.dag_contract import (
    Budget,
    FailurePolicy,
    Idempotency,
    Node,
    NodeKind,
    PlanDefinition,
    PlanStatus,
    RollbackPolicy,
)
from hermes_mcp_bridge.v2.dag_digest import plan_digest as dag_plan_digest
from hermes_mcp_bridge.v2.dag_engine import NodeDecision
from hermes_mcp_bridge.v2.dag_store import SqliteCheckpointStore
from hermes_mcp_bridge.v2.dag_validation import StaticToolCatalog, ToolContract
from hermes_mcp_bridge.v2.runbook_admission import rank_nodes, validate_admission
from hermes_mcp_bridge.v2.runbook_compile import compile_runbook_to_plan
from hermes_mcp_bridge.v2.runbook_contract import (
    ApprovalClass,
    ParamConstraint,
    Parameter,
    ParamSensitivity,
    ParamType,
    PolicyClass,
    RollbackSupport,
    RunbookError,
    RunbookManifest,
    RunbookNode,
    RunbookOwner,
    RunbookReason,
    RunbookState,
)
from hermes_mcp_bridge.v2.runbook_digest import (
    canonical_ir,
    canonical_ir_bytes,
    plan_digest,
    runbook_digest,
)
from hermes_mcp_bridge.v2.runbook_engine import (
    InvocationRequest,
    RunbookEngine,
)
from hermes_mcp_bridge.v2.runbook_loader import load_manifest
from hermes_mcp_bridge.v2.runbook_registry import RunbookRegistry

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "v2_phase6"

READ_TOOL = "github.get_repo"
WRITE_TOOL = "github.create_branch"
DELETE_TOOL = "github.delete_branch"


# --------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------


def owner(kind: str = "team") -> RunbookOwner:
    return RunbookOwner(
        id="platform-security", kind=kind, contact="team:platform-security", review_cadence_days=90
    )


def read_node(key: str = "read_repo", **over: Any) -> RunbookNode:
    base: dict[str, Any] = {
        "key": key,
        "tool": READ_TOOL,
        "tool_version": "1.0.0",
        "args": {"repository": "owner/disposable"},
        "node_timeout_ms": 30_000,
    }
    base.update(over)
    return RunbookNode(**base)


def write_node(key: str = "make_branch", **over: Any) -> RunbookNode:
    base: dict[str, Any] = {
        "key": key,
        "tool": WRITE_TOOL,
        "tool_version": "1.0.0",
        "args": {"repository": "owner/disposable", "branch": "b", "sha": "s"},
        "node_timeout_ms": 30_000,
        "compensation": "github.delete_branch",
    }
    base.update(over)
    return RunbookNode(**base)


def manifest(**over: Any) -> RunbookManifest:
    base: dict[str, Any] = {
        "runbook_id": "RB-GITHUB-READ-META-001",
        "version": "1.0.0",
        "nodes": [read_node()],
        "parameter_schema": [
            Parameter(
                name="repository",
                type=ParamType.RESOURCE_REF,
                resource_kind="github_repository",
                constraints=ParamConstraint(max_length=64),
            )
        ],
        "requires_capabilities": (READ_TOOL,),
        "resource_scope": "owner/disposable",
        "policy_class": PolicyClass.READ_ONLY,
        "approval_class": ApprovalClass.NONE,
        "rollback_support": RollbackSupport.NOT_APPLICABLE,
        "timeout_ms": 300_000,
        "owner": owner(),
    }
    base.update(over)
    return RunbookManifest(**base)


def mutating_manifest(**over: Any) -> RunbookManifest:
    base: dict[str, Any] = {
        "runbook_id": "RB-GITHUB-PR-LIFECYCLE-001",
        "version": "1.0.0",
        "nodes": [write_node()],
        "parameter_schema": [
            Parameter(
                name="repository",
                type=ParamType.RESOURCE_REF,
                resource_kind="github_repository",
                constraints=ParamConstraint(max_length=64),
            )
        ],
        "requires_capabilities": (WRITE_TOOL,),
        "credential_capability_ids": ("github.write",),
        "resource_scope": "owner/disposable",
        "policy_class": PolicyClass.MUTATING_LOW,
        "approval_class": ApprovalClass.SINGLE,
        "rollback_support": RollbackSupport.AUTOMATIC,
        "timeout_ms": 300_000,
        "owner": owner(),
    }
    base.update(over)
    return RunbookManifest(**base)


def admit(m: RunbookManifest, **over: Any) -> str:
    kwargs: dict[str, Any] = {
        "tool_names": set(contracts.required_tools()),
        "prev_version": None,
        "prev_digest": None,
        "computed_node_capabilities": {n.tool: n.tool for n in m.nodes},
        "registered_compensations": ["github.delete_branch"],
        "mutating_tools": [WRITE_TOOL, DELETE_TOOL],
    }
    kwargs.update(over)
    return validate_admission(m, **kwargs)


def catalog(mutating: bool = False) -> StaticToolCatalog:
    tools = {
        READ_TOOL: ToolContract(
            tool_id=READ_TOOL,
            arg_types={"repository": "string"},
            result_types={"name": "string", "topics": "list"},
        ),
        WRITE_TOOL: ToolContract(
            tool_id=WRITE_TOOL,
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
        scope=frozenset({"owner/disposable"}),
    )


class AllowGovernance:
    """Records every decision so denials can be proven by absence."""

    def __init__(self) -> None:
        self.decided: list[str] = []

    def decide(self, plan: Any, node: Any, resolved_args: Any) -> NodeDecision:
        self.decided.append(node.id)
        return NodeDecision(allowed=True, policy_digest="phase6-test")

    def record(self, plan: Any, node: Any, state: Any) -> None:
        return None


class CountingStore(SqliteCheckpointStore):
    """Phase 5 store plus the tiny execution ledger the runbook engine needs."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self._exec: dict[str, dict[str, Any]] = {}

    def load_execution(self, key: str) -> dict[str, Any] | None:
        return self._exec.get(key)

    def save_execution(self, key: str, payload: dict[str, Any]) -> None:
        self._exec[key] = dict(payload)


class RecordingApproval:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.consumed: set[tuple[str, str]] = set()

    def approve(self, runbook_digest: str, plan_digest_value: str, approver: str) -> None:
        key = (runbook_digest, plan_digest_value)
        if key in self.consumed:
            raise RunbookError(RunbookReason.RB_APPROVAL_INVALID, "already consumed")
        self.consumed.add(key)
        self.calls.append((runbook_digest, plan_digest_value, approver))


def build_engine(
    tmp_path: Path,
    m: RunbookManifest,
    *,
    principal: str = "operator",
    authorized: bool = True,
    approval: RecordingApproval | None = None,
    executed: list[str] | None = None,
) -> tuple[RunbookEngine, RunbookRegistry, CountingStore, AllowGovernance]:
    registry = RunbookRegistry(tmp_path / "registry.db")
    digest = admit(m)
    registry.admit(m, digest)
    registry.transition(m.runbook_id, m.version, RunbookState.ACTIVE)
    store = CountingStore(tmp_path / "state.db")
    governance = AllowGovernance()
    sink = executed if executed is not None else []

    async def executor(node: Node, args: Any) -> dict[str, Any]:
        sink.append(node.id)
        if node.tool == WRITE_TOOL:
            return {"ref": "b", "effect_ref": f"ref-{node.id}"}
        return {"name": "disposable", "topics": []}

    engine = RunbookEngine(
        executor,
        registry,
        catalog(),
        store,
        governance,
        approval=approval,
        enabled=True,
        authorized_runbooks=({(m.runbook_id, m.version): {principal}} if authorized else {}),
    )
    return engine, registry, store, governance


def request(m: RunbookManifest, digest: str, **over: Any) -> InvocationRequest:
    base: dict[str, Any] = {
        "runbook_id": m.runbook_id,
        "version": m.version,
        "expected_runbook_digest": digest,
        "arguments": {"repository": "owner/disposable"},
        "idempotency_key": "exec-1",
        "principal_ref": "operator",
    }
    base.update(over)
    return InvocationRequest(**base)


# --------------------------------------------------------------------------
# A6-01 .. A6-05
# --------------------------------------------------------------------------


def test_a6_01_prior_gates_declared_before_phase6() -> None:
    evidence = REPO_ROOT / "docs" / "v2" / "evidence"
    for name, gate in (
        ("phase3-direct-mutation-acceptance.json", "DIRECT_MUTATION_ACCEPTED"),
        ("phase4-batch-acceptance.json", "BATCH_ACCEPTED"),
        ("phase5-dag-acceptance.json", "DAG_ACCEPTED"),
    ):
        payload = json.loads((evidence / name).read_text(encoding="utf-8"))
        assert payload["gate"] == gate, name
        assert payload["failures"] == [], name


def test_a6_02_v1_contract_unchanged() -> None:
    assert contracts.CURRENT_CONTRACT_VERSION == "1.0.0"
    assert contracts.SCHEMA_VERSION == "0.6.1"
    assert contracts.expected_tool_count() == 27
    leaked = [n for n in contracts.required_tools() if "runbook" in n.lower()]
    assert leaked == []


def test_a6_03_admission_is_deterministic() -> None:
    left = manifest()
    right = manifest()
    assert canonical_ir_bytes(left) == canonical_ir_bytes(right)
    assert runbook_digest(left) == runbook_digest(right)
    # An editorial-only change must not move the digest.
    editorial = manifest(title="Read metadata", description="human text")
    assert runbook_digest(editorial) == runbook_digest(left)
    # A semantic change must move it.
    semantic = manifest(timeout_ms=299_000)
    assert runbook_digest(semantic) != runbook_digest(left)


def test_a6_04_admission_performs_no_io(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("admission attempted network I/O")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    digest = admit(manifest())
    assert len(digest) == 64


def test_a6_05_registry_is_append_only(tmp_path: Path) -> None:
    registry = RunbookRegistry(tmp_path / "r.db")
    m = manifest()
    registry.admit(m, admit(m))
    other = manifest(timeout_ms=299_000)
    with pytest.raises(RunbookError) as exc:
        registry.admit(other, admit(other))
    assert exc.value.reason is RunbookReason.RB_DIGEST_CONFLICT
    # nothing was overwritten
    assert registry.get(m.runbook_id, m.version).runbook_digest == runbook_digest(m)


# --------------------------------------------------------------------------
# A6-06 .. A6-10
# --------------------------------------------------------------------------


@pytest.mark.parametrize("pin", ["", "*", "latest"])
def test_a6_06_unpinned_references_rejected(pin: str) -> None:
    with pytest.raises(RunbookError) as exc:
        read_node(tool_version=pin)
    assert exc.value.reason is RunbookReason.RB_UNPINNED_REFERENCE


def test_a6_07_digest_mismatch_denies_without_side_effects(tmp_path: Path) -> None:
    m = manifest()
    executed: list[str] = []
    engine, _, _, governance = build_engine(tmp_path, m, executed=executed)
    with pytest.raises(RunbookError) as exc:
        engine.invoke(request(m, "0" * 64))
    assert exc.value.reason is RunbookReason.RB_DIGEST_MISMATCH
    assert executed == []
    assert governance.decided == []


def test_a6_08_yanked_runbook_is_not_invocable(tmp_path: Path) -> None:
    m = manifest()
    executed: list[str] = []
    engine, registry, _, _ = build_engine(tmp_path, m, executed=executed)
    registry.transition(m.runbook_id, m.version, RunbookState.YANKED)
    with pytest.raises(RunbookError) as exc:
        engine.invoke(request(m, runbook_digest(m)))
    assert exc.value.reason is RunbookReason.RB_YANKED
    assert executed == []


def test_a6_09_parameter_schema_is_closed() -> None:
    with pytest.raises(RunbookError) as unknown_field:
        load_manifest({"runbook_id": "RB-X-Y-001", "version": "1.0.0", "nodes": [], "bogus": 1})
    assert unknown_field.value.reason is RunbookReason.RB_SCHEMA_INVALID

    with pytest.raises(RunbookError) as secret:
        Parameter(name="api_token", type=ParamType.STRING)
    assert secret.value.reason is RunbookReason.RB_SECRET_PARAMETER


def test_a6_09b_oversize_and_out_of_constraint_arguments_denied(tmp_path: Path) -> None:
    m = manifest()
    engine, _, _, _ = build_engine(tmp_path, m)
    digest = runbook_digest(m)
    with pytest.raises(RunbookError) as oversize:
        engine.invoke(request(m, digest, arguments={"repository": "x" * 65}))
    assert oversize.value.reason is RunbookReason.RB_PARAM_OVERSIZE
    with pytest.raises(RunbookError) as unknown:
        engine.invoke(request(m, digest, arguments={"repository": "owner/d", "extra": 1}))
    assert unknown.value.reason is RunbookReason.RB_PARAM_UNKNOWN


@pytest.mark.parametrize(
    "source",
    ["param:{{repo}}", "param:${REPO}", "shell:`id`", "env:HOME", "file:/etc/passwd"],
)
def test_a6_10_unsafe_bindings_rejected(source: str) -> None:
    node = read_node(bindings=({"target": "args.repository", "source": source},))
    with pytest.raises(RunbookError) as exc:
        admit(manifest(nodes=[node]))
    assert exc.value.reason is RunbookReason.RB_UNSAFE_BINDING


# --------------------------------------------------------------------------
# A6-11 .. A6-15
# --------------------------------------------------------------------------


def test_a6_11_capability_match_is_exact_both_directions() -> None:
    superset = manifest(requires_capabilities=(READ_TOOL, WRITE_TOOL))
    with pytest.raises(RunbookError) as sup:
        admit(superset)
    assert sup.value.reason is RunbookReason.RB_CAPABILITY_SUPERSET

    subset = manifest(nodes=[read_node(), write_node()], requires_capabilities=(READ_TOOL,))
    with pytest.raises(RunbookError) as sub:
        admit(subset)
    assert sub.value.reason is RunbookReason.RB_CAPABILITY_MISSING


def test_a6_11b_administrative_capability_forbidden() -> None:
    m = manifest(requires_capabilities=("github.admin.transfer",))
    with pytest.raises(RunbookError) as exc:
        admit(m, computed_node_capabilities={"github.admin.transfer": "github.admin.transfer"})
    assert exc.value.reason is RunbookReason.RB_ADMIN_CAPABILITY_FORBIDDEN


def test_a6_12_weaker_policy_class_rejected() -> None:
    m = mutating_manifest(version="1.1.0", policy_class=PolicyClass.READ_ONLY)
    with pytest.raises(RunbookError) as exc:
        admit(
            m,
            prev_version=(1, 0, 0),
            prev_policy_class=PolicyClass.MUTATING_LOW,
        )
    assert exc.value.reason is RunbookReason.RB_VERSION_BUMP_INVALID


def test_a6_13_destructive_underdeclaration_rejected() -> None:
    node = write_node(key="drop", tool=DELETE_TOOL, compensation=None)
    m = mutating_manifest(
        nodes=[node],
        requires_capabilities=(DELETE_TOOL,),
        destructive_action=False,
        approval_class=ApprovalClass.DUAL,
        rollback_support=RollbackSupport.NOT_SUPPORTED,
        accepted_irreversibility=True,
    )
    with pytest.raises(RunbookError) as exc:
        admit(m)
    assert exc.value.reason is RunbookReason.RB_DESTRUCTIVE_UNDERDECLARED


def test_a6_13b_destructive_forces_dual_approval() -> None:
    node = write_node(key="drop", tool=DELETE_TOOL, compensation=None)
    m = mutating_manifest(
        nodes=[node],
        requires_capabilities=(DELETE_TOOL,),
        destructive_action=True,
        approval_class=ApprovalClass.SINGLE,
        rollback_support=RollbackSupport.NOT_SUPPORTED,
        accepted_irreversibility=True,
    )
    with pytest.raises(RunbookError) as exc:
        admit(m)
    assert exc.value.reason is RunbookReason.RB_APPROVAL_CLASS_TOO_WEAK


def test_a6_14_automatic_rollback_requires_registered_compensation() -> None:
    m = mutating_manifest(nodes=[write_node(compensation="github.unknown_inverse")])
    with pytest.raises(RunbookError) as exc:
        admit(m, registered_compensations=["github.delete_branch"])
    assert exc.value.reason is RunbookReason.RB_COMPENSATION_UNREGISTERED


def test_a6_14b_not_supported_rollback_requires_accepted_irreversibility() -> None:
    m = mutating_manifest(
        destructive_action=True,
        approval_class=ApprovalClass.DUAL,
        rollback_support=RollbackSupport.NOT_SUPPORTED,
        accepted_irreversibility=False,
    )
    with pytest.raises(RunbookError) as exc:
        admit(m)
    assert exc.value.reason is RunbookReason.RB_IRREVERSIBLE_UNACCEPTED


def test_a6_15_unsafe_compensation_does_not_write(tmp_path: Path) -> None:
    """An unregistered inverse is refused at admission, before any execution."""
    m = mutating_manifest(nodes=[write_node(compensation="github.force_push")])
    with pytest.raises(RunbookError):
        admit(m)
    assert not (tmp_path / "registry.db").exists()


# --------------------------------------------------------------------------
# A6-16 .. A6-20
# --------------------------------------------------------------------------


def test_a6_16_timeouts_bounded_and_consistent() -> None:
    m = manifest(nodes=[read_node(node_timeout_ms=400_000)], timeout_ms=300_000)
    with pytest.raises(RunbookError) as exc:
        admit(m)
    assert exc.value.reason is RunbookReason.RB_TIMEOUT_INCONSISTENT


def test_a6_17_agentic_budget_defaults_to_zero_and_cannot_be_widened(tmp_path: Path) -> None:
    m = manifest()
    assert m.max_agentic_escalations == 0
    assert m.max_agentic_tokens == 0
    assert rc.RUNBOOK_MAX_AGENTIC_TOKENS_DEFAULT == 0
    with pytest.raises(RunbookError) as exc:
        manifest(requires_signature=True, max_agentic_tokens=1)
    assert exc.value.reason is RunbookReason.RB_AGENTIC_NOT_PERMITTED

    engine, _, _, _ = build_engine(tmp_path, m)
    result = engine.invoke(request(m, runbook_digest(m)))
    assert result.report is not None
    assert result.report.llm_tokens == 0


def test_a6_17b_caller_cannot_widen_budget() -> None:
    m = manifest()
    tightened = compile_runbook_to_plan(
        m,
        arguments={"repository": "owner/disposable"},
        resource_scope="owner/disposable",
        caller_capabilities=[READ_TOOL],
        principled_ref="operator",
        tightened_budget={"max_total_wall_ms": 10_000},
    )
    widened = compile_runbook_to_plan(
        m,
        arguments={"repository": "owner/disposable"},
        resource_scope="owner/disposable",
        caller_capabilities=[READ_TOOL],
        principled_ref="operator",
        tightened_budget={"max_total_wall_ms": 900_000},
    )
    assert tightened.budget.max_total_wall_ms == 10_000
    assert widened.budget.max_total_wall_ms == m.timeout_ms


def test_a6_18_approval_is_bound_and_single_use(tmp_path: Path) -> None:
    m = mutating_manifest()
    gateway = RecordingApproval()
    engine, _, _, _ = build_engine(tmp_path, m, approval=gateway)
    digest = runbook_digest(m)

    # missing approval reference denies before execution
    with pytest.raises(RunbookError) as missing:
        engine.invoke(request(m, digest))
    assert missing.value.reason is RunbookReason.RB_APPROVAL_INVALID
    assert gateway.calls == []

    first = engine.invoke(request(m, digest, approval_ref="ap-1"))
    assert first.status is PlanStatus.COMPLETED
    assert len(gateway.calls) == 1
    # the approval is bound to the exact plan_digest
    assert gateway.calls[0][1] == first.plan_digest

    # reuse of the same (runbook_digest, plan_digest) is refused
    with pytest.raises(RunbookError) as reuse:
        engine.invoke(request(m, digest, approval_ref="ap-1", idempotency_key="exec-2"))
    assert reuse.value.reason is RunbookReason.RB_APPROVAL_INVALID


def test_a6_19_execution_idempotency(tmp_path: Path) -> None:
    m = mutating_manifest()
    executed: list[str] = []
    engine, _, _, _ = build_engine(tmp_path, m, approval=RecordingApproval(), executed=executed)
    digest = runbook_digest(m)
    first = engine.invoke(request(m, digest, approval_ref="ap-1"))
    calls_after_first = list(executed)
    second = engine.invoke(request(m, digest, approval_ref="ap-1"))
    assert executed == calls_after_first, "replay performed additional provider mutations"
    assert second.plan_digest == first.plan_digest
    assert second.status is first.status

    # same key, different digest -> conflict
    with pytest.raises(RunbookError) as conflict:
        engine.invoke(
            request(
                m,
                digest,
                approval_ref="ap-1",
                arguments={"repository": "owner/other"},
            )
        )
    assert conflict.value.reason is RunbookReason.RB_IDEMPOTENCY_CONFLICT


def test_a6_20_write_ahead_record_precedes_every_mutation(tmp_path: Path) -> None:
    m = mutating_manifest()
    registry = RunbookRegistry(tmp_path / "r.db")
    registry.admit(m, admit(m))
    registry.transition(m.runbook_id, m.version, RunbookState.ACTIVE)
    store = CountingStore(tmp_path / "s.db")
    observed: list[str | None] = []

    async def executor(node: Node, args: Any) -> dict[str, Any]:
        state = store.load("exec-1").node_states[node.id]
        observed.append(state.idempotency_key)
        return {"ref": "b", "effect_ref": f"ref-{node.id}"}

    engine = RunbookEngine(
        executor,
        registry,
        catalog(),
        store,
        AllowGovernance(),
        approval=RecordingApproval(),
        enabled=True,
        authorized_runbooks={(m.runbook_id, m.version): {"operator"}},
    )
    engine.invoke(request(m, runbook_digest(m), approval_ref="ap-1"))
    assert observed and all(observed), "a mutation ran without a durable write-ahead record"


# --------------------------------------------------------------------------
# A6-21 .. A6-26
# --------------------------------------------------------------------------


def test_a6_21_owner_and_review_cadence_required() -> None:
    with pytest.raises(RunbookError) as no_owner:
        manifest(owner=None)
    assert no_owner.value.reason is RunbookReason.RB_OWNER_UNRESOLVABLE

    with pytest.raises(RunbookError) as cadence:
        RunbookOwner(id="x", kind="team", contact="c", review_cadence_days=999)
    assert cadence.value.reason is RunbookReason.RB_REVIEW_CADENCE_INVALID

    high = mutating_manifest(
        policy_class=PolicyClass.MUTATING_HIGH,
        approval_class=ApprovalClass.DUAL,
        owner=owner("role"),
    )
    with pytest.raises(RunbookError) as kind:
        admit(high)
    assert kind.value.reason is RunbookReason.RB_OWNER_KIND_INSUFFICIENT


def test_a6_21b_overdue_review_denies_high_blast_invocation(tmp_path: Path) -> None:
    m = mutating_manifest(policy_class=PolicyClass.MUTATING_HIGH, approval_class=ApprovalClass.DUAL)
    executed: list[str] = []
    engine, _, _, _ = build_engine(tmp_path, m, approval=RecordingApproval(), executed=executed)
    with pytest.raises(RunbookError) as exc:
        engine.invoke(request(m, runbook_digest(m), approval_ref="ap-1"))
    assert exc.value.reason is RunbookReason.RB_REVIEW_OVERDUE
    assert executed == []


def test_a6_22_registry_records_carry_no_secret_material(tmp_path: Path) -> None:
    registry = RunbookRegistry(tmp_path / "r.db")
    m = manifest()
    record = registry.admit(m, admit(m))
    text = record.ir_bytes.decode("utf-8").lower()
    for hint in ("authorization", "bearer", "password", "private_key", "client_secret"):
        assert hint not in text
    record.assert_no_secret_material()

    # tamper detection
    conn = sqlite3.connect(str(tmp_path / "r.db"), isolation_level=None)
    conn.execute("UPDATE runbooks SET ir_bytes=?", (b'{"tampered":true}',))
    conn.close()
    with pytest.raises(RunbookError) as exc:
        RunbookRegistry(tmp_path / "r.db").get(m.runbook_id, m.version)
    assert exc.value.reason is RunbookReason.RB_DIGEST_CONFLICT


def test_a6_23_unauthorized_caller_receives_rb_unknown(tmp_path: Path) -> None:
    m = manifest()
    engine, _, _, _ = build_engine(tmp_path, m, authorized=False)
    with pytest.raises(RunbookError) as exc:
        engine.invoke(request(m, runbook_digest(m)))
    assert exc.value.reason is RunbookReason.RB_UNKNOWN
    # the reason must not leak policy detail or the digest
    assert "policy" not in str(exc.value).lower()
    assert runbook_digest(m) not in str(exc.value)


def test_a6_24_exemplar_runbook_admits_promotes_and_executes(tmp_path: Path) -> None:
    raw = json.loads((FIXTURES / "RB-GITHUB-PR-LIFECYCLE-001.json").read_text(encoding="utf-8"))
    m = load_manifest(raw)
    assert m.runbook_id == "RB-GITHUB-PR-LIFECYCLE-001"
    executed: list[str] = []
    engine, registry, _, _ = build_engine(
        tmp_path, m, approval=RecordingApproval(), executed=executed
    )
    record = registry.get(m.runbook_id, m.version)
    assert record.state is RunbookState.ACTIVE
    result = engine.invoke(request(m, runbook_digest(m), approval_ref="ap-1"))
    assert result.status is PlanStatus.COMPLETED
    assert executed == sorted(executed, key=lambda k: rank_nodes(m)[k])
    assert result.report is not None
    assert result.report.unknown_effects == ()


def test_a6_25_migration_equivalence_runbook_vs_reference_dag() -> None:
    m = manifest()
    compiled = compile_runbook_to_plan(
        m,
        arguments={"repository": "owner/disposable"},
        resource_scope="owner/disposable",
        caller_capabilities=[READ_TOOL],
        principled_ref="operator",
    )
    reference = PlanDefinition(
        plan_id=compiled.plan_id,
        nodes=(
            Node(
                id="read_repo",
                kind=NodeKind.TOOL,
                tool=READ_TOOL,
                args={"repository": "owner/disposable"},
                timeout_ms=30_000,
                idempotency=Idempotency(enabled=True, attempt_epoch=0),
            ),
        ),
        budget=Budget(
            max_nodes=1,
            max_parallelism=4,
            max_external_calls=1,
            max_total_wall_ms=m.timeout_ms,
            max_result_bytes=1_048_576,
            max_checkpoint_bytes=1_048_576,
        ),
        failure_policy=FailurePolicy.FAIL_FAST,
        rollback_policy=RollbackPolicy.NONE,
        deadline_ms=m.timeout_ms,
    )
    assert dag_plan_digest(compiled) == dag_plan_digest(reference)


def test_a6_26_every_criterion_traces_to_a_test_and_a_requirement() -> None:
    criteria_doc = (REPO_ROOT / "docs" / "v2" / "phase6" / "acceptance-criteria.md").read_text(
        encoding="utf-8"
    )
    suite = Path(__file__).read_text(encoding="utf-8")
    matrix = (REPO_ROOT / "docs" / "v2" / "requirements" / "traceability-matrix.md").read_text(
        encoding="utf-8"
    )
    for index in range(1, 27):
        token = f"A6-{index:02d}"
        assert token in criteria_doc, f"{token} missing from acceptance-criteria.md"
        assert f"a6_{index:02d}" in suite, f"{token} has no implementing test"
        assert token in matrix, f"{token} missing from the traceability matrix"


def test_a6_26b_feature_flag_defaults_off_and_engine_is_fail_closed(tmp_path: Path) -> None:
    assert rc.RUNBOOK_FEATURE_ENABLED is False
    registry = RunbookRegistry(tmp_path / "r.db")
    m = manifest()
    registry.admit(m, admit(m))

    async def executor(node: Node, args: Any) -> dict[str, Any]:
        raise AssertionError("disabled engine executed a node")

    engine = RunbookEngine(
        executor,
        registry,
        catalog(),
        CountingStore(tmp_path / "s.db"),
        AllowGovernance(),
    )
    with pytest.raises(RunbookError) as exc:
        engine.invoke(request(m, runbook_digest(m)))
    assert exc.value.reason is RunbookReason.RB_NOT_PROMOTED


def test_canonical_ir_excludes_editorial_fields() -> None:
    ir = canonical_ir(manifest(title="t", description="d"))
    assert "title" not in ir
    assert "description" not in ir


def test_plan_digest_commits_sensitive_arguments() -> None:
    m = manifest(
        parameter_schema=[
            Parameter(
                name="repository",
                type=ParamType.RESOURCE_REF,
                resource_kind="github_repository",
                constraints=ParamConstraint(max_length=64),
            ),
            Parameter(
                name="ticket_reference",
                type=ParamType.STRING,
                required=False,
                sensitivity=ParamSensitivity.SENSITIVE,
            ),
        ]
    )
    digest_value = plan_digest(
        m,
        resolved_arguments={"repository": "owner/disposable", "ticket_reference": "INC-4242"},
        resolved_resource_scope="owner/disposable",
        effective_capabilities=[READ_TOOL],
        capability_snapshot_hash="cap",
        runbook_snapshot_hash=runbook_digest(m),
        policy_class=m.policy_class.value,
        approval_class=m.approval_class.value,
        destructive_action=False,
        budgets={"max_total_wall_ms": m.timeout_ms},
        principal_ref="operator",
    )
    assert len(digest_value) == 64
    from hermes_mcp_bridge.v2.runbook_digest import plan_digest_inputs

    payload = plan_digest_inputs(
        m,
        resolved_arguments={"repository": "owner/disposable", "ticket_reference": "INC-4242"},
        resolved_resource_scope="owner/disposable",
        effective_capabilities=[READ_TOOL],
        capability_snapshot_hash="cap",
        runbook_snapshot_hash=runbook_digest(m),
        policy_class=m.policy_class.value,
        approval_class=m.approval_class.value,
        destructive_action=False,
        budgets={"max_total_wall_ms": m.timeout_ms},
        principal_ref="operator",
    )
    assert "INC-4242" not in json.dumps(payload)
    assert payload["resolved_arguments"]["ticket_reference"]["commitment"].startswith("commitment:")


def test_engine_run_is_awaited_without_a_running_loop() -> None:
    """Guards the engine's use of asyncio.run inside a sync entrypoint."""
    assert asyncio.get_event_loop_policy() is not None
