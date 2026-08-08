"""Deterministic capability projection.

Projection takes the registry, the policy rule set, the credential broker and a
minimal opaque context, and returns only the tools that are both **authorized**
and **operational**.

Phase 1 decisions:

* projection is **static and deterministic** — the same inputs always produce
  the same ordered output and the same projection hash. Dynamic/adaptive
  projection stays deferred (OD-013), as does the discovery/refresh protocol
  (OD-012);
* the principal context is an **opaque, minimal** token pair so that the
  principal/tenant model (OD-007) is not closed prematurely; it participates in
  no authorization decision in Phase 1 and is not part of the projected payload;
* only ``ALLOW`` and ``APPROVAL_REQUIRED`` are projected. ``APPROVAL_REQUIRED``
  is always flagged explicitly (``requires_approval=True``) and is never
  silently presented as executable;
* the projected payload contains a strict, normalized allow-list of fields. It
  never contains credential values, credential capability ids, secret paths or
  backend-supplied metadata: there is **no pass-through** of backend metadata,
  each projected field is copied explicitly from the canonical definition;
* free-text editorial metadata (``ToolDefinition.description``) is **not
  projected** in Phase 1. Prose cannot be secret-scanned with confidence, so
  rather than relying on a heuristic the field is simply absent from the
  projected payload and from the projection hash.

What the Phase 1 surface guarantee actually is
----------------------------------------------

Only tools that are (a) explicitly registered as canonical
:class:`~hermes_mcp_bridge.v2.schema.ToolDefinition` objects, (b) backed by a
capability in state READY, and (c) resolved by policy to ALLOW or
APPROVAL_REQUIRED are projected. Nothing is discovered, inferred or forwarded
from a backend.

Consequently **no generic terminal or filesystem tool is registered in this
phase**, so none can be projected. This is a property of the registered set,
not a hard-coded provider ban: if a later phase deliberately registers a
*typed, constrained* wrapper (a specific command with a fixed argument schema,
say), its security tier and policy rule decide the outcome like any other tool.
An unregistered id such as ``terminal.exec`` is simply unknown and denies.
"""

from __future__ import annotations

from typing import Any

from pydantic import ConfigDict, Field

from ._models import RegistryModel
from .canonical import canonical_json_bytes, sha256_hex
from .credentials import CredentialBroker
from .enums import ExecutionMode, MutationClass, PolicyDecision, ResultShaping, SecurityTier
from .policy import PolicyEngine, PolicyEvaluation, PolicyRuleSet, ReasonCode
from .registry import ToolRegistry
from .schema import ToolDefinition

#: Envelope version for the projection payload.
PROJECTION_SCHEMA_VERSION = "v2.phase1.1"


class ProjectionContext(RegistryModel):
    """Minimal opaque caller context.

    ``principal_ref`` and ``resource_scope_ref`` are opaque handles: Phase 1
    performs no principal-based filtering, and the values are deliberately not
    serialized into the projection payload. This keeps OD-007 open.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    principal_ref: str | None = None
    resource_scope_ref: str | None = None


class ProjectedTool(RegistryModel):
    """A single tool as exposed to a client. Non-secret fields only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_id: str
    provider: str
    operation: str
    version: int = Field(ge=1)
    execution_mode: ExecutionMode
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    security_tier: SecurityTier
    read_only: bool
    mutation_class: MutationClass
    result_shaping: ResultShaping
    timeout_seconds: int = Field(ge=1)
    requires_approval: bool

    def canonical(self) -> dict[str, Any]:
        return {
            "execution_mode": self.execution_mode.value,
            "input_schema": self.input_schema,
            "mutation_class": self.mutation_class.value,
            "operation": self.operation,
            "output_schema": self.output_schema,
            "provider": self.provider,
            "read_only": self.read_only,
            "requires_approval": self.requires_approval,
            "result_shaping": self.result_shaping.value,
            "security_tier": self.security_tier.value,
            "timeout_seconds": self.timeout_seconds,
            "tool_id": self.tool_id,
            "version": self.version,
        }


def _project_one(tool: ToolDefinition, *, requires_approval: bool) -> ProjectedTool:
    return ProjectedTool(
        tool_id=tool.tool_id,
        provider=tool.provider,
        operation=tool.operation,
        version=tool.version,
        execution_mode=tool.execution_mode,
        input_schema=tool.input_schema,
        output_schema=tool.output_schema,
        security_tier=tool.security_tier,
        read_only=tool.read_only,
        mutation_class=tool.mutation_class,
        result_shaping=tool.result_shaping,
        timeout_seconds=tool.timeout_seconds,
        requires_approval=requires_approval,
    )


class ProjectionResult:
    """Ordered projected tools plus the excluded decisions, for auditability."""

    __slots__ = ("_excluded", "_snapshot_hash", "_tools")

    def __init__(
        self,
        tools: list[ProjectedTool],
        excluded: list[PolicyEvaluation],
        capability_snapshot_hash: str,
    ) -> None:
        self._tools = tools
        self._excluded = excluded
        self._snapshot_hash = capability_snapshot_hash

    @property
    def tools(self) -> list[ProjectedTool]:
        return list(self._tools)

    @property
    def excluded(self) -> list[PolicyEvaluation]:
        return list(self._excluded)

    @property
    def tool_ids(self) -> list[str]:
        return [tool.tool_id for tool in self._tools]

    @property
    def capability_snapshot_hash(self) -> str:
        return self._snapshot_hash

    def canonical(self) -> dict[str, Any]:
        return {
            "capability_snapshot_hash": self._snapshot_hash,
            "schema_version": PROJECTION_SCHEMA_VERSION,
            "tools": [tool.canonical() for tool in self._tools],
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.canonical())

    def projection_hash(self) -> str:
        return sha256_hex(self.canonical_bytes())


def project_capabilities(
    registry: ToolRegistry,
    rules: PolicyRuleSet,
    credential_broker: CredentialBroker | None = None,
    context: ProjectionContext | None = None,
) -> ProjectionResult:
    """Project the authorized, operational tool surface.

    ``context`` is accepted and validated but does not influence Phase 1
    filtering; see the module docstring.
    """
    _ = context if context is not None else ProjectionContext()
    engine = PolicyEngine(registry, rules, credential_broker)

    projected: list[ProjectedTool] = []
    excluded: list[PolicyEvaluation] = []

    for tool in registry.ordered():  # deterministic order by tool_id
        evaluation = engine.evaluate_tool(tool)
        if evaluation.decision is PolicyDecision.ALLOW:
            projected.append(_project_one(tool, requires_approval=False))
        elif evaluation.decision is PolicyDecision.APPROVAL_REQUIRED:
            projected.append(_project_one(tool, requires_approval=True))
        else:
            excluded.append(evaluation)

    projected.sort(key=lambda item: item.tool_id)
    excluded.sort(key=lambda item: item.tool_id)
    return ProjectionResult(projected, excluded, registry.capability_snapshot_hash())


__all__ = [
    "PROJECTION_SCHEMA_VERSION",
    "ProjectedTool",
    "ProjectionContext",
    "ProjectionResult",
    "ReasonCode",
    "project_capabilities",
]
