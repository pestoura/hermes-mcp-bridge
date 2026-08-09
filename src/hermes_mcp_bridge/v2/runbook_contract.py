"""Phase 6 runbook contract types and fail-closed validators.

> **V2 · PHASE 6 · runtime, disabled by default behind ``RUNBOOK_FEATURE_ENABLED``**

A runbook is the governed, reusable form of a DAG plan. This module defines the
source manifest, the closed enums (policy/approval/rollback/destructive),
the typed parameter/output schema, capability and credential declarations,
ownership, timeouts, budgets and the admission-time validators. No provider
call, no credential resolution and no network access happen here: admission is a
pure function of the manifest and the registry state.

See ``docs/v2/phase6/*.md`` for the full design and ADR-0028..ADR-0031.
"""

from __future__ import annotations

import re as _re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .dag_contract import DAG_MAX_NODE_TIMEOUT_MS, DAG_MAX_NODES

RUNBOOK_FEATURE_ENABLED = False

RUNBOOK_IR_SCHEMA_VERSION = 1
RUNBOOK_MAX_ID_LEN = 64
RUNBOOK_MAX_PARAM_BYTES = 65_536
RUNBOOK_MAX_VERSION = 64
RUNBOOK_MAX_REVIEW_CADENCE_DAYS = 180
#: Agentic budgets are zero unless a runbook is explicitly permitted; a
#: deterministic runbook consumes exactly 0 Hermes LLM tokens (A6-17).
RUNBOOK_MAX_AGENTIC_ESCALATIONS_DEFAULT = 0
RUNBOOK_MAX_AGENTIC_TOKENS_DEFAULT = 0


class RunbookReason(StrEnum):
    """Stable admission/invocation reason codes (Phase 6)."""

    RB_MALFORMED = "RB_MALFORMED"
    RB_IR_VERSION_UNSUPPORTED = "RB_IR_VERSION_UNSUPPORTED"
    RB_TOO_LARGE = "RB_TOO_LARGE"
    RB_ID_INVALID = "RB_ID_INVALID"
    RB_NAMESPACE_COLLISION = "RB_NAMESPACE_COLLISION"
    RB_DIGEST_CONFLICT = "RB_DIGEST_CONFLICT"
    RB_VERSION_BUMP_INVALID = "RB_VERSION_BUMP_INVALID"
    RB_SCHEMA_INVALID = "RB_SCHEMA_INVALID"
    RB_SECRET_PARAMETER = "RB_SECRET_PARAMETER"
    RB_PARAM_UNKNOWN = "RB_PARAM_UNKNOWN"
    RB_PARAM_OVERSIZE = "RB_PARAM_OVERSIZE"
    RB_PARAM_OUT_OF_CONSTRAINT = "RB_PARAM_OUT_OF_CONSTRAINT"
    RB_GRAPH_CYCLE = "RB_GRAPH_CYCLE"
    RB_UNREACHABLE_NODE = "RB_UNREACHABLE_NODE"
    RB_UNSAFE_BINDING = "RB_UNSAFE_BINDING"
    RB_TYPE_MISMATCH = "RB_TYPE_MISMATCH"
    RB_UNPINNED_REFERENCE = "RB_UNPINNED_REFERENCE"
    RB_REFERENCE_UNKNOWN = "RB_REFERENCE_UNKNOWN"
    RB_REFERENCE_YANKED = "RB_REFERENCE_YANKED"
    RB_CAPABILITY_SUPERSET = "RB_CAPABILITY_SUPERSET"
    RB_CAPABILITY_MISSING = "RB_CAPABILITY_MISSING"
    RB_ADMIN_CAPABILITY_FORBIDDEN = "RB_ADMIN_CAPABILITY_FORBIDDEN"
    RB_POLICY_MISSING = "RB_POLICY_MISSING"
    RB_POLICY_CLASS_TOO_WEAK = "RB_POLICY_CLASS_TOO_WEAK"
    RB_APPROVAL_CLASS_TOO_WEAK = "RB_APPROVAL_CLASS_TOO_WEAK"
    RB_DESTRUCTIVE_UNDERDECLARED = "RB_DESTRUCTIVE_UNDERDECLARED"
    RB_IRREVERSIBLE_UNACCEPTED = "RB_IRREVERSIBLE_UNACCEPTED"
    RB_ROLLBACK_UNDECLARED = "RB_ROLLBACK_UNDECLARED"
    RB_COMPENSATION_UNREGISTERED = "RB_COMPENSATION_UNREGISTERED"
    RB_TIMEOUT_MISSING = "RB_TIMEOUT_MISSING"
    RB_TIMEOUT_INCONSISTENT = "RB_TIMEOUT_INCONSISTENT"
    RB_RETRY_CLASS_MISSING = "RB_RETRY_CLASS_MISSING"
    RB_AGENTIC_NOT_PERMITTED = "RB_AGENTIC_NOT_PERMITTED"
    RB_OWNER_UNRESOLVABLE = "RB_OWNER_UNRESOLVABLE"
    RB_OWNER_KIND_INSUFFICIENT = "RB_OWNER_KIND_INSUFFICIENT"
    RB_REVIEW_CADENCE_INVALID = "RB_REVIEW_CADENCE_INVALID"
    RB_TESTS_MISSING = "RB_TESTS_MISSING"
    RB_TESTS_STALE = "RB_TESTS_FAILED"
    RB_COMPILE_NONDETERMINISTIC = "RB_COMPILE_NONDETERMINISTIC"
    RB_DIGEST_MISMATCH = "RB_DIGEST_MISMATCH"
    RB_DIGEST_REQUIRED = "RB_DIGEST_REQUIRED"
    RB_UNKNOWN = "RB_UNKNOWN"
    RB_UNKNOWN_VERSION = "RB_UNKNOWN_VERSION"
    RB_YANKED = "RB_YANKED"
    RB_NOT_PROMOTED = "RB_NOT_PROMOTED"
    RB_REVIEW_OVERDUE = "RB_REVIEW_OVERDUE"
    RB_SCOPE_DENIED = "RB_SCOPE_DENIED"
    RB_POLICY_DENIED = "RB_POLICY_DENIED"
    RB_CAPABILITY_NOT_READY = "RB_CAPABILITY_NOT_READY"
    RB_INSUFFICIENT_CAPABILITY = "RB_INSUFFICIENT_CAPABILITY"
    RB_CAPABILITY_DRIFT = "RB_CAPABILITY_DRIFT"
    RB_IDEMPOTENCY_CONFLICT = "RB_IDEMPOTENCY_CONFLICT"
    RB_APPROVAL_INVALID = "RB_APPROVAL_INVALID"
    RB_LEASE_UNAVAILABLE = "RB_LEASE_UNAVAILABLE"
    RB_AUDIT_WRITE_FAILED = "RB_AUDIT_WRITE_FAILED"
    RB_REDACTION_UNPROVEN = "RB_REDACTION_UNPROVEN"
    RB_SECRET_IN_RESULT = "RB_SECRET_IN_RESULT"


class RunbookError(Exception):
    """Admission/invocation failure carrying a stable reason code."""

    def __init__(self, reason: RunbookReason, detail: str = "") -> None:
        super().__init__(f"{reason.value}: {detail}")
        self.reason = reason
        self.detail = detail


class RunbookState(StrEnum):
    ADMITTED = "ADMITTED"
    ACTIVE = "ACTIVE"
    DEPRECATED = "DEPRECATED"
    YANKED = "YANKED"


class PolicyClass(StrEnum):
    READ_ONLY = "READ_ONLY"
    MUTATING_LOW = "MUTATING_LOW"
    MUTATING_HIGH = "MUTATING_HIGH"
    RESTRICTED = "RESTRICTED"

    @classmethod
    def _rank(cls, value: PolicyClass) -> int:
        return {
            cls.READ_ONLY: 0,
            cls.MUTATING_LOW: 1,
            cls.MUTATING_HIGH: 2,
            cls.RESTRICTED: 3,
        }[value]

    def at_least(self, other: PolicyClass) -> bool:
        return self._rank(self) >= self._rank(other)


class ApprovalClass(StrEnum):
    NONE = "NONE"
    SINGLE = "SINGLE"
    DUAL = "DUAL"
    OWNER_PLUS_SECURITY = "OWNER_PLUS_SECURITY"

    @classmethod
    def _rank(cls, value: ApprovalClass) -> int:
        return {
            cls.NONE: 0,
            cls.SINGLE: 1,
            cls.DUAL: 2,
            cls.OWNER_PLUS_SECURITY: 3,
        }[value]

    def at_least(self, other: ApprovalClass) -> bool:
        return self._rank(self) >= self._rank(other)


class RollbackSupport(StrEnum):
    NOT_APPLICABLE = "NOT_APPLICABLE"
    AUTOMATIC = "AUTOMATIC"
    MANUAL = "MANUAL"
    NOT_SUPPORTED = "NOT_SUPPORTED"

    @classmethod
    def _rank(cls, value: RollbackSupport) -> int:
        return {
            cls.NOT_APPLICABLE: 0,
            cls.AUTOMATIC: 1,
            cls.MANUAL: 2,
            cls.NOT_SUPPORTED: 3,
        }[value]

    def weaker_than(self, other: RollbackSupport) -> bool:
        return self._rank(self) > self._rank(other)


class ParamType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    ENUM = "enum"
    RESOURCE_REF = "resource_ref"
    OBJECT = "object"
    ARRAY = "array"


class ParamSensitivity(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    SENSITIVE = "sensitive"


_SECRET_NAME_HINTS = (
    "token",
    "refresh_token",
    "api_token",
    "auth_token",
    "id_token",
    "secret",
    "password",
    "private_key",
    "cookie",
    "authorization",
    "credential",
    "client_secret",
)


@dataclass(frozen=True, slots=True)
class ParamConstraint:
    max_length: int | None = None
    pattern: str | None = None
    minimum: int | None = None
    maximum: int | None = None
    enum_values: tuple[str, ...] = ()
    max_items: int | None = None
    max_depth: int | None = None


@dataclass(frozen=True, slots=True)
class Parameter:
    name: str
    type: ParamType
    required: bool = True
    default: Any = None
    constraints: ParamConstraint = field(default_factory=ParamConstraint)
    sensitivity: ParamSensitivity = ParamSensitivity.PUBLIC
    resource_kind: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _NAME_RE.match(self.name):
            raise RunbookError(RunbookReason.RB_SCHEMA_INVALID, f"param name {self.name!r}")
        if self.required and self.default is not None:
            raise RunbookError(
                RunbookReason.RB_SCHEMA_INVALID, f"{self.name}: required params have no default"
            )
        if self.type is ParamType.RESOURCE_REF and not self.resource_kind:
            raise RunbookError(
                RunbookReason.RB_SCHEMA_INVALID, f"{self.name}: resource_ref needs resource_kind"
            )
        low = self.name.lower()
        if any(hint in low for hint in _SECRET_NAME_HINTS):
            raise RunbookError(RunbookReason.RB_SECRET_PARAMETER, self.name)


@dataclass(frozen=True, slots=True)
class RunbookNode:
    key: str
    tool: str
    tool_version: str
    args: Mapping[str, Any] = field(default_factory=dict)
    depends_on: tuple[str, ...] = ()
    bindings: tuple[Mapping[str, Any], ...] = ()
    compensation: str | None = None
    node_timeout_ms: int = DAG_MAX_NODE_TIMEOUT_MS
    idempotency_attempt_epoch: int = 0
    retry_class: str = "NONE"

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not _NAME_RE.match(self.key):
            raise RunbookError(RunbookReason.RB_SCHEMA_INVALID, f"node key {self.key!r}")
        if self.tool_version in ("", "*", "latest") or not self.tool_version:
            raise RunbookError(
                RunbookReason.RB_UNPINNED_REFERENCE, f"{self.key}: unpinned {self.tool}"
            )


@dataclass(frozen=True, slots=True)
class RunbookOwner:
    id: str
    kind: str
    contact: str
    review_cadence_days: int

    def __post_init__(self) -> None:
        if self.kind not in ("role", "team"):
            raise RunbookError(RunbookReason.RB_OWNER_KIND_INSUFFICIENT, self.kind)
        if not (1 <= self.review_cadence_days <= RUNBOOK_MAX_REVIEW_CADENCE_DAYS):
            raise RunbookError(
                RunbookReason.RB_REVIEW_CADENCE_INVALID,
                f"cadence {self.review_cadence_days}",
            )


@dataclass(frozen=True, slots=True)
class RunbookManifest:
    runbook_id: str
    version: str
    nodes: Sequence[RunbookNode]
    parameter_schema: Sequence[Parameter] = ()
    output_schema: Sequence[Parameter] = ()
    requires_capabilities: tuple[str, ...] = ()
    credential_capability_ids: tuple[str, ...] = ()
    resource_scope: str = ""
    min_capability_state: str = "READY"
    policy_class: PolicyClass = PolicyClass.READ_ONLY
    approval_class: ApprovalClass = ApprovalClass.NONE
    destructive_action: bool = False
    accepted_irreversibility: bool = False
    rollback_support: RollbackSupport = RollbackSupport.NOT_APPLICABLE
    timeout_ms: int = 900_000
    approval_ttl_ms: int = 300_000
    lease_ttl_ms: int = 60_000
    max_agentic_escalations: int = RUNBOOK_MAX_AGENTIC_ESCALATIONS_DEFAULT
    max_agentic_tokens: int = RUNBOOK_MAX_AGENTIC_TOKENS_DEFAULT
    owner: RunbookOwner | None = None
    requires_signature: bool = False
    # Editorial, non-digest fields:
    title: str = ""
    description: str = ""

    def __post_init__(self) -> None:
        if not _RUNBOOK_ID_RE.match(self.runbook_id):
            raise RunbookError(RunbookReason.RB_ID_INVALID, self.runbook_id)
        if len(self.runbook_id) > RUNBOOK_MAX_ID_LEN:
            raise RunbookError(RunbookReason.RB_ID_INVALID, "id too long")
        if self.requires_signature and self.max_agentic_tokens != 0:
            raise RunbookError(
                RunbookReason.RB_AGENTIC_NOT_PERMITTED, "signed runbook must not widen agentic"
            )
        if self.owner is None:
            raise RunbookError(RunbookReason.RB_OWNER_UNRESOLVABLE, "owner required")
        if self.timeout_ms <= 0 or self.timeout_ms > 900_000:
            raise RunbookError(RunbookReason.RB_TIMEOUT_MISSING, "timeout out of range")
        if self.approval_class is not ApprovalClass.NONE and self.approval_ttl_ms <= 0:
            raise RunbookError(RunbookReason.RB_TIMEOUT_MISSING, "approval_ttl required")
        if len(self.nodes) > DAG_MAX_NODES:
            raise RunbookError(RunbookReason.RB_TOO_LARGE, "too many nodes")

    @property
    def version_tuple(self) -> tuple[int, int, int]:
        return _parse_semver(self.version)


_NAME_RE = _re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_RUNBOOK_ID_RE = _re.compile(r"^RB-[A-Z0-9]+(-[A-Z0-9]+)+-[0-9]{3}$")


def _parse_semver(version: str) -> tuple[int, int, int]:
    parts = version.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise RunbookError(RunbookReason.RB_VERSION_BUMP_INVALID, version)
    return int(parts[0]), int(parts[1]), int(parts[2])


def runbook_id_disjoint_from_tools(runbook_id: str, tool_names: set[str]) -> None:
    """Runbooks and tools live in disjoint namespaces (ADR-0004)."""
    if runbook_id in tool_names:
        raise RunbookError(RunbookReason.RB_NAMESPACE_COLLISION, runbook_id)


def version_bump_valid(
    new: tuple[int, int, int],
    prev: tuple[int, int, int] | None,
    weakening: bool,
) -> None:
    """A weakening change forces a MAJOR bump (ADR-0017/Phase 6 rule 3)."""
    if prev is None:
        if new != (1, 0, 0):
            raise RunbookError(RunbookReason.RB_VERSION_BUMP_INVALID, "first version must be 1.0.0")
        return
    if new <= prev:
        raise RunbookError(RunbookReason.RB_VERSION_BUMP_INVALID, "must increase")
    if weakening and new[0] == prev[0]:
        raise RunbookError(RunbookReason.RB_VERSION_BUMP_INVALID, "weakening requires MAJOR bump")


# Re-export for callers that construct manifests from plain dicts.
__all__ = [
    "RUNBOOK_FEATURE_ENABLED",
    "RUNBOOK_IR_SCHEMA_VERSION",
    "ApprovalClass",
    "ParamConstraint",
    "ParamSensitivity",
    "ParamType",
    "Parameter",
    "PolicyClass",
    "RollbackSupport",
    "RunbookError",
    "RunbookManifest",
    "RunbookNode",
    "RunbookOwner",
    "RunbookReason",
    "RunbookState",
    "runbook_id_disjoint_from_tools",
    "version_bump_valid",
]
