from __future__ import annotations

import pytest

from hermes_mcp_bridge import prompt_approvals
from hermes_mcp_bridge.protocol import ApprovalStatus
from hermes_mcp_bridge.registry import compute_fingerprint


class FakeApprovalRegistry:
    def __init__(self) -> None:
        self.records = {}
        self.consume_calls = 0

    def initialize(self) -> None:
        return None

    def get(self, approval_id):
        if approval_id not in self.records:
            raise prompt_approvals.ApprovalNotFound("not found")
        return self.records[approval_id]

    def create(self, record):
        self.records[record.approval_id] = record
        return record

    def respond(self, approval_id, decision, *, principal=None):
        record = self.get(approval_id)
        record = record.model_copy(
            update={"decision": decision, "principal": principal or record.principal}
        )
        self.records[approval_id] = record
        return record

    def consume(
        self,
        approval_id,
        resource_fingerprint,
        *,
        require_fingerprint=False,
        expected_action=None,
    ):
        record = self.get(approval_id)
        if record.decision == ApprovalStatus.CONSUMED:
            raise prompt_approvals.ApprovalConsumedError("approval already consumed")
        if record.decision != ApprovalStatus.APPROVED:
            raise prompt_approvals.ApprovalStatusError("approval not approved")
        if expected_action is not None and record.action != expected_action:
            raise prompt_approvals.ApprovalStaleError("action mismatch")
        if require_fingerprint and record.resource_fingerprint != resource_fingerprint:
            raise prompt_approvals.ApprovalStaleError("fingerprint mismatch")
        self.consume_calls += 1
        record = record.model_copy(update={"decision": ApprovalStatus.CONSUMED})
        self.records[approval_id] = record
        return record

    def expire(self, approval_id):
        record = self.get(approval_id).model_copy(update={"decision": ApprovalStatus.EXPIRED})
        self.records[approval_id] = record
        return record


class FakeExecutionRegistry:
    def __init__(self) -> None:
        self.mappings = {}

    def get(self, client_request_id):
        return self.mappings.get(client_request_id)


def _request(**overrides):
    values = {
        "action": "hermes_prompt",
        "prompt": "Inspect only the local lab runtime; return sanitized state.",
        "client_request_id": "runtime-acceptance-rta003-1",
        "session_id": None,
        "agent": None,
        "subagents": None,
        "orchestration": "auto",
        "expected_actions": ["read-only-host-inspection"],
        "resource_scopes": ["local-lab"],
        "trust_labels": ["authorized-local-lab"],
        "principal": None,
    }
    values.update(overrides)
    return values


@pytest.fixture
def registries(monkeypatch):
    approvals = FakeApprovalRegistry()
    executions = FakeExecutionRegistry()
    monkeypatch.setattr(prompt_approvals, "get_approval_registry", lambda: approvals)
    monkeypatch.setattr(prompt_approvals, "get_registry", lambda: executions)
    return approvals, executions


def test_first_required_request_issues_stable_non_null_approval_id(registries):
    approvals, _ = registries

    first = prompt_approvals.resolve_required_prompt_approval(**_request())
    second = prompt_approvals.resolve_required_prompt_approval(**_request())

    assert first.allowed is False
    assert first.approval_required is True
    assert first.approval_id.startswith("approval-prompt-")
    assert first.approval_id == second.approval_id
    assert first.decision == "requested"
    assert len(approvals.records) == 1


def test_approval_storage_never_contains_raw_prompt(registries):
    approvals, _ = registries
    raw_prompt = "sensitive-local-instruction-that-must-not-be-persisted"

    outcome = prompt_approvals.resolve_required_prompt_approval(**_request(prompt=raw_prompt))
    record = approvals.records[outcome.approval_id]
    serialized = repr(
        {
            "resource": record.resource,
            "metadata": record.metadata_sanitized,
            "fingerprint": record.resource_fingerprint,
        }
    )

    assert raw_prompt not in serialized
    assert record.resource.startswith("request-sha256:")
    assert record.metadata_sanitized["kind"] == "prompt-policy-handoff"


def test_approved_exact_request_is_consumed_and_allowed_once(registries):
    approvals, _ = registries
    pending = prompt_approvals.resolve_required_prompt_approval(**_request())
    approvals.respond(pending.approval_id, ApprovalStatus.APPROVED)

    allowed = prompt_approvals.resolve_required_prompt_approval(**_request())

    assert allowed.allowed is True
    assert allowed.approval_required is False
    assert allowed.decision == "consumed"
    assert approvals.consume_calls == 1
    assert approvals.get(pending.approval_id).decision == ApprovalStatus.CONSUMED


def test_consumed_request_fails_closed_without_persisted_execution(registries):
    approvals, _ = registries
    pending = prompt_approvals.resolve_required_prompt_approval(**_request())
    approvals.respond(pending.approval_id, ApprovalStatus.APPROVED)
    prompt_approvals.resolve_required_prompt_approval(**_request())

    blocked = prompt_approvals.resolve_required_prompt_approval(**_request())

    assert blocked.allowed is False
    assert blocked.approval_required is True
    assert blocked.decision == "consumed"
    assert "fresh client_request_id" in (blocked.reason or "")


def test_consumed_request_allows_only_matching_idempotent_execution(registries):
    approvals, executions = registries
    request = _request()
    pending = prompt_approvals.resolve_required_prompt_approval(**request)
    approvals.respond(pending.approval_id, ApprovalStatus.APPROVED)
    allowed = prompt_approvals.resolve_required_prompt_approval(**request)
    assert allowed.allowed is True

    run_fingerprint = compute_fingerprint(
        prompt=request["prompt"],
        session_id=request["session_id"],
        agent=request["agent"],
        subagents=request["subagents"],
        orchestration=request["orchestration"],
    )
    executions.mappings[request["client_request_id"]] = {
        "fingerprint": run_fingerprint,
        "execution_id": "exec-1",
        "last_status": "running",
    }

    replay = prompt_approvals.resolve_required_prompt_approval(**request)

    assert replay.allowed is True
    assert replay.idempotent_existing is True
    assert approvals.consume_calls == 1


def test_changed_request_cannot_reuse_prior_approval(registries):
    approvals, _ = registries
    first = prompt_approvals.resolve_required_prompt_approval(**_request())
    approvals.respond(first.approval_id, ApprovalStatus.APPROVED)

    changed_prompt = prompt_approvals.resolve_required_prompt_approval(
        **_request(prompt="Different request")
    )
    changed_scope = prompt_approvals.resolve_required_prompt_approval(
        **_request(resource_scopes=["different-scope"])
    )
    changed_trust = prompt_approvals.resolve_required_prompt_approval(
        **_request(trust_labels=["untrusted_content"])
    )

    assert changed_prompt.approval_id != first.approval_id
    assert changed_scope.approval_id != first.approval_id
    assert changed_trust.approval_id != first.approval_id
    assert all(
        outcome.allowed is False for outcome in (changed_prompt, changed_scope, changed_trust)
    )


def test_action_binding_prevents_submit_from_reusing_prompt_approval(registries):
    approvals, _ = registries
    first = prompt_approvals.resolve_required_prompt_approval(**_request())
    approvals.respond(first.approval_id, ApprovalStatus.APPROVED)

    submit = prompt_approvals.resolve_required_prompt_approval(**_request(action="hermes_submit"))

    assert submit.approval_id != first.approval_id
    assert submit.allowed is False
    assert submit.decision == "requested"


def test_rejected_approval_remains_fail_closed(registries):
    approvals, _ = registries
    pending = prompt_approvals.resolve_required_prompt_approval(**_request())
    approvals.respond(pending.approval_id, ApprovalStatus.REJECTED)

    rejected = prompt_approvals.resolve_required_prompt_approval(**_request())

    assert rejected.allowed is False
    assert rejected.approval_required is True
    assert rejected.decision == "rejected"
