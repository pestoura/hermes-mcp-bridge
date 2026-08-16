"""EPIC-03 capability-oriented Vault credential provider.

This module deliberately stops at the in-process capability boundary. It does
not know a caller-supplied Vault path, perform AppRole bootstrap, unwrap a
SecretID, hold a Vault token, or expose credential material. A concrete live
Vault transport is injected behind :class:`VaultCapabilityClient` only after the
live/HITL bootstrap gates are satisfied.

The initial MVP allow-list contains only the GitHub read credential capability.
Operational capabilities such as ``github.repo_read`` remain owned by the
ProviderGateway/manifest layer; this provider resolves only credential
capabilities such as ``github.read``.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any, Protocol

from .provider_contract import ProviderReason
from .provider_credentials import CredentialError, CredentialRecord

DEFAULT_VAULT_CAPABILITIES: frozenset[tuple[str, str]] = frozenset(
    {("github", "github.read")}
)


class VaultCapabilityGrant(Protocol):
    """Opaque request-scoped grant returned by an injected Vault client."""

    def apply(self, headers: dict[str, str]) -> dict[str, str]: ...

    def revoke(self) -> None: ...


class VaultCapabilityClient(Protocol):
    """Minimal internal port; logical capabilities only, never arbitrary paths."""

    def status(self, provider_id: str, credential_capability_id: str) -> bool: ...

    def request(
        self, provider_id: str, credential_capability_id: str
    ) -> VaultCapabilityGrant: ...

    def revoke(self, provider_id: str, credential_capability_id: str) -> None: ...


def _call_with_sanitized_error(
    operation: Callable[[], Any],
    credential_capability_id: str,
) -> tuple[Any | None, CredentialError | None]:
    """Run a Vault/grant call and detach any backend exception before raising."""
    try:
        return operation(), None
    except CredentialError as exc:
        return None, CredentialError(exc.reason, credential_capability_id)
    except Exception:
        return None, CredentialError(
            ProviderReason.E_CRED_UNAVAILABLE, credential_capability_id
        )


class VaultCredentialProvider:
    """ProviderCredentialBroker backend for an explicitly allowed Vault capability set."""

    __slots__ = ("_allowed", "_client")

    def __init__(
        self,
        *,
        client: VaultCapabilityClient,
        allowed_capabilities: frozenset[tuple[str, str]] = DEFAULT_VAULT_CAPABILITIES,
    ) -> None:
        allowed = frozenset(allowed_capabilities)
        if not allowed:
            raise ValueError("allowed_capabilities must not be empty")
        for provider_id, capability_id in allowed:
            if (
                not provider_id
                or not capability_id
                or provider_id.strip() != provider_id
                or capability_id.strip() != capability_id
                or "*" in provider_id
                or "*" in capability_id
            ):
                raise ValueError("allowed_capabilities must contain exact identifiers")
        self._client = client
        self._allowed = allowed

    def _allowed_pair(self, provider_id: str, credential_capability_id: str) -> bool:
        return (provider_id, credential_capability_id) in self._allowed

    def _require_allowed(self, provider_id: str, credential_capability_id: str) -> None:
        if not self._allowed_pair(provider_id, credential_capability_id):
            raise CredentialError(ProviderReason.E_CRED_CROSS_DOMAIN, credential_capability_id)

    @staticmethod
    def _sanitize_grant_apply(
        apply: Callable[[dict[str, str]], dict[str, str]],
        credential_capability_id: str,
    ) -> Callable[[dict[str, str]], dict[str, str]]:
        def _apply(headers: dict[str, str]) -> dict[str, str]:
            result, error = _call_with_sanitized_error(
                lambda: apply(dict(headers)), credential_capability_id
            )
            if error is not None:
                raise error
            return result

        return _apply

    @staticmethod
    def _sanitize_grant_revoke(
        revoke: Callable[[], None],
        credential_capability_id: str,
    ) -> Callable[[], None]:
        def _revoke() -> None:
            _, error = _call_with_sanitized_error(revoke, credential_capability_id)
            if error is not None:
                raise error

        return _revoke

    def status(self, provider_id: str, credential_capability_id: str) -> bool:
        """Return readiness only; backend errors degrade to unavailable, never allow."""
        if not self._allowed_pair(provider_id, credential_capability_id):
            return False
        try:
            return bool(self._client.status(provider_id, credential_capability_id))
        except Exception:
            return False

    def request(self, provider_id: str, credential_capability_id: str) -> CredentialRecord:
        """Mint one opaque request-scoped credential record or fail closed."""
        self._require_allowed(provider_id, credential_capability_id)
        if not self.status(provider_id, credential_capability_id):
            raise CredentialError(ProviderReason.E_CRED_UNAVAILABLE, credential_capability_id)

        grant, error = _call_with_sanitized_error(
            lambda: self._client.request(provider_id, credential_capability_id),
            credential_capability_id,
        )
        if error is not None:
            raise error

        apply: Callable[[dict[str, str]], dict[str, str]] | None = getattr(
            grant, "apply", None
        )
        revoke: Callable[[], None] | None = getattr(grant, "revoke", None)
        if not callable(apply) or not callable(revoke):
            if callable(revoke):
                with contextlib.suppress(Exception):
                    revoke()
            raise CredentialError(
                ProviderReason.E_CRED_UNAVAILABLE, credential_capability_id
            ) from None

        return CredentialRecord(
            provider_id=provider_id,
            credential_capability_id=credential_capability_id,
            ready=True,
            apply=self._sanitize_grant_apply(apply, credential_capability_id),
            revoke=self._sanitize_grant_revoke(revoke, credential_capability_id),
        )

    def revoke(self, provider_id: str, credential_capability_id: str) -> None:
        """Disable/revoke the logical capability at the injected client boundary."""
        self._require_allowed(provider_id, credential_capability_id)
        _, error = _call_with_sanitized_error(
            lambda: self._client.revoke(provider_id, credential_capability_id),
            credential_capability_id,
        )
        if error is not None:
            raise error

    def __repr__(self) -> str:
        return "<VaultCredentialProvider redacted>"

    __str__ = __repr__


__all__ = [
    "DEFAULT_VAULT_CAPABILITIES",
    "VaultCapabilityClient",
    "VaultCapabilityGrant",
    "VaultCredentialProvider",
]
