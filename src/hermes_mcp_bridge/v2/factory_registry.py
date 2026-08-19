"""Canonical typed tool identities for Factory northbound control."""

from __future__ import annotations

FACTORY_NORTHBOUND_TOOL_IDS: tuple[str, ...] = (
    "factory.evidence",
    "factory.prepare_mutation",
    "factory.projects",
    "factory.status",
)

__all__ = ["FACTORY_NORTHBOUND_TOOL_IDS"]
