from __future__ import annotations

import importlib
import types
from pathlib import Path

import pytest
from pydantic import SecretStr

from hermes_mcp_bridge.config import Settings
from hermes_mcp_bridge.models import Plan, PlanDependency, PlanStep, QuotaDecision
from hermes_mcp_bridge.plans import PlanStore, validate_plan_structure
from hermes_mcp_bridge.protocol import ApprovalRecord
from hermes_mcp_bridge.quotas import QuotaRegistry
from hermes_mcp_bridge.tracing import (
    build_trace_metadata,
    parse_traceparent,
    sanitize_trace_context,
    tracing_readiness,
)


def _make_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> types.ModuleType:
    db_path = str(tmp_path / "state.sqlite3")
    server = importlib.import_module("hermes_mcp_bridge.server")
    settings = Settings(
        hermes_api_base_url="http://127.0.0.1:8642",
        hermes_api_key=SecretStr("unit-test-key-0123456789"),
        hermes_model="hermes-agent",
        hermes_request_timeout_seconds=1.0,
        hermes_run_poll_interval_seconds=1.0,
        hermes_run_default_wait_seconds=0.0,
        hermes_run_max_wait_seconds=10.0,
        hermes_progress_interval_seconds=1.0,
        hermes_event_stream_connect_timeout_seconds=1.0,
        mcp_host="127.0.0.1",
        mcp_port=8765,
        mcp_path="/mcp",
        log_level="INFO",
        bridge_state_db_path=db_path,
    )
    quota = QuotaRegistry(db_path)
    quota.initialize()
    monkeypatch.setattr(server, "settings", settings, raising=False)
    monkeypatch.setattr(server, "quota_registry", quota, raising=False)
    return server


def test_plan_hash_and_dag_validation(tmp_path: Path) -> None:
    plan = Plan(
        plan_id="plan-test-1",
        title="t",
        steps=[PlanStep(step_id="s1", title="s")],
        dependencies=[PlanDependency(step_id="s1", depends_on=[])],
    )
    errors = validate_plan_structure(plan)
    assert not errors
    store = PlanStore(str(tmp_path / "state.sqlite3"))
    store.initialize()
    saved, plan_hash = store.create(plan)
    assert saved.plan_hash == plan_hash
    assert len(plan_hash) == 64


def test_traceparent_parsing_and_bridge_only() -> None:
    valid = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
    parsed = parse_traceparent(valid)
    assert parsed is not None
    assert parsed["trace_id"] == "0af7651916cd43dd8448eb211c80319c"
    assert parse_traceparent("bad") is None

    meta = build_trace_metadata({"traceparent": valid}, upstream_supported=False)
    assert meta["effective_support"] == "bridge_only"
    assert meta["trace_id"] == parsed["trace_id"]
    assert "prompt" not in meta.get("context", {})


def test_tracing_sanitization_removes_sensitive_fields() -> None:
    cleaned = sanitize_trace_context(
        {"traceparent": "x", "prompt": "secret", "leaseToken": "abc", "correlation_id": "c1"}
    )
    assert "prompt" not in cleaned
    assert "leaseToken" not in cleaned
    assert cleaned.get("correlation_id") == "c1"


def test_tracing_readiness_reported() -> None:
    readiness = tracing_readiness()
    assert readiness["tracing_ready"] is True
    assert "traceparent" in readiness["allowed_context_fields"]


@pytest.mark.asyncio
async def test_quota_decisions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _make_server(monkeypatch, tmp_path)
    result = await server.hermes_quota_status(principal="p", resource="r", mutation=True)
    assert result["quota"]["decision"] in {QuotaDecision.ALLOW.value, QuotaDecision.REJECT.value}


def test_empty_db_creates_all_expected_tables(tmp_path: Path) -> None:
    from hermes_mcp_bridge.approvals import ApprovalRegistry
    from hermes_mcp_bridge.checkpoints import CheckpointRegistry
    from hermes_mcp_bridge.locks import LockRegistry
    from hermes_mcp_bridge.migrations import apply_migrations
    from hermes_mcp_bridge.plans import PlanStore
    from hermes_mcp_bridge.quotas import QuotaRegistry
    from hermes_mcp_bridge.registry import RunRegistry
    from hermes_mcp_bridge.sagas import SagaRegistry

    db_path = str(tmp_path / "state.sqlite3")
    apply_migrations(db_path)
    RunRegistry(db_path).initialize()
    PlanStore(db_path).initialize()
    ApprovalRegistry(db_path).initialize()
    CheckpointRegistry(db_path).initialize()
    LockRegistry(db_path).initialize()
    SagaRegistry(db_path).initialize()
    QuotaRegistry(db_path).initialize()

    conn = __import__("sqlite3").connect(db_path, check_same_thread=False)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        }
        expected = {
            "schema_migrations",
            "run_mappings",
            "plans",
            "plan_approvals",
            "checkpoints",
            "continuations",
            "sagas",
            "resource_locks",
            "quota_profiles",
            "approvals",
        }
        assert expected.issubset(tables)
    finally:
        conn.close()


def test_approval_health_up_after_startup_on_empty_db(tmp_path: Path) -> None:
    from hermes_mcp_bridge.approvals import ApprovalRegistry, get_approval_registry

    db_path = str(tmp_path / "state.sqlite3")
    registry = ApprovalRegistry(db_path)
    registry.initialize()
    health = registry.health()
    assert health["status"] == "up"
    assert health["table_exists"] is True

    cached = get_approval_registry()
    cached._db_path = db_path
    assert cached.health()["table_exists"] is True


def test_approval_first_create_and_status_on_empty_db(tmp_path: Path) -> None:
    from hermes_mcp_bridge.approvals import ApprovalRegistry

    db_path = str(tmp_path / "state.sqlite3")
    registry = ApprovalRegistry(db_path)
    registry.initialize()

    record = registry.create(
        ApprovalRecord(
            approval_id="approval-test-1",
            action="mutation",
            principal="tester",
            created_at="2026-01-01T00:00:00+00:00",
        )
    )
    assert record.approval_id == "approval-test-1"
    assert record.decision.value == "requested"

    status = registry.get("approval-test-1")
    assert status.action == "mutation"
    assert status.principal == "tester"


def test_tool_inventory_exact_26_matches_manifest_and_smoke() -> None:
    import asyncio
    import importlib

    from hermes_mcp_bridge.server import _build_capability_manifest

    server = importlib.import_module("hermes_mcp_bridge.server")
    discovered = set(server.server_tool_names())
    expected = {
        "hermes_approval_create",
        "hermes_approval_respond",
        "hermes_approval_status",
        "hermes_capabilities",
        "hermes_agent_card",
        "hermes_checkpoint_create",
        "hermes_checkpoint_status",
        "hermes_continue",
        "hermes_execute_approved_plan",
        "hermes_health",
        "hermes_lock_acquire",
        "hermes_lock_release",
        "hermes_lock_status",
        "hermes_policy_evaluate",
        "hermes_plan",
        "hermes_prompt",
        "hermes_quota_status",
        "hermes_recent_runs",
        "hermes_result_manifest",
        "hermes_saga_compensate",
        "hermes_saga_start",
        "hermes_saga_status",
        "hermes_status",
        "hermes_stop",
        "hermes_submit",
        "hermes_wait",
    }
    assert discovered == expected
    assert len(discovered) == 26

    manifest_obj = asyncio.run(_build_capability_manifest())
    assert set(manifest_obj.effective_tools) == expected
    assert len(manifest_obj.effective_tools) == 26


def test_migrations_are_idempotent_after_restart(tmp_path: Path) -> None:
    from hermes_mcp_bridge.migrations import _current_version, apply_migrations

    db_path = str(tmp_path / "state.sqlite3")
    first = apply_migrations(db_path)
    assert first == 8
    conn = __import__("sqlite3").connect(db_path, check_same_thread=False)
    try:
        assert _current_version(conn) == 8
    finally:
        conn.close()
    second = apply_migrations(db_path)
    assert second == 8
    conn = __import__("sqlite3").connect(db_path, check_same_thread=False)
    try:
        assert _current_version(conn) == 8
    finally:
        conn.close()
