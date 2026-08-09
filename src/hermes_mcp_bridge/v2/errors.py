"""Error taxonomy for the V2 registry core.

Every error message is safe to log: callers must never place credential
material, secret paths or raw arguments in these messages.
"""

from __future__ import annotations

from .enums import MutationReasonCode, MutationStage


class V2Error(Exception):
    """Base class for all V2 registry-core errors."""


class RegistryValidationError(V2Error, ValueError):
    """A tool/capability definition violated a canonical invariant."""


class DuplicateToolError(RegistryValidationError):
    """A tool_id was registered more than once."""


class DuplicateCapabilityError(RegistryValidationError):
    """A capability_id was registered more than once."""


class UnknownToolError(V2Error, KeyError):
    """Fail-closed lookup of a tool that is not registered."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.args[0]) if self.args else self.__class__.__name__


class UnknownCapabilityError(V2Error, KeyError):
    """Fail-closed lookup of a capability that is not registered."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.args[0]) if self.args else self.__class__.__name__


class RegistryFrozenError(V2Error, RuntimeError):
    """Mutation attempted on a frozen registry."""


class PolicyValidationError(V2Error, ValueError):
    """A policy rule set is not acceptable (e.g. permissive wildcard)."""


class MutationError(V2Error):
    """Base class for every Phase 3 mutation error.

    Instances carry a stable :class:`~hermes_mcp_bridge.v2.enums.MutationReasonCode`
    and the :class:`~hermes_mcp_bridge.v2.enums.MutationStage` at which the
    failure was raised. The string form is exactly ``"<STAGE>:<REASON>"`` — a
    fixed, redacted token pair. Arguments, repository content, provider bodies
    and credential material must never be added to the message.
    """

    def __init__(
        self,
        reason: MutationReasonCode,
        stage: MutationStage,
        *,
        detail: str = "",
    ) -> None:
        self.reason = reason
        self.stage = stage
        #: Optional operator-facing, non-secret note. Excluded from ``str``.
        self.detail = detail
        super().__init__(f"{stage.value}:{reason.value}")

    def __str__(self) -> str:
        return f"{self.stage.value}:{self.reason.value}"


class MutationDeniedError(MutationError):
    """Fail-closed DENY. No provider call was issued and no state changed."""


class MutationScopeError(MutationDeniedError):
    """Repository outside the exact allow-list.

    Raised before any credential resolution and before any HTTP request.
    """


class WriteCapabilityError(MutationDeniedError):
    """Write capability unusable: not ready, mismatched, or over-permissioned."""


class ApprovalError(MutationDeniedError):
    """Approval missing, unknown, expired, mis-scoped, reused or mis-bound."""


class DigestMismatchError(ApprovalError):
    """The approval's ``operation_digest`` does not bind these arguments."""


class IdempotencyConflictError(MutationDeniedError):
    """A record for this idempotency key forbids issuing a new write."""


class ConcurrencyDriftError(MutationDeniedError):
    """Expected head/base SHA drifted between approval and execution."""


class AuditWriteError(MutationDeniedError):
    """The write-ahead audit record could not be made durable.

    Fail-closed: no mutation may be attempted after this error.
    """


class MutationIndeterminateError(MutationError):
    """Provider state is unknown (timeout, reset, unverifiable read-back).

    This is neither success nor clean failure: a reconciliation read is
    mandatory and a blind retry is forbidden.
    """


__all__ = [
    "ApprovalError",
    "AuditWriteError",
    "ConcurrencyDriftError",
    "DigestMismatchError",
    "DuplicateCapabilityError",
    "DuplicateToolError",
    "IdempotencyConflictError",
    "MutationDeniedError",
    "MutationError",
    "MutationIndeterminateError",
    "MutationScopeError",
    "PolicyValidationError",
    "RegistryFrozenError",
    "RegistryValidationError",
    "UnknownCapabilityError",
    "UnknownToolError",
    "V2Error",
    "WriteCapabilityError",
]
