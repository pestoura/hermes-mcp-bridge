"""Real SQLite call-site and active-runs wiring tests.

These confirm the observability metrics are actually connected to the bridge's
real SQLite paths (state registry, approvals, locks, migrations) and to the
upstream health signal, not just exported as always-zero.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import hermes_mcp_bridge.approvals as approvals_mod
import hermes_mcp_bridge.registry as registry_mod
from hermes_mcp_bridge.approvals import ApprovalRecord, ApprovalRegistry, ApprovalStatus
from hermes_mcp_bridge.observability import instrumentation as inst
from hermes_mcp_bridge.observability.metrics import get_metrics, get_registry
from hermes_mcp_bridge.registry import RunRegistry


@pytest.fixture(autouse=True)
def _fresh_registry():
    get_registry().reset()
    yield
    get_registry().reset()


def test_state_registry_error_records_sqlite_metric(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "state.sqlite3"
    reg = RunRegistry(str(db))
    reg.initialize()

    def _boom(db_path: str):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(registry_mod, "_open_connection", _boom)
    with pytest.raises(sqlite3.OperationalError):
        reg.record(
            client_request_id="abc123",
            fingerprint="fp",
            execution_id="exec1",
            last_status="queued",
        )
    m = get_metrics()
    assert m.sqlite_errors_total.value(kind="state lock") >= 1
    assert m.sqlite_lock_contention_total.value() >= 1


def test_approvals_error_records_sqlite_metric(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "approvals.sqlite3"
    reg = ApprovalRegistry(str(db))
    reg.initialize()

    def _boom(db_path: str):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(approvals_mod, "_open_connection", _boom)
    rec = ApprovalRecord(
        approval_id="appr-fixed0000000000000000000000000000",
        action="mutate",
        resource="x",
        decision=ApprovalStatus.REQUESTED,
        created_at="2026-01-01T00:00:00Z",
    )
    with pytest.raises(sqlite3.OperationalError):
        reg.create(rec)
    m = get_metrics()
    assert m.sqlite_errors_total.value(kind="approvals lock") >= 1


def test_active_runs_updated_from_health(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib

    import httpx

    monkeypatch.setenv("HERMES_API_KEY", "sk-test")
    from hermes_mcp_bridge.config import get_settings

    get_settings.cache_clear()
    server = importlib.import_module("hermes_mcp_bridge.server")
    server = importlib.reload(server)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/health"):
            return httpx.Response(200, json={"status": "ok", "active_api_runs": 3})
        return httpx.Response(404, json={"error": "unexpected"})

    server.client._transport_factory = lambda: httpx.MockTransport(handler)
    get_registry().reset()
    import asyncio

    asyncio.run(server.hermes_health())
    assert get_metrics().active_runs.value() == 3


def test_record_sqlite_operation_no_double_count_on_success() -> None:
    before = get_metrics().sqlite_errors_total.value(kind="migrations")
    inst.record_sqlite_operation(kind="migrations", outcome="success")
    after = get_metrics().sqlite_errors_total.value(kind="migrations")
    assert before == after


def test_record_sqlite_operation_lock_detection() -> None:
    inst.record_sqlite_operation(
        kind="locks", outcome="error", exc=Exception("database is locked")
    )
    m = get_metrics()
    assert m.sqlite_errors_total.value(kind="locks lock") >= 1
    assert m.sqlite_lock_contention_total.value() >= 1
