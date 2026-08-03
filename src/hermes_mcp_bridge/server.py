"""Streamable HTTP MCP server exposing Hermes as a delegated agent."""

from __future__ import annotations

import asyncio
import importlib
import os
import weakref
from contextlib import suppress
from types import ModuleType
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from .client import HermesAPIError, HermesClient
from .config import get_settings
from .models import (
    TERMINAL_STATUSES,
    HermesPromptResult,
    OrchestrationMode,
    RunStatus,
)
from .protocol import (
    AgentCard,
    CapabilityManifest,
    ToolManifest,
    load_agent_card_from_env,
    load_upstream_capabilities,
)
from .registry import RegistryError, compute_fingerprint, get_registry

settings = get_settings()
client = HermesClient(settings)
registry = get_registry()
registry.initialize()

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

_KEY_LOCKS: weakref.WeakValueDictionary[str, asyncio.Lock] = (
    weakref.WeakValueDictionary()
)


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


def _error_result(message: str, *, execution_id: str = "not-created") -> dict[str, Any]:
    result = HermesPromptResult(
        execution_id=execution_id,
        status=RunStatus.FAILED,
        error=message,
    )
    return result.model_dump(mode="json")


def _validate_prompt(prompt: str) -> None:
    normalized = prompt.strip()
    if not normalized:
        raise HermesAPIError("Prompt must not be empty")


async def _persist_mapping(
    *,
    client_request_id: str,
    fingerprint: str,
    execution_id: str,
    session_id: str | None,
    last_status: str,
) -> dict[str, object]:
    try:
        mapping = await asyncio.to_thread(
            registry.record,
            client_request_id=client_request_id,
            fingerprint=fingerprint,
            execution_id=execution_id,
            session_id=session_id,
            last_status=last_status,
        )
        return mapping
    except RegistryError:
        raise
    except Exception:
        warning = "registry record failed after run creation"
        return {
            "client_request_id": client_request_id,
            "execution_id": execution_id,
            "session_id": session_id,
            "last_status": last_status,
            "warning": warning,
        }


def _session_value(value: object | None) -> str | None:
    if value is None:
        return None
    return str(value)


def _registry_result(
    mapping: dict[str, object],
    *,
    execution_id_override: str | None = None,
) -> dict[str, Any]:
    return HermesPromptResult(
        session_id=_session_value(mapping.get("session_id")),
        execution_id=execution_id_override or str(mapping.get("execution_id", "not-created")),
        status=RunStatus(str(mapping.get("last_status", "unknown"))),
        metadata={"bridge_recovery_source": "registry"},
    ).model_dump(mode="json")


def _key_lock(client_request_id: str) -> asyncio.Lock:
    lock = _KEY_LOCKS.get(client_request_id)
    if lock is None:
        lock = asyncio.Lock()
        _KEY_LOCKS[client_request_id] = lock
    return lock


async def _submit_and_record(
    *,
    server: ModuleType,
    client_request_id: str | None,
    fingerprint: str,
    prompt: str,
    agent: str | None,
    subagents: list[str] | None,
    orchestration: OrchestrationMode,
    ctx: Context,
) -> dict[str, Any]:
    try:
        result = await client.submit_prompt(
            prompt=prompt,
            agent=agent,
            subagents=subagents,
            orchestration=orchestration,
            wait_seconds=0,
        )
    except HermesAPIError as exc:
        return _error_result(str(exc))

    if client_request_id is None:
        return result.model_dump(mode="json")

    async def _persist() -> dict[str, object]:
        return await _persist_mapping(
            client_request_id=client_request_id,
            fingerprint=fingerprint,
            execution_id=result.execution_id,
            session_id=result.session_id,
            last_status=result.status.value,
        )

    try:
        persisted = await asyncio.shield(_persist())
    except asyncio.CancelledError:
        persisted = await _persist()
        raise
    warning = persisted.get("warning") if isinstance(persisted, dict) else None
    if warning is None:
        return result.model_dump(mode="json")
    result.metadata = {**result.metadata, "warning": str(warning)}
    return result.model_dump(mode="json")


@mcp.tool()
async def hermes_submit(
    prompt: str,
    ctx: Context,
    client_request_id: str | None = None,
    agent: str | None = None,
    subagents: list[str] | None = None,
    orchestration: OrchestrationMode = OrchestrationMode.AUTO,
) -> dict[str, Any]:
    """Create a Hermes run and return execution/session identifiers immediately."""

    _validate_prompt(prompt)

    fingerprint = compute_fingerprint(
        prompt=prompt,
        session_id=None,
        agent=agent,
        subagents=subagents,
        orchestration=orchestration.value,
    )

    if client_request_id is not None:
        async with _key_lock(client_request_id):
            try:
                existing = await asyncio.to_thread(registry.get, client_request_id)
            except RegistryError as exc:
                return _error_result(str(exc), execution_id="not-created")

            if existing is not None:
                stored_fingerprint = str(existing.get("fingerprint", ""))
                if stored_fingerprint != fingerprint:
                    return _error_result(
                        "existing mapping has a different fingerprint",
                        execution_id="not-created",
                    )
                return _registry_result(existing)
            return await _submit_and_record(
                server=importlib.import_module("hermes_mcp_bridge.server"),
                client_request_id=client_request_id,
                fingerprint=fingerprint,
                prompt=prompt,
                agent=agent,
                subagents=subagents,
                orchestration=orchestration,
                ctx=ctx,
            )

    return await _submit_and_record(
        server=importlib.import_module("hermes_mcp_bridge.server"),
        client_request_id=client_request_id,
        fingerprint=fingerprint,
        prompt=prompt,
        agent=agent,
        subagents=subagents,
        orchestration=orchestration,
        ctx=ctx,
    )


@mcp.tool()
async def hermes_prompt(
    prompt: str,
    ctx: Context,
    client_request_id: str | None = None,
    session_id: str | None = None,
    agent: str | None = None,
    subagents: list[str] | None = None,
    orchestration: OrchestrationMode = OrchestrationMode.AUTO,
    wait_seconds: float | None = None,
    stop_on_disconnect: bool = False,
) -> dict[str, Any]:
    """Delegate an objective to Hermes, keep the MCP request connected, and wait."""

    _validate_prompt(prompt)

    fingerprint = compute_fingerprint(
        prompt=prompt,
        session_id=session_id,
        agent=agent,
        subagents=subagents,
        orchestration=orchestration.value,
    )

    if client_request_id is not None:
        async with _key_lock(client_request_id):
            try:
                existing = await asyncio.to_thread(registry.get, client_request_id)
            except RegistryError as exc:
                return _error_result(str(exc), execution_id="not-created")

            if existing is not None:
                stored_fingerprint = str(existing.get("fingerprint", ""))
                if stored_fingerprint != fingerprint:
                    return _error_result(
                        "existing mapping has a different fingerprint",
                        execution_id="not-created",
                    )
                execution_id = str(existing.get("execution_id", ""))
                try:
                    result = await client.get_run(
                        execution_id,
                        fallback_session_id=_session_value(existing.get("session_id")),
                        agent=agent,
                        subagents=subagents,
                    )
                except HermesAPIError:
                    result = HermesPromptResult(
                        session_id=_session_value(existing.get("session_id")),
                        execution_id=execution_id,
                        status=RunStatus(str(existing.get("last_status", "unknown"))),
                        metadata={"bridge_recovery_source": "registry"},
                    )
                if result.status in TERMINAL_STATUSES:
                    return result.model_dump(mode="json")
                return await client.wait_for_run(
                    execution_id,
                    max_wait_seconds=(
                        settings.hermes_run_default_wait_seconds
                        if wait_seconds is None
                        else client._bounded_wait(wait_seconds)
                    ),
                    fallback_session_id=result.session_id,
                    agent=agent,
                    subagents=subagents,
                    progress_callback=None,
                )

            try:
                result = await client.submit_prompt(
                    prompt=prompt,
                    session_id=session_id,
                    agent=agent,
                    subagents=subagents,
                    orchestration=orchestration,
                    wait_seconds=wait_seconds,
                    stop_on_cancel=stop_on_disconnect,
                )
            except HermesAPIError as exc:
                return _error_result(str(exc))

            persisted = await _persist_mapping(
                client_request_id=client_request_id,
                fingerprint=fingerprint,
                execution_id=result.execution_id,
                session_id=result.session_id,
                last_status=result.status.value,
            )
            warning = persisted.get("warning") if isinstance(persisted, dict) else None
            if warning is None:
                return result.model_dump(mode="json")
            result.metadata = {**result.metadata, "warning": str(warning)}
            return result.model_dump(mode="json")

    try:
        result = await client.submit_prompt(
            prompt=prompt,
            session_id=session_id,
            agent=agent,
            subagents=subagents,
            orchestration=orchestration,
            wait_seconds=wait_seconds,
            stop_on_cancel=stop_on_disconnect,
        )
    except HermesAPIError as exc:
        return _error_result(str(exc))
    return result.model_dump(mode="json")


@mcp.tool()
async def hermes_wait(
    execution_id: str,
    ctx: Context,
    wait_seconds: float | None = None,
) -> dict[str, Any]:
    """Wait for an existing Hermes run and return when it completes or the budget expires."""

    if not execution_id.strip():
        return _error_result("execution_id must not be empty")

    max_wait = (
        settings.hermes_run_default_wait_seconds
        if wait_seconds is None
        else client._bounded_wait(wait_seconds)
    )

    progress_event_count = 0

    async def progress(event: dict[str, Any]) -> None:
        nonlocal progress_event_count
        message = _progress_message(event)
        if message is None:
            return
        progress_event_count += 1
        await ctx.report_progress(
            progress=float(progress_event_count),
            message=message,
        )

    try:
        result = await client.wait_for_run(
            execution_id,
            max_wait_seconds=max_wait,
            progress_callback=progress,
        )
        return result.model_dump(mode="json")
    except HermesAPIError as exc:
        return _error_result(str(exc), execution_id=execution_id)


@mcp.tool()
async def hermes_status(execution_id: str) -> dict[str, Any]:
    """Retrieve the current status and output of a Hermes execution."""

    if not execution_id.strip():
        return _error_result("execution_id must not be empty")

    try:
        result = await client.get_run(execution_id)
        return result.model_dump(mode="json")
    except HermesAPIError:
        try:
            mapping = await asyncio.to_thread(registry.get, execution_id)
            if mapping is not None:
                return _registry_result(mapping, execution_id_override=execution_id)
        except RegistryError:
            pass
        return _error_result(
            str(HermesAPIError("unavailable")),
            execution_id=execution_id,
        )


@mcp.tool()
async def hermes_stop(execution_id: str) -> dict[str, Any]:
    """Request cancellation of a Hermes execution at the next safe interruption point."""

    if not execution_id.strip():
        return _error_result("execution_id must not be empty")

    try:
        result = await client.stop_run(execution_id)
        await asyncio.to_thread(
            registry.update_status,
            client_request_id=execution_id,
            last_status=result.status.value,
            execution_id=result.execution_id,
        )
        return result.model_dump(mode="json")
    except HermesAPIError as exc:
        with suppress(RegistryError):
            await asyncio.to_thread(
                registry.update_status,
                client_request_id=execution_id,
                last_status="stopping",
            )
        return _error_result(str(exc), execution_id=execution_id)


@mcp.tool()
async def hermes_recent_runs(
    limit: int = 50,
    status: str | None = None,
) -> dict[str, Any]:
    """List recent Hermes runs visible to this bridge."""

    try:
        limit = max(1, min(100, int(limit)))
    except (TypeError, ValueError):
        limit = 50

    try:
        items = await asyncio.to_thread(
            registry.list_recent,
            limit=limit,
            status=status,
        )
    except RegistryError as exc:
        return {"object": "list", "data": [], "warning": str(exc)}

    sanitized = [
        {
            "client_request_id": item.get("client_request_id"),
            "execution_id": item.get("execution_id"),
            "session_id": item.get("session_id"),
            "last_status": item.get("last_status"),
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
        }
        for item in items
    ]
    return {"object": "list", "data": sanitized}


@mcp.tool()
async def hermes_health(detailed: bool = False) -> dict[str, Any]:
    """Check the Hermes API server liveness or authenticated readiness."""

    try:
        upstream = await client.health(detailed=detailed)
    except HermesAPIError as exc:
        upstream = {"status": "error", "error": str(exc)}

    registry_health = await asyncio.to_thread(registry.health)
    bridge: dict[str, Any] = {
        "default_wait_seconds": settings.hermes_run_default_wait_seconds,
        "max_wait_seconds": settings.hermes_run_max_wait_seconds,
        "state_registry": registry_health,
    }
    upstream_payload = upstream if isinstance(upstream, dict) else {}
    manifest = await _build_capability_manifest()
    bridge["manifest_version"] = manifest.manifest_version
    bridge["manifest_hash"] = manifest.manifest_hash
    bridge["bridge_version"] = manifest.bridge_version
    bridge["schema_version"] = manifest.schema_version
    upstream_manifest_hash = (
        upstream_payload.get("capability_manifest_hash")
        or upstream_payload.get("manifest_hash")
    )
    if upstream_manifest_hash and upstream_manifest_hash != manifest.manifest_hash:
        bridge["manifest_hash_divergence"] = True
    elif upstream_manifest_hash:
        bridge["manifest_hash_divergence"] = False
    return {"upstream": upstream, "bridge": bridge}


@mcp.tool()
async def hermes_capabilities() -> dict[str, Any]:
    """Return the canonical capability manifest for this bridge."""
    manifest = await _build_capability_manifest()
    payload = manifest.model_dump(mode="json")
    payload["capability_hash"] = manifest.manifest_hash
    return payload


@mcp.tool()
async def hermes_agent_card() -> dict[str, Any]:
    """Return the versioned agent card for this bridge."""
    card = load_agent_card_from_env()
    payload = card.to_canonical_dict()
    payload["card_hash"] = _card_hash(card)
    return payload


async def _build_capability_manifest() -> CapabilityManifest:
    upstream_payload = None
    try:
        upstream_payload = await load_upstream_capabilities(
            base_url=settings.hermes_api_base_url,
            api_key=settings.hermes_api_key.get_secret_value(),
            timeout=5.0,
        )
    except Exception:
        upstream_payload = None

    tools = [
        ToolManifest(
            name="hermes_submit",
            description="Create a Hermes run and return execution/session identifiers.",
            version_added="0.1.0",
            stability="stable",
            read_only=False,
        ),
        ToolManifest(
            name="hermes_prompt",
            description="Delegate an objective to Hermes and wait.",
            version_added="0.1.0",
            stability="stable",
            read_only=False,
        ),
        ToolManifest(
            name="hermes_wait",
            description="Wait for an existing Hermes run.",
            version_added="0.1.0",
            stability="stable",
            read_only=False,
        ),
        ToolManifest(
            name="hermes_status",
            description="Retrieve the current status of a Hermes execution.",
            version_added="0.1.0",
            stability="stable",
            read_only=True,
        ),
        ToolManifest(
            name="hermes_stop",
            description="Request cancellation of a Hermes execution.",
            version_added="0.1.0",
            stability="stable",
            read_only=False,
        ),
        ToolManifest(
            name="hermes_health",
            description="Check Hermes liveness/readiness and bridge registry state.",
            version_added="0.1.0",
            stability="stable",
            read_only=True,
        ),
        ToolManifest(
            name="recent_runs",
            description="List recent registry entries by status or recency.",
            version_added="0.1.0",
            stability="stable",
            read_only=True,
        ),
        ToolManifest(
            name="hermes_capabilities",
            description="Return the canonical capability manifest for this bridge.",
            version_added="0.4.0",
            stability="experimental",
            read_only=True,
        ),
        ToolManifest(
            name="hermes_agent_card",
            description="Return the versioned agent card for this bridge.",
            version_added="0.4.0",
            stability="experimental",
            read_only=True,
        ),
    ]

    return CapabilityManifest.build(
        bridge_version="0.4.0",
        manifest_version="0.4.0",
        tools=tools,
        orchestration_modes=["auto", "explicit"],
        limits={
            "max_wait_seconds": settings.hermes_run_max_wait_seconds,
            "default_wait_seconds": settings.hermes_run_default_wait_seconds,
            "max_prompt_chars": 200000,
            "max_subagents": 16,
        },
        provenance={
            "source": "bridge",
            "package": "hermes-mcp-bridge",
            "commit": os.environ.get("HERMES_BRIDGE_COMMIT", "unknown"),
        },
        upstream_capabilities=upstream_payload,
    )


def _card_hash(card: AgentCard) -> str:
    return _canonical_json_hash(card.to_canonical_dict())


@staticmethod
def _canonical_json_hash(payload: Any) -> str:
    from .protocol import _canonical_json_hash as _hash

    return _hash(payload)


def _load_server_module() -> ModuleType:
    return importlib.import_module("hermes_mcp_bridge.server")


def server_tool_names() -> list[str]:
    server = _load_server_module()
    tools = server.mcp._tool_manager.list_tools()
    return sorted(tool.name for tool in tools)


def main() -> None:
    """Run the bridge using MCP Streamable HTTP transport."""

    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
