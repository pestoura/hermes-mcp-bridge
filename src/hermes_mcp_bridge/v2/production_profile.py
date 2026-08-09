"""V2 production activation profile — the single typed activation surface.

> **V2 · ACTIVATION (release 2.0.1)**

Release ``2.0.0`` shipped every accepted V2 execution lane behind a *hardcoded*
module constant (``BATCH_FEATURE_ENABLED`` and friends), all ``False``. The code
was present but not activatable: there was no supported way for an operator to
turn a lane on, and no way to prove in production that a lane was reachable.

This module is that missing piece, and deliberately nothing more:

* the per-module ``*_FEATURE_ENABLED`` constants keep their ``False`` default —
  they remain the *import-time* fail-closed posture asserted by the Phase 4..8
  gates, and this module does not mutate them;
* activation is expressed once, as an immutable typed record
  (:class:`V2ProductionProfile`), never as scattered booleans;
* the profile is **fail-closed**: the default profile enables nothing, an
  unparsable or unknown setting is a refusal (:class:`ProfileConfigError`), and
  a capability that is not explicitly enabled is off;
* :data:`DISABLED_PROFILE` plus :func:`V2ProductionProfile.disabled` are the
  clean rollback switch: one env var back to ``0``, or one call, and every lane
  returns to the 2.0.0 posture with no code change;
* :class:`V2Composition` is the intended internal composition root. It is the
  only sanctioned way to build the accepted engines with their activation state,
  so "is BATCH reachable?" is answered by constructing the real object, not by
  importing a module and reading a constant.

Compatibility is untouched by design: nothing here is imported by any V1 module
(gate check ``P9-03``), no tool is added or removed, the public surface stays at
27 effective tools, contract ``1.0.0`` and schema ``0.6.1``, and no generic
shell/HTTP capability is introduced.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum, unique
from typing import Any, Final

from .canonical import canonical_hash, canonical_json_text

#: Profile record contract version. Bumped only on a shape change.
V2_PROFILE_CONTRACT_VERSION: Final[str] = "1"

#: Environment prefix for every activation setting.
ENV_PREFIX: Final[str] = "BRIDGE_V2_"

#: Master switch. When absent or false every capability is off regardless of
#: the per-capability settings — a single, unambiguous rollback lever.
ENV_ENABLED: Final[str] = ENV_PREFIX + "ENABLED"

#: Agentic token budget. Zero (the default) means the HYBRID coordinator may
#: never escalate: the zero-default-agentic property of the accepted Phase 8
#: contract is preserved as the production default.
ENV_AGENTIC_TOKEN_BUDGET: Final[str] = ENV_PREFIX + "AGENTIC_TOKEN_BUDGET"

#: Ceiling the profile refuses to exceed, so a misconfigured environment cannot
#: widen the accepted Hybrid contract into open-ended agentic expansion.
MAX_AGENTIC_TOKEN_BUDGET: Final[int] = 4096


@unique
class V2Capability(StrEnum):
    """The closed set of activatable V2 capabilities.

    Ordering here is the deterministic mode-preference order of the accepted
    Phase 8 resolver: DIRECT > BATCH > DAG/RUNBOOK > AGENTIC. INTEGRATIONS is
    the provider substrate the other lanes call through, not a competing mode.
    """

    DIRECT = "DIRECT"
    BATCH = "BATCH"
    DAG = "DAG"
    RUNBOOK = "RUNBOOK"
    INTEGRATIONS = "INTEGRATIONS"
    HYBRID = "HYBRID"


#: Every capability that a fully activated production profile must carry. The
#: production acceptance gate fails if any one of these is disabled.
REQUIRED_PRODUCTION_CAPABILITIES: Final[tuple[V2Capability, ...]] = (
    V2Capability.DIRECT,
    V2Capability.BATCH,
    V2Capability.DAG,
    V2Capability.RUNBOOK,
    V2Capability.INTEGRATIONS,
    V2Capability.HYBRID,
)

#: capability -> environment variable name.
ENV_FOR_CAPABILITY: Final[Mapping[V2Capability, str]] = {
    capability: ENV_PREFIX + capability.value for capability in V2Capability
}

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"", "0", "false", "no", "off"})


class ProfileConfigError(ValueError):
    """A profile input was not acceptable; the caller must fail closed.

    Raised instead of silently defaulting, so a typo in an activation variable
    can never be read as "enabled" or quietly swallowed as "disabled".
    """

    def __init__(self, setting: str, detail: str = "") -> None:
        self.setting = setting
        self.detail = detail
        super().__init__(f"{setting}:{detail}" if detail else setting)


def _parse_bool(setting: str, raw: str | None) -> bool:
    if raw is None:
        return False
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ProfileConfigError(setting, "not a boolean")


def _parse_int(setting: str, raw: str | None, *, maximum: int) -> int:
    if raw is None or raw.strip() == "":
        return 0
    value = raw.strip()
    if not value.isdigit():
        raise ProfileConfigError(setting, "not a non-negative integer")
    parsed = int(value)
    if parsed > maximum:
        raise ProfileConfigError(setting, "above the accepted ceiling")
    return parsed


@dataclass(frozen=True, slots=True)
class V2ProductionProfile:
    """Immutable activation record for the accepted V2 execution lanes."""

    enabled: bool = False
    direct: bool = False
    batch: bool = False
    dag: bool = False
    runbook: bool = False
    integrations: bool = False
    hybrid: bool = False
    agentic_token_budget: int = 0

    def __post_init__(self) -> None:
        for name in (
            "enabled",
            "direct",
            "batch",
            "dag",
            "runbook",
            "integrations",
            "hybrid",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ProfileConfigError(name, "not a boolean")
        budget = self.agentic_token_budget
        if not isinstance(budget, int) or isinstance(budget, bool) or budget < 0:
            raise ProfileConfigError("agentic_token_budget", "not a non-negative integer")
        if budget > MAX_AGENTIC_TOKEN_BUDGET:
            raise ProfileConfigError("agentic_token_budget", "above the accepted ceiling")
        # HYBRID orchestrates the deterministic lanes; enabling it without them
        # would leave the resolver with nothing deterministic to prefer, which
        # is precisely the agentic-expansion posture the contract forbids.
        if self.is_enabled(V2Capability.HYBRID):
            missing = [
                capability.value
                for capability in (
                    V2Capability.DIRECT,
                    V2Capability.BATCH,
                    V2Capability.DAG,
                    V2Capability.RUNBOOK,
                )
                if not self.is_enabled(capability)
            ]
            if missing:
                raise ProfileConfigError("HYBRID", "requires " + ",".join(missing))

    # -- queries ----------------------------------------------------------
    def is_enabled(self, capability: V2Capability) -> bool:
        """Fail-closed lookup. The master switch dominates every capability."""
        if not self.enabled:
            return False
        return bool(getattr(self, capability.value.lower()))

    @property
    def active_capabilities(self) -> tuple[V2Capability, ...]:
        return tuple(c for c in V2Capability if self.is_enabled(c))

    @property
    def disabled_capabilities(self) -> tuple[V2Capability, ...]:
        return tuple(c for c in V2Capability if not self.is_enabled(c))

    @property
    def fully_active(self) -> bool:
        """True only when every required production capability is enabled."""
        return all(self.is_enabled(c) for c in REQUIRED_PRODUCTION_CAPABILITIES)

    @property
    def allows_agentic(self) -> bool:
        """An allowance exists only when a positive budget was declared."""
        return self.is_enabled(V2Capability.HYBRID) and self.agentic_token_budget > 0

    # -- switches ---------------------------------------------------------
    def disabled(self) -> V2ProductionProfile:
        """The rollback switch: same record, everything off."""
        return DISABLED_PROFILE

    def without(self, capability: V2Capability) -> V2ProductionProfile:
        """Disable exactly one capability, keeping the rest intact."""
        if capability is V2Capability.HYBRID:
            return replace(self, hybrid=False)
        # Disabling a deterministic lane must also drop HYBRID, or the record
        # would violate its own invariant.
        return replace(self, hybrid=False, **{capability.value.lower(): False})

    # -- evidence ---------------------------------------------------------
    def canonical(self) -> dict[str, Any]:
        """Canonical, secret-free view. Safe for logs, metrics and evidence."""
        return {
            "agentic_token_budget": self.agentic_token_budget,
            "capabilities": {c.value: self.is_enabled(c) for c in V2Capability},
            "contract_version": V2_PROFILE_CONTRACT_VERSION,
            "enabled": self.enabled,
            "fully_active": self.fully_active,
        }

    def canonical_text(self) -> str:
        return canonical_json_text(self.canonical())

    def digest(self) -> str:
        return canonical_hash(self.canonical())

    # -- construction -----------------------------------------------------
    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> V2ProductionProfile:
        """Build a profile from the environment, fail-closed.

        Unknown ``BRIDGE_V2_*`` variables are a refusal rather than a silent
        no-op, so a misspelt activation flag cannot look like a successful
        activation.
        """
        source: Mapping[str, str] = os.environ if env is None else env
        known = {ENV_ENABLED, ENV_AGENTIC_TOKEN_BUDGET, *ENV_FOR_CAPABILITY.values()}
        for key in source:
            if key.startswith(ENV_PREFIX) and key not in known:
                raise ProfileConfigError(key, "unknown activation setting")

        enabled = _parse_bool(ENV_ENABLED, source.get(ENV_ENABLED))
        flags = {
            capability.value.lower(): _parse_bool(name, source.get(name))
            for capability, name in ENV_FOR_CAPABILITY.items()
        }
        budget = _parse_int(
            ENV_AGENTIC_TOKEN_BUDGET,
            source.get(ENV_AGENTIC_TOKEN_BUDGET),
            maximum=MAX_AGENTIC_TOKEN_BUDGET,
        )
        return cls(enabled=enabled, agentic_token_budget=budget, **flags)

    @classmethod
    def production(cls, *, agentic_token_budget: int = 0) -> V2ProductionProfile:
        """The intended fully activated production profile."""
        return cls(
            enabled=True,
            direct=True,
            batch=True,
            dag=True,
            runbook=True,
            integrations=True,
            hybrid=True,
            agentic_token_budget=agentic_token_budget,
        )


#: Explicit, importable rollback posture — byte-identical to 2.0.0 behaviour.
DISABLED_PROFILE: Final[V2ProductionProfile] = V2ProductionProfile()


__all__ = [
    "DISABLED_PROFILE",
    "ENV_AGENTIC_TOKEN_BUDGET",
    "ENV_ENABLED",
    "ENV_FOR_CAPABILITY",
    "ENV_PREFIX",
    "MAX_AGENTIC_TOKEN_BUDGET",
    "REQUIRED_PRODUCTION_CAPABILITIES",
    "V2_PROFILE_CONTRACT_VERSION",
    "ProfileConfigError",
    "V2Capability",
    "V2ProductionProfile",
]
