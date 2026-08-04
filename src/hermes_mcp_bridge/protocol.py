"""Protocol foundations: execution envelope, event types, manifests, agent card."""

from __future__ import annotations

import hashlib
import json
import os
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class SchemaVersion(StrEnum):
    V0_4_0 = "0.4.0"
    V0_5_0 = "0.5.0"
    V0_6_0 = "0.6.0"
    V0_6_1 = "0.6.1"


class ExecutionEnvelope(BaseModel):
    """Versioned execution envelope attached to tool outputs."""

    schema_version: str = Field(default=SchemaVersion.V0_6_1)
    payload_version: str | None = Field(default=None)
    origin_type: str | None = Field(default=None)
    context_key: str | None = Field(default=None)
    project_key: str | None = Field(default=None)
    correlation_id: str | None = Field(default=None)
    causation_id: str | None = Field(default=None)
    principal: str | None = Field(default=None)
    delegation_chain: list[str] = Field(default_factory=list)

    model_config = {"extra": "ignore"}

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "payload_version": self.payload_version,
            "origin_type": self.origin_type,
            "context_key": self.context_key,
            "project_key": self.project_key,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "principal": self.principal,
            "delegation_chain": list(self.delegation_chain),
        }


class MessageType(StrEnum):
    PROGRESS = "progress"
    APPROVAL = "approval"
    TOOL = "tool"
    LIFECYCLE = "lifecycle"
    UNKNOWN = "unknown"


class EventType(StrEnum):
    MESSAGE_DELTA = "message.delta"
    REASONING_AVAILABLE = "reasoning.available"
    BRIDGE_RUN_ACCEPTED = "bridge.run.accepted"
    BRIDGE_HEARTBEAT = "bridge.heartbeat"
    BRIDGE_EVENT_STREAM_FALLBACK = "bridge.event_stream_fallback"
    BRIDGE_WAIT_EXPIRED = "bridge.wait_expired"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    SUBAGENT_START = "subagent.start"
    SUBAGENT_COMPLETE = "subagent.complete"
    APPROVAL_REQUEST = "approval.request"
    APPROVAL_RESPONDED = "approval.responded"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"


class ProgressEvent(BaseModel):
    message_type: MessageType = MessageType.PROGRESS
    event_type: EventType
    run_id: str | None = None
    session_id: str | None = None
    status: str | None = None
    elapsed_seconds: float | None = None
    message: str | None = None
    tool: str | None = None
    subagent_id: str | None = None
    remaining_seconds: float | None = None
    error: str | None = None


class ApprovalEvent(BaseModel):
    message_type: MessageType = MessageType.APPROVAL
    event_type: EventType = EventType.APPROVAL_REQUEST
    run_id: str | None = None
    approval_id: str | None = None
    message: str | None = None


class ToolEvent(BaseModel):
    message_type: MessageType = MessageType.TOOL
    event_type: EventType
    run_id: str | None = None
    tool: str
    error: str | None = None
    output_summary: str | None = None


class LifecycleEvent(BaseModel):
    message_type: MessageType = MessageType.LIFECYCLE
    event_type: EventType
    run_id: str | None = None
    session_id: str | None = None
    status: str | None = None
    elapsed_seconds: float | None = None


class UnknownEvent(BaseModel):
    message_type: MessageType = MessageType.UNKNOWN
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class ToolManifest(BaseModel):
    """Manifest entry for a single MCP tool."""

    name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    version_added: str | None = None
    stability: str | None = None
    read_only: bool = False
    effective_mode: str | None = None
    depends_on_upstream: bool = False


class CapabilityManifest(BaseModel):
    """Canonical capability manifest for the bridge."""

    schema_version: str = Field(default=SchemaVersion.V0_6_1)
    bridge_version: str
    manifest_version: str
    manifest_hash: str
    tools: list[ToolManifest] = Field(default_factory=list)
    orchestration_modes: list[str] = Field(default_factory=list)
    limits: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    upstream_capabilities_source: str | None = None
    upstream_capabilities_status: str | None = None
    upstream_capabilities_hash: str | None = None
    effective_tools: list[str] = Field(default_factory=list)
    advisory_tools: list[str] = Field(default_factory=list)
    unsupported_tools: list[str] = Field(default_factory=list)

    model_config = {"extra": "ignore"}

    @classmethod
    def build(
        cls,
        *,
        bridge_version: str,
        manifest_version: str,
        tools: list[ToolManifest],
        orchestration_modes: list[str],
        limits: dict[str, Any],
        provenance: dict[str, Any],
        upstream_capabilities: dict[str, Any] | None,
    ) -> CapabilityManifest:
        schema_version = SchemaVersion.V0_6_1
        canonical = {
            "schema_version": schema_version,
            "bridge_version": bridge_version,
            "manifest_version": manifest_version,
            "tools": [tool.model_dump() for tool in tools],
            "orchestration_modes": orchestration_modes,
            "limits": limits,
            "provenance": provenance,
        }
        manifest_hash = _canonical_json_hash(canonical)
        effective_tools = sorted(
            tool.name for tool in tools if tool.effective_mode != "unsupported"
        )
        advisory_tools = sorted(
            tool.name for tool in tools if tool.effective_mode == "advisory"
        )
        unsupported_tools = sorted(
            tool.name for tool in tools if tool.effective_mode == "unsupported"
        )
        upstream_capabilities_source = "fallback"
        upstream_capabilities_status = "unavailable"
        upstream_capabilities_hash = None
        if upstream_capabilities is not None:
            upstream_capabilities_source = "upstream"
            upstream_capabilities_status = str(
                upstream_capabilities.get("status", "unknown")
            )
            upstream_hash_payload = upstream_capabilities.get("canonical")
            if upstream_hash_payload is None:
                upstream_hash_payload = upstream_capabilities
            try:
                upstream_capabilities_hash = _canonical_json_hash(upstream_hash_payload)
            except (TypeError, ValueError):
                upstream_capabilities_hash = None
        return cls(
            schema_version=schema_version,
            bridge_version=bridge_version,
            manifest_version=manifest_version,
            manifest_hash=manifest_hash,
            tools=tools,
            orchestration_modes=orchestration_modes,
            limits=limits,
            provenance=provenance,
            upstream_capabilities_source=upstream_capabilities_source,
            upstream_capabilities_status=upstream_capabilities_status,
            upstream_capabilities_hash=upstream_capabilities_hash,
            effective_tools=effective_tools,
            advisory_tools=advisory_tools,
            unsupported_tools=unsupported_tools,
        )


class AgentCard(BaseModel):
    """Versioned agent card for the bridge identity."""

    schema_version: str = Field(default=SchemaVersion.V0_6_1)
    agent_id: str
    name: str
    purpose: str
    version: str
    capabilities: list[str] = Field(default_factory=list)
    tools: list[ToolManifest] = Field(default_factory=list)
    orchestration_modes: list[str] = Field(default_factory=list)
    limits: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "ignore"}

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "agent_id": self.agent_id,
            "name": self.name,
            "purpose": self.purpose,
            "version": self.version,
            "capabilities": list(self.capabilities),
            "tools": [tool.model_dump() for tool in self.tools],
            "orchestration_modes": list(self.orchestration_modes),
            "limits": dict(self.limits),
            "provenance": dict(self.provenance),
        }


# === 0.5.0 governance additions ===


class OrchestrationMode(StrEnum):
    AUTO = "auto"
    EXPLICIT = "explicit"
    SINGLE = "single"
    PARALLEL = "parallel"
    PIPELINE = "pipeline"
    REVIEW = "review"


class TrustLabel(StrEnum):
    TRUSTED_POLICY = "trusted_policy"
    USER_INSTRUCTION = "user_instruction"
    AGENT_PROPOSAL = "agent_proposal"
    TOOL_RESULT = "tool_result"
    UNTRUSTED_CONTENT = "untrusted_content"
    UNKNOWN = "unknown"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    UNKNOWN = "unknown"


class ProvenanceClaimType(StrEnum):
    OBSERVED = "OBSERVED"
    DERIVED = "DERIVED"
    INFERRED = "INFERRED"
    UNVERIFIED = "UNVERIFIED"


class DecisionType(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"


class ApprovalStatus(StrEnum):
    REQUESTED = "requested"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CONSUMED = "consumed"
    STALE = "stale"


class MutationClass(StrEnum):
    NONE = "none"
    WRITE = "write"
    DELETE = "delete"
    ADMIN = "admin"


class OrchestrationPolicy(BaseModel):
    prefer_parallelism: bool | None = None
    min_agents: int | None = Field(default=None, ge=1)
    max_agents: int | None = Field(default=None, ge=1)
    max_parallel_agents: int | None = Field(default=None, ge=1)
    require_supervisor: bool | None = None
    single_writer_per_resource: bool | None = None
    require_independent_review: bool | None = None
    max_round_trips: int | None = Field(default=None, ge=1)
    max_replans: int | None = Field(default=None, ge=0)
    max_delegation_depth: int | None = Field(default=None, ge=1)
    stop_redundant_agents: bool | None = None
    cancel_losing_hypotheses: bool | None = None

    model_config = {"extra": "ignore"}

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "prefer_parallelism": self.prefer_parallelism,
            "min_agents": self.min_agents,
            "max_agents": self.max_agents,
            "max_parallel_agents": self.max_parallel_agents,
            "require_supervisor": self.require_supervisor,
            "single_writer_per_resource": self.single_writer_per_resource,
            "require_independent_review": self.require_independent_review,
            "max_round_trips": self.max_round_trips,
            "max_replans": self.max_replans,
            "max_delegation_depth": self.max_delegation_depth,
            "stop_redundant_agents": self.stop_redundant_agents,
            "cancel_losing_hypotheses": self.cancel_losing_hypotheses,
        }


class PolicyEvaluationInput(BaseModel):
    action: str
    origin_type: str | None = None
    project_key: str | None = None
    resource: str | None = None
    trust_label: TrustLabel = TrustLabel.UNKNOWN
    mutation_class: MutationClass = MutationClass.NONE
    principal: str | None = None
    delegation_chain: list[str] = Field(default_factory=list)


class PolicyEvaluationResult(BaseModel):
    decision: DecisionType
    reason: str | None = None
    effective_policy: dict[str, Any] = Field(default_factory=dict)
    approval_required: bool = False


class ApprovalRecord(BaseModel):
    approval_id: str
    action: str
    resource: str | None = None
    resource_fingerprint: str | None = None
    principal: str | None = None
    delegation_chain_sanitized: list[str] = Field(default_factory=list)
    decision: ApprovalStatus = ApprovalStatus.REQUESTED
    expires_at: str | None = None
    created_at: str
    decided_at: str | None = None
    consumed_at: str | None = None
    metadata_sanitized: dict[str, Any] = Field(default_factory=dict)
    approval_identity_assurance: str = "caller_asserted"

    model_config = {"extra": "ignore"}


class EvidenceRef(BaseModel):
    source: str
    digest: str | None = None
    observed_at: str | None = None


class ProvenanceClaim(BaseModel):
    claim_type: ProvenanceClaimType = ProvenanceClaimType.UNVERIFIED
    subject: str
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    asserted_by: str | None = None
    notes: str | None = None


class ResultManifest(BaseModel):
    execution_id: str
    session_id: str | None = None
    status: str
    schema_versions: dict[str, str] = Field(default_factory=dict)
    timestamps: dict[str, str] = Field(default_factory=dict)
    tool_manifest_hashes: list[str] = Field(default_factory=list)
    claims: list[ProvenanceClaim] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)
    canonical_digest: str | None = None
    signature_status: str = "unsigned"
    signature: str | None = None

    model_config = {"extra": "ignore"}


class ExtendedToolManifest(ToolManifest):
    trust_level: str | None = None
    mutation_class: str | None = None
    reversible: bool | None = None
    idempotency_class: str | None = None
    approval_requirement: str | None = None
    attestation_status: str | None = None


def _canonical_json_hash(payload: Any) -> str:
    if isinstance(payload, dict):
        if "__canonical__" in payload:
            return str(payload["__canonical__"])
        canonical = _normalize(payload)
        encoded = json.dumps(canonical, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    raise TypeError("unsupported payload for canonical hash")


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _normalize(value[key]) for key in sorted(value.keys())}
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    return value


def canonical_event(event: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize(event)
    return normalized


def parse_event(
    event: dict[str, Any],
) -> ProgressEvent | ApprovalEvent | ToolEvent | LifecycleEvent | UnknownEvent:
    raw_type = str(event.get("event") or "").strip()
    try:
        event_type = EventType(raw_type)
    except ValueError:
        return UnknownEvent(
            event_type=raw_type or "unknown",
            payload=_normalize(event),
        )

    if event_type in {
        EventType.TOOL_STARTED,
        EventType.TOOL_COMPLETED,
    }:
        return ToolEvent(
            event_type=event_type,
            run_id=event.get("run_id"),
            tool=str(event.get("tool") or "unknown"),
            error=event.get("error"),
            output_summary=event.get("output_summary"),
        )
    if event_type in {
        EventType.APPROVAL_REQUEST,
        EventType.APPROVAL_RESPONDED,
    }:
        return ApprovalEvent(event_type=event_type, run_id=event.get("run_id"))
    if event_type in {
        EventType.RUN_COMPLETED,
        EventType.RUN_FAILED,
        EventType.RUN_CANCELLED,
        EventType.BRIDGE_RUN_ACCEPTED,
        EventType.BRIDGE_EVENT_STREAM_FALLBACK,
        EventType.BRIDGE_WAIT_EXPIRED,
    }:
        return LifecycleEvent(
            event_type=event_type,
            run_id=event.get("run_id"),
            session_id=event.get("session_id"),
            status=str(event.get("status") or "").lower() or None,
            elapsed_seconds=event.get("elapsed_seconds"),
        )
    return ProgressEvent(
        event_type=event_type,
        run_id=event.get("run_id"),
        status=str(event.get("status") or "").lower() or None,
        elapsed_seconds=event.get("elapsed_seconds"),
        message=event.get("message"),
        tool=str(event.get("tool") or "").lower() or None,
        subagent_id=str(event.get("subagent_id") or "").lower() or None,
        remaining_seconds=event.get("remaining_seconds"),
        error=event.get("error"),
    )


def load_agent_card_from_env() -> AgentCard:
    provenance = {
        "source": "environment",
        "bridge_package": os.getenv("HERMES_BRIDGE_PACKAGE", "hermes-mcp-bridge"),
    }
    return AgentCard(
        agent_id=os.getenv("HERMES_AGENT_CARD_ID", "hermes-mcp-bridge"),
        name=os.getenv("HERMES_AGENT_CARD_NAME", "Hermes MCP Bridge"),
        purpose=os.getenv(
            "HERMES_AGENT_CARD_PURPOSE",
            "Thin MCP bridge that delegates natural-language objectives to hermes-agent.",
        ),
        version=os.getenv("HERMES_AGENT_CARD_VERSION", SchemaVersion.V0_6_1),
        capabilities=[
            "delegate-objectives",
            "connected-wait",
            "progress-notifications",
            "session-reuse",
            "registry-recovery",
            "plan-governance",
            "checkpoint-continuation",
            "saga-compensation",
            "resource-locking",
            "quota-backpressure",
            "trace-context",
        ],
        orchestration_modes=["auto", "explicit"],
        limits={
            "max_wait_seconds": 7200,
            "default_wait_seconds": 45,
            "max_prompt_chars": 200000,
            "max_subagents": 16,
        },
        provenance=provenance,
    )


def load_upstream_capabilities(
    base_url: str, api_key: str, timeout: float = 5.0
) -> dict[str, Any] | None:
    import urllib.request

    url = base_url.rstrip("/") + "/v1/capabilities"
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload
    except (OSError, ValueError):
        return None


def canonical_capability_fallback(bridge_version: str) -> CapabilityManifest:
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
    ]
    orchestration_modes = ["auto", "explicit"]
    limits = {
        "max_wait_seconds": 7200,
        "default_wait_seconds": 45,
        "max_prompt_chars": 200000,
        "max_subagents": 16,
    }
    provenance = {
        "source": "fallback",
        "bridge_package": "hermes-mcp-bridge",
        "note": (
            "Upstream capabilities were unavailable; "
            "manifest reflects bridge-known tools only."
        ),
    }
    return CapabilityManifest.build(
        bridge_version=bridge_version,
        manifest_version="0.6.1",
        tools=tools,
        orchestration_modes=orchestration_modes,
        limits=limits,
        provenance=provenance,
        upstream_capabilities=None,
    )


_MANIFEST_TOOL_NAMES = {
    "hermes_submit",
    "hermes_prompt",
    "hermes_wait",
    "hermes_status",
    "hermes_stop",
    "hermes_health",
    "hermes_recent_runs",
    "hermes_capabilities",
    "hermes_agent_card",
    "hermes_policy_evaluate",
    "hermes_approval_create",
    "hermes_approval_respond",
    "hermes_approval_status",
    "hermes_result_manifest",
    "hermes_plan",
    "hermes_execute_approved_plan",
    "hermes_checkpoint_create",
    "hermes_checkpoint_status",
    "hermes_continue",
    "hermes_saga_start",
    "hermes_saga_status",
    "hermes_saga_compensate",
    "hermes_lock_acquire",
    "hermes_lock_status",
    "hermes_lock_release",
    "hermes_quota_status",
}


def validate_manifest_tools(manifest: CapabilityManifest) -> list[str]:
    mismatched = [
        tool.name for tool in manifest.tools if tool.name not in _MANIFEST_TOOL_NAMES
    ]
    return sorted(mismatched)
