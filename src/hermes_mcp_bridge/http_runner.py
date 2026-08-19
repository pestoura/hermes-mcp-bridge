"""Production HTTP runner with deterministic bridge-owned logging and graceful drain.

The server now tracks in-flight tool invocations and, on shutdown, drains them
within a bounded grace window before stopping. A turn that completes clears its
registry entry in a `finally`, so the runner never waits on a ghost turn and
never requires a manual restart to clear an orphan. This is the same invariant
the upstream gateway drain defect violated; kept here so the bridge's own
lifecycle is demonstrably correct.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal

import uvicorn

from .lifecycle import (
    DEFAULT_GRACE_SECONDS,
    InFlightRegistry,
    drain_in_flight,
)
from .observability import (
    configure_logging,
    instrument_all_tools,
    log_event,
    start_exporter_if_enabled,
)


def _reset_logging() -> None:
    """Remove framework handlers and restore the bridge redacting pipeline."""

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    configure_logging(force=True)


# Module-level registry so the MCP tool surface can register in-flight work.
INFLOW = InFlightRegistry()


async def _serve() -> None:
    # Importing FastMCP may configure its own root handler. Reapply the bridge
    # policy afterwards so every subsequent line is redacted structured JSON.
    from .factory_northbound import configure_factory_northbound
    from .server import mcp, server_tool_names, settings

    # Factory northbound is an additive external surface on the same FastMCP
    # instance. It is absent by default and must be composed before the HTTP app
    # is created. Re-running central instrumentation is idempotent for the
    # already-covered baseline and wraps only newly registered Factory tools.
    configure_factory_northbound(mcp, settings)
    instrument_all_tools(mcp)

    _reset_logging()
    start_exporter_if_enabled()
    log_event(
        "bridge.startup",
        outcome="success",
        bridge_version=settings.bridge_version,
        instrumented_tools=len(server_tool_names()),
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

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _request_stop(*_args: object) -> None:
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _request_stop)

    serve_task = asyncio.ensure_future(server.serve())
    await stop_event.wait()

    # Graceful drain: stop accepting, let in-flight turns finish, sweep survivors.
    log_event("bridge.shutdown.requested", outcome="in_progress")
    server.should_exit = True
    try:
        drain = await drain_in_flight(
            INFLOW, grace_seconds=float(os.environ.get("BRIDGE_DRAIN_GRACE", DEFAULT_GRACE_SECONDS))
        )
    except BaseException as exc:  # pragma: no cover - defensive
        drain = {"error": type(exc).__name__}
    log_event("bridge.shutdown.drain", outcome="complete", **drain)

    with contextlib.suppress(asyncio.TimeoutError, BaseException):
        await asyncio.wait_for(serve_task, timeout=10.0)

    log_event("bridge.shutdown.complete", outcome="success")


def main() -> None:
    """Run the Streamable HTTP bridge without framework-owned text handlers."""

    asyncio.run(_serve())


if __name__ == "__main__":
    main()
