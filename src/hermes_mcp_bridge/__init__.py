"""Hermes MCP Bridge package bootstrap.

The upstream capability loader predates the asynchronous server and remains a
small synchronous urllib helper. The server is its only internal consumer and
awaits it. Install one explicit async adapter at package import so the blocking
network lookup runs in a worker thread instead of silently falling back after a
``TypeError``.

This compatibility adapter is intentionally private, idempotent and preserves
the public loader arguments and returned payload.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any

from . import protocol as _protocol

__version__ = "1.0.0"


if not inspect.iscoroutinefunction(_protocol.load_upstream_capabilities):
    _sync_load_upstream_capabilities: Callable[..., dict[str, Any] | None] = (
        _protocol.load_upstream_capabilities
    )

    async def _async_load_upstream_capabilities(
        *,
        base_url: str,
        api_key: str,
        timeout: float = 5.0,
    ) -> dict[str, Any] | None:
        """Run the legacy capability probe without blocking the event loop."""

        return await asyncio.to_thread(
            _sync_load_upstream_capabilities,
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
        )

    _protocol.load_upstream_capabilities = _async_load_upstream_capabilities
