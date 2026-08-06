"""Production HTTP runner with deterministic bridge-owned logging."""

from __future__ import annotations

import asyncio
import logging
import os

import uvicorn

from .observability import (
    configure_logging,
    log_event,
    start_exporter_if_enabled,
)


def _reset_logging() -> None:
    """Remove framework handlers and restore the bridge redacting pipeline."""

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    configure_logging(force=True)


async def _serve() -> None:
    # Importing FastMCP may configure its own root handler. Reapply the bridge
    # policy afterwards so every subsequent line is redacted structured JSON.
    from .server import INSTRUMENTED_TOOL_COUNT, mcp, settings

    _reset_logging()
    start_exporter_if_enabled()
    log_event(
        "bridge.startup",
        outcome="success",
        bridge_version=settings.bridge_version,
        instrumented_tools=INSTRUMENTED_TOOL_COUNT,
    )

    app = mcp.streamable_http_app()
    config = uvicorn.Config(
        app,
        host=settings.mcp_host,
        port=settings.mcp_port,
        log_level=os.environ.get("BRIDGE_LOG_LEVEL", "INFO").lower(),
        log_config=None,
        access_log=False,
    )
    server = uvicorn.Server(config)
    await server.serve()


def main() -> None:
    """Run the Streamable HTTP bridge without framework-owned text handlers."""

    asyncio.run(_serve())


if __name__ == "__main__":
    main()
