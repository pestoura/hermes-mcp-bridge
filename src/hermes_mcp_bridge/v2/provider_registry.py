"""Phase 7 provider registry: allow-list, discovery, readiness and snapshots.

> **V2 · PHASE 7 · runtime, disabled by default behind ``PROVIDER_FEATURE_ENABLED``**

Discovery is *declarative first, probe second, demote only*:

1. a manifest is validated against the registered typed tools and the credential
   domain (:mod:`provider_contract`);
2. a bounded, read-only ``health`` probe classifies readiness;
3. a probe may only move a capability downward — it can never create, promote or
   widen one.

Providers are resolved exclusively from an explicit in-repo allow-list keyed by
provider id: no entry-point scanning, no plugin directory, no network fetch and
no dynamic import. An unknown provider id is a fail-closed refusal.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .canonical import canonical_hash
from .enums import CapabilityState
from .provider_contract import (
    PROVIDER_FEATURE_ENABLED,
    CapabilityDeclaration,
    ProviderContractError,
    ProviderManifest,
    ProviderReason,
    ProviderStatus,
)

#: Ordering used to guarantee a probe never promotes a capability. Lower is
#: strictly less usable; ``max`` is never applied, only ``min``.
_STATE_RANK: dict[CapabilityState, int] = {
    CapabilityState.DENIED: 0,
    CapabilityState.UNAVAILABLE: 1,
    CapabilityState.DEGRADED: 2,
    CapabilityState.CONFIGURED: 3,
    CapabilityState.AVAILABLE: 4,
    CapabilityState.HEALTHY: 5,
    CapabilityState.READY: 6,
}


@dataclass(frozen=True, slots=True)
class HealthReport:
    """Outcome of a bounded, read-only provider probe."""

    capability_id: str
    state: CapabilityState
    reason: ProviderReason = ProviderReason.OK

    def __post_init__(self) -> None:
        if not isinstance(self.state, CapabilityState):
            raise ProviderContractError(ProviderReason.E_MANIFEST_INVALID, "state")
        if not isinstance(self.reason, ProviderReason):
            raise ProviderContractError(ProviderReason.E_MANIFEST_INVALID, "reason")

    def canonical(self) -> dict[str, object]:
        return {
            "capability_id": self.capability_id,
            "reason": self.reason.value,
            "state": self.state.value,
        }


@runtime_checkable
class ProviderPlugin(Protocol):
    """The entire surface a provider exposes to the gateway.

    ``describe`` is pure. ``health`` receives credential *status* only, never
    material. ``execute`` lives on the execution boundary and is invoked by
    :mod:`provider_gateway`, never by discovery.
    """

    def describe(self) -> ProviderManifest: ...

    def health(self, auth_status: Mapping[str, bool]) -> tuple[HealthReport, ...]: ...


class ProviderRegistryError(RuntimeError):
    """Fail-closed registry refusal carrying a stable reason code."""

    def __init__(self, reason: ProviderReason, subject: str) -> None:
        self.reason = reason
        self.subject = subject
        super().__init__(f"{reason.value}:{subject}")


class ProviderRegistry:
    """Fail-closed registry of provider manifests and capability readiness."""

    __slots__ = ("_allow_list", "_manifests", "_states", "_tool_ids", "_frozen")

    def __init__(self, *, allow_list: Iterable[str], tool_ids: Iterable[str]) -> None:
        self._allow_list = tuple(sorted({str(item).strip().lower() for item in allow_list}))
        self._tool_ids = frozenset(str(item).strip().lower() for item in tool_ids)
        self._manifests: dict[str, ProviderManifest] = {}
        self._states: dict[str, CapabilityState] = {}
        self._frozen = False

    # -- registration ----------------------------------------------------
    @property
    def allow_list(self) -> tuple[str, ...]:
        return self._allow_list

    @property
    def frozen(self) -> bool:
        return self._frozen

    def register(self, manifest: ProviderManifest) -> ProviderManifest:
        if self._frozen:
            raise ProviderRegistryError(ProviderReason.E_PROVIDER_DISABLED, manifest.provider_id)
        if manifest.provider_id not in self._allow_list:
            raise ProviderRegistryError(ProviderReason.E_PROVIDER_UNKNOWN, manifest.provider_id)
        if manifest.status is ProviderStatus.BLOCKED_UNCONFIRMED:
            raise ProviderRegistryError(ProviderReason.E_PROVIDER_DISABLED, manifest.provider_id)
        if manifest.provider_id in self._manifests:
            raise ProviderRegistryError(ProviderReason.E_CAP_DUPLICATE, manifest.provider_id)
        for capability in manifest.capabilities:
            if capability.tool_id not in self._tool_ids:
                raise ProviderRegistryError(
                    ProviderReason.E_CAP_UNDECLARED, capability.capability_id
                )
            if capability.capability_id in self._states:
                raise ProviderRegistryError(
                    ProviderReason.E_CAP_DUPLICATE, capability.capability_id
                )
        self._manifests[manifest.provider_id] = manifest
        for capability in manifest.capabilities:
            # Declaration alone never means usable: a capability starts
            # CONFIGURED and only a successful probe can classify it READY.
            self._states[capability.capability_id] = CapabilityState.CONFIGURED
        return manifest

    def freeze(self) -> ProviderRegistry:
        self._frozen = True
        return self

    # -- lookup ----------------------------------------------------------
    def manifest(self, provider_id: str) -> ProviderManifest:
        try:
            return self._manifests[provider_id]
        except KeyError:
            raise ProviderRegistryError(ProviderReason.E_PROVIDER_UNKNOWN, provider_id) from None

    def capability(self, capability_id: str) -> CapabilityDeclaration:
        provider_id = capability_id.split(".", 1)[0]
        manifest = self.manifest(provider_id)
        return manifest.capability(capability_id)

    def state(self, capability_id: str) -> CapabilityState:
        try:
            return self._states[capability_id]
        except KeyError:
            raise ProviderRegistryError(ProviderReason.E_CAP_UNDECLARED, capability_id) from None

    @property
    def provider_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._manifests))

    @property
    def capability_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._states))

    # -- readiness -------------------------------------------------------
    def apply_health(self, reports: Iterable[HealthReport]) -> tuple[HealthReport, ...]:
        """Apply probe reports. Demote-only: a probe never raises a state."""
        applied: list[HealthReport] = []
        for report in reports:
            current = self.state(report.capability_id)
            proposed = report.state
            effective = (
                proposed if _STATE_RANK[proposed] <= _STATE_RANK[current] else current
            )
            self._states[report.capability_id] = effective
            applied.append(
                HealthReport(
                    capability_id=report.capability_id,
                    state=effective,
                    reason=report.reason,
                )
            )
        return tuple(applied)

    def promote_configured(self, reports: Iterable[HealthReport]) -> tuple[HealthReport, ...]:
        """Initial classification from CONFIGURED. Only legal once per capability.

        A capability that has already been probed can never be re-promoted; this
        is the single entry point that raises a state, and only out of the
        declaration-time ``CONFIGURED`` placeholder.
        """
        applied: list[HealthReport] = []
        for report in reports:
            current = self.state(report.capability_id)
            if current is not CapabilityState.CONFIGURED:
                applied.extend(self.apply_health([report]))
                continue
            declaration = self.capability(report.capability_id)
            state = report.state
            if declaration.is_write and state is not CapabilityState.READY:
                # Fail closed: an inconclusive write probe is UNAVAILABLE.
                state = CapabilityState.UNAVAILABLE
            self._states[report.capability_id] = state
            applied.append(
                HealthReport(
                    capability_id=report.capability_id,
                    state=state,
                    reason=report.reason,
                )
            )
        return tuple(applied)

    def is_usable(self, capability_id: str) -> bool:
        """Write requires READY; read may serve DEGRADED with a marker."""
        declaration = self.capability(capability_id)
        state = self.state(capability_id)
        if declaration.is_write:
            return state is CapabilityState.READY
        return state in (CapabilityState.READY, CapabilityState.DEGRADED)

    # -- snapshots -------------------------------------------------------
    def snapshot_payload(self) -> dict[str, object]:
        return {
            "capabilities": [
                {
                    **self.capability(capability_id).canonical(),
                    "state": self.state(capability_id).value,
                }
                for capability_id in self.capability_ids
            ],
            "providers": [
                self._manifests[provider_id].canonical() for provider_id in self.provider_ids
            ],
        }

    def capability_snapshot_hash(self) -> str:
        return canonical_hash(self.snapshot_payload())

    def write_capability_digest(self) -> str:
        """Digest of the exposed *write* surface alone, so a write-surface
        change is individually detectable even if the read surface is stable."""
        payload = [
            {
                **capability.canonical(),
                "state": self.state(capability.capability_id).value,
            }
            for provider_id in self.provider_ids
            for capability in self._manifests[provider_id].write_capabilities
        ]
        return canonical_hash({"write_capabilities": payload})


def build_registry(
    *,
    allow_list: Iterable[str],
    tool_ids: Iterable[str],
    manifests: Iterable[ProviderManifest],
) -> ProviderRegistry:
    registry = ProviderRegistry(allow_list=allow_list, tool_ids=tool_ids)
    for manifest in manifests:
        registry.register(manifest)
    return registry.freeze()


__all__ = [
    "PROVIDER_FEATURE_ENABLED",
    "HealthReport",
    "ProviderPlugin",
    "ProviderRegistry",
    "ProviderRegistryError",
    "build_registry",
]
