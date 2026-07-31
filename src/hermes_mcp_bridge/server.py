"""Streamable HTTP MCP server exposing Hermes as a delegated agent."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

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
        "Use hermes_prompt for new work, hermes_status for long runs, and hermes_stop "
        "only when the user requests cancellation. Reuse the native Hermes session_id "
        "returned by hermes_prompt when continuing the same task."
    ),
    host=settings.mcp_host,
    port=settings.mcp_port,
    streamable_http_path=settings.mcp_path,
    stateless_http=True,
    json_response=True,
)


@mcp.tool()
async def hermes_prompt(
    prompt: str,
    session_id: str | None = None,
    agent: str | None = None,
    subagents: list[str] | None = None,
    orchestration: OrchestrationMode = OrchestrationMode.AUTO,
    wait_seconds: float | None = None,
) -> dict[str, Any]:
    """Delegate an objective to Hermes and return its normalized output.

    Hermes performs the actual planning and execution using its configured tools,
    skills, agents, subagents, credentials, servers, and Kanban integrations.
    Omit ``session_id`` to create a native Hermes session. Reuse the returned
    ``session_id`` to continue that session with its persisted history. ``agent``
    and ``subagents`` are optional routing hints; omit them to let Hermes decide.
    If the wait budget expires, this tool returns the current status and
    ``execution_id`` for a later ``hermes_status`` call.
    """

    try:
        result = await client.submit_prompt(
            prompt=prompt,
            session_id=session_id,
            agent=agent,
            subagents=subagents,
            orchestration=orchestration,
            wait_seconds=wait_seconds,
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
