"""Phase 5 DAG typed contract — ``PlanDefinition`` and its closed vocabulary.

> **V2 · PHASE 5 · runtime, disabled by default behind ``DAG_FEATURE_ENABLED``**

A plan is **data**: no credentials, no URLs, no commands, no expressions. Every
node either invokes one already-typed registry tool (``kind=TOOL``) or applies
one operation from the closed, pure transform set (``kind=TRANSFORM``, see
:mod:`hermes_mcp_bridge.v2.dag_transform`).

Design source: ``docs/v2/phase5/plan-definition.md``. Construction is total
validation of *shape*; graph/typing/budget validation lives in
:mod:`hermes_mcp_bridge.v2.dag_validation` so that a rejected plan performs zero
credential resolution and zero I/O.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum, unique
from types import MappingProxyType
from typing import Any

from .errors import V2Error

DAG_SCHEMA_VERSION = "dag/1"
#: Bumped by any canonicalization or digest-algorithm change (ADR-0025).
DAG_DIGEST_VERSION = "dagdigest/1"
#: Phase 5 ships disabled and unwired from MCP.
DAG_FEATURE_ENABLED = False

#: Server-authoritative ceilings (``docs/v2/phase5/scheduling.md``).
DAG_MAX_NODES = 64
DAG_MAX_PARALLELISM = 4
#: A mutating plan is never widened: same ceiling as Phase 4 mutation batches.
DAG_MAX_PARALLELISM_MUTATION = 1
#: Hard per-node wall ceiling, independent of the plan-level deadline.
DAG_MAX_NODE_TIMEOUT_MS = 120_000
DAG_MAX_DEPTH = 16
DAG_MAX_FANOUT = 16
DAG_MAX_EXTERNAL_CALLS = 64
DAG_MAX_TOTAL_WALL_MS = 900_000
DAG_MAX_RESULT_BYTES = 1_048_576
DAG_MAX_CHECKPOINT_BYTES = 1_048_576
DAG_MAX_BINDING_BYTES = 262_144
DAG_MAX_STRING_BYTES = 4096
DAG_MAX_CANONICAL_BYTES = 262_144
DAG_MAX_NESTING_DEPTH = 8

_NODE_ID_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
_PLAN_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_BINDING_TARGET_RE = re.compile(r"^args\.[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)*$")
_BINDING_SOURCE_RE = re.compile(r"^([a-z0-9_-]{1,64})\.result(\.[A-Za-z0-9_]+)*$")

#: Fields carried for humans and deliberately excluded from the digest.
EDITORIAL_FIELDS = ("description", "label", "comment", "notes")


@unique
class PlanReason(StrEnum):
    """Stable, redacted reason codes. Error bodies are part of the contract."""

    PLAN_SCHEMA_INVALID = "PLAN_SCHEMA_INVALID"
    PLAN_UNKNOWN_FIELD = "PLAN_UNKNOWN_FIELD"
    PLAN_DUPLICATE_NODE = "PLAN_DUPLICATE_NODE"
    PLAN_CYCLE_DETECTED = "PLAN_CYCLE_DETECTED"
    PLAN_SELF_DEPENDENCY = "PLAN_SELF_DEPENDENCY"
    PLAN_UNKNOWN_DEPENDENCY = "PLAN_UNKNOWN_DEPENDENCY"
    PLAN_DUPLICATE_DEPENDENCY = "PLAN_DUPLICATE_DEPENDENCY"
    PLAN_UNREACHABLE_NODE = "PLAN_UNREACHABLE_NODE"
    PLAN_DEPTH_EXCEEDED = "PLAN_DEPTH_EXCEEDED"
    PLAN_FANOUT_EXCEEDED = "PLAN_FANOUT_EXCEEDED"
    PLAN_BUDGET_EXCEEDED = "PLAN_BUDGET_EXCEEDED"
    PLAN_TOOL_UNKNOWN = "PLAN_TOOL_UNKNOWN"
    PLAN_TOOL_NOT_PROJECTED = "PLAN_TOOL_NOT_PROJECTED"
    PLAN_SCOPE_DENIED = "PLAN_SCOPE_DENIED"
    PLAN_ARG_INVALID = "PLAN_ARG_INVALID"
    PLAN_IDEMPOTENCY_MISSING = "PLAN_IDEMPOTENCY_MISSING"
    PLAN_COMPENSATION_UNDECLARED = "PLAN_COMPENSATION_UNDECLARED"
    BINDING_EDGE_NOT_DECLARED = "BINDING_EDGE_NOT_DECLARED"
    BINDING_SOURCE_UNSHAPED = "BINDING_SOURCE_UNSHAPED"
    BINDING_FIELD_UNKNOWN = "BINDING_FIELD_UNKNOWN"
    BINDING_TYPE_MISMATCH = "BINDING_TYPE_MISMATCH"
    BINDING_TARGET_TYPE_MISMATCH = "BINDING_TARGET_TYPE_MISMATCH"
    BINDING_SIZE_EXCEEDED = "BINDING_SIZE_EXCEEDED"
    BINDING_CONTROL_FIELD_FORBIDDEN = "BINDING_CONTROL_FIELD_FORBIDDEN"
    BINDING_RUNTIME_REJECT = "BINDING_RUNTIME_REJECT"
    TRANSFORM_OP_UNKNOWN = "TRANSFORM_OP_UNKNOWN"
    TRANSFORM_TYPE_MISMATCH = "TRANSFORM_TYPE_MISMATCH"
    TRANSFORM_OUTPUT_TOO_LARGE = "TRANSFORM_OUTPUT_TOO_LARGE"
    POLICY_MISSING = "POLICY_MISSING"
    POLICY_DENIED = "POLICY_DENIED"
    APPROVAL_MISSING = "APPROVAL_MISSING"
    APPROVAL_DIGEST_MISMATCH = "APPROVAL_DIGEST_MISMATCH"
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    APPROVAL_SCOPE_INSUFFICIENT = "APPROVAL_SCOPE_INSUFFICIENT"
    APPROVAL_ALREADY_CONSUMED = "APPROVAL_ALREADY_CONSUMED"
    APPROVAL_OPERATION_DIGEST_UNCOVERED = "APPROVAL_OPERATION_DIGEST_UNCOVERED"
    UPSTREAM_ABORT = "UPSTREAM_ABORT"
    UPSTREAM_FAILED = "UPSTREAM_FAILED"
    UPSTREAM_INDETERMINATE = "UPSTREAM_INDETERMINATE"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    CHECKPOINT_TAMPERED = "CHECKPOINT_TAMPERED"
    CHECKPOINT_SCHEMA_UNSUPPORTED = "CHECKPOINT_SCHEMA_UNSUPPORTED"
    PLAN_DIGEST_MISMATCH = "PLAN_DIGEST_MISMATCH"
    LEASE_FENCE_STALE = "LEASE_FENCE_STALE"
    COMPENSATION_UNSAFE = "COMPENSATION_UNSAFE"
    DAG_DISABLED = "DAG_DISABLED"


@unique
class NodeKind(StrEnum):
    TOOL = "TOOL"
    TRANSFORM = "TRANSFORM"


@unique
class FailurePolicy(StrEnum):
    FAIL_FAST = "fail_fast"
    CONTINUE_INDEPENDENT = "continue_independent"


@unique
class RollbackPolicy(StrEnum):
    NONE = "none"
    COMPENSATE_ON_FAILURE = "compensate_on_failure"
    COMPENSATE_ON_ABORT = "compensate_on_abort"


@unique
class OnFailure(StrEnum):
    ABORT_PLAN = "abort_plan"
    ISOLATE_BRANCH = "isolate_branch"
    DEAD_LETTER = "dead_letter"


@unique
class NodeStatus(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    DISPATCHED = "DISPATCHED"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    DENIED = "DENIED"
    SKIPPED = "SKIPPED"
    INDETERMINATE = "INDETERMINATE"
    COMPENSATION_PENDING = "COMPENSATION_PENDING"
    COMPENSATED = "COMPENSATED"
    COMPENSATION_SKIPPED = "COMPENSATION_SKIPPED"
    COMPENSATION_UNSAFE = "COMPENSATION_UNSAFE"
    COMPENSATION_FAILED = "COMPENSATION_FAILED"
    COMPENSATION_INDETERMINATE = "COMPENSATION_INDETERMINATE"
    DEAD_LETTER = "DEAD_LETTER"


TERMINAL_NODE_STATUSES = frozenset(
    {
        NodeStatus.SUCCESS,
        NodeStatus.FAILED,
        NodeStatus.DENIED,
        NodeStatus.SKIPPED,
        NodeStatus.INDETERMINATE,
        NodeStatus.DEAD_LETTER,
    }
)


@unique
class PlanStatus(StrEnum):
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    ABORTED = "ABORTED"
    INDETERMINATE = "INDETERMINATE"
    DEAD_LETTER = "DEAD_LETTER"


#: Precedence, highest first (``docs/v2/phase5/failure-semantics.md``).
PLAN_STATUS_PRECEDENCE: tuple[PlanStatus, ...] = (
    PlanStatus.DEAD_LETTER,
    PlanStatus.INDETERMINATE,
    PlanStatus.ABORTED,
    PlanStatus.PARTIAL,
    PlanStatus.FAILED,
    PlanStatus.COMPLETED,
)


class DagError(V2Error):
    """Base class for Phase 5 errors; string form is ``reason`` plus a note."""

    def __init__(self, reason: PlanReason, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason.value}:{detail}" if detail else reason.value)


class PlanValidationError(DagError):
    """A plan was rejected. Zero credential resolution, zero I/O occurred."""


class DagDisabledError(DagError):
    def __init__(self) -> None:
        super().__init__(PlanReason.DAG_DISABLED, "dag feature flag is off")


def _fail(reason: PlanReason, detail: str) -> None:
    raise PlanValidationError(reason, detail)


def _check_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        _fail(PlanReason.PLAN_SCHEMA_INVALID, f"{field_name}: must be a string")
    text = str(value)
    if len(text.encode("utf-8")) > DAG_MAX_STRING_BYTES:
        _fail(PlanReason.PLAN_SCHEMA_INVALID, f"{field_name}: too long")
    if any(unicodedata.category(ch) == "Cc" for ch in text):
        _fail(PlanReason.PLAN_SCHEMA_INVALID, f"{field_name}: control characters")
    normalized = unicodedata.normalize("NFC", text)
    if normalized != text:
        _fail(PlanReason.PLAN_SCHEMA_INVALID, f"{field_name}: must be NFC-normalized")
    return normalized


def _check_int(value: object, *, field_name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(PlanReason.PLAN_SCHEMA_INVALID, f"{field_name}: must be an int")
    number = int(value)  # type: ignore[arg-type]
    if number < minimum or number > maximum:
        _fail(PlanReason.PLAN_SCHEMA_INVALID, f"{field_name}: out of range")
    return number


def _check_plain(value: object, *, field_name: str, depth: int = 0) -> Any:
    """Accept only int/str/bool/None and nested list/dict of those."""
    if depth > DAG_MAX_NESTING_DEPTH:
        _fail(PlanReason.PLAN_SCHEMA_INVALID, f"{field_name}: nesting too deep")
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, str):
        return _check_text(value, field_name=field_name)
    if isinstance(value, float):
        _fail(PlanReason.PLAN_SCHEMA_INVALID, f"{field_name}: floats are forbidden")
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                _fail(PlanReason.PLAN_SCHEMA_INVALID, f"{field_name}: non-string key")
            out[str(key)] = _check_plain(item, field_name=f"{field_name}.{key}", depth=depth + 1)
        return out
    if isinstance(value, Sequence):
        return [_check_plain(item, field_name=f"{field_name}[]", depth=depth + 1) for item in value]
    _fail(PlanReason.PLAN_SCHEMA_INVALID, f"{field_name}: unsupported type")
    raise AssertionError("unreachable")  # pragma: no cover


@dataclass(frozen=True, slots=True)
class Binding:
    """Typed injection of one upstream shaped result field into one argument."""

    source: str
    type: str
    required: bool = True
    max_bytes: int = DAG_MAX_BINDING_BYTES

    def __post_init__(self) -> None:
        _check_text(self.source, field_name="binding.from")
        if not _BINDING_SOURCE_RE.match(self.source):
            _fail(PlanReason.PLAN_SCHEMA_INVALID, "binding.from: malformed path")
        _check_text(self.type, field_name="binding.type")
        if not isinstance(self.required, bool):
            _fail(PlanReason.PLAN_SCHEMA_INVALID, "binding.required: must be a bool")
        _check_int(
            self.max_bytes, field_name="binding.max_bytes", minimum=1, maximum=DAG_MAX_BINDING_BYTES
        )

    @property
    def source_node(self) -> str:
        return self.source.split(".", 1)[0]

    @property
    def source_path(self) -> tuple[str, ...]:
        parts = self.source.split(".")
        return tuple(parts[2:])


@dataclass(frozen=True, slots=True)
class Idempotency:
    """Per-node idempotency configuration for mutating nodes."""

    enabled: bool = True
    attempt_epoch: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            _fail(PlanReason.PLAN_SCHEMA_INVALID, "idempotency.enabled: must be a bool")
        _check_int(
            self.attempt_epoch,
            field_name="idempotency.attempt_epoch",
            minimum=0,
            maximum=1_000_000,
        )


@dataclass(frozen=True, slots=True)
class Compensation:
    """Declarative inverse, selected from the registry compensation table."""

    operation: str

    def __post_init__(self) -> None:
        _check_text(self.operation, field_name="compensation.operation")


@dataclass(frozen=True, slots=True)
class Budget:
    """Ceilings enforced at admission and again per dispatch. Never trimmed."""

    max_nodes: int = DAG_MAX_NODES
    max_parallelism: int = 1
    max_external_calls: int = DAG_MAX_EXTERNAL_CALLS
    max_total_wall_ms: int = DAG_MAX_TOTAL_WALL_MS
    max_result_bytes: int = DAG_MAX_RESULT_BYTES
    max_checkpoint_bytes: int = DAG_MAX_CHECKPOINT_BYTES
    per_provider_limits: Mapping[str, int] = field(default_factory=dict)
    per_credential_limits: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _check_int(self.max_nodes, field_name="budget.max_nodes", minimum=1, maximum=DAG_MAX_NODES)
        _check_int(
            self.max_parallelism,
            field_name="budget.max_parallelism",
            minimum=1,
            maximum=DAG_MAX_PARALLELISM,
        )
        _check_int(
            self.max_external_calls,
            field_name="budget.max_external_calls",
            minimum=0,
            maximum=DAG_MAX_EXTERNAL_CALLS,
        )
        _check_int(
            self.max_total_wall_ms,
            field_name="budget.max_total_wall_ms",
            minimum=1,
            maximum=DAG_MAX_TOTAL_WALL_MS,
        )
        _check_int(
            self.max_result_bytes,
            field_name="budget.max_result_bytes",
            minimum=1,
            maximum=DAG_MAX_RESULT_BYTES,
        )
        _check_int(
            self.max_checkpoint_bytes,
            field_name="budget.max_checkpoint_bytes",
            minimum=1,
            maximum=DAG_MAX_CHECKPOINT_BYTES,
        )
        for name in ("per_provider_limits", "per_credential_limits"):
            mapping = getattr(self, name)
            if not isinstance(mapping, Mapping):
                _fail(PlanReason.PLAN_SCHEMA_INVALID, f"budget.{name}: must be a mapping")
            for key, value in mapping.items():
                _check_text(key, field_name=f"budget.{name} key")
                _check_int(
                    value,
                    field_name=f"budget.{name}[{key}]",
                    minimum=1,
                    maximum=DAG_MAX_PARALLELISM,
                )
        object.__setattr__(
            self, "per_provider_limits", MappingProxyType(dict(self.per_provider_limits))
        )
        object.__setattr__(
            self, "per_credential_limits", MappingProxyType(dict(self.per_credential_limits))
        )


@dataclass(frozen=True, slots=True)
class Approval:
    """Single-use approval bound to an immutable ``plan_digest``."""

    approval_id: str
    digest: str
    nonce: str
    expires_at_ms: int
    scope: frozenset[str]
    required_for: tuple[str, ...] = ()
    runtime_bound: bool = False
    operation_digests: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        for name in ("approval_id", "digest", "nonce"):
            _check_text(getattr(self, name), field_name=f"approval.{name}")
        _check_int(
            self.expires_at_ms,
            field_name="approval.expires_at_ms",
            minimum=0,
            maximum=2**63 - 1,
        )
        if not isinstance(self.scope, frozenset):
            _fail(PlanReason.PLAN_SCHEMA_INVALID, "approval.scope: must be a frozenset")
        if not isinstance(self.runtime_bound, bool):
            _fail(PlanReason.PLAN_SCHEMA_INVALID, "approval.runtime_bound: must be a bool")
        for item in self.scope:
            _check_text(item, field_name="approval.scope entry")
        for node_id in self.required_for:
            _check_text(node_id, field_name="approval.required_for entry")


@dataclass(frozen=True, slots=True)
class Node:
    """One plan node. ``TOOL`` invokes a typed registry tool; ``TRANSFORM`` is pure."""

    id: str
    kind: NodeKind
    tool: str | None = None
    op: str | None = None
    args: Mapping[str, Any] = field(default_factory=dict)
    bindings: Mapping[str, Binding] = field(default_factory=dict)
    depends_on: tuple[str, ...] = ()
    policy_ref: str | None = None
    idempotency: Idempotency | None = None
    on_failure: OnFailure = OnFailure.ABORT_PLAN
    timeout_ms: int = 30_000
    retry_ref: str | None = None
    compensation: Compensation | None = None
    description: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not _NODE_ID_RE.match(self.id):
            _fail(PlanReason.PLAN_SCHEMA_INVALID, "node.id: charset/length")
        if not isinstance(self.kind, NodeKind):
            _fail(PlanReason.PLAN_SCHEMA_INVALID, "node.kind: must be a NodeKind")
        if self.kind is NodeKind.TOOL:
            if not self.tool or self.op is not None:
                _fail(PlanReason.PLAN_SCHEMA_INVALID, "node: TOOL requires tool and no op")
            _check_text(self.tool, field_name="node.tool")
        else:
            if not self.op or self.tool is not None:
                _fail(PlanReason.PLAN_SCHEMA_INVALID, "node: TRANSFORM requires op and no tool")
            _check_text(self.op, field_name="node.op")
        if not isinstance(self.args, Mapping):
            _fail(PlanReason.PLAN_SCHEMA_INVALID, "node.args: must be a mapping")
        object.__setattr__(
            self, "args", MappingProxyType(_check_plain(dict(self.args), field_name="node.args"))
        )
        if not isinstance(self.bindings, Mapping):
            _fail(PlanReason.PLAN_SCHEMA_INVALID, "node.bindings: must be a mapping")
        for target, binding in self.bindings.items():
            _check_text(target, field_name="node.bindings key")
            if not isinstance(binding, Binding):
                _fail(PlanReason.PLAN_SCHEMA_INVALID, "node.bindings: value must be a Binding")
            if not _BINDING_TARGET_RE.match(target):
                # Data may flow into arguments only — never into what is executed.
                _fail(PlanReason.BINDING_CONTROL_FIELD_FORBIDDEN, "node.bindings: target")
        object.__setattr__(self, "bindings", MappingProxyType(dict(self.bindings)))
        if isinstance(self.depends_on, str) or not isinstance(self.depends_on, Sequence):
            _fail(PlanReason.PLAN_SCHEMA_INVALID, "node.depends_on: must be a sequence")
        seen: set[str] = set()
        for dep in self.depends_on:
            if not isinstance(dep, str) or not _NODE_ID_RE.match(dep):
                _fail(PlanReason.PLAN_SCHEMA_INVALID, "node.depends_on: bad node id")
            if dep in seen:
                _fail(PlanReason.PLAN_DUPLICATE_DEPENDENCY, f"{self.id}->{dep}")
            seen.add(dep)
            if dep == self.id:
                _fail(PlanReason.PLAN_SELF_DEPENDENCY, self.id)
        object.__setattr__(self, "depends_on", tuple(self.depends_on))
        if self.policy_ref is not None:
            _check_text(self.policy_ref, field_name="node.policy_ref")
        if self.retry_ref is not None:
            _check_text(self.retry_ref, field_name="node.retry_ref")
        if self.idempotency is not None and not isinstance(self.idempotency, Idempotency):
            _fail(PlanReason.PLAN_SCHEMA_INVALID, "node.idempotency: bad type")
        if self.compensation is not None and not isinstance(self.compensation, Compensation):
            _fail(PlanReason.PLAN_SCHEMA_INVALID, "node.compensation: bad type")
        if not isinstance(self.on_failure, OnFailure):
            _fail(PlanReason.PLAN_SCHEMA_INVALID, "node.on_failure: bad value")
        _check_int(
            self.timeout_ms,
            field_name="node.timeout_ms",
            minimum=1,
            maximum=DAG_MAX_TOTAL_WALL_MS,
        )
        _check_text(self.description, field_name="node.description")


@dataclass(frozen=True, slots=True)
class PlanDefinition:
    """Validated *shape* of a DAG plan. Graph semantics: ``dag_validation``."""

    plan_id: str
    nodes: tuple[Node, ...]
    budget: Budget
    failure_policy: FailurePolicy
    deadline_ms: int
    rollback_policy: RollbackPolicy = RollbackPolicy.NONE
    approval: Approval | None = None
    dry_run: bool = False
    schema_version: str = DAG_SCHEMA_VERSION
    mode: str = "DAG"
    description: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != DAG_SCHEMA_VERSION:
            _fail(PlanReason.PLAN_SCHEMA_INVALID, "schema_version: unsupported")
        if self.mode != "DAG":
            _fail(PlanReason.PLAN_SCHEMA_INVALID, "mode: must be DAG")
        if not isinstance(self.plan_id, str) or not _PLAN_ID_RE.match(self.plan_id):
            _fail(PlanReason.PLAN_SCHEMA_INVALID, "plan_id: charset/length")
        if isinstance(self.nodes, str) or not isinstance(self.nodes, Sequence):
            _fail(PlanReason.PLAN_SCHEMA_INVALID, "nodes: must be a sequence")
        if not self.nodes:
            _fail(PlanReason.PLAN_SCHEMA_INVALID, "nodes: at least one node is required")
        if len(self.nodes) > DAG_MAX_NODES:
            _fail(PlanReason.PLAN_BUDGET_EXCEEDED, "nodes: exceeds DAG_MAX_NODES")
        seen: set[str] = set()
        for node in self.nodes:
            if not isinstance(node, Node):
                _fail(PlanReason.PLAN_SCHEMA_INVALID, "nodes: entries must be Node")
            if node.id in seen:
                _fail(PlanReason.PLAN_DUPLICATE_NODE, node.id)
            seen.add(node.id)
        object.__setattr__(self, "nodes", tuple(self.nodes))
        if not isinstance(self.budget, Budget):
            _fail(PlanReason.PLAN_SCHEMA_INVALID, "budget: must be a Budget")
        if not isinstance(self.failure_policy, FailurePolicy):
            _fail(PlanReason.PLAN_SCHEMA_INVALID, "failure_policy: must be explicit")
        if not isinstance(self.rollback_policy, RollbackPolicy):
            _fail(PlanReason.PLAN_SCHEMA_INVALID, "rollback_policy: bad value")
        if not isinstance(self.dry_run, bool):
            _fail(PlanReason.PLAN_SCHEMA_INVALID, "dry_run: must be a bool")
        if self.approval is not None and not isinstance(self.approval, Approval):
            _fail(PlanReason.PLAN_SCHEMA_INVALID, "approval: bad type")
        _check_int(
            self.deadline_ms,
            field_name="deadline_ms",
            minimum=1,
            maximum=DAG_MAX_TOTAL_WALL_MS,
        )
        if len(self.nodes) > self.budget.max_nodes:
            _fail(PlanReason.PLAN_BUDGET_EXCEEDED, "nodes: exceeds budget.max_nodes")
        for node in self.nodes:
            if node.timeout_ms > self.deadline_ms:
                _fail(PlanReason.PLAN_BUDGET_EXCEEDED, f"node {node.id}: timeout > deadline")
        _check_text(self.description, field_name="description")

    @property
    def node_ids(self) -> tuple[str, ...]:
        return tuple(node.id for node in self.nodes)

    def node(self, node_id: str) -> Node:
        for node in self.nodes:
            if node.id == node_id:
                return node
        raise PlanValidationError(PlanReason.PLAN_UNKNOWN_DEPENDENCY, node_id)


__all__ = [
    "DAG_DIGEST_VERSION",
    "DAG_FEATURE_ENABLED",
    "DAG_MAX_BINDING_BYTES",
    "DAG_MAX_CANONICAL_BYTES",
    "DAG_MAX_CHECKPOINT_BYTES",
    "DAG_MAX_DEPTH",
    "DAG_MAX_EXTERNAL_CALLS",
    "DAG_MAX_FANOUT",
    "DAG_MAX_NODES",
    "DAG_MAX_PARALLELISM",
    "DAG_MAX_RESULT_BYTES",
    "DAG_MAX_TOTAL_WALL_MS",
    "DAG_SCHEMA_VERSION",
    "EDITORIAL_FIELDS",
    "PLAN_STATUS_PRECEDENCE",
    "TERMINAL_NODE_STATUSES",
    "Approval",
    "Binding",
    "Budget",
    "Compensation",
    "DagDisabledError",
    "DagError",
    "FailurePolicy",
    "Idempotency",
    "Node",
    "NodeKind",
    "NodeStatus",
    "OnFailure",
    "PlanDefinition",
    "PlanReason",
    "PlanStatus",
    "PlanValidationError",
    "RollbackPolicy",
]
