from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from .protocol import ExecutionEnvelope, ProvenanceClaimType


class OrchestrationMode(StrEnum):
    AUTO = "auto"
    EXPLICIT = "explicit"


class RunStatus(StrEnum):
    QUEUED = "queued"
    STARTED = "started"
    RUNNING = "running"
    STOPPING = "stopping"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


TERMINAL_STATUSES = {
    RunStatus.COMPLETED,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
}


class HermesPromptResult(BaseModel):
    "Normalized result returned to the MCP client."

    session_id: str | None = None
    execution_id: str
    status: RunStatus
    output: str | None = None
    error: str | None = None
    agent: str | None = None
    subagents: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    envelope: ExecutionEnvelope | None = None
    policy_decision: str | None = None
    approval_id: str | None = None
    result_manifest: dict[str, Any] | None = None
    policy: dict[str, Any] | None = None


class PlanStatus(StrEnum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    STALE = "stale"


class PlanStepStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"


class PlanRiskSeverity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class LockType(StrEnum):
    READ_SHARED = "READ_SHARED"
    INTENT_TO_WRITE = "INTENT_TO_WRITE"
    WRITE_EXCLUSIVE = "WRITE_EXCLUSIVE"
    APPROVAL_PENDING = "APPROVAL_PENDING"


class LockStatus(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    RELEASED = "released"


class SagaStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    COMPENSATING = "compensating"
    COMPENSATED = "compensated"
    FAILED = "failed"


class QuotaDecision(StrEnum):
    ALLOW = "ALLOW"
    THROTTLE = "THROTTLE"
    QUEUE = "QUEUE"
    REJECT = "REJECT"


class TraceContext(BaseModel):
    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    correlation_id: str | None = None
    causation_id: str | None = None
    traceparent: str | None = None
    baggage: dict[str, str] = Field(default_factory=dict)
    provenance: str = Field(default=ProvenanceClaimType.UNVERIFIED)

    model_config = {"extra": "ignore"}


class PlanDependency(BaseModel):
    step_id: str
    depends_on: list[str] = Field(default_factory=list)
    type: str = "hard"


class PlanRisk(BaseModel):
    id: str
    severity: PlanRiskSeverity = PlanRiskSeverity.MEDIUM
    description: str = ""
    mitigation: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    provenance: str = ProvenanceClaimType.INFERRED


class PlanApprovalPoint(BaseModel):
    step_index: int
    label: str = ""
    required: bool = True
    expires_in_seconds: int = 0
    stale_on_plan_change: bool = True


class PlanStep(BaseModel):
    step_id: str
    title: str
    description: str = ""
    tool: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    parallel_group: str | None = None
    status: PlanStepStatus = PlanStepStatus.PENDING
    depends_on: list[str] = Field(default_factory=list)
    budget: dict[str, Any] = Field(default_factory=dict)
    risk_ids: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Plan(BaseModel):
    plan_id: str
    title: str
    description: str = ""
    version: str = "1"
    status: PlanStatus = PlanStatus.DRAFT
    steps: list[PlanStep] = Field(default_factory=list)
    dependencies: list[PlanDependency] = Field(default_factory=list)
    risks: list[PlanRisk] = Field(default_factory=list)
    approval_points: list[PlanApprovalPoint] = Field(default_factory=list)
    parallel_groups: list[str] = Field(default_factory=list)
    critical_path: list[str] = Field(default_factory=list)
    locks: list[str] = Field(default_factory=list)
    budgets: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None
    updated_at: str | None = None
    plan_hash: str | None = None
    policy: dict[str, Any] = Field(default_factory=dict)
    trace: TraceContext = Field(default_factory=TraceContext)


class PlanApproval(BaseModel):
    approval_id: str
    plan_id: str
    plan_hash: str
    status: str = "pending"
    approver: str | None = None
    expires_at: str | None = None
    consumed_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Checkpoint(BaseModel):
    checkpoint_id: str
    execution_id: str | None = None
    plan_id: str | None = None
    phase: str = "unknown"
    step_index: int = 0
    state_ref: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    trace: TraceContext = Field(default_factory=TraceContext)
    created_at: str | None = None


class Continuation(BaseModel):
    continuation_id: str
    execution_id: str | None = None
    checkpoint_id: str | None = None
    continuation_of: str | None = None
    mode: str = "advisory_only"
    resume_supported: bool = False
    trace: TraceContext = Field(default_factory=TraceContext)
    created_at: str | None = None


class SagaStep(BaseModel):
    step_id: str
    action: str = ""
    compensation: str = ""
    status: str = "pending"
    evidence_ref: str | None = None
    upstream_confirmed: bool = False


class Saga(BaseModel):
    saga_id: str
    execution_id: str | None = None
    current_step: str | None = None
    status: SagaStatus = SagaStatus.RUNNING
    steps: list[SagaStep] = Field(default_factory=list)
    state: dict[str, Any] = Field(default_factory=dict)
    trace: TraceContext = Field(default_factory=TraceContext)
    created_at: str | None = None
    updated_at: str | None = None


class ResourceLock(BaseModel):
    lock_key: str
    lock_type: LockType = LockType.WRITE_EXCLUSIVE
    owner: str
    execution_id: str | None = None
    context: str | None = None
    ttl_seconds: int = 0
    acquired_at: str | None = None
    renewed_at: str | None = None
    expires_at: str | None = None
    status: LockStatus = LockStatus.ACTIVE
    metadata: dict[str, Any] = Field(default_factory=dict)


class QuotaProfile(BaseModel):
    profile_id: str
    max_parallel_runs: int = 1
    max_parallel_mutations_per_resource: int = 1
    max_runtime_seconds: int = 7200
    max_tool_calls: int = 256
    max_tokens: int = 200000
    priority: int = 0
