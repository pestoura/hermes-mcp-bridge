"""Bridge request and response models."""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from .protocol import ExecutionEnvelope


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
    """Normalized result returned to the MCP client."""

    session_id: str | None = None
    execution_id: str
    status: RunStatus
    output: str | None = None
    error: str | None = None
    agent: str | None = None
    subagents: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    envelope: ExecutionEnvelope | None = None
