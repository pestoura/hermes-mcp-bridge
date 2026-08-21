"""Optional FastMCP exposure of the Hermes Factory northbound control port.

This module owns no Factory state and performs no internal Factory IPC. It only
registers a closed external surface on the *existing* Hermes MCP Bridge when an
operator explicitly enables the integration and supplies a control port.

The default is no registration, preserving the stable 27-tool Bridge baseline.
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol


class FactoryNorthboundUnavailable(RuntimeError):
    """Raised when Factory northbound was explicitly enabled but cannot be bound."""


class FactoryControlPort(Protocol):
    """Transport-neutral subset of the Factory northbound control contract."""

    def status(self, *, candidate_sha: str, principal: str) -> dict[str, Any]: ...

    def evidence(self, *, candidate_sha: str, principal: str) -> dict[str, Any]: ...

    def acceptance(self, *, candidate_sha: str, principal: str) -> dict[str, Any]: ...

    def protected_mutation_intent(
        self,
        *,
        candidate_sha: str,
        principal: str,
        action: str,
        resource: str,
        authority_evidence_id: str,
        human_decision_id: str,
    ) -> dict[str, Any]: ...


class FactoryLibraryControlPort:
    """Lazy binding from the Bridge to the installed ``hermes-factory`` package.

    The external caller never supplies an origin. This adapter always constructs
    ``NorthboundCaller(..., origin=EXTERNAL)`` and delegates to the Factory's own
    authorization and evidence rules.
    """

    def __init__(
        self,
        registry_path: str,
        *,
        module_loader: Callable[[str], Any] = importlib.import_module,
    ) -> None:
        normalized_path = registry_path.strip()
        if not normalized_path:
            raise FactoryNorthboundUnavailable("Factory registry path is required")

        try:
            registry_module = module_loader("hermes_factory.traceability.registry")
            control_module = module_loader("hermes_factory.control.northbound")
            registry_type = registry_module.SemanticRegistry
            control_type = control_module.NorthboundControl
            self._caller_type = control_module.NorthboundCaller
            self._external_origin = control_module.NorthboundOrigin.EXTERNAL
            self._action_type = control_module.ProtectedMutationAction
        except (ImportError, AttributeError) as exc:
            raise FactoryNorthboundUnavailable(
                "hermes-factory package does not expose the required northbound contract"
            ) from exc

        try:
            self._control = control_type(registry_type(Path(normalized_path)))
        except Exception as exc:
            raise FactoryNorthboundUnavailable(
                "Hermes Factory control port could not initialize its registry"
            ) from exc

    def _caller(self, principal: str) -> Any:
        return self._caller_type(principal=principal, origin=self._external_origin)

    def status(self, *, candidate_sha: str, principal: str) -> dict[str, Any]:
        return self._control.status(
            candidate_sha=candidate_sha,
            caller=self._caller(principal),
        )

    def evidence(self, *, candidate_sha: str, principal: str) -> dict[str, Any]:
        return self._control.evidence(
            candidate_sha=candidate_sha,
            caller=self._caller(principal),
        )

    def acceptance(self, *, candidate_sha: str, principal: str) -> dict[str, Any]:
        return self._control.acceptance(
            candidate_sha=candidate_sha,
            caller=self._caller(principal),
        )

    def protected_mutation_intent(
        self,
        *,
        candidate_sha: str,
        principal: str,
        action: str,
        resource: str,
        authority_evidence_id: str,
        human_decision_id: str,
    ) -> dict[str, Any]:
        return self._control.protected_mutation_intent(
            action=self._action_type(action),
            resource=resource,
            candidate_sha=candidate_sha,
            caller=self._caller(principal),
            authority_evidence_id=authority_evidence_id,
            human_decision_id=human_decision_id,
        )


FACTORY_MCP_TOOL_NAMES: tuple[str, ...] = (
    "factory_acceptance",
    "factory_evidence",
    "factory_protected_mutation_intent",
    "factory_status",
)


def configure_factory_northbound(
    mcp: Any,
    settings: Any,
    *,
    port_factory: Callable[[str], FactoryControlPort] = FactoryLibraryControlPort,
) -> tuple[str, ...]:
    """Compose the optional Factory boundary from explicit Bridge settings."""
    enabled = bool(getattr(settings, "hermes_factory_northbound_enabled", False))
    if not enabled:
        return ()
    registry_path = str(getattr(settings, "hermes_factory_registry_path", ""))
    port = port_factory(registry_path)
    return register_factory_northbound_tools(mcp, enabled=True, port=port)


def register_factory_northbound_tools(
    mcp: Any,
    *,
    enabled: bool,
    port: FactoryControlPort | None,
) -> tuple[str, ...]:
    """Register the four Factory tools or leave the Bridge surface unchanged.

    Explicit enablement without a bound port is a startup/configuration error.
    There is deliberately no fallback to ``hermes_prompt`` or another execution
    lane because that would bypass the Factory governance contract.
    """
    if not enabled:
        return ()
    if port is None:
        raise FactoryNorthboundUnavailable(
            "Factory northbound is enabled but no Factory control port is configured"
        )

    async def factory_acceptance(candidate_sha: str, principal: str) -> dict[str, Any]:
        return await asyncio.to_thread(
            port.acceptance,
            candidate_sha=candidate_sha,
            principal=principal,
        )

    async def factory_evidence(candidate_sha: str, principal: str) -> dict[str, Any]:
        return await asyncio.to_thread(
            port.evidence,
            candidate_sha=candidate_sha,
            principal=principal,
        )

    async def factory_protected_mutation_intent(
        candidate_sha: str,
        principal: str,
        action: str,
        resource: str,
        authority_evidence_id: str,
        human_decision_id: str,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            port.protected_mutation_intent,
            candidate_sha=candidate_sha,
            principal=principal,
            action=action,
            resource=resource,
            authority_evidence_id=authority_evidence_id,
            human_decision_id=human_decision_id,
        )

    async def factory_status(candidate_sha: str, principal: str) -> dict[str, Any]:
        return await asyncio.to_thread(
            port.status,
            candidate_sha=candidate_sha,
            principal=principal,
        )

    for function in (
        factory_acceptance,
        factory_evidence,
        factory_protected_mutation_intent,
        factory_status,
    ):
        mcp.tool()(function)

    return FACTORY_MCP_TOOL_NAMES


__all__ = [
    "FACTORY_MCP_TOOL_NAMES",
    "FactoryControlPort",
    "FactoryLibraryControlPort",
    "FactoryNorthboundUnavailable",
    "configure_factory_northbound",
    "register_factory_northbound_tools",
]
