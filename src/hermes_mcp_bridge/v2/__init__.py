"""Hermes MCP Bridge V2 — Phase 1 canonical registry core.

> **PHASE 1 CORE IMPLEMENTED · NOT YET ACCEPTED**

This package is deliberately **isolated**: nothing here is imported by the V1
server, client, protocol or tool registration path, and importing it has no
effect on the frozen 27-tool V1 surface (see
:mod:`hermes_mcp_bridge.contracts`).

Phase 1 scope (see ``docs/v2/roadmap.md``):

* canonical tool schema with validated invariants;
* capability registry with an unambiguous readiness model;
* deterministic canonical serialization and ``capability_snapshot_hash``;
* credential *contract* only (no secret backend);
* fail-closed policy-as-code evaluation;
* deterministic capability projection.

Explicitly **deferred** (unchanged open decisions): registry persistence/
signing (OD-003), real credential backends (OD-005), principal/tenant model
(OD-007), dynamic projection and discovery protocol (OD-012, OD-013).
"""

from __future__ import annotations

from .canonical import canonical_json_bytes, canonical_json_text, sha256_hex
from .capabilities import CapabilityDescriptor, CapabilityRegistry
from .credentials import (
    CredentialBroker,
    CredentialCapabilityStatus,
    StaticCredentialBroker,
)
from .enums import (
    ApprovalRequirement,
    CapabilityState,
    ExecutionMode,
    IdempotencySemantics,
    MutationClass,
    PolicyDecision,
    ResultShaping,
    RetryClass,
    SecurityTier,
)
from .errors import (
    DuplicateCapabilityError,
    RegistryValidationError,
    UnknownCapabilityError,
    UnknownToolError,
    V2Error,
)
from .policy import (
    PolicyEngine,
    PolicyEvaluation,
    PolicyRule,
    PolicyRuleSet,
    ReasonCode,
)
from .projection import ProjectedTool, ProjectionContext, ProjectionResult, project_capabilities
from .registry import CapabilitySnapshot, ToolRegistry
from .schema import ResourceKey, RetryPolicy, ToolDefinition

__all__ = [
    "ApprovalRequirement",
    "CapabilityDescriptor",
    "CapabilityRegistry",
    "CapabilitySnapshot",
    "CapabilityState",
    "CredentialBroker",
    "CredentialCapabilityStatus",
    "DuplicateCapabilityError",
    "ExecutionMode",
    "IdempotencySemantics",
    "MutationClass",
    "PolicyDecision",
    "PolicyEngine",
    "PolicyEvaluation",
    "PolicyRule",
    "PolicyRuleSet",
    "ProjectedTool",
    "ProjectionContext",
    "ProjectionResult",
    "ReasonCode",
    "RegistryValidationError",
    "ResourceKey",
    "ResultShaping",
    "RetryClass",
    "RetryPolicy",
    "SecurityTier",
    "StaticCredentialBroker",
    "ToolDefinition",
    "ToolRegistry",
    "UnknownCapabilityError",
    "UnknownToolError",
    "V2Error",
    "canonical_json_bytes",
    "canonical_json_text",
    "project_capabilities",
    "sha256_hex",
]

#: Phase 1 marker. Not an acceptance gate.
PHASE1_STATUS = "PHASE_1_CORE_IMPLEMENTED_NOT_ACCEPTED"
