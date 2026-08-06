"""Cardinality and redaction checks for state run-status diagnostics."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from hermes_mcp_bridge.config import get_settings
from hermes_mcp_bridge.migrations import apply_migrations
from hermes_mcp_bridge.models import RunStatus
from hermes_mcp_bridge.state_operations import diagnose_state_db


def test_arbitrary_database_status_is_aggregated_as_other(monkeypatch, tmp_path) -> None:
    db = tmp_path / "state.sqlite3"
    monkeypatch.setenv("HERMES_API_KEY", "test-key")
    monkeypatch.setenv("BRIDGE_SECURITY_MODE", "test")
    monkeypatch.setenv("BRIDGE_STATE_DB_PATH", str(db))
    monkeypatch.setenv("HERMES_BRIDGE_BACKUP_ROOT", str(tmp_path))
    get_settings.cache_clear()
    apply_migrations(str(db))

    sensitive_status = "secret-status-value-from-corrupt-state"
    now = datetime(2026, 8, 6, 1, 0, tzinfo=UTC).isoformat()
    connection = sqlite3.connect(db)
    try:
        connection.execute(
            "INSERT INTO run_mappings VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("request", "fingerprint", "execution", None, sensitive_status, now, now),
        )
        connection.commit()
    finally:
        connection.close()

    payload = diagnose_state_db(
        str(db),
        now=datetime(2026, 8, 6, 1, 0, tzinfo=UTC),
    )
    counts = payload["runs"]["status_counts"]

    assert set(counts) == {status.value for status in RunStatus} | {"other"}
    assert counts["other"] == 1
    assert sensitive_status not in str(payload)
