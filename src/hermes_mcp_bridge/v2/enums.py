"""Canonical V2 enums.

Security-relevant decisions are enumerated, never free strings. Every member
value is the lowercase/uppercase canonical token used in serialization; the
serialized form is the ``value``, which is stable and part of the capability
snapshot hash.
"""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class ExecutionMode(StrEnum):
    """How an operation is executed. See ``docs/v2/architecture/execution-modes.md``."""

    DIRECT = "DIRECT"
    BATCH = "BATCH"
    DAG = "DAG"
    RUNBOOK = "RUNBOOK"
    HYBRID = "HYBRID"
    AGENTIC = "AGENTIC"


@unique
class SecurityTier(StrEnum):
    """Exactly the T0..T4 tiers from ``docs/v2/architecture/tool-registry.md``."""

    T0 = "T0"  # read-only harmless
    T1 = "T1"  # read-only sensitive
    T2 = "T2"  # low-risk mutation
    T3 = "T3"  # privileged mutation
    T4 = "T4"  # destructive/admin

    @property
    def is_read_only_tier(self) -> bool:
        return self in (SecurityTier.T0, SecurityTier.T1)

    @property
    def is_destructive(self) -> bool:
        return self is SecurityTier.T4


@unique
class MutationClass(StrEnum):
    """Nature of the state change an operation performs."""

    NONE = "NONE"
    LOW = "LOW"
    STANDARD = "STANDARD"
    PRIVILEGED = "PRIVILEGED"
    DESTRUCTIVE = "DESTRUCTIVE"

    @property
    def mutates(self) -> bool:
        return self is not MutationClass.NONE

    @property
    def is_destructive(self) -> bool:
        return self is MutationClass.DESTRUCTIVE


@unique
class IdempotencySemantics(StrEnum):
    """Whether repeating the operation is safe, and under which condition."""

    READ = "READ"
    NATURALLY_IDEMPOTENT = "NATURALLY_IDEMPOTENT"
    KEYED_IDEMPOTENT = "KEYED_IDEMPOTENT"
    NON_IDEMPOTENT = "NON_IDEMPOTENT"

    @property
    def requires_idempotency_key(self) -> bool:
        return self is IdempotencySemantics.KEYED_IDEMPOTENT


@unique
class RetryClass(StrEnum):
    """Retry taxonomy from ``docs/v2/architecture/policy-and-governance.md``."""

    RETRY_SAFE = "RETRY_SAFE"
    RETRY_CONDITIONAL = "RETRY_CONDITIONAL"
    NO_RETRY = "NO_RETRY"


@unique
class ApprovalRequirement(StrEnum):
    """Whether human approval is required before execution.

    Phase 1 semantics, as enforced by
    :meth:`hermes_mcp_bridge.v2.policy.PolicyEngine.evaluate_tool`:

    * ``NOT_REQUIRED`` — an ALLOW rule yields ALLOW.
    * ``CONDITIONAL`` — the *condition is the policy rule*. An explicit
      ``APPROVAL_REQUIRED`` rule yields APPROVAL_REQUIRED; an explicit
      ``ALLOW`` rule yields ALLOW. This is what distinguishes it from
      ``REQUIRED``.
    * ``REQUIRED`` — always APPROVAL_REQUIRED, even under an ALLOW rule.

    The destructive/T4 backstop is applied before any of this and denies first.
    """

    NOT_REQUIRED = "NOT_REQUIRED"
    CONDITIONAL = "CONDITIONAL"
    REQUIRED = "REQUIRED"

    @property
    def may_require_approval(self) -> bool:
        return self is not ApprovalRequirement.NOT_REQUIRED


@unique
class ResultShaping(StrEnum):
    """Whether the tool supports server-side result shaping."""

    UNSUPPORTED = "UNSUPPORTED"
    SUPPORTED = "SUPPORTED"
    REQUIRED = "REQUIRED"


@unique
class Stability(StrEnum):
    """Lifecycle stability of a tool definition."""

    EXPERIMENTAL = "EXPERIMENTAL"
    BETA = "BETA"
    STABLE = "STABLE"
    DEPRECATED = "DEPRECATED"


@unique
class PolicyDecision(StrEnum):
    """Phase 1 policy decisions. There is no implicit fourth outcome."""

    ALLOW = "ALLOW"
    DENY = "DENY"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"

    @property
    def is_executable_without_approval(self) -> bool:
        return self is PolicyDecision.ALLOW


@unique
class CapabilityState(StrEnum):
    """Capability lifecycle state.

    ``configured`` != ``available`` != ``healthy`` != ``ready``. The four
    boolean properties are the only sanctioned way to ask those questions:

    ==============  ==========  =========  =======  =====
    state           configured  available  healthy  ready
    ==============  ==========  =========  =======  =====
    CONFIGURED      yes         no         no       no
    AVAILABLE       yes         yes        no       no
    HEALTHY         yes         yes        yes      no
    READY           yes         yes        yes      yes
    DEGRADED        yes         yes        no       no
    UNAVAILABLE     yes         no         no       no
    DENIED          yes         no         no       no
    ==============  ==========  =========  =======  =====

    ``DEGRADED`` is deliberately *available but not healthy*: the backend
    answers, but the capability must not be treated as ready. ``DENIED``
    (authorization refused, the ``UNAUTHORIZED`` state named in ADR-0004) is
    configured but neither available nor ready.
    """

    CONFIGURED = "CONFIGURED"
    AVAILABLE = "AVAILABLE"
    HEALTHY = "HEALTHY"
    READY = "READY"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"
    DENIED = "DENIED"

    @property
    def is_configured(self) -> bool:
        """Declared and wired up. True for every member of this enum."""
        return True

    @property
    def is_available(self) -> bool:
        """The backend is reachable/usable in principle."""
        return self in (
            CapabilityState.AVAILABLE,
            CapabilityState.HEALTHY,
            CapabilityState.READY,
            CapabilityState.DEGRADED,
        )

    @property
    def is_healthy(self) -> bool:
        """Available *and* passing its health check."""
        return self in (CapabilityState.HEALTHY, CapabilityState.READY)

    @property
    def is_ready(self) -> bool:
        """Healthy *and* authorized for use. The only state that may execute."""
        return self is CapabilityState.READY

    @property
    def is_denied(self) -> bool:
        return self is CapabilityState.DENIED


__all__ = [
    "ApprovalRequirement",
    "CapabilityState",
    "ExecutionMode",
    "IdempotencySemantics",
    "MutationClass",
    "PolicyDecision",
    "ResultShaping",
    "RetryClass",
    "SecurityTier",
    "Stability",
]
