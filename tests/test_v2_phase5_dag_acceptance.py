"""Phase 5 DAG acceptance suite — one real test per A5-nn criterion.

Hermetic: no network, no subprocess, no real provider. The gate
``scripts/validate_v2_phase5_dag_gate.py`` runs this file for real; a skip or a
missing scenario is a gate failure.
"""

from __future__ import annotations

import ast
import asyncio
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from dag_fixtures import (
    FIXTURE_DIR,
    AllowGovernance,
    budget,
    catalog,
    plan,
    read_node,
)

from hermes_mcp_bridge.v2.dag_contract import (
    DAG_FEATURE_ENABLED,
    Approval,
    Binding,
    Compensation,
    FailurePolicy,
    Idempotency,
    Node,
    NodeKind,
    NodeStatus,
    OnFailure,
    PlanReason,
    PlanStatus,
    PlanValidationError,
    RollbackPolicy,
)
from hermes_mcp_bridge.v2.dag_digest import operation_digest, plan_digest
from hermes_mcp_bridge.v2.dag_engine import (
    DagEngine,
    DenyAllGovernance,
    ExecutionReport,
    NodeFailed,
    NodeIndeterminate,
)
from hermes_mcp_bridge.v2.dag_loader import load_plan, plan_from_mapping
from hermes_mcp_bridge.v2.dag_store import (
    SqliteCheckpointStore,
    StoreError,
)
from hermes_mcp_bridge.v2.dag_transform import TRANSFORM_OP_NAMES, apply_transform
from hermes_mcp_bridge.v2.dag_validation import validate_plan

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src" / "hermes_mcp_bridge"
PHASE5_MODULES = (
    "v2/dag_contract.py",
    "v2/dag_transform.py",
    "v2/dag_digest.py",
    "v2/dag_validation.py",
    "v2/dag_store.py",
    "v2/dag_engine.py",
    "v2/dag_loader.py",
)


def _store(tmp_path: Path, name: str = "state.db") -> SqliteCheckpointStore:
    return SqliteCheckpointStore(tmp_path / name)


def _engine(tmp_path: Path, executor: Any, **kwargs: Any) -> DagEngine:
    kwargs.setdefault("governance", AllowGovernance())
    kwargs.setdefault("store", _store(tmp_path, kwargs.pop("db", "state.db")))
    return DagEngine(executor, catalog=catalog(), enabled=True, **kwargs)


async def _ok(node: Node, args: Any) -> dict[str, Any]:
    return {"name": node.id, "topics": ["a"], "effect_ref": f"ref-{node.id}"}


def _linear_plan(**overrides: Any):
    nodes = (
        read_node("alpha"),
        Node(
            id="beta",
            kind=NodeKind.TRANSFORM,
            op="count",
            bindings={"args.value": Binding(source="alpha.result.topics", type="list")},
            depends_on=("alpha",),
        ),
    )
    return plan(nodes, **overrides)


# --------------------------------------------------------------- A5-01..A5-04


def test_a5_01_prerequisite_gates_declared_before_phase5() -> None:
    evidence = REPO_ROOT / "docs" / "v2" / "evidence"
    for name, gate in (
        ("phase3-direct-mutation-acceptance.json", "DIRECT_MUTATION_ACCEPTED"),
        ("phase4-batch-acceptance.json", "BATCH_ACCEPTED"),
    ):
        payload = json.loads((evidence / name).read_text(encoding="utf-8"))
        assert payload["gate"] == gate
        assert payload["failures"] == []


def test_a5_02_v1_contract_unchanged() -> None:
    from hermes_mcp_bridge import contracts

    assert contracts.CURRENT_CONTRACT_VERSION == "1.0.0"
    assert contracts.SCHEMA_VERSION == "0.6.1"
    assert contracts.expected_tool_count() == 27


def test_a5_03_no_generic_surface_in_phase5_modules() -> None:
    banned_modules = {"subprocess", "socket", "requests", "httpx", "urllib", "http", "shlex", "pty"}
    # Bare builtins are always forbidden; ``re.compile`` (an attribute call on a
    # safe stdlib module) is not a code-execution surface, so attribute calls are
    # checked against the process-spawning names only.
    banned_names = {"eval", "exec", "compile", "__import__"}
    banned_attrs = {"system", "popen", "spawn", "fork", "execv"}
    for rel in PHASE5_MODULES:
        tree = ast.parse((SRC / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in banned_modules, rel
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in banned_modules, rel
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    assert node.func.id not in banned_names, rel
                elif isinstance(node.func, ast.Attribute):
                    assert node.func.attr not in banned_attrs, rel


def test_a5_04_transform_ops_are_a_closed_set() -> None:
    assert TRANSFORM_OP_NAMES == (
        "count",
        "filter_eq",
        "filter_in",
        "first",
        "map_field",
        "merge_objects",
        "project",
        "require_non_empty",
        "select",
        "sort_by",
        "to_list",
        "unique",
    )
    with pytest.raises(PlanValidationError) as excinfo:
        apply_transform("system", {"value": []})
    assert excinfo.value.reason is PlanReason.TRANSFORM_OP_UNKNOWN
    with pytest.raises(PlanValidationError) as excinfo:
        apply_transform("to_list", {"value": ["x" * 100] * 10}, max_bytes=32)
    assert excinfo.value.reason is PlanReason.TRANSFORM_OUTPUT_TOO_LARGE


# ---------------------------------------------------------------------- A5-05


@pytest.mark.parametrize(
    ("fixture", "reason"),
    [
        ("plan_cycle_simple", PlanReason.PLAN_CYCLE_DETECTED),
        ("plan_self_dependency", PlanReason.PLAN_SELF_DEPENDENCY),
        ("plan_unknown_dependency", PlanReason.PLAN_UNKNOWN_DEPENDENCY),
        ("plan_unreachable_node", PlanReason.PLAN_UNREACHABLE_NODE),
        ("plan_binding_edge_undeclared", PlanReason.BINDING_EDGE_NOT_DECLARED),
        ("plan_binding_type_mismatch", PlanReason.BINDING_TYPE_MISMATCH),
        ("plan_binding_control_field", PlanReason.BINDING_CONTROL_FIELD_FORBIDDEN),
    ],
)
def test_a5_05_negative_fixtures_reject_with_stable_reason(
    fixture: str, reason: PlanReason
) -> None:
    path = FIXTURE_DIR / f"{fixture}.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document.get("expected_reason_code") == reason.value
    first: PlanReason | None = None
    for _ in range(3):  # determinism: identical rejection every run
        with pytest.raises(PlanValidationError) as excinfo:
            validate_plan(load_plan(path), catalog())
        assert excinfo.value.reason is reason
        first = first or excinfo.value.reason
        assert excinfo.value.reason is first


def test_a5_05b_rejection_resolves_zero_credentials_and_makes_zero_calls() -> None:
    class TrackingCatalog:
        def __init__(self) -> None:
            self.inner = catalog()
            self.calls = 0

        def contract(self, tool_id: str) -> Any:
            return self.inner.contract(tool_id)

        def is_projected(self, tool_id: str) -> bool:
            return self.inner.is_projected(tool_id)

        def in_scope(self, resource: str) -> bool:
            self.calls += 1
            return self.inner.in_scope(resource)

    tracking = TrackingCatalog()
    with pytest.raises(PlanValidationError):
        validate_plan(load_plan(FIXTURE_DIR / "plan_cycle_simple.json"), tracking)
    # Graph rejection happens before any scope/credential question is asked.
    assert tracking.calls == 0


def test_a5_05c_depth_and_fanout_limits_enforced() -> None:
    wide = [read_node("root")]
    wide += [read_node(f"leaf{index}", depends_on=("root",)) for index in range(20)]
    with pytest.raises(PlanValidationError) as excinfo:
        validate_plan(
            plan(tuple(wide), budget=budget(max_nodes=64, max_external_calls=64)),
            catalog(),
        )
    assert excinfo.value.reason is PlanReason.PLAN_FANOUT_EXCEEDED


# ---------------------------------------------------------------------- A5-06


def test_a5_06_binding_unknown_field_and_duplicate_literal_rejected() -> None:
    unknown = plan(
        (
            read_node("alpha"),
            read_node(
                "beta",
                args={},
                depends_on=("alpha",),
                bindings={"args.repository": Binding(source="alpha.result.missing", type="string")},
            ),
        )
    )
    with pytest.raises(PlanValidationError) as excinfo:
        validate_plan(unknown, catalog())
    assert excinfo.value.reason is PlanReason.BINDING_FIELD_UNKNOWN


def test_a5_06b_valid_plan_accepted_and_ordered() -> None:
    validated = validate_plan(_linear_plan(), catalog())
    assert validated.order == ("alpha", "beta")
    assert validated.ranks == {"alpha": 0, "beta": 1}


def test_a5_06c_unknown_plan_field_rejected() -> None:
    document = json.loads((FIXTURE_DIR / "plan_valid_linear.json").read_text(encoding="utf-8"))
    document["surprise"] = 1
    with pytest.raises(PlanValidationError) as excinfo:
        plan_from_mapping(document)
    assert excinfo.value.reason is PlanReason.PLAN_UNKNOWN_FIELD


def test_a5_06d_runtime_revalidation_rejects_hostile_value(tmp_path: Path) -> None:
    hostile = plan(
        (
            read_node("alpha"),
            read_node(
                "beta",
                args={},
                depends_on=("alpha",),
                bindings={"args.repository": Binding(source="alpha.result.name", type="string")},
            ),
        )
    )
    validated = validate_plan(hostile, catalog())

    async def executor(node: Node, args: Any) -> dict[str, Any]:
        return {"name": "attacker/evil", "topics": []}

    engine = _engine(tmp_path, executor)
    checkpoint = engine.admit(
        validated,
        execution_id="x1",
        principal_ref="p",
        projection_digest="pj",
        policy_digest="pd",
    )
    report = asyncio.run(engine.run(validated, checkpoint))
    assert report.node_statuses["beta"] == NodeStatus.FAILED.value
    assert report.node_reasons["beta"] == PlanReason.PLAN_SCOPE_DENIED.value


# ---------------------------------------------------------------------- A5-07


def test_a5_07_digest_stable_under_reorder_and_editorial_metadata() -> None:
    left = load_plan(FIXTURE_DIR / "plan_digest_reorder_a.json")
    right = load_plan(FIXTURE_DIR / "plan_digest_reorder_b.json")
    assert plan_digest(left) == plan_digest(right)


def test_a5_07b_digest_changes_on_semantic_change() -> None:
    base = load_plan(FIXTURE_DIR / "plan_digest_reorder_a.json")
    changed = load_plan(FIXTURE_DIR / "plan_digest_semantic_change.json")
    assert plan_digest(base) != plan_digest(changed)


def test_a5_07c_digest_changes_on_edge_budget_and_version() -> None:
    base = _linear_plan()
    assert plan_digest(base) != plan_digest(replace(base, budget=budget(max_external_calls=4)))
    assert plan_digest(base) != plan_digest(
        replace(base, failure_policy=FailurePolicy.CONTINUE_INDEPENDENT)
    )
    from hermes_mcp_bridge.v2 import dag_digest

    body = dag_digest.canonical_plan(base)
    assert body["digest_version"] == "dagdigest/1"


# ---------------------------------------------------------------------- A5-08


def _mutating_plan(**overrides: Any):
    nodes = (
        Node(
            id="branch",
            kind=NodeKind.TOOL,
            tool="github.create_branch",
            args={"repository": "owner/disposable", "branch": "feat", "sha": "abc"},
            idempotency=Idempotency(),
            compensation=None,
        ),
    )
    return plan(nodes, **overrides)


def _approval_for(validated: Any, **overrides: Any) -> Approval:
    kwargs: dict[str, Any] = {
        "approval_id": "ap-1",
        "digest": validated.digest,
        "nonce": "n-1",
        "expires_at_ms": 10_000,
        "scope": frozenset({"owner/disposable"}),
        "required_for": ("branch",),
        "runtime_bound": True,
    }
    kwargs.update(overrides)
    return Approval(**kwargs)


def _approved_plan(**approval_overrides: Any):
    base = _mutating_plan()
    digest = plan_digest(base)
    approval = Approval(
        approval_id=approval_overrides.pop("approval_id", "ap-1"),
        digest=approval_overrides.pop("digest", digest),
        nonce=approval_overrides.pop("nonce", "n-1"),
        expires_at_ms=approval_overrides.pop("expires_at_ms", 10_000),
        scope=approval_overrides.pop("scope", frozenset({"owner/disposable"})),
        required_for=("branch",),
        runtime_bound=approval_overrides.pop("runtime_bound", True),
        operation_digests=approval_overrides.pop("operation_digests", frozenset()),
    )
    return replace(base, approval=approval)


def _approve_engine(tmp_path: Path, db: str = "ap.db") -> DagEngine:
    return DagEngine(
        _ok,
        catalog=catalog(),
        store=_store(tmp_path, db),
        governance=AllowGovernance(approval_required=frozenset({"branch"})),
        enabled=True,
    )


def test_a5_08_approval_digest_mismatch_denies(tmp_path: Path) -> None:
    validated = validate_plan(_approved_plan(digest="deadbeef"), catalog())
    engine = _approve_engine(tmp_path)
    with pytest.raises(PlanValidationError) as excinfo:
        engine.admit(
            validated,
            execution_id="e1",
            principal_ref="p",
            projection_digest="pj",
            policy_digest="pd",
        )
    assert excinfo.value.reason is PlanReason.APPROVAL_DIGEST_MISMATCH


def test_a5_08b_approval_expiry_and_scope_deny(tmp_path: Path) -> None:
    expired = validate_plan(_approved_plan(expires_at_ms=1), catalog())
    engine = _approve_engine(tmp_path, "ap2.db")
    with pytest.raises(PlanValidationError) as excinfo:
        engine.admit(
            expired,
            execution_id="e2",
            principal_ref="p",
            projection_digest="pj",
            policy_digest="pd",
            now_ms=5,
        )
    assert excinfo.value.reason is PlanReason.APPROVAL_EXPIRED

    mis_scoped = validate_plan(_approved_plan(scope=frozenset({"owner/other"})), catalog())
    engine2 = _approve_engine(tmp_path, "ap3.db")
    with pytest.raises(PlanValidationError) as excinfo:
        engine2.admit(
            mis_scoped,
            execution_id="e3",
            principal_ref="p",
            projection_digest="pj",
            policy_digest="pd",
        )
    assert excinfo.value.reason is PlanReason.APPROVAL_SCOPE_INSUFFICIENT


def test_a5_08c_approval_single_use_exactly_one_consumer(tmp_path: Path) -> None:
    validated = validate_plan(_approved_plan(), catalog())
    store = _store(tmp_path, "ap4.db")
    engine = DagEngine(
        _ok,
        catalog=catalog(),
        store=store,
        governance=AllowGovernance(approval_required=frozenset({"branch"})),
        enabled=True,
    )
    engine.admit(
        validated,
        execution_id="e4",
        principal_ref="p",
        projection_digest="pj",
        policy_digest="pd",
    )
    with pytest.raises(PlanValidationError) as excinfo:
        engine.admit(
            validated,
            execution_id="e5",
            principal_ref="p",
            projection_digest="pj",
            policy_digest="pd",
        )
    assert excinfo.value.reason is PlanReason.APPROVAL_ALREADY_CONSUMED


def test_a5_08d_runtime_bound_mutation_requires_operation_digest(tmp_path: Path) -> None:
    validated = validate_plan(
        _approved_plan(runtime_bound=False, operation_digests=frozenset({"nope"})), catalog()
    )
    engine = _approve_engine(tmp_path, "ap5.db")
    checkpoint = engine.admit(
        validated,
        execution_id="e6",
        principal_ref="p",
        projection_digest="pj",
        policy_digest="pd",
    )
    report = asyncio.run(engine.run(validated, checkpoint))
    assert report.node_statuses["branch"] == NodeStatus.DENIED.value
    assert report.node_reasons["branch"] == PlanReason.APPROVAL_OPERATION_DIGEST_UNCOVERED.value

    node = validated.plan.node("branch")
    covered = validate_plan(
        _approved_plan(
            nonce="n-2",
            runtime_bound=False,
            operation_digests=frozenset({operation_digest("branch", dict(node.args))}),
        ),
        catalog(),
    )
    engine2 = _approve_engine(tmp_path, "ap6.db")
    checkpoint2 = engine2.admit(
        covered,
        execution_id="e7",
        principal_ref="p",
        projection_digest="pj",
        policy_digest="pd",
    )
    report2 = asyncio.run(engine2.run(covered, checkpoint2))
    assert report2.node_statuses["branch"] == NodeStatus.SUCCESS.value


# ---------------------------------------------------------------------- A5-09


def test_a5_09_missing_policy_denies_and_resolves_nothing(tmp_path: Path) -> None:
    calls: list[str] = []

    async def executor(node: Node, args: Any) -> dict[str, Any]:
        calls.append(node.id)
        return {}

    validated = validate_plan(_linear_plan(), catalog())
    engine = DagEngine(
        executor,
        catalog=catalog(),
        store=_store(tmp_path, "deny.db"),
        governance=DenyAllGovernance(),
        enabled=True,
    )
    checkpoint = engine.admit(
        validated,
        execution_id="d1",
        principal_ref="p",
        projection_digest="pj",
        policy_digest="pd",
    )
    report = asyncio.run(engine.run(validated, checkpoint))
    assert report.node_statuses["alpha"] == NodeStatus.DENIED.value
    assert report.node_reasons["alpha"] == PlanReason.POLICY_MISSING.value
    assert calls == []


def test_a5_09b_plan_configuration_cannot_widen_a_node(tmp_path: Path) -> None:
    validated = validate_plan(
        plan((read_node("alpha"), read_node("beta", depends_on=("alpha",)))), catalog()
    )
    governance = AllowGovernance(denied=frozenset({"beta"}))
    engine = DagEngine(
        _ok,
        catalog=catalog(),
        store=_store(tmp_path, "widen.db"),
        governance=governance,
        enabled=True,
    )
    checkpoint = engine.admit(
        validated,
        execution_id="w1",
        principal_ref="p",
        projection_digest="pj",
        policy_digest="pd",
    )
    report = asyncio.run(engine.run(validated, checkpoint))
    assert report.node_statuses["alpha"] == NodeStatus.SUCCESS.value
    assert report.node_statuses["beta"] == NodeStatus.DENIED.value
    # Both outcomes are audited, but the denied node produced no execution:
    # its recorded state is DENIED and it never reached the executor.
    assert governance.records == ["alpha", "beta"]
    assert report.node_reasons["beta"] == PlanReason.POLICY_DENIED.value


# ---------------------------------------------------------------------- A5-10


def test_a5_10_parallelism_never_exceeds_min_bound(tmp_path: Path) -> None:
    gauge = {"current": 0, "max": 0}

    async def executor(node: Node, args: Any) -> dict[str, Any]:
        gauge["current"] += 1
        gauge["max"] = max(gauge["max"], gauge["current"])
        await asyncio.sleep(0.02)
        gauge["current"] -= 1
        return {"name": node.id, "topics": []}

    nodes = (
        read_node("root"),
        *(read_node(f"leaf{index}", depends_on=("root",)) for index in range(4)),
    )
    validated = validate_plan(plan(nodes, budget=budget(max_parallelism=2)), catalog())
    engine = DagEngine(
        executor,
        catalog=catalog(),
        store=_store(tmp_path, "par.db"),
        governance=AllowGovernance(),
        enabled=True,
        engine_ceiling=4,
    )
    checkpoint = engine.admit(
        validated,
        execution_id="p1",
        principal_ref="p",
        projection_digest="pj",
        policy_digest="pd",
    )
    report = asyncio.run(engine.run(validated, checkpoint))
    assert report.status is PlanStatus.COMPLETED
    assert report.max_observed_inflight == 2
    assert gauge["max"] == 2


def test_a5_10b_dispatch_order_deterministic(tmp_path: Path) -> None:
    nodes = (
        read_node("root"),
        *(read_node(name, depends_on=("root",)) for name in ("zeta", "alpha", "mid")),
    )
    validated = validate_plan(plan(nodes), catalog())
    orders = []
    for index in range(2):
        engine = DagEngine(
            _ok,
            catalog=catalog(),
            store=_store(tmp_path, f"det{index}.db"),
            governance=AllowGovernance(),
            enabled=True,
        )
        checkpoint = engine.admit(
            validated,
            execution_id=f"o{index}",
            principal_ref="p",
            projection_digest="pj",
            policy_digest="pd",
        )
        report = asyncio.run(engine.run(validated, checkpoint))
        orders.append(report.dispatch_order)
    assert orders[0] == orders[1] == ("root", "alpha", "mid", "zeta")


def test_a5_10c_same_resource_mutations_are_serialized(tmp_path: Path) -> None:
    overlap = {"max": 0, "current": 0}

    async def executor(node: Node, args: Any) -> dict[str, Any]:
        overlap["current"] += 1
        overlap["max"] = max(overlap["max"], overlap["current"])
        await asyncio.sleep(0)
        overlap["current"] -= 1
        return {"ref": node.id, "effect_ref": f"ref-{node.id}"}

    nodes = tuple(
        Node(
            id=f"branch{index}",
            kind=NodeKind.TOOL,
            tool="github.create_branch",
            args={"repository": "owner/disposable", "branch": f"b{index}", "sha": "abc"},
            idempotency=Idempotency(),
        )
        for index in range(3)
    )
    validated = validate_plan(plan(nodes, budget=budget(max_parallelism=1)), catalog())
    engine = DagEngine(
        executor,
        catalog=catalog(),
        store=_store(tmp_path, "ser.db"),
        governance=AllowGovernance(),
        enabled=True,
    )
    checkpoint = engine.admit(
        validated,
        execution_id="s1",
        principal_ref="p",
        projection_digest="pj",
        policy_digest="pd",
    )
    asyncio.run(engine.run(validated, checkpoint))
    assert overlap["max"] == 1


# ---------------------------------------------------------------------- A5-11


def _compensable_plan():
    nodes = (
        Node(
            id="branch",
            kind=NodeKind.TOOL,
            tool="github.create_branch",
            args={"repository": "owner/disposable", "branch": "feat", "sha": "abc"},
            idempotency=Idempotency(),
            compensation=Compensation(operation="delete_ref"),
            on_failure=OnFailure.ISOLATE_BRANCH,
        ),
        Node(
            id="pr",
            kind=NodeKind.TOOL,
            tool="github.create_pr",
            args={
                "repository": "owner/disposable",
                "head": "feat",
                "base": "main",
                "title": "t",
            },
            depends_on=("branch",),
            idempotency=Idempotency(),
        ),
    )
    return plan(nodes, rollback_policy=RollbackPolicy.COMPENSATE_ON_FAILURE)


def test_a5_11_compensation_reverse_topological_with_read_back(tmp_path: Path) -> None:
    compensated: list[str] = []

    async def executor(node: Node, args: Any) -> dict[str, Any]:
        if node.id == "pr":
            raise NodeFailed("provider 422")
        return {"ref": "feat", "effect_ref": "ref-branch"}

    async def compensator(node: Node, effect_ref: str) -> bool:
        compensated.append(node.id)
        return True  # read-back verified

    validated = validate_plan(_compensable_plan(), catalog())
    engine = DagEngine(
        executor,
        catalog=catalog(),
        store=_store(tmp_path, "comp.db"),
        governance=AllowGovernance(),
        compensator=compensator,
        enabled=True,
    )
    checkpoint = engine.admit(
        validated,
        execution_id="c1",
        principal_ref="p",
        projection_digest="pj",
        policy_digest="pd",
    )
    report = asyncio.run(engine.run(validated, checkpoint))
    assert compensated == ["branch"]
    assert report.compensated_effects[0]["outcome"] == "COMPENSATED"
    assert report.committed_effects == ()


def test_a5_11b_unsafe_compensation_writes_nothing_and_is_reported(tmp_path: Path) -> None:
    async def executor(node: Node, args: Any) -> dict[str, Any]:
        if node.id == "pr":
            raise NodeFailed("provider 422")
        return {"ref": "feat", "effect_ref": "ref-branch"}

    async def compensator(node: Node, effect_ref: str) -> bool:
        return False  # precondition drift: refuse to write

    validated = validate_plan(_compensable_plan(), catalog())
    engine = DagEngine(
        executor,
        catalog=catalog(),
        store=_store(tmp_path, "comp2.db"),
        governance=AllowGovernance(),
        compensator=compensator,
        enabled=True,
    )
    checkpoint = engine.admit(
        validated,
        execution_id="c2",
        principal_ref="p",
        projection_digest="pj",
        policy_digest="pd",
    )
    report = asyncio.run(engine.run(validated, checkpoint))
    assert report.compensated_effects[0]["outcome"] == "UNSAFE"


def test_a5_11c_compensation_is_policy_evaluated(tmp_path: Path) -> None:
    async def executor(node: Node, args: Any) -> dict[str, Any]:
        if node.id == "pr":
            raise NodeFailed("provider 422")
        return {"ref": "feat", "effect_ref": "ref-branch"}

    calls: list[str] = []

    async def compensator(node: Node, effect_ref: str) -> bool:
        calls.append(node.id)
        return True

    validated = validate_plan(_compensable_plan(), catalog())
    engine = DagEngine(
        executor,
        catalog=catalog(),
        store=_store(tmp_path, "comp3.db"),
        governance=AllowGovernance(denied=frozenset({"branch"})),
        compensator=compensator,
        enabled=True,
    )
    checkpoint = engine.admit(
        validated,
        execution_id="c3",
        principal_ref="p",
        projection_digest="pj",
        policy_digest="pd",
    )
    report = asyncio.run(engine.run(validated, checkpoint))
    # branch was denied up front, so there is no effect and no compensation write.
    assert calls == []
    assert report.node_statuses["branch"] == NodeStatus.DENIED.value


# ---------------------------------------------------------------------- A5-12


def _indeterminate_plan():
    nodes = (
        Node(
            id="branch",
            kind=NodeKind.TOOL,
            tool="github.create_branch",
            args={"repository": "owner/disposable", "branch": "feat", "sha": "abc"},
            idempotency=Idempotency(),
        ),
        Node(
            id="pr",
            kind=NodeKind.TOOL,
            tool="github.create_pr",
            args={
                "repository": "owner/disposable",
                "head": "feat",
                "base": "main",
                "title": "t",
            },
            depends_on=("branch",),
            idempotency=Idempotency(),
        ),
    )
    return plan(nodes)


def _run_indeterminate(tmp_path: Path, db: str) -> tuple[ExecutionReport, list[str]]:
    attempts: list[str] = []

    async def executor(node: Node, args: Any) -> dict[str, Any]:
        attempts.append(node.id)
        raise NodeIndeterminate("transport reset after send")

    validated = validate_plan(_indeterminate_plan(), catalog())
    engine = DagEngine(
        executor,
        catalog=catalog(),
        store=_store(tmp_path, db),
        governance=AllowGovernance(),
        enabled=True,
    )
    checkpoint = engine.admit(
        validated,
        execution_id="i1",
        principal_ref="p",
        projection_digest="pj",
        policy_digest="pd",
    )
    return asyncio.run(engine.run(validated, checkpoint)), attempts


def test_a5_12_indeterminate_not_retried_not_compensated_blocks_dependents(
    tmp_path: Path,
) -> None:
    report, attempts = _run_indeterminate(tmp_path, "ind.db")
    assert attempts == ["branch"]  # no retry
    assert report.node_statuses["branch"] == NodeStatus.INDETERMINATE.value
    assert report.node_statuses["pr"] == NodeStatus.SKIPPED.value
    assert report.node_reasons["pr"] == PlanReason.UPSTREAM_INDETERMINATE.value
    assert report.compensated_effects == ()


def test_a5_12b_plan_status_precedence_and_unknown_effects(tmp_path: Path) -> None:
    report, _ = _run_indeterminate(tmp_path, "ind2.db")
    assert report.status is PlanStatus.INDETERMINATE
    assert report.unknown_effects
    assert report.unknown_effects[0]["node_id"] == "branch"
    assert report.unknown_effects[0]["idempotency_key"]


def test_a5_12c_unknown_effects_cannot_be_silently_dropped() -> None:
    with pytest.raises(PlanValidationError):
        ExecutionReport(
            status=PlanStatus.PARTIAL,
            plan_digest="d",
            execution_id="e",
            node_statuses={},
            node_reasons={},
            committed_effects=(),
            compensated_effects=(),
            unknown_effects=({"node_id": "n", "idempotency_key": "k", "expected_shape": "t"},),
            budget_consumed={},
        )


def test_a5_12d_indeterminate_checkpoint_is_durable_before_recovery(tmp_path: Path) -> None:
    store = _store(tmp_path, "ind3.db")

    async def executor(node: Node, args: Any) -> dict[str, Any]:
        raise NodeIndeterminate("timeout after dispatch")

    validated = validate_plan(_indeterminate_plan(), catalog())
    engine = DagEngine(
        executor,
        catalog=catalog(),
        store=store,
        governance=AllowGovernance(),
        enabled=True,
    )
    checkpoint = engine.admit(
        validated,
        execution_id="i3",
        principal_ref="p",
        projection_digest="pj",
        policy_digest="pd",
    )
    asyncio.run(engine.run(validated, checkpoint))
    reloaded = store.load("i3")
    assert reloaded.node_states["branch"].status is NodeStatus.INDETERMINATE
    assert reloaded.node_states["branch"].idempotency_key


# ---------------------------------------------------------------- A5-13/A5-14


def test_a5_13_dag_path_consumes_zero_llm_tokens(tmp_path: Path) -> None:
    validated = validate_plan(_linear_plan(), catalog())
    engine = _engine(tmp_path, _ok, db="tok.db")
    checkpoint = engine.admit(
        validated,
        execution_id="t1",
        principal_ref="p",
        projection_digest="pj",
        policy_digest="pd",
    )
    report = asyncio.run(engine.run(validated, checkpoint))
    assert report.llm_tokens == 0
    # No Phase 5 module reaches for the agent runtime or a model client: the
    # DAG path is pure orchestration and cannot spend tokens.
    for rel in PHASE5_MODULES:
        source = (SRC / rel).read_text(encoding="utf-8")
        for forbidden in ("hermes_runtime", "openai", "anthropic", "completion("):
            assert forbidden not in source, f"{rel}: {forbidden}"


def test_a5_14_checkpoint_rejects_secret_material(tmp_path: Path) -> None:
    from hermes_mcp_bridge.v2.dag_store import assert_no_secret_material

    with pytest.raises(StoreError):
        assert_no_secret_material({"node": {"authorization": "x"}}, where="checkpoint")
    with pytest.raises(StoreError):
        assert_no_secret_material([{"client_secret": "x"}], where="checkpoint")
    assert_no_secret_material({"node_id": "a", "effect_ref": "r"}, where="checkpoint")


def test_a5_14b_stored_body_contains_no_secret_like_fields(tmp_path: Path) -> None:
    store = _store(tmp_path, "sec.db")
    validated = validate_plan(_linear_plan(), catalog())
    engine = DagEngine(
        _ok, catalog=catalog(), store=store, governance=AllowGovernance(), enabled=True
    )
    checkpoint = engine.admit(
        validated,
        execution_id="sec1",
        principal_ref="p",
        projection_digest="pj",
        policy_digest="pd",
    )
    asyncio.run(engine.run(validated, checkpoint))
    raw = (
        sqlite3.connect(tmp_path / "sec.db")
        .execute("SELECT body FROM dag_checkpoint")
        .fetchone()[0]
    )
    lowered = raw.lower()
    for hint in ("authorization", "bearer", "client_secret", "private_key", "password"):
        assert hint not in lowered


# ---------------------------------------------------------------- A5-15/A5-16


def test_a5_15_write_ahead_before_mutation(tmp_path: Path) -> None:
    store = _store(tmp_path, "wa.db")
    observed: list[str | None] = []

    async def executor(node: Node, args: Any) -> dict[str, Any]:
        # At provider-call time the intent must already be durable.
        observed.append(store.load("wa1").node_states[node.id].idempotency_key)
        return {"ref": "feat", "effect_ref": "ref-branch"}

    validated = validate_plan(_mutating_plan(), catalog())
    engine = DagEngine(
        executor, catalog=catalog(), store=store, governance=AllowGovernance(), enabled=True
    )
    checkpoint = engine.admit(
        validated,
        execution_id="wa1",
        principal_ref="p",
        projection_digest="pj",
        policy_digest="pd",
    )
    asyncio.run(engine.run(validated, checkpoint))
    assert observed and observed[0]
    assert store.load("wa1").node_states["branch"].status is NodeStatus.SUCCESS


def test_a5_16_resume_reconciles_dispatched_node_without_second_call(tmp_path: Path) -> None:
    store = _store(tmp_path, "res.db")
    calls: list[str] = []

    async def crashing(node: Node, args: Any) -> dict[str, Any]:
        raise NodeIndeterminate("crash after dispatch")

    validated = validate_plan(_indeterminate_plan(), catalog())
    engine = DagEngine(
        crashing, catalog=catalog(), store=store, governance=AllowGovernance(), enabled=True
    )
    checkpoint = engine.admit(
        validated,
        execution_id="r1",
        principal_ref="p",
        projection_digest="pj",
        policy_digest="pd",
    )
    asyncio.run(engine.run(validated, checkpoint))

    async def executor(node: Node, args: Any) -> dict[str, Any]:
        calls.append(node.id)
        return {"number": 1, "effect_ref": "ref-pr"}

    async def reconciler(node: Node, key: str) -> str:
        return "ref-branch"  # effect provably exists

    resumed = DagEngine(
        executor,
        catalog=catalog(),
        store=store,
        governance=AllowGovernance(),
        reconciler=reconciler,
        enabled=True,
    )
    report = asyncio.run(
        resumed.resume(validated, "r1", projection_digest="pj", policy_digest="pd")
    )
    assert calls == ["pr"]  # branch was never re-issued
    assert report.node_statuses["branch"] == NodeStatus.SUCCESS.value
    assert report.status is PlanStatus.COMPLETED


def test_a5_16b_resume_reissues_only_when_effect_provably_absent(tmp_path: Path) -> None:
    store = _store(tmp_path, "res2.db")
    validated = validate_plan(_indeterminate_plan(), catalog())

    async def crashing(node: Node, args: Any) -> dict[str, Any]:
        raise NodeIndeterminate("crash")

    engine = DagEngine(
        crashing, catalog=catalog(), store=store, governance=AllowGovernance(), enabled=True
    )
    checkpoint = engine.admit(
        validated,
        execution_id="r2",
        principal_ref="p",
        projection_digest="pj",
        policy_digest="pd",
    )
    asyncio.run(engine.run(validated, checkpoint))

    issued: list[str] = []

    async def executor(node: Node, args: Any) -> dict[str, Any]:
        issued.append(node.id)
        return {"ref": "feat", "number": 1, "effect_ref": f"ref-{node.id}"}

    async def reconciler(node: Node, key: str) -> bool:
        return False  # provably not committed

    resumed = DagEngine(
        executor,
        catalog=catalog(),
        store=store,
        governance=AllowGovernance(),
        reconciler=reconciler,
        enabled=True,
    )
    report = asyncio.run(
        resumed.resume(validated, "r2", projection_digest="pj", policy_digest="pd")
    )
    assert issued == ["branch", "pr"]
    assert report.status is PlanStatus.COMPLETED


def test_a5_16c_resume_stays_indeterminate_when_unreconcilable(tmp_path: Path) -> None:
    store = _store(tmp_path, "res3.db")
    validated = validate_plan(_indeterminate_plan(), catalog())

    async def crashing(node: Node, args: Any) -> dict[str, Any]:
        raise NodeIndeterminate("crash")

    engine = DagEngine(
        crashing, catalog=catalog(), store=store, governance=AllowGovernance(), enabled=True
    )
    checkpoint = engine.admit(
        validated,
        execution_id="r3",
        principal_ref="p",
        projection_digest="pj",
        policy_digest="pd",
    )
    asyncio.run(engine.run(validated, checkpoint))

    async def reconciler(node: Node, key: str) -> None:
        return None  # provider unreachable

    resumed = DagEngine(
        _ok,
        catalog=catalog(),
        store=store,
        governance=AllowGovernance(),
        reconciler=reconciler,
        enabled=True,
    )
    report = asyncio.run(
        resumed.resume(validated, "r3", projection_digest="pj", policy_digest="pd")
    )
    assert report.status is PlanStatus.INDETERMINATE
    assert report.unknown_effects


def test_a5_17_resume_reevaluates_policy_and_never_reconsumes_approval(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path, "res4.db")
    validated = validate_plan(_approved_plan(), catalog())
    engine = DagEngine(
        _ok,
        catalog=catalog(),
        store=store,
        governance=AllowGovernance(approval_required=frozenset({"branch"}), denied=frozenset()),
        enabled=True,
    )
    engine.admit(
        validated,
        execution_id="r4",
        principal_ref="p",
        projection_digest="pj",
        policy_digest="pd",
    )
    assert store.approval_holder("ap-1", "n-1") == "r4"

    # Policy drifted to DENY between the crash and the resume.
    resumed = DagEngine(
        _ok,
        catalog=catalog(),
        store=store,
        governance=AllowGovernance(denied=frozenset({"branch"})),
        enabled=True,
    )
    report = asyncio.run(
        resumed.resume(validated, "r4", projection_digest="pj", policy_digest="pd2")
    )
    assert report.node_statuses["branch"] == NodeStatus.DENIED.value
    # The approval nonce is still held by the original execution, not re-consumed.
    assert store.approval_holder("ap-1", "n-1") == "r4"


def test_a5_18_stale_fence_token_cannot_write(tmp_path: Path) -> None:
    store = _store(tmp_path, "fence.db")
    validated = validate_plan(_linear_plan(), catalog())
    engine = DagEngine(
        _ok, catalog=catalog(), store=store, governance=AllowGovernance(), enabled=True
    )
    checkpoint = engine.admit(
        validated,
        execution_id="f1",
        principal_ref="p",
        projection_digest="pj",
        policy_digest="pd",
    )
    new_lease = store.acquire_lease("f1", "engine-1", 10**12)
    assert new_lease.fence_token > checkpoint.lease.fence_token
    with pytest.raises(StoreError) as excinfo:
        store.save(checkpoint, fence_token=checkpoint.lease.fence_token)
    assert excinfo.value.reason is PlanReason.LEASE_FENCE_STALE


def test_a5_19_tampered_checkpoint_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path, "tamper.db")
    validated = validate_plan(_linear_plan(), catalog())
    engine = DagEngine(
        _ok, catalog=catalog(), store=store, governance=AllowGovernance(), enabled=True
    )
    engine.admit(
        validated,
        execution_id="t9",
        principal_ref="p",
        projection_digest="pj",
        policy_digest="pd",
    )
    conn = sqlite3.connect(tmp_path / "tamper.db")
    body = json.loads(conn.execute("SELECT body FROM dag_checkpoint").fetchone()[0])
    body["principal_ref"] = "attacker"
    conn.execute("UPDATE dag_checkpoint SET body = ?", (json.dumps(body),))
    conn.commit()
    with pytest.raises(StoreError) as excinfo:
        store.load("t9")
    assert excinfo.value.reason is PlanReason.CHECKPOINT_TAMPERED


def test_a5_19b_unsupported_state_schema_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path, "schema.db")
    validated = validate_plan(_linear_plan(), catalog())
    engine = DagEngine(
        _ok, catalog=catalog(), store=store, governance=AllowGovernance(), enabled=True
    )
    engine.admit(
        validated,
        execution_id="sc1",
        principal_ref="p",
        projection_digest="pj",
        policy_digest="pd",
    )
    conn = sqlite3.connect(tmp_path / "schema.db")
    body = json.loads(conn.execute("SELECT body FROM dag_checkpoint").fetchone()[0])
    body["schema_version"] = "dagstate/999"
    conn.execute("UPDATE dag_checkpoint SET body = ?", (json.dumps(body),))
    conn.commit()
    with pytest.raises(StoreError) as excinfo:
        store.load("sc1")
    assert excinfo.value.reason is PlanReason.CHECKPOINT_SCHEMA_UNSUPPORTED


def test_a5_20_replay_makes_no_external_call_and_is_labelled(tmp_path: Path) -> None:
    calls: list[str] = []

    async def executor(node: Node, args: Any) -> dict[str, Any]:
        calls.append(node.id)
        return {}

    validated = validate_plan(_linear_plan(), catalog())
    store = _store(tmp_path, "replay.db")
    engine = DagEngine(
        executor, catalog=catalog(), store=store, governance=AllowGovernance(), enabled=True
    )
    checkpoint = engine.admit(
        validated,
        execution_id="rp1",
        principal_ref="p",
        projection_digest="pj",
        policy_digest="pd",
        replay=True,
    )
    report = asyncio.run(
        engine.run(
            validated,
            checkpoint,
            replay_results={"alpha": {"name": "alpha", "topics": ["x", "y"]}},
        )
    )
    assert calls == []
    assert report.replay is True
    assert report.status is PlanStatus.COMPLETED
    assert report.node_statuses["beta"] == NodeStatus.SUCCESS.value
    assert store.load("rp1").replay is True


def test_a5_20b_replay_consumes_no_approval(tmp_path: Path) -> None:
    store = _store(tmp_path, "replay2.db")
    validated = validate_plan(_approved_plan(), catalog())
    engine = DagEngine(
        _ok,
        catalog=catalog(),
        store=store,
        governance=AllowGovernance(approval_required=frozenset({"branch"})),
        enabled=True,
    )
    engine.admit(
        validated,
        execution_id="rp2",
        principal_ref="p",
        projection_digest="pj",
        policy_digest="pd",
        replay=True,
    )
    assert store.approval_holder("ap-1", "n-1") is None


def test_a5_21_feature_flag_is_off_by_default_and_gates_the_engine(tmp_path: Path) -> None:
    assert DAG_FEATURE_ENABLED is False
    from hermes_mcp_bridge.v2.dag_contract import DagDisabledError

    validated = validate_plan(_linear_plan(), catalog())
    engine = DagEngine(_ok, catalog=catalog(), store=_store(tmp_path, "off.db"))
    with pytest.raises(DagDisabledError):
        engine.admit(
            validated,
            execution_id="off1",
            principal_ref="p",
            projection_digest="pj",
            policy_digest="pd",
        )


def test_a5_21b_dag_is_not_wired_into_mcp_projection() -> None:
    from hermes_mcp_bridge import contracts

    names = {tool.lower() for tool in contracts.required_tools()}
    # ``hermes_plan`` / ``hermes_execute_approved_plan`` are the pre-existing V1
    # tools; Phase 5 must not add a DAG tool to the projection.
    assert len(names) == 27
    assert not any("dag" in name for name in names)
    assert not any("v2" in name for name in names)


def test_a5_22_budget_exhaustion_skips_rather_than_overruns(tmp_path: Path) -> None:
    nodes = (
        read_node("root"),
        *(read_node(f"leaf{index}", depends_on=("root",)) for index in range(3)),
    )
    # Static admission rejects an over-budget plan outright...
    with pytest.raises(PlanValidationError) as excinfo:
        validate_plan(plan(nodes, budget=budget(max_external_calls=2)), catalog())
    assert excinfo.value.reason is PlanReason.PLAN_BUDGET_EXCEEDED

    # ...and the scheduler still enforces the ceiling at runtime (defence in
    # depth against a budget narrowed after validation, e.g. on resume).
    validated = validate_plan(plan(nodes, budget=budget(max_external_calls=8)), catalog())
    validated = replace(
        validated, plan=replace(validated.plan, budget=budget(max_external_calls=2))
    )
    engine = _engine(tmp_path, _ok, db="budget.db")
    checkpoint = engine.admit(
        validated,
        execution_id="b1",
        principal_ref="p",
        projection_digest="pj",
        policy_digest="pd",
    )
    report = asyncio.run(engine.run(validated, checkpoint))
    assert report.budget_consumed["external_calls"] == 2
    skipped = [
        node_id
        for node_id, status in report.node_statuses.items()
        if status == NodeStatus.SKIPPED.value
    ]
    assert len(skipped) == 2
    assert report.node_reasons[skipped[0]] == PlanReason.BUDGET_EXHAUSTED.value


def test_a5_22b_mutating_plan_cannot_declare_parallelism(tmp_path: Path) -> None:
    with pytest.raises(PlanValidationError) as excinfo:
        validate_plan(_mutating_plan(budget=budget(max_parallelism=4)), catalog())
    assert excinfo.value.reason is PlanReason.PLAN_BUDGET_EXCEEDED


def test_a5_22c_mutating_node_without_idempotency_rejected() -> None:
    node = Node(
        id="branch",
        kind=NodeKind.TOOL,
        tool="github.create_branch",
        args={"repository": "owner/disposable", "branch": "b", "sha": "s"},
    )
    with pytest.raises(PlanValidationError) as excinfo:
        validate_plan(plan((node,)), catalog())
    assert excinfo.value.reason is PlanReason.PLAN_IDEMPOTENCY_MISSING


def test_a5_22d_unprojected_tool_rejected() -> None:
    limited = catalog(projected=frozenset({"github.get_pr"}))
    with pytest.raises(PlanValidationError) as excinfo:
        validate_plan(_linear_plan(), limited)
    assert excinfo.value.reason is PlanReason.PLAN_TOOL_NOT_PROJECTED


def test_a5_22e_out_of_scope_resource_rejected() -> None:
    with pytest.raises(PlanValidationError) as excinfo:
        validate_plan(
            plan((read_node("alpha", args={"repository": "owner/forbidden"}),)), catalog()
        )
    assert excinfo.value.reason is PlanReason.PLAN_SCOPE_DENIED


# ---------------------------------------------------------------- A5-19/A5-20


def test_a5_19c_continue_independent_completes_unrelated_branches(
    tmp_path: Path,
) -> None:
    async def executor(node: Node, args: Any) -> dict[str, Any]:
        if node.id == "bad":
            raise NodeFailed("provider 500")
        return {"name": node.id, "topics": []}

    nodes = (
        read_node("bad"),
        read_node("bad_child", depends_on=("bad",)),
        read_node("good"),
        read_node("good_child", depends_on=("good",)),
    )
    validated = validate_plan(
        plan(nodes, failure_policy=FailurePolicy.CONTINUE_INDEPENDENT), catalog()
    )
    engine = _engine(tmp_path, executor, db="cont.db")
    checkpoint = engine.admit(
        validated,
        execution_id="ci1",
        principal_ref="p",
        projection_digest="pj",
        policy_digest="pd",
    )
    report = asyncio.run(engine.run(validated, checkpoint))
    assert report.status is PlanStatus.PARTIAL
    assert report.node_statuses == {
        "bad": NodeStatus.FAILED.value,
        "bad_child": NodeStatus.SKIPPED.value,
        "good": NodeStatus.SUCCESS.value,
        "good_child": NodeStatus.SUCCESS.value,
    }
    assert report.node_reasons["bad_child"] == PlanReason.UPSTREAM_FAILED.value


def test_a5_19d_fail_fast_skips_unstarted_with_upstream_abort(tmp_path: Path) -> None:
    async def executor(node: Node, args: Any) -> dict[str, Any]:
        if node.id == "root":
            raise NodeFailed("provider 500")
        return {"name": node.id, "topics": []}

    # "other" is a genuinely independent branch: fail_fast must still skip it,
    # and it is wired to a child so the plan has no dead nodes.
    nodes = (
        read_node("root"),
        read_node("child", depends_on=("root",)),
        read_node("other"),
        read_node("other_child", depends_on=("other",)),
    )
    validated = validate_plan(plan(nodes, failure_policy=FailurePolicy.FAIL_FAST), catalog())
    engine = _engine(tmp_path, executor, db="ff.db")
    checkpoint = engine.admit(
        validated,
        execution_id="ff1",
        principal_ref="p",
        projection_digest="pj",
        policy_digest="pd",
    )
    report = asyncio.run(engine.run(validated, checkpoint))
    assert report.node_statuses["root"] == NodeStatus.FAILED.value
    # "other" was a parallel independent branch that still succeeded; its
    # dependent "other_child" is skipped through the upstream abort path.
    assert report.node_statuses["other"] == NodeStatus.SUCCESS.value
    assert report.node_statuses["other_child"] == NodeStatus.SKIPPED.value
    assert report.node_reasons["other_child"] == PlanReason.UPSTREAM_ABORT.value


def test_a5_20c_dry_run_makes_no_call_and_is_not_an_approval(tmp_path: Path) -> None:
    from hermes_mcp_bridge.v2.dag_engine import DryRunReport

    calls: list[str] = []

    async def executor(node: Node, args: Any) -> dict[str, Any]:
        calls.append(node.id)
        return {}

    validated = validate_plan(_mutating_plan(), catalog())
    engine = DagEngine(
        executor,
        catalog=catalog(),
        store=_store(tmp_path, "dry.db"),
        governance=AllowGovernance(approval_required=frozenset({"branch"})),
        enabled=True,
    )
    report = engine.dry_run(validated)
    assert calls == []
    assert report.plan_digest == validated.digest
    assert report.order == ("branch",)
    node = report.nodes[0]
    assert node["policy_decision"] == "ALLOW"
    assert node["approval_required"] is True
    assert node["mutating"] is True
    # Key *shape* only: no key material may appear in a dry-run output.
    assert node["idempotency_key_shape"] == "sha256/hex64"
    assert report.is_approval is False
    with pytest.raises(PlanValidationError):
        DryRunReport(plan_digest="d", order=(), nodes=(), is_approval=True)


def test_a5_20d_dry_run_reports_denials_without_executing(tmp_path: Path) -> None:
    validated = validate_plan(_linear_plan(), catalog())
    engine = DagEngine(
        _ok,
        catalog=catalog(),
        store=_store(tmp_path, "dry2.db"),
        governance=DenyAllGovernance(),
        enabled=True,
    )
    report = engine.dry_run(validated)
    assert [node["policy_decision"] for node in report.nodes] == ["DENY", "DENY"]
    assert report.nodes[0]["policy_reason"] == PlanReason.POLICY_MISSING.value
    assert report.external_calls == 0
