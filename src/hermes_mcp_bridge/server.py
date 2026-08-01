"""Streamable HTTP MCP server exposing Hermes as a delegated agent."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from .client import HermesAPIError, HermesClient
from .config import get_settings
from .models import HermesPromptResult, OrchestrationMode, RunStatus

settings = get_settings()
client = HermesClient(settings)

mcp = FastMCP(
    "Hermes MCP Bridge",
    instructions=(
        "Delegate natural-language objectives to hermes-agent. Hermes owns execution, "
        "tools, skills, agents, subagents, network access, and Kanban operations. "
        "hermes_prompt remains connected by default and reports progress while Hermes "
        "works. Use wait_seconds=0 only for detached execution, hermes_status as a "
        "recovery fallback, and hermes_stop only when cancellation is requested. Reuse "
        "the native Hermes session_id returned by hermes_prompt for follow-up work."
    ),
    host=settings.mcp_host,
    port=settings.mcp_port,
    streamable_http_path=settings.mcp_path,
    stateless_http=True,
    json_response=False,
)


@mcp.tool()
async def hermes_prompt(
    prompt: str,
    ctx: Context,
    session_id: str | None = None,
    agent: str | None = None,
    subagents: list[str] | None = None,
    orchestration: OrchestrationMode = OrchestrationMode.AUTO,
    wait_seconds: float | None = None,
    stop_on_disconnect: bool = False,
) -> dict[str, Any]:
    """Delegate an objective to Hermes and keep the MCP request connected.

    By default this tool waits up to the configured maximum, consumes Hermes run
    events, emits MCP progress notifications and returns the final output in the
    original tool call. Set ``wait_seconds=0`` only when detached execution is
    explicitly required. A lost client connection does not stop Hermes unless
    ``stop_on_disconnect`` is true; the run remains recoverable by execution ID.
    """

    progress = 0.0

    async def report(event: dict[str, Any]) -> None:
        nonlocal progress
        message = _progress_message(event)
        if message is None:
            return
        progress += 1.0
        await ctx.report_progress(progress=progress, message=message)

    try:
        result = await client.submit_prompt(
            prompt=prompt,
            session_id=session_id,
            agent=agent,
            subagents=subagents,
            orchestration=orchestration,
            wait_seconds=wait_seconds,
            progress_callback=report,
            stop_on_cancel=stop_on_disconnect,
        )
        return result.model_dump(mode="json")
    except HermesAPIError as exc:
        return _error_result(str(exc)).model_dump(mode="json")


@mcp.tool()
async def hermes_status(execution_id: str) -> dict[str, Any]:
    """Retrieve the current status and output of a Hermes execution."""

    try:
        result = await client.get_run(execution_id)
        return result.model_dump(mode="json")
    except HermesAPIError as exc:
        return _error_result(str(exc), execution_id=execution_id).model_dump(mode="json")


@mcp.tool()
async def hermes_stop(execution_id: str) -> dict[str, Any]:
    """Request cancellation of a Hermes execution at the next safe interruption point."""

    try:
        result = await client.stop_run(execution_id)
        return result.model_dump(mode="json")
    except HermesAPIError as exc:
        return _error_result(str(exc), execution_id=execution_id).model_dump(mode="json")


@mcp.tool()
async def hermes_health(detailed: bool = False) -> dict[str, Any]:
    """Check the Hermes API server liveness or authenticated readiness."""

    try:
        return await client.health(detailed=detailed)
    except HermesAPIError as exc:
        return {"status": "error", "error": str(exc)}


def _progress_message(event: dict[str, Any]) -> str | None:
    event_type = str(event.get("event") or "")
    if event_type in {"message.delta", "reasoning.available"}:
        return None
    if event_type == "bridge.run.accepted":
        return f"Hermes accepted run {event.get('run_id')} in session {event.get('session_id')}."
    if event_type == "bridge.heartbeat":
        elapsed = event.get("elapsed_seconds", 0)
        return f"Hermes is still working ({event.get('status', 'running')}, {elapsed}s elapsed)."
    if event_type == "bridge.event_stream_fallback":
        return "Hermes event streaming ended; the bridge is continuing with status polling."
    if event_type == "bridge.wait_expired":
        return (
            "The connected wait budget expired; the Hermes run remains available "
            "by execution ID."
        )
    if event_type == "tool.started":
        return f"Hermes started tool: {_safe_label(event.get('tool'))}."
    if event_type == "tool.completed":
        outcome = "with an error" if event.get("error") else "successfully"
        return f"Hermes completed tool {_safe_label(event.get('tool'))} {outcome}."
    if event_type == "subagent.start":
        return f"Hermes started subagent {_safe_label(event.get('subagent_id'))}."
    if event_type == "subagent.complete":
        return f"Hermes completed subagent {_safe_label(event.get('subagent_id'))}."
    if event_type == "approval.request":
        return "Hermes is waiting for an approval before it can continue."
    if event_type == "approval.responded":
        return "The pending Hermes approval was resolved."
    if event_type == "run.completed":
        return "Hermes completed the run and is returning the final result."
    if event_type == "run.failed":
        return "Hermes reported that the run failed."
    if event_type == "run.cancelled":
        return "The Hermes run was cancelled."
    return f"Hermes progress: {_safe_label(event_type or 'running')}."


def _safe_label(value: Any) -> str:
    text = str(value or "unknown").replace("\n", " ").replace("\r", " ")
    return text[:120]


def _error_result(message: str, *, execution_id: str = "not-created") -> HermesPromptResult:
    return HermesPromptResult(
        execution_id=execution_id,
        status=RunStatus.FAILED,
        error=message,
    )


def main() -> None:
    """Run the bridge using MCP Streamable HTTP transport."""

    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
