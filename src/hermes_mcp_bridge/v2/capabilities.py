"""Capability registry: stable capability IDs, provider and readiness state.

A capability is *what a tool needs to work*: a backend integration plus its
health/authorization state. Registration never implies usability — see
``docs/v2/architecture/tool-registry.md`` ("Capability health") and ADR-0004.

No secret material, secret path or environment variable name is stored here;
credential readiness is expressed only as a :class:`CapabilityState`.
Free-form ``description`` is editorial metadata: it remains available in-memory
but is deliberately excluded from canonical snapshot serialization.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import ConfigDict, Field, field_validator

from ._models import RegistryModel
from .enums import CapabilityState
from .errors import DuplicateCapabilityError, RegistryFrozenError, UnknownCapabilityError
from .schema import normalize_identifier


class CapabilityDescriptor(RegistryModel):
    """Immutable description of one capability.

    ``description`` is human/editorial text. It is not audit-safe metadata and
    therefore never enters :meth:`canonical` or the capability snapshot hash.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    capability_id: str
    provider: str
    state: CapabilityState
    version: Annotated[int, Field(ge=1)] = 1
    description: str = ""

    @field_validator("capability_id", "provider")
    @classmethod
    def _identifiers(cls, value: str, info: Any) -> str:
        return normalize_identifier(value, field=str(info.field_name))

    @field_validator("description")
    @classmethod
    def _description(cls, value: str) -> str:
        return value.strip()

    # Readiness questions are answered by the state, never inferred elsewhere.
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
        """Return the audit-safe non-secret capability metadata."""
        return {
            "capability_id": self.capability_id,
            "provider": self.provider,
            "state": self.state.value,
            "version": self.version,
        }

    def with_state(self, state: CapabilityState) -> CapabilityDescriptor:
        return self.model_copy(update={"state": state})


class CapabilityRegistry:
    """Deterministic capability registry with controlled mutation.

    Mutation is allowed until :meth:`freeze` is called; duplicates are always
    rejected and lookups are fail-closed.
    """

    __slots__ = ("_capabilities", "_frozen")

    def __init__(self, capabilities: list[CapabilityDescriptor] | None = None) -> None:
        self._capabilities: dict[str, CapabilityDescriptor] = {}
        self._frozen = False
        for capability in capabilities or []:
            self.register(capability)

    # --- mutation ---------------------------------------------------------
    def register(self, capability: CapabilityDescriptor) -> CapabilityDescriptor:
        if self._frozen:
            raise RegistryFrozenError("capability registry is frozen")
        if capability.capability_id in self._capabilities:
            raise DuplicateCapabilityError(f"duplicate capability_id: {capability.capability_id}")
        self._capabilities[capability.capability_id] = capability
        return capability

    def freeze(self) -> CapabilityRegistry:
        self._frozen = True
        return self

    @property
    def frozen(self) -> bool:
        return self._frozen

    # --- lookup (fail-closed) --------------------------------------------
    def get(self, capability_id: str) -> CapabilityDescriptor:
        try:
            key = normalize_identifier(capability_id, field="capability_id")
        except Exception as exc:  # invalid ids are unknown, never permissive
            raise UnknownCapabilityError(f"unknown capability: {capability_id!r}") from exc
        try:
            return self._capabilities[key]
        except KeyError as exc:
            raise UnknownCapabilityError(f"unknown capability: {key}") from exc

    def contains(self, capability_id: str) -> bool:
        try:
            self.get(capability_id)
        except UnknownCapabilityError:
            return False
        return True

    def is_ready(self, capability_id: str) -> bool:
        """Fail-closed readiness: unknown capabilities are never ready."""
        try:
            return self.get(capability_id).is_ready
        except UnknownCapabilityError:
            return False

    def __len__(self) -> int:
        return len(self._capabilities)

    def __contains__(self, capability_id: object) -> bool:
        return isinstance(capability_id, str) and self.contains(capability_id)

    def ordered(self) -> list[CapabilityDescriptor]:
        """Capabilities sorted by ``capability_id`` — insertion order agnostic."""
        return [self._capabilities[key] for key in sorted(self._capabilities)]

    def canonical(self) -> list[dict[str, Any]]:
        return [capability.canonical() for capability in self.ordered()]


__all__ = ["CapabilityDescriptor", "CapabilityRegistry"]
