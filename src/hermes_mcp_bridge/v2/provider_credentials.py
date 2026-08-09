"""Phase 7 per-provider credential domains and request-scoped authorization.

> **V2 · PHASE 7 · runtime, disabled by default behind ``PROVIDER_FEATURE_ENABLED``**

Resolution is keyed by ``(provider_id, credential_capability_id)``. A provider
requesting a capability outside its own domain is refused *at the broker* with
``E-CRED-CROSS-DOMAIN`` and the refusal is auditable.

The broker returns two very different things:

* **status** — a boolean readiness answer, safe for the registry/health path;
* **an authorization handle** — request-scoped, deadline-bound, single-use, and
  deliberately **non-serializable**: ``__repr__``/``__str__`` never render the
  material, the object rejects ``copy``/``pickle``/``json`` and the material is
  reachable only through :meth:`AuthorizationHandle.apply` at the execution
  boundary.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .provider_contract import CredentialDomain, ProviderReason

#: A handle is valid only for the request that minted it.
DEFAULT_HANDLE_TTL_MS = 30_000


class CredentialError(RuntimeError):
    """Fail-closed credential refusal with a stable, redacted reason code."""

    def __init__(self, reason: ProviderReason, subject: str) -> None:
        self.reason = reason
        self.subject = subject
        super().__init__(f"{reason.value}:{subject}")


class AuthorizationHandle:
    """Single-use, deadline-bound authorization. Never serializable.

    The credential material is held in a closure and applied to an outbound
    request by :meth:`apply`; there is no accessor that returns it.
    """

    __slots__ = ("_apply", "_capability_id", "_deadline_ms", "_provider_id", "_spent")

    def __init__(
        self,
        *,
        provider_id: str,
        capability_id: str,
        apply: Callable[[dict[str, str]], dict[str, str]],
        deadline_ms: int,
    ) -> None:
        self._provider_id = provider_id
        self._capability_id = capability_id
        self._apply = apply
        self._deadline_ms = deadline_ms
        self._spent = False

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def credential_capability_id(self) -> str:
        return self._capability_id

    @property
    def spent(self) -> bool:
        return self._spent

    def expired(self, *, now_ms: int | None = None) -> bool:
        current = time.monotonic_ns() // 1_000_000 if now_ms is None else now_ms
        return current > self._deadline_ms

    def apply(self, headers: Mapping[str, str]) -> dict[str, str]:
        """Return ``headers`` with the authorization applied. Single use."""
        if self._spent:
            raise CredentialError(ProviderReason.E_CRED_UNAVAILABLE, self._capability_id)
        if self.expired():
            self._spent = True
            raise CredentialError(ProviderReason.E_CRED_UNAVAILABLE, self._capability_id)
        self._spent = True
        return self._apply(dict(headers))

    def revoke(self) -> None:
        self._spent = True

    # -- non-serializable, non-renderable --------------------------------
    def __repr__(self) -> str:
        return f"<AuthorizationHandle {self._provider_id}:{self._capability_id} redacted>"

    __str__ = __repr__

    def __reduce__(self) -> Any:  # pragma: no cover - defensive
        raise TypeError("AuthorizationHandle is not serializable")

    def __copy__(self) -> Any:  # pragma: no cover - defensive
        raise TypeError("AuthorizationHandle is not copyable")

    def __deepcopy__(self, memo: Any) -> Any:  # pragma: no cover - defensive
        raise TypeError("AuthorizationHandle is not copyable")


@dataclass(frozen=True, slots=True)
class CredentialRecord:
    """What the broker knows about one credential capability.

    ``apply`` is the only path to the material; the record itself holds no raw
    value and its canonical projection carries the scope digest, never a scope
    value that could be secret-shaped.
    """

    provider_id: str
    credential_capability_id: str
    ready: bool
    apply: Callable[[dict[str, str]], dict[str, str]]
    broad_credential: bool = False


class ProviderCredentialBroker:
    """Least-privilege broker enforcing non-crossing credential domains."""

    __slots__ = ("_domains", "_records", "_revoked")

    def __init__(self, domains: Mapping[str, CredentialDomain]) -> None:
        self._domains = dict(domains)
        self._records: dict[tuple[str, str], CredentialRecord] = {}
        self._revoked: set[tuple[str, str]] = set()

    def register(self, record: CredentialRecord) -> CredentialRecord:
        domain = self._domains.get(record.provider_id)
        if domain is None or not domain.contains(record.credential_capability_id):
            raise CredentialError(
                ProviderReason.E_CRED_CROSS_DOMAIN, record.credential_capability_id
            )
        if record.broad_credential:
            # Admin-class / broad credentials are never provisioned to the gateway.
            raise CredentialError(
                ProviderReason.E_CRED_CROSS_DOMAIN, record.credential_capability_id
            )
        self._records[(record.provider_id, record.credential_capability_id)] = record
        return record

    def status(self, provider_id: str, credential_capability_id: str) -> bool:
        """Readiness only — never material, never a handle."""
        key = (provider_id, credential_capability_id)
        if key in self._revoked:
            return False
        record = self._records.get(key)
        return bool(record and record.ready)

    def status_map(self, provider_id: str) -> dict[str, bool]:
        domain = self._domains.get(provider_id)
        if domain is None:
            raise CredentialError(ProviderReason.E_CRED_CROSS_DOMAIN, provider_id)
        return {
            capability: self.status(provider_id, capability)
            for capability in domain.capability_ids
        }

    def scope_digest(self, provider_id: str, credential_capability_id: str) -> str:
        domain = self._domains.get(provider_id)
        if domain is None or not domain.contains(credential_capability_id):
            raise CredentialError(ProviderReason.E_CRED_CROSS_DOMAIN, credential_capability_id)
        return domain.scope_digest(credential_capability_id)

    def resolve(
        self,
        *,
        provider_id: str,
        credential_capability_id: str,
        requested_scopes: tuple[str, ...] = (),
        ttl_ms: int = DEFAULT_HANDLE_TTL_MS,
    ) -> AuthorizationHandle:
        """Mint a request-scoped handle, or fail closed."""
        domain = self._domains.get(provider_id)
        if domain is None or not domain.contains(credential_capability_id):
            raise CredentialError(ProviderReason.E_CRED_CROSS_DOMAIN, credential_capability_id)
        granted = set(domain.granted_scopes.get(credential_capability_id, ()))
        if not set(requested_scopes).issubset(granted):
            # Escalation can never widen credential scope (I3).
            raise CredentialError(ProviderReason.E_CRED_CROSS_DOMAIN, credential_capability_id)
        key = (provider_id, credential_capability_id)
        if key in self._revoked:
            raise CredentialError(ProviderReason.E_CRED_REVOKED, credential_capability_id)
        record = self._records.get(key)
        if record is None or not record.ready:
            raise CredentialError(ProviderReason.E_CRED_UNAVAILABLE, credential_capability_id)
        deadline = time.monotonic_ns() // 1_000_000 + max(1, int(ttl_ms))
        return AuthorizationHandle(
            provider_id=provider_id,
            capability_id=credential_capability_id,
            apply=record.apply,
            deadline_ms=deadline,
        )

    # -- rotation / revocation -------------------------------------------
    def rotate(self, record: CredentialRecord) -> CredentialRecord:
        """Replace the material for a domain without a gateway restart.

        In-flight handles keep working against the old material until they are
        spent or expire; they are never silently retried on the new one.
        """
        key = (record.provider_id, record.credential_capability_id)
        self._revoked.discard(key)
        return self.register(record)

    def revoke(self, provider_id: str, credential_capability_id: str) -> None:
        self._revoked.add((provider_id, credential_capability_id))

    @property
    def revoked(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(self._revoked))


__all__ = [
    "DEFAULT_HANDLE_TTL_MS",
    "AuthorizationHandle",
    "CredentialError",
    "CredentialRecord",
    "ProviderCredentialBroker",
]
