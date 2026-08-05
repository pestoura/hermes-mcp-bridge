"""Streamable HTTP MCP server exposing Hermes as a delegated agent."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
import uuid
import weakref
from contextlib import suppress
from datetime import UTC, datetime
from types import ModuleType
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from .approvals import (
    ApprovalConsumedError,
    ApprovalExpiredError,
    ApprovalNotFound,
    ApprovalStaleError,
    ApprovalStatusError,
    get_approval_registry,
)
from .checkpoints import CheckpointRegistry
from .client import HermesAPIError, HermesClient
from .config import get_settings
from .contracts import CURRENT_CONTRACT_VERSION, SCHEMA_VERSION, validate_tools
from .locks import LockError, LockRegistry, LockType, ResourceLock
from .migrations import apply_migrations
from .models import (
    TERMINAL_STATUSES,
    Checkpoint,
    Continuation,
    HermesPromptResult,
    OrchestrationMode,
    Plan,
    PlanApprovalPoint,
    PlanDependency,
    PlanStatus,
    PlanStep,
    RunStatus,
    Saga,
    SagaStatus,
    SagaStep,
)
from .observability import (
    configure_logging,
    instrument_all_tools,
    log_event,
    observability_health,
    record_approval,
    set_active_runs,
    set_bridge_info,
    set_migrations_version,
    start_exporter_if_enabled,
)
from .policy import (
    PolicyError,
    classify_action,
    evaluate_policy,
    get_active_policy,
)
from .protocol import (
    AgentCard,
    ApprovalRecord,
    ApprovalStatus,
    CapabilityManifest,
    MutationClass,
    OrchestrationPolicy,
    PolicyEvaluationInput,
    ProvenanceClaimType,
    ToolManifest,
    TrustLabel,
    load_agent_card_from_env,
    load_upstream_capabilities,
)
from .provenance import ProvenanceClaim, build_result_manifest
from .quotas import get_quota_registry
from .registry import RegistryError, compute_fingerprint, get_registry
from .sagas import SagaRegistry
from .signing import signing_posture
from .tracing import build_trace_metadata

settings = get_settings()
configure_logging()
set_bridge_info(settings.bridge_version)
_schema_version = apply_migrations(settings.bridge_state_db_path)
set_migrations_version(_schema_version or 0)
client = HermesClient(settings)
registry = get_registry()
registry.initialize()
approval_registry = get_approval_registry()
approval_registry.initialize()
checkpoint_registry = CheckpointRegistry(settings.bridge_state_db_path)
checkpoint_registry.initialize()
lock_registry = LockRegistry(settings.bridge_state_db_path)
lock_registry.initialize()
saga_registry = SagaRegistry(settings.bridge_state_db_path)
saga_registry.initialize()
quota_registry = get_quota_registry()
quota_registry.initialize()

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


def _structured_error(
    payload: dict[str, Any],
    *,
    execution_id: str = "not-created",
) -> dict[str, Any]:
    result = HermesPromptResult(
        execution_id=execution_id,
        status=RunStatus.FAILED,
        metadata={"policy": payload},
    )
    dumped = result.model_dump(mode="json")
    dumped.update(payload)
    return dumped


def _validate_prompt(prompt: str) -> None:
    normalized = prompt.strip()
    if not normalized:
        raise HermesAPIError("Prompt must not be empty")


def _policy_decision_from_inputs(
    action: str,
    *,
    mutation_class: MutationClass | None = None,
    trust_label: str | TrustLabel | None = None,
    principal: str | None = None,
    resource: str | None = None,
) -> tuple[str, str | None]:
    try:
        trust_enum = (
            trust_label
            if isinstance(trust_label, TrustLabel)
            else TrustLabel(trust_label)
            if trust_label
            else TrustLabel.UNKNOWN
        )
    except ValueError:
        # An unparseable trust label is untrusted input, not a free pass.
        trust_enum = TrustLabel.UNTRUSTED_CONTENT
    try:
        evaluation = PolicyEvaluationInput(
            action=action,
            resource=resource,
            trust_label=trust_enum,
            mutation_class=(
                mutation_class or _mutation_from_action(action) or MutationClass.NONE
            ),
            principal=principal,
        )
        result = evaluate_policy(evaluation)
    except Exception as exc:
        return "error", str(exc)
    return result.decision.value, result.reason


def _enforce_policy(
    action: str,
    *,
    mutation_class: MutationClass | None = None,
    trust_label: str | TrustLabel | None = None,
    principal: str | None = None,
    resource: str | None = None,
) -> dict[str, Any] | None:
    """Return a blocking payload, or ``None`` when the call may proceed.

    Fail-closed: anything that is not an explicit ALLOW blocks. Evaluation
    errors and unrecognised decisions are treated as DENY.
    """

    decision, reason = _policy_decision_from_inputs(
        action,
        mutation_class=mutation_class,
        trust_label=trust_label,
        principal=principal,
        resource=resource,
    )
    if decision == "ALLOW":
        return None
    if decision == "REQUIRE_APPROVAL":
        return _structured_error(
            {
                "message": f"policy requires approval: {reason or 'mutation requires approval'}",
                "approval_required": True,
                "action": action,
                "resource": resource,
                "principal": principal,
            },
            execution_id="not-created",
        )
    if decision == "DENY":
        return _error_result(
            f"policy denied: {reason or 'deny_actions'}",
            execution_id="not-created",
        )
    # decision == "error" or anything unknown: fail closed.
    return _error_result(
        "policy denied: evaluation failed or returned an unknown decision",
        execution_id="not-created",
    )


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
    expected_actions: list[str] | None = None,
    resource_scopes: list[str] | None = None,
    trust_labels: list[str] | None = None,
) -> dict[str, Any]:
    """Create a Hermes run and return execution/session identifiers immediately."""

    _validate_prompt(prompt)

    if trust_labels:
        policy_block = _enforce_policy(
            "hermes_submit",
            trust_label=trust_labels[0] if trust_labels else None,
        )
        if policy_block is not None:
            return policy_block

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
    expected_actions: list[str] | None = None,
    resource_scopes: list[str] | None = None,
    trust_labels: list[str] | None = None,
) -> dict[str, Any]:
    """Delegate an objective to Hermes, keep the MCP request connected, and wait."""

    _validate_prompt(prompt)

    if trust_labels:
        policy_block = _enforce_policy(
            "hermes_prompt",
            trust_label=trust_labels[0] if trust_labels else None,
        )
        if policy_block is not None:
            return policy_block

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

    upstream_payload = upstream if isinstance(upstream, dict) else {}
    observed_runs = upstream_payload.get("active_api_runs")
    if isinstance(observed_runs, int) and observed_runs >= 0:
        set_active_runs(observed_runs)

    registry_health = await asyncio.to_thread(registry.health)
    approval_registry = get_approval_registry()
    approval_registry_health = await asyncio.to_thread(approval_registry.health)
    bridge: dict[str, Any] = {
        "default_wait_seconds": settings.hermes_run_default_wait_seconds,
        "max_wait_seconds": settings.hermes_run_max_wait_seconds,
        "state_registry": registry_health,
        "approval_registry": approval_registry_health,
        "bridge_version": settings.bridge_version,
        "security_posture": _security_posture(approval_registry_health),
        "observability": observability_health(),
    }
    upstream_payload = upstream if isinstance(upstream, dict) else {}
    manifest = await _build_capability_manifest()
    bridge["manifest_version"] = manifest.manifest_version
    bridge["manifest_hash"] = manifest.manifest_hash
    bridge["bridge_version"] = manifest.bridge_version
    bridge["schema_version"] = manifest.schema_version
    bridge["effective_tools"] = manifest.effective_tools
    bridge["advisory_tools"] = manifest.advisory_tools
    bridge["unsupported_tools"] = manifest.unsupported_tools
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
    payload["upstream_support"] = {
        "requested": ["auto", "explicit"],
        "effective": ["auto", "explicit"],
        "unsupported": [],
    }
    return payload


@mcp.tool()
async def hermes_agent_card() -> dict[str, Any]:
    """Return the versioned agent card for this bridge."""

    card = load_agent_card_from_env()
    payload = card.to_canonical_dict()
    payload["card_hash"] = _card_hash(card)
    requested_modes, effective_upstream, _ = _requested_effective_upstream(
        ",".join(card.orchestration_modes)
    )
    payload["orchestration_contract_modes"] = requested_modes
    payload["upstream_effective_modes"] = effective_upstream
    return payload


@mcp.tool()
async def hermes_policy_evaluate(
    action: str,
    resource: str | None = None,
    trust_label: str | None = None,
    mutation_class: str | None = None,
    origin_type: str | None = None,
    project_key: str | None = None,
    principal: str | None = None,
    delegation_chain: list[str] | None = None,
) -> dict[str, Any]:
    """Evaluate a policy decision for the given action and trust/mutation context."""

    try:
        trust_enum = TrustLabel(trust_label) if trust_label else TrustLabel.UNKNOWN
    except ValueError:
        trust_enum = TrustLabel.UNKNOWN
    try:
        mutation_enum = (
            MutationClass(mutation_class)
            if mutation_class
            else _mutation_from_action(action)
        ) or MutationClass.NONE
    except ValueError:
        mutation_enum = _mutation_from_action(action) or MutationClass.NONE

    evaluation = PolicyEvaluationInput(
        action=action,
        origin_type=origin_type,
        project_key=project_key,
        resource=resource,
        trust_label=trust_enum,
        mutation_class=mutation_enum,
        principal=principal,
        delegation_chain=list(delegation_chain or []),
    )
    result = evaluate_policy(evaluation)
    return {
        "decision": result.decision.value,
        "reason": result.reason,
        "approval_required": result.approval_required,
        "effective_policy": result.effective_policy,
    }


@mcp.tool()
async def hermes_approval_create(
    action: str,
    resource: str | None = None,
    expires_in_seconds: int | None = None,
    metadata_sanitized: dict[str, Any] | None = None,
    principal: str | None = None,
    delegation_chain: list[str] | None = None,
    trust_label: str | None = None,
    mutation_class: str | None = None,
) -> dict[str, Any]:
    """Create a new approval request for a mutation/policy action."""

    approval_id = f"approval-{uuid.uuid4().hex}"
    resource_fingerprint = None
    if resource is not None:
        payload = {"resource": resource, "metadata": metadata_sanitized or {}}
        normalized = json.dumps(payload, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
        resource_fingerprint = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    expires_at = None
    if expires_in_seconds is not None and expires_in_seconds > 0:
        expires_at = (datetime.now(UTC).timestamp() + expires_in_seconds)
        expires_at = datetime.fromtimestamp(expires_at, tz=UTC).isoformat()
    record = ApprovalRecord(
        approval_id=approval_id,
        action=action,
        resource=resource,
        resource_fingerprint=resource_fingerprint,
        principal=principal,
        delegation_chain_sanitized=list(delegation_chain or []),
        decision=ApprovalStatus.REQUESTED,
        expires_at=expires_at,
        created_at=datetime.now(UTC).isoformat(),
        metadata_sanitized=metadata_sanitized or {},
    )
    created = get_approval_registry().create(record)
    return {
        "approval_id": created.approval_id,
        "action": created.action,
        "resource": created.resource,
        "decision": created.decision.value,
        "expires_at": created.expires_at,
        "created_at": created.created_at,
        "approval_identity_assurance": created.approval_identity_assurance,
    }


@mcp.tool()
async def hermes_approval_respond(
    approval_id: str,
    decision: str,
    principal: str | None = None,
) -> dict[str, Any]:
    """Respond to an approval request with approved/rejected."""

    try:
        status = ApprovalStatus(decision)
    except ValueError:
        return {"approval_id": approval_id, "error": f"invalid decision: {decision}"}
    try:
        updated = get_approval_registry().respond(approval_id, status, principal=principal)
    except ApprovalNotFound as exc:
        return {"approval_id": approval_id, "error": str(exc)}
    except ApprovalExpiredError as exc:
        return {"approval_id": approval_id, "error": str(exc)}
    except ApprovalStatusError as exc:
        return {"approval_id": approval_id, "error": str(exc)}
    record_approval(updated.decision.value)
    return {
        "approval_id": updated.approval_id,
        "action": updated.action,
        "decision": updated.decision.value,
        "decided_at": updated.decided_at,
        "approval_identity_assurance": updated.approval_identity_assurance,
    }


@mcp.tool()
async def hermes_approval_status(approval_id: str) -> dict[str, Any]:
    """Return the current status of an approval request."""

    try:
        record = get_approval_registry().get(approval_id)
    except ApprovalNotFound as exc:
        return {"approval_id": approval_id, "error": str(exc)}
    return {
        "approval_id": record.approval_id,
        "action": record.action,
        "resource": record.resource,
        "resource_fingerprint": record.resource_fingerprint,
        "decision": record.decision.value,
        "expires_at": record.expires_at,
        "created_at": record.created_at,
        "decided_at": record.decided_at,
        "consumed_at": record.consumed_at,
        "approval_identity_assurance": record.approval_identity_assurance,
    }


@mcp.tool()
async def hermes_result_manifest(execution_id: str) -> dict[str, Any]:
    """Return a persisted/sanitized result manifest for an execution if available."""

    manifest = build_result_manifest(
        execution_id=execution_id,
        session_id=None,
        status="unknown",
        claims=[
            ProvenanceClaim(
                subject=f"execution:{execution_id}",
                claim_type=ProvenanceClaimType.UNVERIFIED,
            )
        ],
    )
    return manifest.model_dump(mode="json")


@mcp.tool()
async def hermes_plan(
    title: str,
    description: str = "",
    steps: list[dict[str, Any]] | None = None,
    dependencies: list[dict[str, Any]] | None = None,
    approval_points: list[dict[str, Any]] | None = None,
    principal: str | None = None,
    trust_label: str | None = None,
) -> dict[str, Any]:
    """Create an executable plan/DAG without executing mutations."""

    plan_id = f"plan-{uuid.uuid4().hex}"
    plan_steps = [PlanStep(**step) for step in (steps or [])]
    deps = [PlanDependency(**dep) for dep in (dependencies or [])]
    approval_point_models = [PlanApprovalPoint(**ap) for ap in (approval_points or [])]
    plan = Plan(
        plan_id=plan_id,
        title=title,
        description=description,
        steps=plan_steps,
        dependencies=deps,
        approval_points=approval_point_models,
    )
    from .plans import validate_plan_structure
    errors = validate_plan_structure(plan)
    if errors:
        return {"plan_id": plan_id, "error": "; ".join(errors)}
    from .plans import PlanStore
    store = PlanStore(settings.bridge_state_db_path)
    store.initialize()
    plan, _plan_hash = store.create(plan)
    return {
        "plan_id": plan.plan_id,
        "plan_hash": plan.plan_hash,
        "status": plan.status.value,
        "step_count": len(plan.steps),
        "approval_points": [ap.model_dump(mode="json") for ap in plan.approval_points],
    }


@mcp.tool()
async def hermes_execute_approved_plan(
    plan_id: str,
    approval_id: str,
    principal: str | None = None,
    trust_label: str | None = None,
) -> dict[str, Any]:
    """Execute a previously approved plan, consuming the approval atomically.

    The approval is consumed inside the authorized execution path itself: there
    is no separate ``hermes_approval_consume`` tool and the approval can never
    be replayed.
    """

    # Policy gate: a DENY still blocks. A REQUIRE_APPROVAL is *satisfied* by
    # this call, because the approval supplied here is verified and consumed
    # atomically below -- that is exactly the authorized execution path.
    decision, reason = _policy_decision_from_inputs(
        "hermes_execute_approved_plan",
        trust_label=trust_label,
        principal=principal,
        resource=plan_id,
    )
    if decision not in {"ALLOW", "REQUIRE_APPROVAL"}:
        return _error_result(
            f"policy denied: {reason or 'evaluation failed'}",
            execution_id="not-created",
        )

    from .plans import (
        ApprovalAdapterError,
        PlanStore,
        plan_approval_from_record,
        validate_approval,
    )

    store = PlanStore(settings.bridge_state_db_path)
    store.initialize()
    plan = store.get(plan_id)
    if plan is None:
        return {"plan_id": plan_id, "error": "plan not found"}
    if plan.status not in {PlanStatus.APPROVED, PlanStatus.PENDING_APPROVAL}:
        return {"plan_id": plan_id, "status": plan.status.value, "error": "plan not approved"}

    approval_registry = get_approval_registry()
    try:
        record = approval_registry.get(approval_id)
    except ApprovalNotFound as exc:
        return {"approval_id": approval_id, "error": str(exc)}

    try:
        plan_approval = plan_approval_from_record(record)
    except ApprovalAdapterError as exc:
        payload = exc.as_error_payload()
        payload["plan_id"] = plan_id
        return payload

    errors = validate_approval(plan_approval, plan)
    if errors:
        return {"approval_id": approval_id, "plan_id": plan_id, "error": "; ".join(errors)}
    if record.decision != ApprovalStatus.APPROVED:
        return {
            "approval_id": approval_id,
            "plan_id": plan_id,
            "error": f"approval status is {record.decision.value}",
        }

    # Bind the consumption to the exact plan revision that was approved.
    fingerprint = record.resource_fingerprint
    try:
        consumed = await asyncio.to_thread(
            approval_registry.consume,
            approval_id,
            fingerprint,
            require_fingerprint=True,
            expected_action=record.action,
        )
    except (
        ApprovalExpiredError,
        ApprovalConsumedError,
        ApprovalStaleError,
        ApprovalStatusError,
    ) as exc:
        return {
            "approval_id": approval_id,
            "plan_id": plan_id,
            "error": f"approval not consumable: {exc}",
        }

    record_approval(consumed.decision.value)
    return {
        "plan_id": plan_id,
        "approval_id": approval_id,
        "approval_consumed_at": consumed.consumed_at,
        "status": "execution_delegated",
    }


@mcp.tool()
async def hermes_checkpoint_create(
    execution_id: str,
    phase: str = "unknown",
    plan_id: str | None = None,
    step_index: int = 0,
    state_ref: str | None = None,
    evidence_refs: list[str] | None = None,
    principal: str | None = None,
    trust_label: str | None = None,
) -> dict[str, Any]:
    """Create a bridge-side checkpoint without storing blobs."""

    policy_block = _enforce_policy(
        "hermes_checkpoint_create",
        trust_label=trust_label,
        principal=principal,
    )
    if policy_block is not None:
        return policy_block

    checkpoint = Checkpoint(
        checkpoint_id=f"ckpt-{uuid.uuid4().hex}",
        execution_id=execution_id,
        plan_id=plan_id,
        phase=phase,
        step_index=step_index,
        state_ref=state_ref,
        evidence_refs=evidence_refs or [],
    )
    checkpoint_registry.initialize()
    created = checkpoint_registry.create(checkpoint)
    trace_metadata = build_trace_metadata(
        None, upstream_supported=False
    )
    return {**created.model_dump(mode="json"), "tracing": trace_metadata}


@mcp.tool()
async def hermes_checkpoint_status(
    execution_id: str | None = None,
    checkpoint_id: str | None = None,
) -> dict[str, Any]:
    """Query checkpoint state for a run or plan."""

    checkpoint_registry.initialize()
    items = checkpoint_registry.status(execution_id=execution_id, checkpoint_id=checkpoint_id)
    return {"checkpoints": items}


@mcp.tool()
async def hermes_continue(
    execution_id: str | None = None,
    checkpoint_id: str | None = None,
    mode: str = "advisory_only",
    principal: str | None = None,
    trust_label: str | None = None,
) -> dict[str, Any]:
    """Resume a previous execution or checkpoint idempotently."""

    policy_block = _enforce_policy(
        "hermes_continue",
        trust_label=trust_label,
        principal=principal,
    )
    if policy_block is not None:
        return policy_block

    checkpoint_registry.initialize()
    continuation = Continuation(
        continuation_id=f"cont-{uuid.uuid4().hex}",
        execution_id=execution_id,
        checkpoint_id=checkpoint_id,
        mode=mode,
        resume_supported=True,
    )
    created = checkpoint_registry.create_continuation(continuation)
    return created.model_dump(mode="json")


@mcp.tool()
async def hermes_saga_start(
    execution_id: str,
    steps: list[dict[str, Any]] | None = None,
    principal: str | None = None,
    trust_label: str | None = None,
) -> dict[str, Any]:
    """Start a saga and register compensation contracts."""

    policy_block = _enforce_policy(
        "hermes_saga_start",
        trust_label=trust_label,
        principal=principal,
    )
    if policy_block is not None:
        return policy_block

    saga_steps = [SagaStep(**step) for step in (steps or [])]
    saga = Saga(
        saga_id=f"saga-{uuid.uuid4().hex}",
        execution_id=execution_id,
        steps=saga_steps,
    )
    saga_registry.initialize()
    created = saga_registry.create(saga)
    return {
        "saga_id": created.saga_id,
        "execution_id": created.execution_id,
        "status": created.status.value,
        "current_step": created.current_step,
    }


@mcp.tool()
async def hermes_saga_status(
    saga_id: str,
    principal: str | None = None,
    trust_label: str | None = None,
) -> dict[str, Any]:
    """Read saga state and compensation evidence."""

    policy_block = _enforce_policy(
        "hermes_saga_status",
        trust_label=trust_label,
        principal=principal,
    )
    if policy_block is not None:
        return policy_block

    saga_registry.initialize()
    saga = saga_registry.get(saga_id)
    if saga is None:
        return {"saga_id": saga_id, "error": "saga not found"}
    return {
        "saga_id": saga.saga_id,
        "execution_id": saga.execution_id,
        "status": saga.status.value,
        "current_step": saga.current_step,
        "steps": [step.model_dump(mode="json") for step in saga.steps],
        "upstream_confirmed": (
            all(step.upstream_confirmed for step in saga.steps) if saga.steps else False
        ),
    }


@mcp.tool()
async def hermes_saga_compensate(
    saga_id: str,
    step_id: str,
    evidence_ref: str | None = None,
    principal: str | None = None,
    trust_label: str | None = None,
) -> dict[str, Any]:
    """Record or inspect a compensation event for a saga."""

    policy_block = _enforce_policy(
        "hermes_saga_compensate",
        trust_label=trust_label,
        principal=principal,
    )
    if policy_block is not None:
        return policy_block

    saga_registry.initialize()
    saga = saga_registry.get(saga_id)
    if saga is None:
        return {"saga_id": saga_id, "error": "saga not found"}
    step = next((step for step in saga.steps if step.step_id == step_id), None)
    if step is None:
        return {"saga_id": saga_id, "step_id": step_id, "error": "step not found"}
    updated_status = SagaStatus.COMPENSATED
    if any(s.status != SagaStatus.COMPLETED.value for s in saga.steps):
        updated_status = SagaStatus.COMPENSATING
    updated = saga_registry.update_status(saga_id, updated_status, current_step=step_id)
    return {
        "saga_id": saga_id,
        "step_id": step_id,
        "upstream_confirmed": False,
        "status": updated.status.value if updated else saga.status.value,
    }


@mcp.tool()
async def hermes_lock_acquire(
    lock_key: str,
    lock_type: str,
    owner: str,
    ttl_seconds: int = 0,
    context: str | None = None,
    execution_id: str | None = None,
    principal: str | None = None,
    trust_label: str | None = None,
) -> dict[str, Any]:
    """Acquire a typed resource lock with TTL."""

    policy_block = _enforce_policy(
        "hermes_lock_acquire",
        trust_label=trust_label,
        principal=principal,
        resource=lock_key,
    )
    if policy_block is not None:
        return policy_block

    try:
        lock_type_enum = LockType(lock_type)
    except ValueError:
        return {"lock_key": lock_key, "error": f"invalid lock_type: {lock_type}"}
    lock_request = ResourceLock(
        lock_key=lock_key,
        lock_type=lock_type_enum,
        owner=owner,
        execution_id=execution_id,
        context=context,
        ttl_seconds=ttl_seconds,
    )
    lock_registry.initialize()
    try:
        lock = lock_registry.acquire(lock_request)
    except LockError as exc:
        return {"lock_key": lock_key, "error": str(exc)}
    return lock.model_dump(mode="json")


@mcp.tool()
async def hermes_lock_status(
    lock_key: str | None = None,
    principal: str | None = None,
    trust_label: str | None = None,
) -> dict[str, Any]:
    """Inspect active locks and expiry state."""

    policy_block = _enforce_policy(
        "hermes_lock_status",
        trust_label=trust_label,
        principal=principal,
    )
    if policy_block is not None:
        return policy_block

    lock_registry.initialize()
    items = lock_registry.list_status(lock_key=lock_key)
    return {"locks": items}


@mcp.tool()
async def hermes_lock_release(
    lock_key: str,
    owner: str,
    principal: str | None = None,
    trust_label: str | None = None,
) -> dict[str, Any]:
    """Release a held lock idempotently."""

    policy_block = _enforce_policy(
        "hermes_lock_release",
        trust_label=trust_label,
        principal=principal,
        resource=lock_key,
    )
    if policy_block is not None:
        return policy_block

    lock_registry.initialize()
    lock = lock_registry.release(lock_key, owner)
    if lock is None:
        return {"lock_key": lock_key, "owner": owner, "status": "released", "released_at": None}
    return lock.model_dump(mode="json")


@mcp.tool()
async def hermes_quota_status(
    principal: str | None = None,
    resource: str | None = None,
    mutation: bool = False,
) -> dict[str, Any]:
    """Return current quota and budget evaluation."""

    quota = get_quota_registry()
    quota.initialize()
    evaluation = quota.evaluate(principal=principal, resource=resource, mutation=mutation)
    return {
        "quota": evaluation,
        "status": quota.status(),
    }



def _requested_effective_upstream(
    requested_mode: str,
) -> tuple[list[str], list[str], dict[str, Any]]:
    policy = OrchestrationPolicy()
    requested_modes = sorted(set(requested_mode.split(","))) if requested_mode else []
    effective_upstream = ["auto", "explicit"]
    if set(requested_modes).issubset(set(effective_upstream)):
        effective_upstream = requested_modes
    return requested_modes, effective_upstream, policy.model_dump()


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
            name="hermes_readiness",
            description=(
                "Report readiness of upstream, state DB, approvals, metrics and config."
            ),
            version_added="0.8.0",
            stability="experimental",
            read_only=True,
        ),
        ToolManifest(
            name="hermes_recent_runs",
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
        ToolManifest(
            name="hermes_policy_evaluate",
            description="Evaluate policy decision for an action.",
            version_added="0.5.0",
            stability="experimental",
            read_only=True,
        ),
        ToolManifest(
            name="hermes_approval_create",
            description="Create an approval request.",
            version_added="0.5.0",
            stability="experimental",
            read_only=False,
        ),
        ToolManifest(
            name="hermes_approval_respond",
            description="Respond to an approval request.",
            version_added="0.5.0",
            stability="experimental",
            read_only=False,
        ),
        ToolManifest(
            name="hermes_approval_status",
            description="Return approval status.",
            version_added="0.5.0",
            stability="experimental",
            read_only=True,
        ),
        ToolManifest(
            name="hermes_result_manifest",
            description="Return sanitized result manifest for an execution.",
            version_added="0.5.0",
            stability="experimental",
            read_only=True,
        ),
        ToolManifest(
            name="hermes_plan",
            description="Create an executable plan/DAG without executing mutations.",
            version_added="0.6.0",
            stability="experimental",
            read_only=True,
            effective_mode="advisory",
            depends_on_upstream=True,
        ),
        ToolManifest(
            name="hermes_execute_approved_plan",
            description="Execute a previously approved plan with policy gating.",
            version_added="0.6.0",
            stability="experimental",
            read_only=False,
            effective_mode="advisory",
            depends_on_upstream=True,
        ),
        ToolManifest(
            name="hermes_checkpoint_create",
            description="Create a bridge-side checkpoint without storing blobs.",
            version_added="0.6.0",
            stability="experimental",
            read_only=False,
        ),
        ToolManifest(
            name="hermes_checkpoint_status",
            description="Query checkpoint state for a run or plan.",
            version_added="0.6.0",
            stability="experimental",
            read_only=True,
        ),
        ToolManifest(
            name="hermes_continue",
            description="Resume a previous execution or checkpoint idempotently.",
            version_added="0.6.0",
            stability="experimental",
            read_only=False,
            effective_mode="advisory",
            depends_on_upstream=True,
        ),
        ToolManifest(
            name="hermes_saga_start",
            description="Start a saga and register compensation contracts.",
            version_added="0.6.0",
            stability="experimental",
            read_only=False,
        ),
        ToolManifest(
            name="hermes_saga_status",
            description="Read saga state and compensation evidence.",
            version_added="0.6.0",
            stability="experimental",
            read_only=True,
        ),
        ToolManifest(
            name="hermes_saga_compensate",
            description="Record or inspect a compensation event for a saga.",
            version_added="0.6.0",
            stability="experimental",
            read_only=False,
            effective_mode="advisory",
            depends_on_upstream=True,
        ),
        ToolManifest(
            name="hermes_lock_acquire",
            description="Acquire a typed resource lock with TTL.",
            version_added="0.6.0",
            stability="experimental",
            read_only=False,
        ),
        ToolManifest(
            name="hermes_lock_status",
            description="Inspect active locks and expiry state.",
            version_added="0.6.0",
            stability="experimental",
            read_only=True,
        ),
        ToolManifest(
            name="hermes_lock_release",
            description="Release a held lock idempotently.",
            version_added="0.6.0",
            stability="experimental",
            read_only=False,
        ),
        ToolManifest(
            name="hermes_quota_status",
            description="Return current quota and budget evaluation.",
            version_added="0.6.0",
            stability="experimental",
            read_only=True,
        ),
    ]

    return CapabilityManifest.build(
        bridge_version=CURRENT_CONTRACT_VERSION,
        manifest_version=CURRENT_CONTRACT_VERSION,
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


def _security_posture(approval_health: dict[str, Any] | None = None) -> dict[str, Any]:
    """Non-sensitive security posture: policy, signing keys, approval registry.

    Never returns key material, secret file paths or policy contents. Only
    ``configured``/``required``/``source_type``/``key_id`` and a policy hash.
    """

    loaded = get_active_policy(refresh=True)
    posture = signing_posture()
    approvals_ready = str((approval_health or {}).get("status", "down")) == "up"

    policy_block = loaded.summary()
    signing_block = posture.summary()

    problems: list[str] = []
    if not loaded.valid:
        problems.append("policy")
    if not posture.ok:
        problems.append("hmac")
    if not approvals_ready:
        problems.append("approval_registry")

    return {
        "status": "ready" if not problems else "not_ready",
        "policy": policy_block,
        "hmac": signing_block,
        "approval_registry": {"status": "ready" if approvals_ready else "not_ready"},
        "failing": problems,
    }


def _mutation_from_action(action: str) -> MutationClass | None:
    """Classify an action using the single policy source of truth.

    Returns ``None`` when the active policy does not know the action; the
    policy engine then applies ``unknown_action_decision`` (DENY by default).
    """

    try:
        return classify_action(action)
    except PolicyError:
        # Fail closed: an unusable policy must not yield a permissive class.
        return MutationClass.ADMIN


def _card_hash(card: AgentCard) -> str:
    return _canonical_json_hash(card.to_canonical_dict())


@staticmethod
def _canonical_json_hash(payload: Any) -> str:
    from .protocol import _canonical_json_hash as _hash

    return _hash(payload)


@mcp.tool()
async def hermes_readiness() -> dict[str, Any]:
    """Report readiness of upstream, state DB, approvals, metrics and config.

    Cheap by design: no SQLite ``integrity_check``, no full table scans, and no
    sensitive values (no paths, keys, tokens or prompts) are ever returned.
    """

    components: dict[str, Any] = {}

    try:
        await client.health(detailed=False)
        components["upstream"] = {"status": "ready"}
    except HermesAPIError:
        components["upstream"] = {"status": "not_ready", "reason": "unreachable"}

    try:
        state_health = await asyncio.to_thread(registry.health)
        ready = str(state_health.get("status", "down")) == "up"
        components["state_db"] = {
            "status": "ready" if ready else "not_ready",
            "schema_version": state_health.get("schema_version"),
        }
    except Exception:
        components["state_db"] = {"status": "not_ready", "reason": "error"}

    approvals_health: dict[str, Any] = {}
    try:
        approvals_health = await asyncio.to_thread(get_approval_registry().health)
        ready = str(approvals_health.get("status", "down")) == "up"
        components["approval_registry"] = {"status": "ready" if ready else "not_ready"}
    except Exception:
        components["approval_registry"] = {"status": "not_ready", "reason": "error"}

    components["security_posture"] = _security_posture(approvals_health)

    observability = observability_health()
    components["metrics_registry"] = {
        "status": "ready"
        if observability["metrics_registry"].get("status") == "up"
        else "not_ready",
        "exporter_enabled": observability["metrics"].get("enabled"),
        "bind_scope": observability["metrics"].get("bind_scope"),
    }
    components["logging"] = {
        "status": "ready" if observability["logging"].get("logging_configured") else "not_ready",
        "mode": observability["logging"].get("logging_mode"),
    }
    components["tracing"] = {
        "status": "ready",
        "implementation": observability["tracing"].get("implementation"),
        "export_enabled": observability["tracing"].get("export_enabled"),
    }
    components["config"] = {
        "status": "ready",
        "bridge_version": settings.bridge_version,
        "api_key_configured": bool(settings.hermes_api_key.get_secret_value()),
    }

    contract = validate_tools(server_tool_names(), version=CURRENT_CONTRACT_VERSION)
    components["tool_contract"] = {
        "status": "ready" if contract["ok"] else "not_ready",
        "contract_version": CURRENT_CONTRACT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "count": contract["count"],
        "expected_count": contract["expected_count"],
        "missing": contract["missing"],
        "extra": contract["extra"],
    }

    overall = "ready"
    for name, component in components.items():
        if component.get("status") != "ready":
            overall = "degraded" if name in {"upstream", "tracing"} else "not_ready"
            if overall == "not_ready":
                break
    return {
        "status": overall,
        "version_added": "0.8.0",
        "bridge_version": settings.bridge_version,
        "contract_version": CURRENT_CONTRACT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "components": components,
    }


def _load_server_module() -> ModuleType:
    return importlib.import_module("hermes_mcp_bridge.server")


def server_tool_names() -> list[str]:
    server = _load_server_module()
    tools = server.mcp._tool_manager.list_tools()
    return sorted(tool.name for tool in tools)


INSTRUMENTED_TOOL_COUNT = instrument_all_tools(mcp)


def main() -> None:
    """Run the bridge using MCP Streamable HTTP transport."""

    configure_logging()
    start_exporter_if_enabled()
    log_event(
        "bridge.startup",
        outcome="success",
        bridge_version=settings.bridge_version,
        instrumented_tools=INSTRUMENTED_TOOL_COUNT,
    )
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
