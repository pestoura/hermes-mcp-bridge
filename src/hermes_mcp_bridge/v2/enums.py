"""Canonical V2 enums.

Security-relevant decisions are enumerated, never free strings. Every member
value is the lowercase/uppercase canonical token used in serialization; the
serialized form is the ``value``, which is stable and part of the capability
snapshot hash.
"""

from __future__ import annotations

from collections.abc import Sequence
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
    IDEMPOTENT_BY_PRECONDITION = "IDEMPOTENT_BY_PRECONDITION"
    NON_IDEMPOTENT = "NON_IDEMPOTENT"

    @property
    def requires_idempotency_key(self) -> bool:
        return self is IdempotencySemantics.KEYED_IDEMPOTENT

    @property
    def requires_precondition(self) -> bool:
        """Phase 3 mutations pin an expected SHA instead of a caller key."""
        return self is IdempotencySemantics.IDEMPOTENT_BY_PRECONDITION


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


@unique
class WriteCapabilityId(StrEnum):
    """Phase 3 write capability ids (``credential-split.md``).

    These are deliberately distinct identifiers from the accepted read
    capability id: no member of this enum may ever equal the read capability,
    and no write capability may satisfy a read tool. See
    :data:`READ_CAPABILITY_ID`.
    """

    BRANCH = "github.write.branch"
    PR = "github.write.pr"
    MERGE = "github.write.merge"

    @property
    def is_merge(self) -> bool:
        return self is WriteCapabilityId.MERGE


#: The accepted Phase 2 read capability id. Kept here so the disjointness rule
#: is expressible in one place; never a member of :class:`WriteCapabilityId`.
READ_CAPABILITY_ID = "github.read"

#: Repository administration permission. Its presence makes a write capability
#: NOT_READY (ADR-0020); it is never requested and never accepted.
FORBIDDEN_PERMISSION = "Administration"


@unique
class MutationOutcome(StrEnum):
    """Terminal (or pending) state of a mutation attempt.

    ``AMBIGUOUS`` is not a failure and not a success: the provider state is
    unknown and a reconciliation read is mandatory before any new attempt. A
    blind retry from this state is forbidden.
    """

    PENDING = "PENDING"
    COMMITTED = "COMMITTED"
    FAILED_CLEAN = "FAILED_CLEAN"
    AMBIGUOUS = "AMBIGUOUS"
    DENIED = "DENIED"

    @property
    def is_terminal(self) -> bool:
        return self is not MutationOutcome.PENDING

    @property
    def requires_reconciliation(self) -> bool:
        return self is MutationOutcome.AMBIGUOUS

    @property
    def allows_new_attempt(self) -> bool:
        """Only a provably clean failure may be attempted again."""
        return self is MutationOutcome.FAILED_CLEAN


@unique
class IdempotencyStatus(StrEnum):
    """What the idempotency layer reported for this request."""

    NEW = "NEW"
    REPLAYED = "REPLAYED"
    IN_PROGRESS = "IN_PROGRESS"

    @property
    def executes_provider_call(self) -> bool:
        return self is IdempotencyStatus.NEW


@unique
class ApprovalState(StrEnum):
    """Lifecycle of a single-use approval bound to an ``operation_digest``."""

    PENDING = "PENDING"
    CONSUMED = "CONSUMED"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"

    @property
    def is_usable(self) -> bool:
        return self is ApprovalState.PENDING


@unique
class MutationReasonCode(StrEnum):
    """Stable, redacted mutation reason codes.

    Never contain repository content, arguments, credential material or
    provider payloads. Shared by L1..L4 so lanes emit one taxonomy.
    """

    # scope / registry / policy (ordering stages 1-3)
    REPOSITORY_OUT_OF_SCOPE = "REPOSITORY_OUT_OF_SCOPE"
    UNKNOWN_MUTATION = "UNKNOWN_MUTATION"
    MUTATION_NOT_REGISTERED = "MUTATION_NOT_REGISTERED"
    DESTRUCTIVE_OPERATION_FORBIDDEN = "DESTRUCTIVE_OPERATION_FORBIDDEN"

    # credentials / capability (stage 4)
    WRITE_CAPABILITY_NOT_READY = "WRITE_CAPABILITY_NOT_READY"
    WRITE_CAPABILITY_MISMATCH = "WRITE_CAPABILITY_MISMATCH"
    READ_CAPABILITY_CANNOT_MUTATE = "READ_CAPABILITY_CANNOT_MUTATE"
    PERMISSION_SUPERSET = "PERMISSION_SUPERSET"
    ADMINISTRATION_PERMISSION_PRESENT = "ADMINISTRATION_PERMISSION_PRESENT"

    # approval / digest (stage 5)
    APPROVAL_MISSING = "APPROVAL_MISSING"
    APPROVAL_UNKNOWN = "APPROVAL_UNKNOWN"
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    APPROVAL_ALREADY_CONSUMED = "APPROVAL_ALREADY_CONSUMED"
    APPROVAL_DIGEST_MISMATCH = "APPROVAL_DIGEST_MISMATCH"
    APPROVAL_SCOPE_MISMATCH = "APPROVAL_SCOPE_MISMATCH"
    APPROVER_NOT_DISTINCT = "APPROVER_NOT_DISTINCT"

    # idempotency / concurrency (stages 6-7)
    IDEMPOTENT_REPLAY = "IDEMPOTENT_REPLAY"
    OPERATION_IN_PROGRESS = "OPERATION_IN_PROGRESS"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    PRECONDITION_DRIFT = "PRECONDITION_DRIFT"
    REF_ALREADY_EXISTS = "REF_ALREADY_EXISTS"

    # governed merge (lane L6)
    MERGE_NOT_PERMITTED = "MERGE_NOT_PERMITTED"
    MERGE_TARGET_DEFAULT_BRANCH = "MERGE_TARGET_DEFAULT_BRANCH"
    PULL_REQUEST_NOT_MERGEABLE = "PULL_REQUEST_NOT_MERGEABLE"
    REQUIRED_CHECKS_NOT_GREEN = "REQUIRED_CHECKS_NOT_GREEN"
    PROTECTION_STATE_UNVERIFIABLE = "PROTECTION_STATE_UNVERIFIABLE"
    REQUIRED_REVIEWS_NOT_SATISFIED = "REQUIRED_REVIEWS_NOT_SATISFIED"

    # audit / evidence (stage 8)
    AUDIT_RECORD_UNWRITABLE = "AUDIT_RECORD_UNWRITABLE"
    REDACTION_UNPROVEN = "REDACTION_UNPROVEN"

    # input validation
    INVALID_REF_NAME = "INVALID_REF_NAME"
    INVALID_ARGUMENTS = "INVALID_ARGUMENTS"


@unique
class MutationStage(StrEnum):
    """The fixed, non-reorderable Phase 3 preflight ordering.

    ``MUTATION_STAGE_ORDER`` is the single source of truth for lane L5; L1..L4
    reference it so no lane invents its own ordering.
    """

    SCOPE = "SCOPE"
    REGISTRY = "REGISTRY"
    POLICY = "POLICY"
    CREDENTIAL = "CREDENTIAL"
    APPROVAL = "APPROVAL"
    IDEMPOTENCY = "IDEMPOTENCY"
    PRECONDITION_REVALIDATION = "PRECONDITION_REVALIDATION"
    WRITE_AHEAD_AUDIT = "WRITE_AHEAD_AUDIT"
    PROVIDER_CALL = "PROVIDER_CALL"
    READ_BACK = "READ_BACK"
    RESULT_SHAPING = "RESULT_SHAPING"


#: Fixed execution order. Lanes must not reorder or skip a stage.
#:
#: Typed as ``Sequence`` rather than a variadic tuple annotation so the module
#: stays free of a literal ellipsis token, which the repository's placeholder
#: scan (``tests/test_server_tools.py``) treats as a stub marker.
MUTATION_STAGE_ORDER: Sequence[MutationStage] = (
    MutationStage.SCOPE,
    MutationStage.REGISTRY,
    MutationStage.POLICY,
    MutationStage.CREDENTIAL,
    MutationStage.APPROVAL,
    MutationStage.IDEMPOTENCY,
    MutationStage.PRECONDITION_REVALIDATION,
    MutationStage.WRITE_AHEAD_AUDIT,
    MutationStage.PROVIDER_CALL,
    MutationStage.READ_BACK,
    MutationStage.RESULT_SHAPING,
)


__all__ = [
    "FORBIDDEN_PERMISSION",
    "MUTATION_STAGE_ORDER",
    "READ_CAPABILITY_ID",
    "ApprovalRequirement",
    "ApprovalState",
    "CapabilityState",
    "ExecutionMode",
    "IdempotencySemantics",
    "IdempotencyStatus",
    "MutationClass",
    "MutationOutcome",
    "MutationReasonCode",
    "MutationStage",
    "PolicyDecision",
    "ResultShaping",
    "RetryClass",
    "SecurityTier",
    "Stability",
    "WriteCapabilityId",
]
