"""Regression gates for 1.x admission readiness and instrumentation coverage."""

from __future__ import annotations

import pytest

from hermes_mcp_bridge.observability.metrics import get_registry, render_prometheus
from hermes_mcp_bridge.observability.operational_intelligence import (
    classify_admission,
    instrument_all_tools,
)


def test_admission_classifier_distinguishes_running_from_draining() -> None:
    running = classify_admission({"status": "ok", "gateway_state": "running"})
    assert running == {
        "status": "ready",
        "accepting_new_work": True,
        "gateway_state": "running",
        "reason": None,
    }

    draining = classify_admission({"status": "ok", "gateway_state": "draining"})
    assert draining == {
        "status": "not_ready",
        "accepting_new_work": False,
        "gateway_state": "draining",
        "reason": "draining",
    }


def test_admission_classifier_fails_closed_when_gateway_state_is_missing() -> None:
    result = classify_admission({"status": "ok"})
    assert result["accepting_new_work"] is False
    assert result["gateway_state"] == "unknown"
    assert result["reason"] == "other"


@pytest.mark.asyncio
async def test_mcp_readiness_exposes_independent_admission_state(monkeypatch) -> None:
    from hermes_mcp_bridge import server

    original = server.client.health

    async def fake_health(*, detailed: bool = False):
        if detailed:
            return {
                "status": "ok",
                "gateway_state": "draining",
                "active_api_runs": 0,
                "active_delegations": 0,
            }
        return {"status": "ok"}

    monkeypatch.setattr(server.client, "health", fake_health)
    try:
        result = await server.hermes_readiness()
    finally:
        monkeypatch.setattr(server.client, "health", original)

    assert result["alive"] is True
    assert result["accepting_new_work"] is False
    assert result["components"]["admission"]["status"] == "not_ready"
    assert result["components"]["admission"]["gateway_state"] == "draining"
    assert result["components"]["admission"]["reason"] == "draining"


@pytest.mark.asyncio
async def test_readiness_running_sets_admission_metric(monkeypatch) -> None:
    from hermes_mcp_bridge import server

    original = server.client.health

    async def fake_health(*, detailed: bool = False):
        if detailed:
            return {"status": "ok", "gateway_state": "running"}
        return {"status": "ok"}

    monkeypatch.setattr(server.client, "health", fake_health)
    try:
        result = await server.hermes_readiness()
    finally:
        monkeypatch.setattr(server.client, "health", original)

    assert result["accepting_new_work"] is True
    text = render_prometheus()
    assert "bridge_upstream_admission_ready 1" in text


def test_instrumentation_coverage_is_complete_for_contract() -> None:
    # The test suite intentionally resets the metrics registry between tests.
    # Re-running instrumentation must therefore be idempotent and republish the
    # actual current 27/27 coverage without wrapping tools a second time.
    from hermes_mcp_bridge import server

    instrument_all_tools(server.mcp)

    text = render_prometheus()
    assert "bridge_expected_tools 27" in text
    assert "bridge_instrumented_tools 27" in text
    assert "bridge_instrumentation_coverage_ratio 1" in text

    health = get_registry().health()
    assert health["unbounded_labels"] == []
