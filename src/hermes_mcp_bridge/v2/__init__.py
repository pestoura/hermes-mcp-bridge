"""Hermes MCP Bridge V2 — accepted registry plus Phase 2 DIRECT read core.

> **PHASE 1 REGISTRY_ACCEPTED · PHASE 2 CORE IMPLEMENTED, NOT ACCEPTED**

This package remains deliberately **isolated**: nothing here is imported by the
V1 server, client, protocol or tool registration path, and importing it has no
effect on the frozen 27-tool V1 surface (see
:mod:`hermes_mcp_bridge.contracts`).

Accepted Phase 1 scope:

* canonical tool schema with validated invariants;
* capability registry with an unambiguous readiness model;
* deterministic canonical serialization and ``capability_snapshot_hash``;
* credential status/broker contract (no real secret backend);
* fail-closed policy-as-code evaluation;
* deterministic static capability projection.

Phase 2 core adds a typed, governed GitHub REST read path for
``github.get_repo``, ``github.get_pr``, ``github.get_checks``,
``github.get_issue`` and repository-scoped ``github.search``. It is not wired to
MCP and does not imply that a Jarvas-side GitHub credential/provider is
available. ``DIRECT_READ_ACCEPTED`` remains blocked on connected provider
discovery plus real DIRECT-vs-V1 shadow evidence.

Explicitly still deferred: registry persistence/signing (ADR-0004), real
credential backends (OD-005/OD-016), principal/tenant model (OD-007), dynamic
projection/discovery (OD-012/OD-013) and later execution engines.
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
    FORBIDDEN_PERMISSION,
    MUTATION_STAGE_ORDER,
    READ_CAPABILITY_ID,
    ApprovalRequirement,
    ApprovalState,
    CapabilityState,
    ExecutionMode,
    IdempotencySemantics,
    IdempotencyStatus,
    MutationClass,
    MutationOutcome,
    MutationReasonCode,
    MutationStage,
    PolicyDecision,
    ResultShaping,
    RetryClass,
    SecurityTier,
    WriteCapabilityId,
)
from .errors import (
    ApprovalError,
    AuditWriteError,
    ConcurrencyDriftError,
    DigestMismatchError,
    DuplicateCapabilityError,
    IdempotencyConflictError,
    MutationDeniedError,
    MutationError,
    MutationIndeterminateError,
    MutationScopeError,
    RegistryValidationError,
    UnknownCapabilityError,
    UnknownToolError,
    V2Error,
    WriteCapabilityError,
)
from .final_gate import (
    FINAL_EVIDENCE_SCHEMA,
    FINAL_MANIFEST_SCHEMA,
    STATE_INTEGRITY_DOC_SCHEMA,
    final_manifest,
    validate_final_evidence,
)
from .github_attestation import (
    ALLOWED_CONFIRMATION_SOURCES,
    ATTESTATION_INPUT_SCHEMA,
    REQUIRED_PERMISSIONS,
    AttestationError,
    ProviderAttestation,
    ProviderAttestationInput,
    attest_provider,
    load_attestation_input,
)
from .github_auth import (
    GitHubAuthorization,
    GitHubAuthorizationError,
    GitHubAuthorizationProvider,
    StaticGitHubAuthorizationProvider,
)
from .github_canary import (
    ExecutionPath,
    FallbackReason,
    GitHubCanaryConfig,
    GitHubCanaryRouter,
    RouteDecision,
)
from .github_direct import (
    GITHUB_ACCEPT,
    GITHUB_API_BASE_URL,
    GITHUB_API_VERSION,
    GitHubDirectDenied,
    GitHubDirectError,
    GitHubDirectReadExecutor,
    GitHubDirectResult,
    GitHubRepositoryScope,
)
from .github_readiness import (
    GitHubReadReadinessBroker,
    capability_state_for,
)
from .github_registry import (
    GITHUB_API_CAPABILITY,
    GITHUB_DIRECT_READ_TOOL_IDS,
    GITHUB_READ_CREDENTIAL_CAPABILITY,
    build_github_direct_read_registry,
    github_direct_read_definitions,
    github_direct_read_policy_rules,
)
from .github_secret_provider import (
    DEFAULT_SECRET_NAME,
    AuthorizationStatus,
    FileGitHubAuthorizationProvider,
    GitHubProviderType,
    MaterialClass,
    classify_material,
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
from .tool_provenance import (
    PROVENANCE_SCHEMA,
    ProvenanceError,
    ToolProvenanceRecord,
    collect_tool_provenance,
)

__all__ = [
    "ALLOWED_CONFIRMATION_SOURCES",
    "ATTESTATION_INPUT_SCHEMA",
    "DEFAULT_SECRET_NAME",
    "FINAL_EVIDENCE_SCHEMA",
    "FINAL_MANIFEST_SCHEMA",
    "FORBIDDEN_PERMISSION",
    "GITHUB_ACCEPT",
    "GITHUB_API_BASE_URL",
    "GITHUB_API_CAPABILITY",
    "GITHUB_API_VERSION",
    "GITHUB_DIRECT_READ_TOOL_IDS",
    "GITHUB_READ_CREDENTIAL_CAPABILITY",
    "MUTATION_STAGE_ORDER",
    "PHASE1_GATE",
    "PHASE1_STATUS",
    "PHASE2_STATUS",
    "PHASE3_CONTRACT",
    "PROVENANCE_SCHEMA",
    "READ_CAPABILITY_ID",
    "REQUIRED_PERMISSIONS",
    "STATE_INTEGRITY_DOC_SCHEMA",
    "ApprovalError",
    "ApprovalRequirement",
    "ApprovalState",
    "AttestationError",
    "AuditWriteError",
    "AuthorizationStatus",
    "CapabilityDescriptor",
    "CapabilityRegistry",
    "CapabilitySnapshot",
    "CapabilityState",
    "ConcurrencyDriftError",
    "CredentialBroker",
    "CredentialCapabilityStatus",
    "DigestMismatchError",
    "DuplicateCapabilityError",
    "ExecutionMode",
    "ExecutionPath",
    "FallbackReason",
    "FileGitHubAuthorizationProvider",
    "GitHubAuthorization",
    "GitHubAuthorizationError",
    "GitHubAuthorizationProvider",
    "GitHubCanaryConfig",
    "GitHubCanaryRouter",
    "GitHubDirectDenied",
    "GitHubDirectError",
    "GitHubDirectReadExecutor",
    "GitHubDirectResult",
    "GitHubProviderType",
    "GitHubReadReadinessBroker",
    "GitHubRepositoryScope",
    "IdempotencyConflictError",
    "IdempotencySemantics",
    "IdempotencyStatus",
    "MaterialClass",
    "MutationClass",
    "MutationDeniedError",
    "MutationError",
    "MutationIndeterminateError",
    "MutationOutcome",
    "MutationReasonCode",
    "MutationScopeError",
    "MutationStage",
    "PolicyDecision",
    "PolicyEngine",
    "PolicyEvaluation",
    "PolicyRule",
    "PolicyRuleSet",
    "ProjectedTool",
    "ProjectionContext",
    "ProjectionResult",
    "ProvenanceError",
    "ProviderAttestation",
    "ProviderAttestationInput",
    "ReasonCode",
    "RegistryValidationError",
    "ResourceKey",
    "ResultShaping",
    "RetryClass",
    "RetryPolicy",
    "RouteDecision",
    "SecurityTier",
    "StaticCredentialBroker",
    "StaticGitHubAuthorizationProvider",
    "ToolDefinition",
    "ToolProvenanceRecord",
    "ToolRegistry",
    "UnknownCapabilityError",
    "UnknownToolError",
    "V2Error",
    "WriteCapabilityError",
    "WriteCapabilityId",
    "attest_provider",
    "build_github_direct_read_registry",
    "canonical_json_bytes",
    "canonical_json_text",
    "capability_state_for",
    "classify_material",
    "collect_tool_provenance",
    "final_manifest",
    "github_direct_read_definitions",
    "github_direct_read_policy_rules",
    "load_attestation_input",
    "project_capabilities",
    "sha256_hex",
    "validate_final_evidence",
]

#: Legacy Phase 1 implementation marker retained for V1/V2 compatibility tests.
#: It was explicitly defined as "not an acceptance gate" in the accepted core.
PHASE1_STATUS = "PHASE_1_CORE_IMPLEMENTED_NOT_ACCEPTED"
#: Formal assurance gate promoted by retained Phase 1 evidence.
PHASE1_GATE = "REGISTRY_ACCEPTED"
#: Phase 2 repo-side core marker. Not an acceptance gate.
PHASE2_STATUS = "CANARY_COLLECTOR_IMPLEMENTED_CONNECTED_CREDENTIAL_BLOCKED"
#: Phase 3 shared-contract marker (Controller C-1). Not an acceptance gate.
PHASE3_CONTRACT = "PHASE_3_SHARED_CONTRACT_PUBLISHED_NOT_ACCEPTED"
