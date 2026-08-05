"""Isolated regression proofs for the 0.9.0 policy/HMAC block.

These two tests encode the pre-0.9.0 defects:

1. ``_enforce_policy`` fails **open**: when policy evaluation raises, the
   decision string is ``"error"`` and the enforcement helper returns ``None``
   (meaning "not blocked"), so the caller proceeds to execute.
2. ``ApprovalRegistry.consume`` never inspects ``expires_at``: an approval that
   expired in the past can still be consumed and drives execution.

Both must FAIL against the pre-fix tree and PASS after the fix.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from hermes_mcp_bridge.approvals import (
    ApprovalExpiredError,
    ApprovalRecord,
    ApprovalRegistry,
    ApprovalStatus,
    _utcnow,
)


def test_enforcement_fails_closed_when_policy_evaluation_errors(monkeypatch) -> None:
    """A policy evaluation error must DENY, never fall through to execution."""

    from hermes_mcp_bridge import server

    def _boom(*_args, **_kwargs):
        raise RuntimeError("policy backend exploded")

    monkeypatch.setattr(server, "evaluate_policy", _boom)

    blocked = server._enforce_policy(
        "hermes_execute_approved_plan",
        principal="user-1",
    )

    assert blocked is not None, "policy evaluation error must block the call"
    assert blocked.get("status") == "failed"
    assert "denied" in str(blocked.get("error", "")).lower()


def test_consume_rejects_expired_approval() -> None:
    """An approved-but-expired approval must never be consumable."""

    with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as tmp:
        db_path = tmp.name
    try:
        registry = ApprovalRegistry(db_path)
        registry.initialize()
        past = (_utcnow().replace(year=_utcnow().year - 1)).isoformat()
        registry.create(
            ApprovalRecord(
                approval_id="approval-expired-consume",
                action="hermes_execute_approved_plan",
                resource="plan-1",
                resource_fingerprint="fp-1",
                principal="user-1",
                decision=ApprovalStatus.APPROVED,
                expires_at=past,
                created_at=_utcnow().isoformat(),
            )
        )
        with pytest.raises(ApprovalExpiredError):
            registry.consume("approval-expired-consume", "fp-1")
    finally:
        os.unlink(db_path)
