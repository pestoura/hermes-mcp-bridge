"""Focused 0.6.0 resilience-gate tests."""

from __future__ import annotations

import importlib
import types
from pathlib import Path

import pytest
from pydantic import SecretStr

from hermes_mcp_bridge.config import Settings
from hermes_mcp_bridge.models import Plan, PlanDependency, PlanStep, QuotaDecision
from hermes_mcp_bridge.plans import PlanStore, validate_plan_structure
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
