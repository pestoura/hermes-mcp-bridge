"""Post-crash recovery tests using a real temporary SQLite database."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from faultkit.sqlite import disk_full_connection

from hermes_mcp_bridge.client import HermesClient
from hermes_mcp_bridge.config import Settings
from hermes_mcp_bridge.locks import LockRegistry
from hermes_mcp_bridge.migrations import apply_migrations
from hermes_mcp_bridge.models import LockType, ResourceLock
from hermes_mcp_bridge.observability.metrics import get_metrics, get_registry
from hermes_mcp_bridge.registry import RunRegistry, compute_fingerprint
from hermes_mcp_bridge.resilience import recover_state
from hermes_mcp_bridge.resilience.events import fingerprint
from hermes_mcp_bridge.resilience.recovery import lookup_execution

RUN_ID = "run-recovery"


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    path = str(tmp_path / "state.sqlite3")
    apply_migrations(path)
    return path


def _seed(db_path: str, key: str, status: str, execution_id: str) -> None:
    registry = RunRegistry(db_path)
    registry.record(
        client_request_id=key,
        fingerprint=compute_fingerprint(prompt=key),
        execution_id=execution_id,
    )
    if status != "queued":
        registry.update_status(client_request_id=key, last_status=status)


def test_recovery_reports_only_non_terminal_runs(db_path: str) -> None:
    _seed(db_path, "r-queued", "queued", "exec-1")
    _seed(db_path, "r-running", "running", "exec-2")
    _seed(db_path, "r-done", "completed", "exec-3")
    _seed(db_path, "r-failed", "failed", "exec-4")

    report = recover_state(db_path)
    assert report.recoverable_runs == 2
    assert report.terminal_runs == 2
    assert report.schema_version >= 10


def test_recovery_report_contains_no_raw_identifiers(db_path: str) -> None:
    _seed(db_path, "sensitive-client-id", "running", "sensitive-execution-id")
    payload = json.dumps(recover_state(db_path).to_dict())
    assert "sensitive-client-id" not in payload
    assert "sensitive-execution-id" not in payload
    assert fingerprint("sensitive-execution-id") in payload


def test_execution_id_survives_a_simulated_restart(db_path: str) -> None:
    _seed(db_path, "resume-me", "running", "exec-resume")
    del_registry = RunRegistry(db_path)
    del del_registry  # simulate process exit

    resolved = lookup_execution(db_path, "resume-me")
    assert resolved == {
        "execution_id": "exec-resume",
        "session_id": "",
        "last_status": "running",
    }


def test_recovery_does_not_resubmit_and_does_not_duplicate_runs(db_path: str) -> None:
    _seed(db_path, "no-dup", "running", "exec-no-dup")
    before = recover_state(db_path)
    after = recover_state(db_path)
    assert before.recoverable_runs == after.recoverable_runs == 1

    connection = sqlite3.connect(db_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM run_mappings").fetchone()[0] == 1
    finally:
        connection.close()


def test_recovery_reaps_expired_locks_only(db_path: str) -> None:
    registry = LockRegistry(db_path)
    registry.acquire(
        ResourceLock(
            lock_key="live",
            lock_type=LockType.WRITE_EXCLUSIVE,
            owner="owner-live",
            ttl_seconds=3600,
        )
    )
    registry.acquire(
        ResourceLock(
            lock_key="stale",
            lock_type=LockType.WRITE_EXCLUSIVE,
            owner="owner-stale",
            ttl_seconds=3600,
        )
    )
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    connection = sqlite3.connect(db_path)
    connection.isolation_level = None
    connection.execute("UPDATE resource_locks SET expires_at = ? WHERE lock_key = 'stale'", (past,))
    connection.close()

    report = recover_state(db_path)
    assert report.locks_reaped == 1
    statuses = {row["lock_key"]: row["status"] for row in registry.list_status()}
    assert statuses["stale"] == "expired"
    assert statuses["live"] == "active"


def test_recovery_clears_leftover_inflight_gauges(db_path: str) -> None:
    get_registry().reset()
    get_metrics().tool_inflight.inc(tool="hermes_wait")
    get_metrics().tool_inflight.inc(tool="hermes_status")
    assert get_metrics().tool_inflight.value(tool="hermes_wait") == 1.0

    report = recover_state(db_path)
    assert report.inflight_cleared == 2
    assert get_metrics().tool_inflight.value(tool="hermes_wait") == 0.0
    assert get_metrics().tool_inflight.value(tool="hermes_status") == 0.0


def test_recovery_is_safe_on_a_fresh_empty_database(tmp_path: Path) -> None:
    path = str(tmp_path / "empty.sqlite3")
    sqlite3.connect(path).close()
    report = recover_state(path)
    assert report.recoverable_runs == 0
    assert report.schema_version == 0


def test_crash_during_persistence_leaves_no_partial_mapping(db_path: str) -> None:
    connection = disk_full_connection(db_path, fail_after=0)
    connection.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.OperationalError):
        connection.execute(
            "INSERT INTO run_mappings VALUES ('partial','fp','e','s','queued','t','t')"
        )
    connection.execute("ROLLBACK")
    connection.close()

    assert recover_state(db_path).recoverable_runs == 0
    verify = sqlite3.connect(db_path)
    try:
        assert verify.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        verify.close()


async def test_status_can_be_read_after_restart_without_resubmission(db_path: str) -> None:
    _seed(db_path, "status-after-restart", "running", RUN_ID)
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        return httpx.Response(200, json={"run_id": RUN_ID, "status": "completed"})

    resolved = lookup_execution(db_path, "status-after-restart")
    assert resolved is not None

    client = HermesClient(
        Settings(  # type: ignore[call-arg]
            hermes_api_key="test-key",
            hermes_api_base_url="http://127.0.0.1:9",
        ),
        transport_factory=lambda: httpx.MockTransport(handler),
    )
    result = await client.get_run(resolved["execution_id"])
    assert result.status.value == "completed"
    assert calls == [f"GET /v1/runs/{RUN_ID}"]
    assert not any(call.startswith("POST /v1/runs") for call in calls)


_CHILD = """
import sys
sys.path.insert(0, {src!r})
from hermes_mcp_bridge.registry import RunRegistry, compute_fingerprint

db = sys.argv[1]
registry = RunRegistry(db)
registry.record(
    client_request_id="child-run",
    fingerprint=compute_fingerprint(prompt="child-run"),
    execution_id="exec-child",
)
registry.update_status(client_request_id="child-run", last_status="running")
"""


def test_state_written_by_a_killed_process_is_recoverable(tmp_path: Path, db_path: str) -> None:
    src = str(Path(__file__).resolve().parents[1] / "src")
    script = tmp_path / "child_recovery.py"
    script.write_text(_CHILD.format(src=src), encoding="utf-8")
    env = dict(os.environ, HERMES_API_KEY="test", BRIDGE_STATE_DB_PATH=db_path)
    completed = subprocess.run(
        [sys.executable, str(script), db_path],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr

    report = recover_state(db_path)
    assert report.recoverable_runs == 1
    assert lookup_execution(db_path, "child-run") == {
        "execution_id": "exec-child",
        "session_id": "",
        "last_status": "running",
    }
