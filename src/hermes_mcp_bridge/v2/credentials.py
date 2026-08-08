"""Credential broker **contract** for V2 Phase 1.

The broker answers one question only: *is this credential capability ready?*
It never returns credential material, secret paths, environment variable names
or any value derived from a secret.

Phase 1 implements a single in-memory :class:`StaticCredentialBroker` for
tests and wiring. Real backends (restricted file provider, keyring, Vault,
cloud secret manager) remain deferred — OD-005 / ADR-0006.
"""

from __future__ import annotations

from typing import Annotated, Any, Protocol, runtime_checkable

from pydantic import ConfigDict, Field, field_validator

from ._models import RegistryModel
from .enums import CapabilityState
from .schema import normalize_identifier


class CredentialCapabilityStatus(RegistryModel):
    """Non-secret readiness status for one credential capability.

    Deliberately has no field able to carry secret material. ``extra`` is
    forbidden, so a caller cannot smuggle a token in through an extra key.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    capability_id: str
    provider: str
    state: CapabilityState
    version: Annotated[int, Field(ge=1)] = 1

    @field_validator("capability_id", "provider")
    @classmethod
    def _identifiers(cls, value: str, info: Any) -> str:
        return normalize_identifier(value, field=str(info.field_name))

    @property
    def is_configured(self) -> bool:
        return self.state.is_configured

    @property
    def is_available(self) -> bool:
        return self.state.is_available

    @property
    def is_healthy(self) -> bool:
        return self.state.is_healthy

    @property
    def is_ready(self) -> bool:
        return self.state.is_ready

    def canonical(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "provider": self.provider,
            "state": self.state.value,
            "version": self.version,
        }


@runtime_checkable
class CredentialBroker(Protocol):
    """Interface returning capability status, never credential material.

    Implementations must be fail-closed: an unknown or unresolvable capability
    yields ``None`` (treated as not ready), never a permissive default.
    """

    def status(self, credential_capability_id: str) -> CredentialCapabilityStatus | None:
        """Return the non-secret status, or ``None`` when unknown."""
        ...

    def is_ready(self, credential_capability_id: str) -> bool:
        """Fail-closed readiness check."""
        ...


class StaticCredentialBroker:
    """In-memory broker over a fixed set of statuses. Test/wiring use only.

    There is intentionally no file, environment or network backend in Phase 1.
    """

    __slots__ = ("_statuses",)

    def __init__(self, statuses: list[CredentialCapabilityStatus] | None = None) -> None:
        self._statuses: dict[str, CredentialCapabilityStatus] = {}
        for status in statuses or []:
            self._statuses[status.capability_id] = status

    def status(self, credential_capability_id: str) -> CredentialCapabilityStatus | None:
        try:
            key = normalize_identifier(
                credential_capability_id, field="credential_capability_id"
            )
        except Exception:
            return None
        return self._statuses.get(key)

    def is_ready(self, credential_capability_id: str) -> bool:
        status = self.status(credential_capability_id)
        return bool(status and status.is_ready)

    def ordered(self) -> list[CredentialCapabilityStatus]:
        return [self._statuses[key] for key in sorted(self._statuses)]


__all__ = ["CredentialBroker", "CredentialCapabilityStatus", "StaticCredentialBroker"]
