"""Fail-closed approval handoff for policy-gated prompt/submit requests.

This module binds a policy approval to the exact logical Hermes request without
persisting prompt content.  It is intentionally internal: the public 1.0.0 tool
catalog and input schemas remain unchanged.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .approvals import (
    ApprovalConsumedError,
    ApprovalExpiredError,
    ApprovalNotFound,
    ApprovalStaleError,
    ApprovalStatusError,
    get_approval_registry,
)
from .protocol import ApprovalRecord, ApprovalStatus
from .registry import RegistryError, compute_fingerprint, get_registry

_PROMPT_APPROVAL_TTL_SECONDS = 900
_APPROVAL_PREFIX = "approval-prompt-"
_RESOURCE_PREFIX = "request-sha256:"


@dataclass(frozen=True)
class PromptApprovalOutcome:
    """Result of resolving a REQUIRE_APPROVAL decision for a Hermes request."""

    allowed: bool
    approval_required: bool
    approval_id: str
    decision: str
    resource: str
    reason: str | None = None
    idempotent_existing: bool = False


def _canonical_hash(payload: dict[str, Any]) -> str:
    normalized = json.dumps(
        payload,
        separators=(",", ":"),
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _resource_fingerprint(resource: str, metadata: dict[str, Any]) -> str:
    return _canonical_hash({"resource": resource, "metadata": metadata})


def _request_binding(
    *,
    action: str,
    prompt: str,
    client_request_id: str | None,
    session_id: str | None,
    agent: str | None,
    subagents: list[str] | None,
    orchestration: str,
    expected_actions: list[str] | None,
    resource_scopes: list[str] | None,
    trust_labels: list[str] | None,
) -> tuple[str, str, dict[str, Any], str]:
    """Return opaque resource, approval fingerprint, metadata and run fingerprint.

    The raw prompt is never included.  Any semantically relevant request field
    changes the digest and therefore cannot reuse a prior approval.
    """

    prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    request_payload = {
        "action": action,
        "prompt_sha256": prompt_sha256,
        "client_request_id": client_request_id,
        "session_id": session_id,
        "agent": agent,
        "subagents": list(subagents or []),
        "orchestration": orchestration,
        "expected_actions": list(expected_actions or []),
        "resource_scopes": list(resource_scopes or []),
        "trust_labels": list(trust_labels or []),
    }
    request_digest = _canonical_hash(request_payload)
    resource = f"{_RESOURCE_PREFIX}{request_digest}"
    metadata = {
        "kind": "prompt-policy-handoff",
        "request_digest": request_digest,
        "client_request_id_present": client_request_id is not None,
    }
    resource_fingerprint = _resource_fingerprint(resource, metadata)
    run_fingerprint = compute_fingerprint(
        prompt=prompt,
        session_id=session_id,
        agent=agent,
        subagents=subagents,
        orchestration=orchestration,
    )
    return resource, resource_fingerprint, metadata, run_fingerprint


def _approval_id_for_resource(resource: str) -> str:
    digest = resource.removeprefix(_RESOURCE_PREFIX)
    return f"{_APPROVAL_PREFIX}{digest[:48]}"


def _is_expired(record: ApprovalRecord) -> bool:
    if record.expires_at is None:
        return False
    try:
        expiry = datetime.fromisoformat(record.expires_at)
    except ValueError:
        return True
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    return expiry <= datetime.now(UTC)


def _existing_execution_matches(
    *, client_request_id: str | None, run_fingerprint: str
) -> bool:
    if client_request_id is None:
        return False
    try:
        mapping = get_registry().get(client_request_id)
    except RegistryError:
        return False
    if mapping is None:
        return False
    return str(mapping.get("fingerprint", "")) == run_fingerprint


def resolve_required_prompt_approval(
    *,
    action: str,
    prompt: str,
    client_request_id: str | None,
    session_id: str | None,
    agent: str | None,
    subagents: list[str] | None,
    orchestration: str,
    expected_actions: list[str] | None,
    resource_scopes: list[str] | None,
    trust_labels: list[str] | None,
    principal: str | None = None,
) -> PromptApprovalOutcome:
    """Issue/reuse/consume the approval bound to one exact prompt request.

    State machine:
    - no record -> create REQUESTED and block;
    - REQUESTED -> return the same approval and block;
    - APPROVED -> atomically consume, then allow this exact request once;
    - CONSUMED -> allow only an already-persisted idempotent execution mapping;
    - REJECTED/EXPIRED/STALE or registry errors -> fail closed.

    A caller that needs a fresh attempt after terminal approval state must use a
    fresh ``client_request_id``.  This makes a new logical request digest rather
    than silently replaying an old authorization.
    """

    resource, fingerprint, metadata, run_fingerprint = _request_binding(
        action=action,
        prompt=prompt,
        client_request_id=client_request_id,
        session_id=session_id,
        agent=agent,
        subagents=subagents,
        orchestration=orchestration,
        expected_actions=expected_actions,
        resource_scopes=resource_scopes,
        trust_labels=trust_labels,
    )
    approval_id = _approval_id_for_resource(resource)
    approval_registry = get_approval_registry()
    approval_registry.initialize()

    try:
        record = approval_registry.get(approval_id)
    except ApprovalNotFound:
        expires_at = (
            datetime.now(UTC) + timedelta(seconds=_PROMPT_APPROVAL_TTL_SECONDS)
        ).isoformat()
        record = ApprovalRecord(
            approval_id=approval_id,
            action=action,
            resource=resource,
            resource_fingerprint=fingerprint,
            principal=principal,
            decision=ApprovalStatus.REQUESTED,
            expires_at=expires_at,
            created_at=datetime.now(UTC).isoformat(),
            metadata_sanitized=metadata,
        )
        try:
            record = approval_registry.create(record)
        except Exception:
            return PromptApprovalOutcome(
                allowed=False,
                approval_required=True,
                approval_id=approval_id,
                decision="error",
                resource=resource,
                reason="approval registry failed closed",
            )

    if record.action != action or record.resource != resource:
        return PromptApprovalOutcome(
            allowed=False,
            approval_required=True,
            approval_id=approval_id,
            decision="stale",
            resource=resource,
            reason="approval binding mismatch",
        )
    if record.resource_fingerprint != fingerprint:
        return PromptApprovalOutcome(
            allowed=False,
            approval_required=True,
            approval_id=approval_id,
            decision="stale",
            resource=resource,
            reason="approval fingerprint mismatch",
        )

    if _is_expired(record):
        try:
            if record.decision == ApprovalStatus.REQUESTED:
                record = approval_registry.expire(approval_id)
        except Exception:
            pass
        return PromptApprovalOutcome(
            allowed=False,
            approval_required=True,
            approval_id=approval_id,
            decision="expired",
            resource=resource,
            reason="approval expired; use a fresh client_request_id",
        )

    if record.decision == ApprovalStatus.REQUESTED:
        return PromptApprovalOutcome(
            allowed=False,
            approval_required=True,
            approval_id=approval_id,
            decision=record.decision.value,
            resource=resource,
            reason="approval pending",
        )

    if record.decision == ApprovalStatus.APPROVED:
        try:
            approval_registry.consume(
                approval_id,
                fingerprint,
                require_fingerprint=True,
                expected_action=action,
            )
        except (
            ApprovalConsumedError,
            ApprovalExpiredError,
            ApprovalStaleError,
            ApprovalStatusError,
        ) as exc:
            return PromptApprovalOutcome(
                allowed=False,
                approval_required=True,
                approval_id=approval_id,
                decision="blocked",
                resource=resource,
                reason=str(exc),
            )
        return PromptApprovalOutcome(
            allowed=True,
            approval_required=False,
            approval_id=approval_id,
            decision=ApprovalStatus.CONSUMED.value,
            resource=resource,
        )

    if record.decision == ApprovalStatus.CONSUMED:
        if _existing_execution_matches(
            client_request_id=client_request_id,
            run_fingerprint=run_fingerprint,
        ):
            return PromptApprovalOutcome(
                allowed=True,
                approval_required=False,
                approval_id=approval_id,
                decision=record.decision.value,
                resource=resource,
                idempotent_existing=True,
            )
        return PromptApprovalOutcome(
            allowed=False,
            approval_required=True,
            approval_id=approval_id,
            decision=record.decision.value,
            resource=resource,
            reason=(
                "approval already consumed without a matching persisted execution; "
                "use a fresh client_request_id"
            ),
        )

    return PromptApprovalOutcome(
        allowed=False,
        approval_required=True,
        approval_id=approval_id,
        decision=record.decision.value,
        resource=resource,
        reason=f"approval is {record.decision.value}",
    )
