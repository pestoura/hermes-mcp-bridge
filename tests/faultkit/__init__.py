"""Deterministic fault-injection framework (test-only).

This package is imported exclusively by the test suite and the CI-safe load
harness. It is *not* part of the installed runtime package and must never be
imported from ``src/hermes_mcp_bridge``.

Design constraints:

* deterministic and seedable — every decision comes from a seeded
  :class:`random.Random`, never from wall-clock or global state;
* no global monkeypatching — faults are injected through explicit collaborator
  objects (httpx transports, sqlite connection factories, byte sinks);
* no external dependencies beyond what the project already requires.
"""

from __future__ import annotations

from .http import FaultProfile, FaultyTransport, ScriptedResponse
from .sqlite import FaultySqlite, disk_full_connection, flaky_connection
from .sse import (
    SSEScript,
    duplicated_events,
    invalid_event_stream,
    out_of_order_events,
    truncated_stream,
)

__all__ = [
    "FaultProfile",
    "FaultySqlite",
    "FaultyTransport",
    "SSEScript",
    "ScriptedResponse",
    "disk_full_connection",
    "duplicated_events",
    "flaky_connection",
    "invalid_event_stream",
    "out_of_order_events",
    "truncated_stream",
]
