"""Minimal runtime readiness broker for the ``github.read`` capability.

This is the Phase 2 runtime counterpart of the Phase 1
:class:`~hermes_mcp_bridge.v2.credentials.StaticCredentialBroker`. It answers
exactly one question — *is ``github.read`` ready?* — by delegating to a
provider probe that never returns secret material.

There is deliberately **no** API here that can return a value, a path, an
environment variable name or anything derived from a secret. The broker maps a
provider status to a :class:`CapabilityState` and nothing else.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from .credentials import CredentialCapabilityStatus
from .enums import (
    FORBIDDEN_PERMISSION,
    READ_CAPABILITY_ID,
    CapabilityState,
    MutationReasonCode,
    WriteCapabilityId,
)
from .github_registry import GITHUB_READ_CREDENTIAL_CAPABILITY
from .github_secret_provider import AuthorizationStatus

#: Statuses that mean "configured but authorization was refused".
_DENIED = frozenset(
    {
        AuthorizationStatus.CLASSIC_PAT_REJECTED,
        AuthorizationStatus.ENV_MATERIAL_REJECTED,
        AuthorizationStatus.PROVIDER_TYPE_MISMATCH,
        AuthorizationStatus.CAPABILITY_MISMATCH,
        AuthorizationStatus.REPOSITORY_OUT_OF_SCOPE,
        AuthorizationStatus.FILE_PERMISSIONS_TOO_OPEN,
    }
)


@runtime_checkable
class AuthorizationProbe(Protocol):
    """Anything able to report a non-secret authorization status."""

    def probe(self) -> AuthorizationStatus: ...


def capability_state_for(status: AuthorizationStatus) -> CapabilityState:
    """Map a provider status to a fail-closed capability state."""
    if status is AuthorizationStatus.READY:
        return CapabilityState.READY
    if status in _DENIED:
        return CapabilityState.DENIED
    return CapabilityState.UNAVAILABLE


def normalize_permissions(granted: Mapping[str, str] | None) -> dict[str, str] | None:
    """Lowercase permission names/levels, or ``None`` when nothing was probed.

    ``None`` is deliberately preserved rather than coerced to an empty map: a
    capability whose permissions were never observed is *not* compliant.
    """
    if granted is None:
        return None
    normalized: dict[str, str] = {}
    for name, level in granted.items():
        if not isinstance(name, str) or not isinstance(level, str):
            continue
        key = name.strip().lower()
        if key:
            normalized[key] = level.strip().lower()
    return normalized


def has_forbidden_permission(granted: Mapping[str, str] | None) -> bool:
    """True when the ``Administration`` permission is present at any level.

    ADR-0020: the permission is never requested and never accepted, so its mere
    presence — even at ``read`` — makes the capability NOT_READY.
    """
    normalized = normalize_permissions(granted)
    if not normalized:
        return False
    forbidden = FORBIDDEN_PERMISSION.strip().lower()
    return forbidden in normalized


def exact_permission_failure(
    intended: Mapping[str, str],
    granted: Mapping[str, str] | None,
) -> MutationReasonCode | None:
    """Compare a granted permission map against the intended one, exactly.

    Returns ``None`` only on an exact match. The verdict order is fixed:

    1. ``Administration`` present  -> ``ADMINISTRATION_PERMISSION_PRESENT``
    2. nothing probed              -> ``WRITE_CAPABILITY_NOT_READY``
    3. any extra permission        -> ``PERMISSION_SUPERSET``
    4. missing/differing level     -> ``WRITE_CAPABILITY_MISMATCH``

    A superset is a failure, not a convenience (``credential-split.md`` rule 5).
    """
    if has_forbidden_permission(granted):
        return MutationReasonCode.ADMINISTRATION_PERMISSION_PRESENT
    normalized = normalize_permissions(granted)
    if normalized is None:
        return MutationReasonCode.WRITE_CAPABILITY_NOT_READY
    expected = normalize_permissions(intended) or {}
    if set(normalized) - set(expected):
        return MutationReasonCode.PERMISSION_SUPERSET
    if normalized != expected:
        return MutationReasonCode.WRITE_CAPABILITY_MISMATCH
    return None


def read_capability_satisfies(credential_capability_id: str) -> bool:
    """True only for the accepted read capability id.

    Kept next to the read broker so the disjointness rule is checkable from the
    read side too: a ``github.write.*`` id never satisfies a read tool.
    """
    key = str(credential_capability_id).strip().lower()
    if key in {member.value for member in WriteCapabilityId}:
        return False
    return key == READ_CAPABILITY_ID


class GitHubReadReadinessBroker:
    """Fail-closed ``github.read`` readiness view over a provider probe."""

    __slots__ = ("_probe", "_provider_name")

    def __init__(
        self,
        probe: AuthorizationProbe,
        *,
        provider_name: str = "github",
    ) -> None:
        self._probe = probe
        self._provider_name = provider_name

    def status(
        self,
        credential_capability_id: str,
    ) -> CredentialCapabilityStatus | None:
        key = str(credential_capability_id).strip().lower()
        if not read_capability_satisfies(key) or key != GITHUB_READ_CREDENTIAL_CAPABILITY:
            return None
        try:
            probe_status = self._probe.probe()
        except Exception:
            probe_status = AuthorizationStatus.FILE_UNREADABLE
        return CredentialCapabilityStatus(
            capability_id=GITHUB_READ_CREDENTIAL_CAPABILITY,
            provider=self._provider_name,
            state=capability_state_for(probe_status),
        )

    def is_ready(self, credential_capability_id: str) -> bool:
        status = self.status(credential_capability_id)
        return bool(status and status.is_ready)

    def __repr__(self) -> str:
        return "GitHubReadReadinessBroker(capability='github.read')"

    __str__ = __repr__


__all__ = [
    "AuthorizationProbe",
    "GitHubReadReadinessBroker",
    "capability_state_for",
    "exact_permission_failure",
    "has_forbidden_permission",
    "normalize_permissions",
    "read_capability_satisfies",
]
