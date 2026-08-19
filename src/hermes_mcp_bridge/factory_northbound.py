"""Optional FastMCP exposure of the Hermes Factory northbound control port.

This module owns no Factory state and performs no internal Factory IPC. It only
registers a closed external surface on the *existing* Hermes MCP Bridge when an
operator explicitly enables the integration and supplies a control port.

The default is no registration, preserving the stable 27-tool Bridge baseline.
"""

from __future__ import annotations

import asyncio
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


FACTORY_MCP_TOOL_NAMES: tuple[str, ...] = (
    "factory_acceptance",
    "factory_evidence",
    "factory_protected_mutation_intent",
    "factory_status",
)


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
    "FactoryNorthboundUnavailable",
    "register_factory_northbound_tools",
]
