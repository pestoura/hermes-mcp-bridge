"""Phase 5 static DAG validation — total, pure and fail-closed.

> **V2 · PHASE 5 · runtime**

Validation completes **before** any credential resolution, any policy-approved
dispatch and any external call. A rejected plan therefore produces zero broker
calls and zero HTTP requests, the same zero-side-effect property Phase 2 proves
for out-of-scope reads.

Ordering (``docs/v2/phase5/dag-validation.md``):

``1`` shape → ``2`` identity → ``3`` registry → ``4`` projection → ``5`` scope →
``6`` graph → ``7`` bindings → ``8`` transforms → ``9`` budget →
``10`` policy → ``11`` approval → ``12`` idempotency → ``13`` digest.

Steps 1-9 are pure and live here together with 12-13; steps 10-11 are delegated
to the injected governance callbacks so the Phase 1 policy engine is used
unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .dag_contract import (
    DAG_MAX_DEPTH,
    DAG_MAX_FANOUT,
    DAG_MAX_NODE_TIMEOUT_MS,
    DAG_MAX_PARALLELISM,
    DAG_MAX_PARALLELISM_MUTATION,
    Node,
    NodeKind,
    PlanDefinition,
    PlanReason,
    PlanValidationError,
)
from .dag_digest import plan_digest
from .dag_transform import (
    TYPE_ANY,
    transform_output_type,
    type_of,
    validate_transform_shape,
)


@dataclass(frozen=True, slots=True)
class ToolContract:
    """The registry facts Phase 5 needs about one typed tool."""

    tool_id: str
    arg_types: Mapping[str, str]
    result_types: Mapping[str, str]
    mutating: bool = False
    provider: str = "github"
    credential_capability_id: str = "github.read"
    resource_arg: str | None = "repository"
    compensations: tuple[str, ...] = ()


@runtime_checkable
class ToolCatalog(Protocol):
    """Read-only view over the caller's projected registry."""

    def contract(self, tool_id: str) -> ToolContract | None: ...

    def is_projected(self, tool_id: str) -> bool: ...

    def in_scope(self, resource: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class StaticToolCatalog:
    """Deterministic in-memory catalog. Absence is denial, never a default."""

    contracts: Mapping[str, ToolContract]
    projected: frozenset[str]
    scope: frozenset[str]

    def contract(self, tool_id: str) -> ToolContract | None:
        return self.contracts.get(tool_id)

    def is_projected(self, tool_id: str) -> bool:
        return tool_id in self.projected

    def in_scope(self, resource: str) -> bool:
        return resource in self.scope


@dataclass(frozen=True, slots=True)
class ValidatedPlan:
    """Result of successful static validation."""

    plan: PlanDefinition
    digest: str
    order: tuple[str, ...]
    ranks: Mapping[str, int]
    edges: frozenset[tuple[str, str]]
    resource_keys: Mapping[str, str | None]
    mutating_nodes: frozenset[str]
    result_types: Mapping[str, Mapping[str, str]] = field(default_factory=dict)


def _fail(reason: PlanReason, detail: str) -> None:
    raise PlanValidationError(reason, detail)


def _declared_edges(plan: PlanDefinition) -> set[tuple[str, str]]:
    edges: set[tuple[str, str]] = set()
    known = set(plan.node_ids)
    for node in plan.nodes:
        for dep in node.depends_on:
            if dep not in known:
                _fail(PlanReason.PLAN_UNKNOWN_DEPENDENCY, f"{node.id}->{dep}")
            edges.add((dep, node.id))
    return edges


def _binding_edges(plan: PlanDefinition) -> set[tuple[str, str]]:
    edges: set[tuple[str, str]] = set()
    known = set(plan.node_ids)
    for node in plan.nodes:
        for binding in node.bindings.values():
            source = binding.source_node
            if source not in known:
                _fail(PlanReason.PLAN_UNKNOWN_DEPENDENCY, f"{node.id}<-{source}")
            if source == node.id:
                _fail(PlanReason.PLAN_SELF_DEPENDENCY, node.id)
            edges.add((source, node.id))
    return edges


def topological_order(
    node_ids: Sequence[str], edges: frozenset[tuple[str, str]]
) -> tuple[tuple[str, ...], dict[str, int]]:
    """Kahn ordering with a deterministic ``(rank, node_id)`` tie-break."""
    indegree = {node_id: 0 for node_id in node_ids}
    successors: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    for src, dst in edges:
        indegree[dst] += 1
        successors[src].append(dst)
    ranks: dict[str, int] = {}
    order: list[str] = []
    ready = sorted(node_id for node_id, degree in indegree.items() if degree == 0)
    while ready:
        current = ready.pop(0)
        parents = [src for src, dst in edges if dst == current]
        ranks[current] = 0 if not parents else max(ranks[src] for src in parents) + 1
        order.append(current)
        for successor in sorted(successors[current]):
            indegree[successor] -= 1
            if indegree[successor] == 0:
                ready.append(successor)
        ready.sort()
    if len(order) != len(node_ids):
        remaining = sorted(set(node_ids) - set(order))
        _fail(PlanReason.PLAN_CYCLE_DETECTED, ",".join(remaining))
    return tuple(order), ranks


def _check_graph_shape(
    plan: PlanDefinition, edges: frozenset[tuple[str, str]], ranks: Mapping[str, int]
) -> None:
    if ranks and max(ranks.values()) + 1 > DAG_MAX_DEPTH:
        _fail(PlanReason.PLAN_DEPTH_EXCEEDED, "graph depth")
    fanout: dict[str, int] = {}
    for src, _dst in edges:
        fanout[src] = fanout.get(src, 0) + 1
    for src, count in sorted(fanout.items()):
        if count > DAG_MAX_FANOUT:
            _fail(PlanReason.PLAN_FANOUT_EXCEEDED, src)
    if len(plan.nodes) > 1 and edges:
        # A plan with no edges at all is a legitimate independent fan-out. Once
        # *some* node is wired, an isolated node is dead weight and hides intent.
        consumed = {src for src, _ in edges}
        depended = {dst for _, dst in edges}
        for node in plan.nodes:
            if node.id not in consumed and node.id not in depended:
                # Dead node: no producer, no consumer. Hides intent.
                _fail(PlanReason.PLAN_UNREACHABLE_NODE, node.id)


def _tool_result_types(catalog: ToolCatalog, node: Node) -> Mapping[str, str]:
    contract = catalog.contract(str(node.tool))
    if contract is None:  # pragma: no cover - checked earlier
        _fail(PlanReason.PLAN_TOOL_UNKNOWN, str(node.tool))
    return contract.result_types  # type: ignore[union-attr]


def _resolve_source_type(
    plan: PlanDefinition,
    catalog: ToolCatalog,
    binding_source_node: str,
    path: tuple[str, ...],
) -> str:
    source = plan.node(binding_source_node)
    if source.kind is NodeKind.TRANSFORM:
        if path:
            # Transform outputs are opaque beyond their declared type.
            _fail(PlanReason.BINDING_SOURCE_UNSHAPED, binding_source_node)
        return transform_output_type(str(source.op))
    if not path:
        return "object"
    dotted = ".".join(path)
    result_types = _tool_result_types(catalog, source)
    if dotted not in result_types:
        _fail(PlanReason.BINDING_FIELD_UNKNOWN, f"{binding_source_node}.{dotted}")
    return result_types[dotted]


def _check_bindings(plan: PlanDefinition, catalog: ToolCatalog) -> None:
    for node in plan.nodes:
        declared = set(node.depends_on)
        for target, binding in sorted(node.bindings.items()):
            if binding.source_node not in declared:
                _fail(PlanReason.BINDING_EDGE_NOT_DECLARED, f"{node.id}:{target}")
            source_type = _resolve_source_type(
                plan, catalog, binding.source_node, binding.source_path
            )
            if source_type != TYPE_ANY and binding.type != source_type:
                _fail(PlanReason.BINDING_TYPE_MISMATCH, f"{node.id}:{target}")
            slot = target.split(".", 1)[1]
            if node.kind is NodeKind.TOOL:
                contract = catalog.contract(str(node.tool))
                assert contract is not None
                expected = contract.arg_types.get(slot)
                if expected is None:
                    _fail(PlanReason.BINDING_FIELD_UNKNOWN, f"{node.id}:{target}")
                if expected != TYPE_ANY and expected != binding.type:
                    _fail(PlanReason.BINDING_TARGET_TYPE_MISMATCH, f"{node.id}:{target}")
            if slot in node.args:
                _fail(
                    PlanReason.PLAN_ARG_INVALID,
                    f"{node.id}:{target} duplicates a literal",
                )


def _check_nodes(
    plan: PlanDefinition, catalog: ToolCatalog
) -> tuple[dict[str, str | None], set[str]]:
    resources: dict[str, str | None] = {}
    mutating: set[str] = set()
    for node in plan.nodes:
        if node.kind is NodeKind.TRANSFORM:
            slots = sorted({*node.args, *(t.split(".", 1)[1] for t in node.bindings)})
            validate_transform_shape(str(node.op), slots)
            resources[node.id] = None
            if node.compensation is not None:
                _fail(PlanReason.PLAN_COMPENSATION_UNDECLARED, node.id)
            continue
        tool_id = str(node.tool)
        contract = catalog.contract(tool_id)
        if contract is None:
            _fail(PlanReason.PLAN_TOOL_UNKNOWN, tool_id)
        assert contract is not None
        if not catalog.is_projected(tool_id):
            _fail(PlanReason.PLAN_TOOL_NOT_PROJECTED, tool_id)
        bound_slots = {t.split(".", 1)[1] for t in node.bindings}
        for name, value in sorted(node.args.items()):
            expected = contract.arg_types.get(name)
            if expected is None:
                _fail(PlanReason.PLAN_ARG_INVALID, f"{node.id}:{name}")
            if expected != TYPE_ANY and type_of(value) != expected:
                _fail(PlanReason.PLAN_ARG_INVALID, f"{node.id}:{name}")
        missing = sorted(set(contract.arg_types) - set(node.args) - bound_slots)
        if missing:
            _fail(PlanReason.PLAN_ARG_INVALID, f"{node.id}: missing {','.join(missing)}")
        resource = None
        if contract.resource_arg is not None:
            literal = node.args.get(contract.resource_arg)
            if isinstance(literal, str) and not catalog.in_scope(literal):
                _fail(PlanReason.PLAN_SCOPE_DENIED, node.id)
            resource = literal if isinstance(literal, str) else None
        resources[node.id] = resource
        if contract.mutating:
            mutating.add(node.id)
            if node.idempotency is None or not node.idempotency.enabled:
                _fail(PlanReason.PLAN_IDEMPOTENCY_MISSING, node.id)
        if (
            node.compensation is not None
            and node.compensation.operation not in contract.compensations
        ):
            _fail(PlanReason.PLAN_COMPENSATION_UNDECLARED, node.id)
    return resources, mutating


def _check_budget(plan: PlanDefinition, mutating: set[str]) -> None:
    tool_nodes = [node for node in plan.nodes if node.kind is NodeKind.TOOL]
    if len(tool_nodes) > plan.budget.max_external_calls:
        _fail(PlanReason.PLAN_BUDGET_EXCEEDED, "external calls")
    if plan.deadline_ms > plan.budget.max_total_wall_ms:
        _fail(PlanReason.PLAN_BUDGET_EXCEEDED, "deadline exceeds wall budget")
    for node in plan.nodes:
        if node.timeout_ms > DAG_MAX_NODE_TIMEOUT_MS:
            _fail(PlanReason.PLAN_BUDGET_EXCEEDED, f"{node.id}: node timeout")
    if plan.budget.max_parallelism > DAG_MAX_PARALLELISM:
        _fail(PlanReason.PLAN_BUDGET_EXCEEDED, "parallelism ceiling")
    if mutating and plan.budget.max_parallelism > DAG_MAX_PARALLELISM_MUTATION:
        # Mutating plans never widen parallelism implicitly; the caller must ask
        # for what the scheduler will actually grant.
        _fail(PlanReason.PLAN_BUDGET_EXCEEDED, "mutating plan parallelism")


def validate_plan(plan: PlanDefinition, catalog: ToolCatalog) -> ValidatedPlan:
    """Run steps 2-9 and 13. Raises :class:`PlanValidationError` on any breach."""
    if not isinstance(catalog, ToolCatalog):  # pragma: no cover - defensive
        raise TypeError("catalog must implement ToolCatalog")

    declared = _declared_edges(plan)
    implied = _binding_edges(plan)
    undeclared = sorted(implied - declared)
    if undeclared:
        src, dst = undeclared[0]
        _fail(PlanReason.BINDING_EDGE_NOT_DECLARED, f"{src}->{dst}")

    edges = frozenset(declared)
    order, ranks = topological_order(plan.node_ids, edges)
    _check_graph_shape(plan, edges, ranks)

    resources, mutating = _check_nodes(plan, catalog)
    _check_bindings(plan, catalog)
    _check_budget(plan, mutating)

    result_types = {
        node.id: dict(_tool_result_types(catalog, node))
        for node in plan.nodes
        if node.kind is NodeKind.TOOL
    }
    return ValidatedPlan(
        plan=plan,
        digest=plan_digest(plan),
        order=order,
        ranks=ranks,
        edges=edges,
        resource_keys=resources,
        mutating_nodes=frozenset(mutating),
        result_types=result_types,
    )


def revalidate_bound_value(
    value: Any, *, declared_type: str, max_bytes: int, catalog: ToolCatalog, is_resource: bool
) -> Any:
    """Runtime re-validation of a provider-produced value. Never retried."""
    from .canonical import canonical_json_bytes

    if type_of(value) != declared_type and declared_type != TYPE_ANY:
        raise PlanValidationError(PlanReason.BINDING_RUNTIME_REJECT, "type")
    encoded = canonical_json_bytes(value)
    if len(encoded) > max_bytes:
        raise PlanValidationError(PlanReason.BINDING_RUNTIME_REJECT, "size")
    if is_resource and (not isinstance(value, str) or not catalog.in_scope(value)):
        raise PlanValidationError(PlanReason.PLAN_SCOPE_DENIED, "bound resource")
    return value


__all__ = [
    "StaticToolCatalog",
    "ToolCatalog",
    "ToolContract",
    "ValidatedPlan",
    "revalidate_bound_value",
    "topological_order",
    "validate_plan",
]
