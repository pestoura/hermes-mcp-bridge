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

EPIC-03 adds a provider-backed source without creating a second broker. A bound
credential provider is consulted on demand and can attach an idempotent cleanup
callback to the request-scoped handle. Provider unavailability never falls back
to a previously registered static record.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from .provider_contract import CredentialDomain, ProviderReason

#: A handle is valid only for the request that minted it.
DEFAULT_HANDLE_TTL_MS = 30_000


def _noop_revoke() -> None:
    return None


class CredentialError(RuntimeError):
    """Fail-closed credential refusal with a stable, redacted reason code."""

    def __init__(self, reason: ProviderReason, subject: str) -> None:
        self.reason = reason
        self.subject = subject
        super().__init__(f"{reason.value}:{subject}")


def _call_with_sanitized_error(
    operation: Callable[[], Any],
    credential_capability_id: str,
) -> tuple[Any | None, CredentialError | None]:
    """Execute a backend call and detach any backend exception before raising.

    The sanitized ``CredentialError`` is constructed while the backend exception
    is active but returned as a value. Callers raise it only after this helper has
    left the ``except`` block, so Python does not retain the backend exception in
    ``__context__``.
    """
    try:
        return operation(), None
    except CredentialError as exc:
        return None, CredentialError(exc.reason, credential_capability_id)
    except Exception:
        return None, CredentialError(
            ProviderReason.E_CRED_UNAVAILABLE, credential_capability_id
        )


class AuthorizationHandle:
    """Single-use, deadline-bound authorization. Never serializable.

    The credential material is held in a closure and applied to an outbound
    request by :meth:`apply`; there is no accessor that returns it. ``revoke``
    is idempotent and releases any provider-owned request resource at most once.
    """

    __slots__ = (
        "_apply",
        "_capability_id",
        "_deadline_ms",
        "_provider_id",
        "_revoke",
        "_revoked",
        "_spent",
    )

    def __init__(
        self,
        *,
        provider_id: str,
        capability_id: str,
        apply: Callable[[dict[str, str]], dict[str, str]],
        deadline_ms: int,
        revoke: Callable[[], None] | None = None,
    ) -> None:
        self._provider_id = provider_id
        self._capability_id = capability_id
        self._apply = apply
        self._deadline_ms = deadline_ms
        self._revoke = revoke or _noop_revoke
        self._revoked = False
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

    @property
    def revoked(self) -> bool:
        return self._revoked

    def expired(self, *, now_ms: int | None = None) -> bool:
        current = time.monotonic_ns() // 1_000_000 if now_ms is None else now_ms
        return current > self._deadline_ms

    def apply(self, headers: Mapping[str, str]) -> dict[str, str]:
        """Return ``headers`` with the authorization applied. Single use."""
        if self._spent or self._revoked:
            raise CredentialError(ProviderReason.E_CRED_UNAVAILABLE, self._capability_id)
        if self.expired():
            self._spent = True
            raise CredentialError(ProviderReason.E_CRED_UNAVAILABLE, self._capability_id)
        self._spent = True
        return self._apply(dict(headers))

    def revoke(self) -> None:
        if self._revoked:
            return
        self._revoked = True
        self._spent = True
        self._revoke()

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
    value. ``revoke`` releases provider-owned request state and is never rendered.
    """

    provider_id: str
    credential_capability_id: str
    ready: bool
    apply: Callable[[dict[str, str]], dict[str, str]] = field(repr=False, compare=False)
    broad_credential: bool = False
    revoke: Callable[[], None] = field(default=_noop_revoke, repr=False, compare=False)


class CredentialProvider(Protocol):
    """Closed capability-oriented source used by the broker.

    Implementations receive logical provider/capability identifiers only. The
    broker has no secret-path API and no material accessor.
    """

    def status(self, provider_id: str, credential_capability_id: str) -> bool: ...

    def request(self, provider_id: str, credential_capability_id: str) -> CredentialRecord: ...

    def revoke(self, provider_id: str, credential_capability_id: str) -> None: ...


class ProviderCredentialBroker:
    """Least-privilege broker enforcing non-crossing credential domains."""

    __slots__ = ("_domains", "_providers", "_records", "_revoked")

    def __init__(self, domains: Mapping[str, CredentialDomain]) -> None:
        self._domains = dict(domains)
        self._records: dict[tuple[str, str], CredentialRecord] = {}
        self._providers: dict[tuple[str, str], CredentialProvider] = {}
        self._revoked: set[tuple[str, str]] = set()

    def _require_domain(self, provider_id: str, credential_capability_id: str) -> CredentialDomain:
        domain = self._domains.get(provider_id)
        if domain is None or not domain.contains(credential_capability_id):
            raise CredentialError(ProviderReason.E_CRED_CROSS_DOMAIN, credential_capability_id)
        return domain

    def _validate_record(
        self,
        record: CredentialRecord,
        *,
        provider_id: str | None = None,
        credential_capability_id: str | None = None,
    ) -> CredentialRecord:
        self._require_domain(record.provider_id, record.credential_capability_id)
        if provider_id is not None and record.provider_id != provider_id:
            raise CredentialError(
                ProviderReason.E_CRED_CROSS_DOMAIN, record.credential_capability_id
            )
        if (
            credential_capability_id is not None
            and record.credential_capability_id != credential_capability_id
        ):
            raise CredentialError(
                ProviderReason.E_CRED_CROSS_DOMAIN, record.credential_capability_id
            )
        if record.broad_credential:
            raise CredentialError(
                ProviderReason.E_CRED_CROSS_DOMAIN, record.credential_capability_id
            )
        return record

    def _cleanup_provider_record(
        self,
        record: CredentialRecord,
        credential_capability_id: str,
    ) -> None:
        """Release a provider-issued record without exposing backend failures."""
        error: CredentialError | None = None
        try:
            record.revoke()
        except Exception:
            error = CredentialError(
                ProviderReason.E_CRED_UNAVAILABLE, credential_capability_id
            )
        if error is not None:
            raise error

    def register(self, record: CredentialRecord) -> CredentialRecord:
        record = self._validate_record(record)
        self._records[(record.provider_id, record.credential_capability_id)] = record
        return record

    def bind_provider(
        self,
        *,
        provider_id: str,
        credential_capability_id: str,
        provider: CredentialProvider,
    ) -> None:
        """Bind one closed credential capability to an on-demand source.

        Binding removes any static record for the same key so source outage can
        never degrade to a stale/local credential implicitly.
        """
        self._require_domain(provider_id, credential_capability_id)
        key = (provider_id, credential_capability_id)
        self._records.pop(key, None)
        self._providers[key] = provider
        self._revoked.discard(key)

    def status(self, provider_id: str, credential_capability_id: str) -> bool:
        """Readiness only — never material, never a handle."""
        self._require_domain(provider_id, credential_capability_id)
        key = (provider_id, credential_capability_id)
        if key in self._revoked:
            return False
        provider = self._providers.get(key)
        if provider is not None:
            try:
                return bool(provider.status(provider_id, credential_capability_id))
            except Exception:
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
        domain = self._require_domain(provider_id, credential_capability_id)
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
        domain = self._require_domain(provider_id, credential_capability_id)
        granted = set(domain.granted_scopes.get(credential_capability_id, ()))
        if not set(requested_scopes).issubset(granted):
            # Escalation can never widen credential scope (I3).
            raise CredentialError(ProviderReason.E_CRED_CROSS_DOMAIN, credential_capability_id)
        key = (provider_id, credential_capability_id)
        if key in self._revoked:
            raise CredentialError(ProviderReason.E_CRED_REVOKED, credential_capability_id)

        provider = self._providers.get(key)
        if provider is not None:
            if not self.status(provider_id, credential_capability_id):
                raise CredentialError(ProviderReason.E_CRED_UNAVAILABLE, credential_capability_id)
            record, error = _call_with_sanitized_error(
                lambda: provider.request(provider_id, credential_capability_id),
                credential_capability_id,
            )
            if error is not None:
                raise error
            try:
                record = self._validate_record(
                    record,
                    provider_id=provider_id,
                    credential_capability_id=credential_capability_id,
                )
            except CredentialError:
                self._cleanup_provider_record(record, credential_capability_id)
                raise
        else:
            record = self._records.get(key)

        if record is None:
            raise CredentialError(ProviderReason.E_CRED_UNAVAILABLE, credential_capability_id)
        if not record.ready:
            if provider is not None:
                self._cleanup_provider_record(record, credential_capability_id)
            raise CredentialError(ProviderReason.E_CRED_UNAVAILABLE, credential_capability_id)
        deadline = time.monotonic_ns() // 1_000_000 + max(1, int(ttl_ms))
        return AuthorizationHandle(
            provider_id=provider_id,
            capability_id=credential_capability_id,
            apply=record.apply,
            revoke=record.revoke,
            deadline_ms=deadline,
        )

    # -- rotation / revocation -------------------------------------------
    def rotate(self, record: CredentialRecord) -> CredentialRecord:
        """Replace static material for a domain without a gateway restart.

        In-flight handles keep working against the old material until they are
        spent or expire; they are never silently retried on the new one.
        """
        key = (record.provider_id, record.credential_capability_id)
        self._revoked.discard(key)
        return self.register(record)

    def revoke(self, provider_id: str, credential_capability_id: str) -> None:
        self._require_domain(provider_id, credential_capability_id)
        key = (provider_id, credential_capability_id)
        self._revoked.add(key)
        provider = self._providers.get(key)
        if provider is None:
            return
        _, error = _call_with_sanitized_error(
            lambda: provider.revoke(provider_id, credential_capability_id),
            credential_capability_id,
        )
        if error is not None:
            raise error

    @property
    def revoked(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(self._revoked))


__all__ = [
    "DEFAULT_HANDLE_TTL_MS",
    "AuthorizationHandle",
    "CredentialError",
    "CredentialProvider",
    "CredentialRecord",
    "ProviderCredentialBroker",
]
