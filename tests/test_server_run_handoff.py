"""Server-side run handoff behavior backed by the registry."""

from __future__ import annotations

import asyncio
import importlib
import types
from contextlib import suppress
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from hermes_mcp_bridge.client import HermesAPIError, HermesClient
from hermes_mcp_bridge.config import Settings
from hermes_mcp_bridge.models import HermesPromptResult, RunStatus
from hermes_mcp_bridge.registry import RunRegistry, compute_fingerprint


def _make_server_module(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> types.ModuleType:
    db_path = str(tmp_path / "state.sqlite3")
    monkeypatch.setenv("HERMES_API_KEY", "unit-test-key-0123456789")
    monkeypatch.setenv("BRIDGE_STATE_DB_PATH", db_path)
    return importlib.import_module("hermes_mcp_bridge.server")


class _DummyContext:
    async def report_progress(
        self, progress: float, message: str
    ) -> None:  # pragma: no cover - noop
        return None


def test_exact_seven_tools_registered(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    server = _make_server_module(monkeypatch, tmp_path)
    tools = server.server_tool_names()
    expected = sorted([
        "hermes_agent_card",
        "hermes_capabilities",
        "hermes_health",
        "hermes_prompt",
        "hermes_recent_runs",
        "hermes_status",
        "hermes_stop",
        "hermes_submit",
        "hermes_wait",
        "hermes_policy_evaluate",
        "hermes_approval_create",
        "hermes_approval_respond",
        "hermes_approval_status",
        "hermes_result_manifest",
        "hermes_plan",
        "hermes_execute_approved_plan",
        "hermes_checkpoint_create",
        "hermes_checkpoint_status",
        "hermes_continue",
        "hermes_saga_start",
        "hermes_saga_status",
        "hermes_saga_compensate",
        "hermes_lock_acquire",
        "hermes_lock_status",
        "hermes_lock_release",
        "hermes_quota_status",
    ])
    assert tools == expected


@pytest.mark.asyncio
async def test_five_concurrent_same_key_creates_one_post(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _make_server_module(monkeypatch, tmp_path)
    db_path = str(tmp_path / "state.sqlite3")
    registry = RunRegistry(db_path)
    registry.initialize()
    monkeypatch.setattr("hermes_mcp_bridge.server.registry", registry)

    post_count = 0
    execution_ids: set[str] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        if request.method == "POST" and request.url.path == "/api/sessions":
            return httpx.Response(
                201, json={"object": "hermes.session", "session": {"id": "session-1"}}
            )
        if request.method == "POST" and request.url.path == "/v1/runs":
            post_count += 1
            execution_id = f"run-{post_count}"
            execution_ids.add(execution_id)
            return httpx.Response(
                202, json={"run_id": execution_id, "status": "started"}
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    settings = Settings(
        hermes_api_key=SecretStr("test-key"),
        hermes_api_base_url="http://hermes.test",
        bridge_state_db_path=db_path,
    )
    client = HermesClient(
        settings, transport_factory=lambda: httpx.MockTransport(handler)
    )
    monkeypatch.setattr("hermes_mcp_bridge.server.client", client)

    async def submit() -> dict[str, Any]:
        return await server.hermes_submit(
            prompt="concurrent task", ctx=_DummyContext(), client_request_id="shared"
        )

    results = await asyncio.gather(*[submit() for _ in range(5)])
    assert post_count == 1
    assert len(execution_ids) == 1
    assert all(result["execution_id"] == next(iter(execution_ids)) for result in results)


@pytest.mark.asyncio
async def test_two_different_keys_can_create_in_parallel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _make_server_module(monkeypatch, tmp_path)
    db_path = str(tmp_path / "state.sqlite3")
    registry = RunRegistry(db_path)
    registry.initialize()
    monkeypatch.setattr("hermes_mcp_bridge.server.registry", registry)

    counter = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal counter
        if request.method == "POST" and request.url.path == "/api/sessions":
            return httpx.Response(
                201, json={"object": "hermes.session", "session": {"id": "session"}}
            )
        if request.method == "POST" and request.url.path == "/v1/runs":
            # If these run truly in parallel, we should see both POSTs
            counter += 1
            return httpx.Response(
                202, json={"run_id": f"run-{counter}", "status": "started"}
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    settings = Settings(
        hermes_api_key=SecretStr("test-key"),
        hermes_api_base_url="http://hermes.test",
        bridge_state_db_path=db_path,
    )
    client = HermesClient(
        settings, transport_factory=lambda: httpx.MockTransport(handler)
    )
    monkeypatch.setattr("hermes_mcp_bridge.server.client", client)

    results = await asyncio.gather(
        server.hermes_submit(
            prompt="task", ctx=_DummyContext(), client_request_id="a"
        ),
        server.hermes_submit(
            prompt="task", ctx=_DummyContext(), client_request_id="b"
        ),
    )
    assert {results[0]["execution_id"], results[1]["execution_id"]} == {"run-1", "run-2"}


@pytest.mark.asyncio
async def test_retry_same_key_reuses_execution_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _make_server_module(monkeypatch, tmp_path)
    db_path = str(tmp_path / "state.sqlite3")
    registry = RunRegistry(db_path)
    registry.initialize()
    registry.record(
        client_request_id="retry",
        fingerprint=compute_fingerprint(
            prompt="same request",
            session_id=None,
            agent=None,
            subagents=None,
            orchestration="auto",
        ),
        execution_id="existing-run",
        session_id="session-1",
        last_status="queued",
    )
    monkeypatch.setattr("hermes_mcp_bridge.server.registry", registry)

    post_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    settings = Settings(
        hermes_api_key=SecretStr("test-key"),
        hermes_api_base_url="http://hermes.test",
        bridge_state_db_path=db_path,
    )
    client = HermesClient(
        settings, transport_factory=lambda: httpx.MockTransport(handler)
    )
    monkeypatch.setattr("hermes_mcp_bridge.server.client", client)
    result = await server.hermes_submit(
        prompt="same request", ctx=_DummyContext(), client_request_id="retry"
    )
    assert result["execution_id"] == "existing-run"
    assert result["metadata"].get("bridge_recovery_source") == "registry"


@pytest.mark.asyncio
async def test_different_fingerprint_fails_before_post(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _make_server_module(monkeypatch, tmp_path)
    db_path = str(tmp_path / "state.sqlite3")
    registry = RunRegistry(db_path)
    registry.initialize()
    registry.record(
        client_request_id="conflict",
        fingerprint="fp-old",
        execution_id="run-old",
        session_id=None,
        last_status="queued",
    )
    monkeypatch.setattr("hermes_mcp_bridge.server.registry", registry)

    post_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    settings = Settings(
        hermes_api_key=SecretStr("test-key"),
        hermes_api_base_url="http://hermes.test",
        bridge_state_db_path=db_path,
    )
    client = HermesClient(
        settings, transport_factory=lambda: httpx.MockTransport(handler)
    )
    monkeypatch.setattr("hermes_mcp_bridge.server.client", client)
    result = await server.hermes_submit(
        prompt="new request", ctx=_DummyContext(), client_request_id="conflict"
    )
    assert result["status"] == "failed"
    assert result["execution_id"] == "not-created"


@pytest.mark.asyncio
async def test_registry_failure_after_post_returns_execution_id_with_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _make_server_module(monkeypatch, tmp_path)
    db_path = str(tmp_path / "state.sqlite3")
    registry = RunRegistry(db_path)
    registry.initialize()
    monkeypatch.setattr("hermes_mcp_bridge.server.registry", registry)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/sessions":
            return httpx.Response(
                201, json={"object": "hermes.session", "session": {"id": "session-1"}}
            )
        if request.method == "POST" and request.url.path == "/v1/runs":
            return httpx.Response(
                202, json={"run_id": "created-run", "status": "started"}
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    class FailAfterPostRegistry(RunRegistry):
        def record(self, **kwargs: object) -> dict[str, object]:
            raise RuntimeError("sqlite is locked")

    registry = FailAfterPostRegistry(db_path)
    registry.initialize()
    monkeypatch.setattr("hermes_mcp_bridge.server.registry", registry)

    settings = Settings(
        hermes_api_key=SecretStr("test-key"),
        hermes_api_base_url="http://hermes.test",
        bridge_state_db_path=db_path,
    )
    client = HermesClient(
        settings, transport_factory=lambda: httpx.MockTransport(handler)
    )
    monkeypatch.setattr("hermes_mcp_bridge.server.client", client)
    result = await server.hermes_submit(
        prompt="recoverable", ctx=_DummyContext(), client_request_id="recover"
    )
    assert result["execution_id"] == "created-run"
    assert "warning" in result.get("metadata", {})


@pytest.mark.asyncio
async def test_cancel_during_persist_does_not_cancel_persistence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _make_server_module(monkeypatch, tmp_path)
    db_path = str(tmp_path / "state.sqlite3")
    registry = RunRegistry(db_path)
    registry.initialize()
    monkeypatch.setattr("hermes_mcp_bridge.server.registry", registry)

    original_record = registry.record

    def slow_record(*args: Any, **kwargs: object) -> dict[str, object]:
        # Simulate slow persistence that survives task cancellation
        import time
        time.sleep(0.05)
        return original_record(*args, **kwargs)

    monkeypatch.setattr(registry, "record", slow_record)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/sessions":
            return httpx.Response(
                201, json={"object": "hermes.session", "session": {"id": "session-1"}}
            )
        if request.method == "POST" and request.url.path == "/v1/runs":
            return httpx.Response(
                202, json={"run_id": "persist-run", "status": "started"}
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    settings = Settings(
        hermes_api_key=SecretStr("test-key"),
        hermes_api_base_url="http://hermes.test",
        bridge_state_db_path=db_path,
    )
    client = HermesClient(
        settings, transport_factory=lambda: httpx.MockTransport(handler)
    )
    monkeypatch.setattr("hermes_mcp_bridge.server.client", client)

    task = asyncio.create_task(
        server.hermes_submit(
            prompt="persist me", ctx=_DummyContext(), client_request_id="persist"
        )
    )
    await asyncio.sleep(0.01)


    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    mapping = await asyncio.to_thread(registry.get, "persist")
    assert mapping is not None
    assert mapping["execution_id"] == "persist-run"


@pytest.mark.asyncio
async def test_get_run_fallback_uses_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _make_server_module(monkeypatch, tmp_path)
    db_path = str(tmp_path / "state.sqlite3")
    registry = RunRegistry(db_path)
    registry.initialize()
    registry.record(
        client_request_id="fallback-run",
        fingerprint="fp",
        execution_id="fallback-run",
        session_id="fallback-session",
        last_status="running",
    )
    monkeypatch.setattr("hermes_mcp_bridge.server.registry", registry)

    async def fake_get_run(
        execution_id: str, **kwargs: object
    ) -> HermesPromptResult:
        raise HermesAPIError("missing upstream")

    monkeypatch.setattr(server.client, "get_run", fake_get_run)
    result = await server.hermes_status("fallback-run")
    assert result["execution_id"] == "fallback-run"
    assert result["metadata"].get("bridge_recovery_source") == "registry"


@pytest.mark.asyncio
async def test_recent_runs_excludes_sensitive_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _make_server_module(monkeypatch, tmp_path)
    db_path = str(tmp_path / "state.sqlite3")
    registry = RunRegistry(db_path)
    registry.initialize()
    registry.record(
        client_request_id="recent",
        fingerprint="fp",
        execution_id="recent-run",
        session_id="recent-session",
        last_status="queued",
    )
    monkeypatch.setattr("hermes_mcp_bridge.server.registry", registry)
    result = await server.hermes_recent_runs()
    assert result["object"] == "list"
    row = result["data"][0]
    for field in {
        "fingerprint",
        "prompt",
        "output",
        "error",
        "tokens",
        "headers",
        "secret",
    }:
        assert field not in row


@pytest.mark.asyncio
async def test_hermes_wait_defaults_to_forty_five_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path = Path("/tmp/hermes-bridge-wait")
    tmp_path.mkdir(exist_ok=True)
    server = _make_server_module(monkeypatch, tmp_path)
    seen: list[float] = []

    async def fake_wait(
        execution_id: str, **kwargs: object
    ) -> HermesPromptResult:
        seen.append(float(kwargs.get("max_wait_seconds", 0.0)))
        return HermesPromptResult(execution_id=execution_id, status=RunStatus.COMPLETED)

    monkeypatch.setattr(server.client, "wait_for_run", fake_wait)
    await server.hermes_wait("run-1", ctx=_DummyContext())
    assert seen == [server.settings.hermes_run_default_wait_seconds]


@pytest.mark.asyncio
async def test_health_includes_bridge_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path = Path("/tmp/hermes-bridge-health")
    tmp_path.mkdir(exist_ok=True)
    server = _make_server_module(monkeypatch, tmp_path)

    async def fake_health(detailed: bool = False) -> dict[str, Any]:
        return {"status": "healthy", "authenticated": True}

    monkeypatch.setattr(server.client, "health", fake_health)
    result = await server.hermes_health()
    assert result["upstream"] == {"status": "healthy", "authenticated": True}
    assert (
        result["bridge"]["default_wait_seconds"]
        == server.settings.hermes_run_default_wait_seconds
    )
    assert (
        result["bridge"]["max_wait_seconds"]
        == server.settings.hermes_run_max_wait_seconds
    )
    assert "state_registry" in result["bridge"]


@pytest.mark.asyncio
async def test_prompt_default_stop_on_disconnect_does_not_request_stop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _make_server_module(monkeypatch, tmp_path)
    db_path = str(tmp_path / "state.sqlite3")
    registry = RunRegistry(db_path)
    registry.initialize()
    monkeypatch.setattr("hermes_mcp_bridge.server.registry", registry)

    captured: dict[str, object] = {}

    async def fake_submit_prompt(**kwargs: object) -> HermesPromptResult:
        captured.update(kwargs)
        return HermesPromptResult(execution_id="run-1", status=RunStatus.COMPLETED)

    monkeypatch.setattr(server.client, "submit_prompt", fake_submit_prompt)

    result = await server.hermes_prompt("task", ctx=_DummyContext())
    assert result["execution_id"] == "run-1"
    assert captured.get("stop_on_cancel") is False


@pytest.mark.asyncio
async def test_prompt_stop_on_disconnect_true_requests_stop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _make_server_module(monkeypatch, tmp_path)
    db_path = str(tmp_path / "state.sqlite3")
    registry = RunRegistry(db_path)
    registry.initialize()
    monkeypatch.setattr("hermes_mcp_bridge.server.registry", registry)

    captured: dict[str, object] = {}

    async def fake_submit_prompt(**kwargs: object) -> HermesPromptResult:
        captured.update(kwargs)
        return HermesPromptResult(execution_id="run-1", status=RunStatus.COMPLETED)

    monkeypatch.setattr(server.client, "submit_prompt", fake_submit_prompt)

    result = await server.hermes_prompt(
        "task", ctx=_DummyContext(), stop_on_disconnect=True
    )
    assert result["execution_id"] == "run-1"
    assert captured.get("stop_on_cancel") is True
