from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from hermes_mcp_bridge.config import Settings
from hermes_mcp_bridge.registry import (
    ClientRequestIdError,
    FingerprintConflictError,
    RunRegistry,
    compute_fingerprint,
)


def _settings(db_path: str) -> Settings:
    return Settings(
        hermes_api_key=SecretStr("test-key"),
        hermes_run_poll_interval_seconds=0.001,
        hermes_run_max_wait_seconds=7200.0,
        bridge_state_db_path=db_path,
    )


@pytest.fixture()
def registry(tmp_path: Path) -> RunRegistry:
    db_path = str(tmp_path / "state.sqlite3")
    settings = _settings(db_path)
    registry = RunRegistry(settings.bridge_state_db_path)
    return registry


def test_validation_rejects_empty_id(registry: RunRegistry) -> None:
    with pytest.raises(ClientRequestIdError):
        registry.record(
            client_request_id="",
            fingerprint="a" * 64,
            execution_id="run-1",
        )


def test_validation_rejects_too_long_id(registry: RunRegistry) -> None:
    with pytest.raises(ClientRequestIdError):
        registry.record(
            client_request_id="x" * 161,
            fingerprint="a" * 64,
            execution_id="run-1",
        )


def test_validation_rejects_invalid_characters(registry: RunRegistry) -> None:
    with pytest.raises(ClientRequestIdError):
        registry.record(
            client_request_id="request/1",
            fingerprint="a" * 64,
            execution_id="run-1",
        )


def test_validation_rejects_leading_special(registry: RunRegistry) -> None:
    with pytest.raises(ClientRequestIdError):
        registry.record(
            client_request_id="_leading",
            fingerprint="a" * 64,
            execution_id="run-1",
        )


def test_fingerprint_is_deterministic_and_changes_with_request() -> None:
    fingerprint_a = compute_fingerprint(prompt="task")
    fingerprint_b = compute_fingerprint(prompt="task")
    assert fingerprint_a == fingerprint_b
    fingerprint_c = compute_fingerprint(prompt="other")
    assert fingerprint_a != fingerprint_c


def test_persistence_between_instances(tmp_path: Path) -> None:
    db_path = str(tmp_path / "state.sqlite3")
    settings = _settings(db_path)
    first = RunRegistry(settings.bridge_state_db_path)
    first.initialize()
    first.record(
        client_request_id="request-1",
        fingerprint=compute_fingerprint(prompt="task"),
        execution_id="run-1",
    )

    second = RunRegistry(settings.bridge_state_db_path)
    mapping = second.get("request-1")
    assert mapping is not None
    assert mapping["execution_id"] == "run-1"
    assert mapping["last_status"] == "queued"


def test_list_recent_excludes_sensitive_fields(registry: RunRegistry) -> None:
    registry.initialize()
    registry.record(
        client_request_id="request-1",
        fingerprint=compute_fingerprint(prompt="task"),
        execution_id="run-1",
    )
    recent = registry.list_recent()
    assert len(recent) == 1
    row = recent[0]
    for sensitive in {"fingerprint", "prompt", "output", "error", "tokens", "headers", "secret"}:
        assert sensitive not in row
    assert row["client_request_id"] == "request-1"
    assert row["execution_id"] == "run-1"


def test_same_key_and_fingerprint_preserves_execution_id(registry: RunRegistry) -> None:
    registry.initialize()
    registry.record(
        client_request_id="request-1",
        fingerprint=compute_fingerprint(prompt="task"),
        execution_id="run-1",
    )
    registry.record(
        client_request_id="request-1",
        fingerprint=compute_fingerprint(prompt="task"),
        execution_id="run-2",
    )
    mapping = registry.get("request-1")
    assert mapping is not None
    assert mapping["execution_id"] == "run-1"


def test_fingerprint_conflict_preserves_original(registry: RunRegistry) -> None:
    registry.initialize()
    registry.record(
        client_request_id="request-1",
        fingerprint=compute_fingerprint(prompt="task"),
        execution_id="run-1",
    )
    with pytest.raises(FingerprintConflictError):
        registry.record(
            client_request_id="request-1",
            fingerprint=compute_fingerprint(prompt="other"),
            execution_id="run-2",
        )
    mapping = registry.get("request-1")
    assert mapping is not None
    assert mapping["execution_id"] == "run-1"


def test_update_status_succeeds(registry: RunRegistry) -> None:
    registry.initialize()
    registry.record(
        client_request_id="request-1",
        fingerprint=compute_fingerprint(prompt="task"),
        execution_id="run-1",
    )
    updated = registry.update_status(client_request_id="request-1", last_status="completed")
    assert updated is not None
    assert updated["last_status"] == "completed"


def test_update_status_with_execution_id(registry: RunRegistry) -> None:
    registry.initialize()
    registry.record(
        client_request_id="request-1",
        fingerprint=compute_fingerprint(prompt="task"),
        execution_id="run-1",
    )
    updated = registry.update_status(
        client_request_id="request-1",
        last_status="completed",
        execution_id="run-2",
    )
    assert updated is not None
    assert updated["execution_id"] == "run-2"
    assert updated["last_status"] == "completed"


def test_health_down_before_initialize(registry: RunRegistry) -> None:
    health = registry.health()
    assert health["status"] == "up"
    assert health["table_exists"] is False


def test_health_up_after_initialize(registry: RunRegistry) -> None:
    registry.initialize()
    health = registry.health()
    assert health["status"] == "up"
    assert health["table_exists"] is True


def test_reported_pragmas_when_supported(registry: RunRegistry) -> None:
    registry.initialize()
    health = registry.health()
    assert health["status"] == "up"
    pragmas = health.get("pragmas", {})
    assert "journal_mode" in pragmas
    assert "synchronous" in pragmas
    assert "busy_timeout" in pragmas


def test_list_recent_respects_status_filter(registry: RunRegistry) -> None:
    registry.initialize()
    registry.record(
        client_request_id="request-1",
        fingerprint=compute_fingerprint(prompt="task"),
        execution_id="run-1",
        last_status="completed",
    )
    registry.record(
        client_request_id="request-2",
        fingerprint=compute_fingerprint(prompt="other"),
        execution_id="run-2",
        last_status="queued",
    )
    recent = registry.list_recent(status="completed")
    assert [item["client_request_id"] for item in recent] == ["request-1"]


def test_list_recent_limits_one_to_hundred(registry: RunRegistry) -> None:
    registry.initialize()
    for index in range(25):
        registry.record(
            client_request_id=f"request-{index}",
            fingerprint=compute_fingerprint(prompt=f"task {index}"),
            execution_id=f"run-{index}",
        )
    assert len(registry.list_recent(limit=100)) == 25
    assert len(registry.list_recent(limit=10)) == 10


def test_get_returns_none_for_unknown_id(registry: RunRegistry) -> None:
    registry.initialize()
    assert registry.get("unknown") is None
