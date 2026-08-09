"""Shared hermetic builders for the Phase 5 DAG suites.

Flat module (not a package-relative import) so every Phase 5 test file can use
it without cross-test imports — see the Phase 3 ``merge_fixtures`` precedent.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from hermes_mcp_bridge.v2.dag_contract import (
    Budget,
    FailurePolicy,
    Node,
    NodeKind,
    PlanDefinition,
)
from hermes_mcp_bridge.v2.dag_engine import NodeDecision
from hermes_mcp_bridge.v2.dag_store import NodeState
from hermes_mcp_bridge.v2.dag_validation import StaticToolCatalog, ToolContract

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "v2_phase5"

SCOPE = frozenset({"owner/disposable", "owner/other-disposable"})

GET_REPO = ToolContract(
    tool_id="github.get_repo",
    arg_types={"repository": "string"},
    result_types={"name": "string", "topics": "list", "default_branch": "string"},
)
GET_PR = ToolContract(
    tool_id="github.get_pr",
    arg_types={"repository": "string", "number": "int"},
    result_types={"title": "string", "head_sha": "string", "state": "string"},
)
GET_CHECKS = ToolContract(
    tool_id="github.get_checks",
    arg_types={"repository": "string", "ref": "string"},
    result_types={"conclusion": "string", "runs": "list"},
)
GET_ISSUE = ToolContract(
    tool_id="github.get_issue",
    arg_types={"repository": "string", "number": "int"},
    result_types={"title": "string", "labels": "list"},
)
CREATE_BRANCH = ToolContract(
    tool_id="github.create_branch",
    arg_types={"repository": "string", "branch": "string", "sha": "string"},
    result_types={"ref": "string", "effect_ref": "string"},
    mutating=True,
    credential_capability_id="github.write",
    compensations=("delete_ref",),
)
CREATE_PR = ToolContract(
    tool_id="github.create_pr",
    arg_types={"repository": "string", "head": "string", "base": "string", "title": "string"},
    result_types={"number": "int", "effect_ref": "string"},
    mutating=True,
    credential_capability_id="github.write",
    compensations=("close_pr",),
)

ALL_CONTRACTS = {
    contract.tool_id: contract
    for contract in (GET_REPO, GET_PR, GET_CHECKS, GET_ISSUE, CREATE_BRANCH, CREATE_PR)
}


def catalog(
    *, projected: frozenset[str] | None = None, scope: frozenset[str] = SCOPE
) -> StaticToolCatalog:
    return StaticToolCatalog(
        contracts=ALL_CONTRACTS,
        projected=projected if projected is not None else frozenset(ALL_CONTRACTS),
        scope=scope,
    )


def budget(**overrides: Any) -> Budget:
    defaults: dict[str, Any] = {
        "max_nodes": 8,
        "max_parallelism": 1,
        "max_external_calls": 8,
        "max_total_wall_ms": 60_000,
        "max_result_bytes": 65_536,
        "max_checkpoint_bytes": 65_536,
    }
    defaults.update(overrides)
    return Budget(**defaults)


def plan(
    nodes: tuple[Node, ...],
    *,
    failure_policy: FailurePolicy = FailurePolicy.FAIL_FAST,
    **overrides: Any,
) -> PlanDefinition:
    kwargs: dict[str, Any] = {
        "plan_id": "test-plan",
        "nodes": nodes,
        "budget": budget(),
        "failure_policy": failure_policy,
        "deadline_ms": 60_000,
    }
    kwargs.update(overrides)
    return PlanDefinition(**kwargs)


def read_node(node_id: str, tool: str = "github.get_repo", **overrides: Any) -> Node:
    kwargs: dict[str, Any] = {
        "id": node_id,
        "kind": NodeKind.TOOL,
        "tool": tool,
        "args": {"repository": "owner/disposable"},
    }
    kwargs.update(overrides)
    return Node(**kwargs)


class AllowGovernance:
    """Explicit ALLOW for tests. Records nothing external."""

    def __init__(
        self,
        *,
        denied: frozenset[str] = frozenset(),
        approval_required: frozenset[str] = frozenset(),
    ) -> None:
        self.denied = denied
        self.approval_required = approval_required
        self.decisions: list[str] = []
        self.records: list[str] = []

    def decide(self, plan: Any, node: Node, resolved_args: Mapping[str, Any]) -> NodeDecision:
        self.decisions.append(node.id)
        if node.id in self.denied:
            return NodeDecision(allowed=False, policy_digest="pd")
        return NodeDecision(
            allowed=True,
            approval_required=node.id in self.approval_required,
            policy_digest="pd",
        )

    def record(self, plan: Any, node: Node, state: NodeState) -> None:
        self.records.append(node.id)


__all__ = [
    "ALL_CONTRACTS",
    "CREATE_BRANCH",
    "CREATE_PR",
    "FIXTURE_DIR",
    "GET_CHECKS",
    "GET_ISSUE",
    "GET_PR",
    "GET_REPO",
    "SCOPE",
    "AllowGovernance",
    "budget",
    "catalog",
    "plan",
    "read_node",
]
