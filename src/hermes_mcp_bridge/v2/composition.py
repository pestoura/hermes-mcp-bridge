"""V2 composition root — the intended internal wiring of the accepted lanes.

> **V2 · ACTIVATION (release 2.0.1)**

:class:`V2Composition` turns a :class:`~.production_profile.V2ProductionProfile`
into live, correctly parameterised engine instances. It exists so that
"capability X is active in production" is answered by *constructing and
exercising the real engine*, never by importing a module and reading a boolean.

Design constraints, all inherited from the accepted V2 contract:

* **fail-closed** — a disabled capability raises :class:`CapabilityDisabled`
  from the builder; it never returns a half-built or permissive object;
* **no new surface** — this module composes only already-accepted engines. It
  registers no tool, opens no network listener, and adds no shell/HTTP
  capability;
* **deterministic preference preserved** — the resolver's mode preference stays
  DIRECT > BATCH > DAG/RUNBOOK > AGENTIC, and the agentic budget comes from the
  profile, defaulting to zero;
* **injected dependencies** — executors, stores, catalogs, governance, policy,
  credentials and adapters are supplied by the caller. The composition root
  decides *whether* and *how* a lane is wired, never *what* it talks to.

Nothing here is imported by any V1 module.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .batch_scheduler import BatchScheduler, GlobalCapacity, StepGovernance
from .dag_engine import DagEngine, NodeExecutor
from .hybrid_execution import DeterministicExecutor, HybridCoordinator
from .production_profile import DISABLED_PROFILE, V2Capability, V2ProductionProfile
from .provider_gateway import (
    ApprovalStore,
    IdempotencyStore,
    PolicyPort,
    ProviderAdapter,
    ProviderGateway,
    ScopeResolver,
)
from .provider_registry import ProviderRegistry
from .resolver import ModeResolver
from .resolver_contract import ResolverBudget
from .runbook_engine import RunbookEngine
from .runbook_registry import RunbookRegistry


class CapabilityDisabled(RuntimeError):
    """A lane was requested while its profile capability is disabled."""

    def __init__(self, capability: V2Capability) -> None:
        self.capability = capability
        super().__init__(f"V2 capability disabled: {capability.value}")


@dataclass(frozen=True, slots=True)
class V2Composition:
    """Builds the accepted V2 engines according to one activation profile."""

    profile: V2ProductionProfile = DISABLED_PROFILE

    # -- guards -----------------------------------------------------------
    def require(self, capability: V2Capability) -> None:
        if not self.profile.is_enabled(capability):
            raise CapabilityDisabled(capability)

    def enabled(self, capability: V2Capability) -> bool:
        return self.profile.is_enabled(capability)

    # -- DIRECT -----------------------------------------------------------
    def direct_enabled(self) -> bool:
        """DIRECT is the V1-compatible single-operation path.

        It owns no separate engine: it is the 27-tool surface itself. The
        profile flag records whether the V2 programme considers that path part
        of the activated set, which is what the acceptance gate asserts.
        """
        return self.enabled(V2Capability.DIRECT)

    # -- BATCH ------------------------------------------------------------
    def batch_scheduler(
        self,
        executor: Any,
        *,
        governance: StepGovernance | None = None,
        capacity: GlobalCapacity | None = None,
    ) -> BatchScheduler:
        self.require(V2Capability.BATCH)
        return BatchScheduler(
            executor,
            governance=governance,
            capacity=capacity,
            enabled=True,
        )

    # -- DAG --------------------------------------------------------------
    def dag_engine(
        self,
        executor: NodeExecutor,
        *,
        catalog: Any,
        store: Any,
        governance: Any = None,
        reconciler: Any = None,
        compensator: Any = None,
        engine_ceiling: int = 4,
        owner_id: str = "engine-0",
    ) -> DagEngine:
        self.require(V2Capability.DAG)
        return DagEngine(
            executor,
            catalog=catalog,
            store=store,
            governance=governance,
            reconciler=reconciler,
            compensator=compensator,
            enabled=True,
            engine_ceiling=engine_ceiling,
            owner_id=owner_id,
        )

    # -- RUNBOOK ----------------------------------------------------------
    def runbook_engine(
        self,
        executor: Any,
        registry: RunbookRegistry,
        catalog: Any,
        store: Any,
        governance: Any,
        *,
        approval: Any = None,
        tool_names: frozenset[str] | None = None,
        authorized_runbooks: Mapping[tuple[str, str], set[str]] | None = None,
    ) -> RunbookEngine:
        self.require(V2Capability.RUNBOOK)
        # RUNBOOK compiles to a plan and executes through the DAG engine, so a
        # RUNBOOK activation without DAG would be structurally unreachable.
        self.require(V2Capability.DAG)
        return RunbookEngine(
            executor,
            registry,
            catalog,
            store,
            governance,
            approval=approval,
            enabled=True,
            tool_names=tool_names,
            authorized_runbooks=authorized_runbooks,
        )

    # -- INTEGRATIONS -----------------------------------------------------
    def provider_gateway(
        self,
        *,
        registry: ProviderRegistry,
        policy: PolicyPort,
        scopes: ScopeResolver,
        broker: Any,
        audit: Any,
        adapters: Mapping[str, ProviderAdapter],
        approvals: ApprovalStore | None = None,
        idempotency: IdempotencyStore | None = None,
    ) -> ProviderGateway:
        self.require(V2Capability.INTEGRATIONS)
        return ProviderGateway(
            registry=registry,
            policy=policy,
            scopes=scopes,
            broker=broker,
            audit=audit,
            adapters=adapters,
            approvals=approvals,
            idempotency=idempotency,
        )

    # -- HYBRID -----------------------------------------------------------
    def resolver_budget(self, base: ResolverBudget | None = None) -> ResolverBudget:
        """Budget carrying the profile's agentic allowance and nothing more."""
        from dataclasses import replace as _replace

        template = base or ResolverBudget()
        return _replace(template, agentic_token_budget=self.profile.agentic_token_budget)

    def mode_resolver(
        self,
        *,
        snapshot: Mapping[str, Any],
        snapshot_digest: str,
        budget: ResolverBudget | None = None,
        runbooks: Mapping[str, bool] | None = None,
        write_capabilities: frozenset[str] | None = None,
    ) -> ModeResolver:
        self.require(V2Capability.HYBRID)
        return ModeResolver(
            snapshot=snapshot,
            snapshot_digest=snapshot_digest,
            budget=self.resolver_budget(budget),
            runbooks=runbooks,
            write_capabilities=write_capabilities,
        )

    def hybrid_coordinator(
        self,
        *,
        resolver: ModeResolver,
        executor: DeterministicExecutor,
        agentic_step: Any = None,
    ) -> HybridCoordinator:
        self.require(V2Capability.HYBRID)
        if agentic_step is not None and not self.profile.allows_agentic:
            # An agentic step with a zero budget would be dead weight that a
            # later misconfiguration could silently wake up. Refuse it.
            raise CapabilityDisabled(V2Capability.HYBRID)
        return HybridCoordinator(
            resolver=resolver,
            executor=executor,
            agentic_step=agentic_step,
        )

    # -- evidence ---------------------------------------------------------
    def canonical(self) -> dict[str, Any]:
        return {
            "composition": "v2",
            "profile": self.profile.canonical(),
            "profile_digest": self.profile.digest(),
        }


__all__ = [
    "CapabilityDisabled",
    "V2Composition",
]
