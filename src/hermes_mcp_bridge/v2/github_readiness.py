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

from typing import Protocol, runtime_checkable

from .credentials import CredentialCapabilityStatus
from .enums import CapabilityState
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
        if key != GITHUB_READ_CREDENTIAL_CAPABILITY:
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
]
