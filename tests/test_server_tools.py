"""Tool surface and edge-case coverage for the MCP bridge server."""

from __future__ import annotations

import asyncio
import importlib
import re
import types
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from hermes_mcp_bridge.client import HermesAPIError, HermesClient
from hermes_mcp_bridge.config import Settings
from hermes_mcp_bridge.models import HermesPromptResult, RunStatus
from hermes_mcp_bridge.registry import RunRegistry

EXPECTED_TOOLS = {
    "hermes_health",
    "hermes_prompt",
    "hermes_recent_runs",
    "hermes_status",
    "hermes_stop",
    "hermes_submit",
    "hermes_wait",
    "hermes_capabilities",
    "hermes_agent_card",
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
}


def _make_server_module(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> types.ModuleType:
    db_path = str(tmp_path / "state.sqlite3")
    monkeypatch.setenv("HERMES_API_KEY", "unit-test-key-0123456789")
    monkeypatch.setenv("BRIDGE_STATE_DB_PATH", db_path)
    return importlib.import_module("hermes_mcp_bridge.server")


class _DummyContext:
    async def report_progress(  # pragma: no cover - noop
        self, progress: float, message: str
    ) -> None:
        return None


def test_exact_seven_tools_registered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _make_server_module(monkeypatch, tmp_path)
    tools = server.server_tool_names()
    assert set(tools) == EXPECTED_TOOLS
    assert len(tools) == len(EXPECTED_TOOLS)


def test_server_imports_have_no_dynamic_import_placeholder() -> None:
    tree = list(Path("src/hermes_mcp_bridge").rglob("*.py"))
    bad_paths: list[Path] = []
    bad_names: list[Path] = []
    for path in tree:
        text = path.read_text(encoding="utf-8")
        if "..." in text and re.search(
            r"\b(placeholder|pass|# TODO).*\...", text, re.IGNORECASE
        ):
            bad_paths.append(path)
        if "__import__(" in text or "exec(" in text or "eval(" in text:
            bad_names.append(path)
    assert not bad_paths, f"placeholder ellipsis found in {bad_paths}"
    assert not bad_names, f"dynamic import/exec/eval found in {bad_names}"


def test_settings_has_no_duplicate_fields() -> None:
    source = Path("src/hermes_mcp_bridge/config.py").read_text(encoding="utf-8")
    names = re.findall(r"^(\w+):", source, re.MULTILINE)
    assert len(names) == len(set(names)), f"duplicate settings fields: {names}"


def test_server_has_no_sensitive_exception_messages() -> None:
    source = Path("src/hermes_mcp_bridge/server.py").read_text(encoding="utf-8")
    lower = source.lower()
    for token in ("password", "secret", "token", "api_key"):
        assert f"raise RuntimeError({token!r})" not in lower
        assert f"raise ValueError({token!r})" not in lower


@pytest.mark.asyncio
async def test_different_keys_parallel_create(
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
        server.hermes_submit(prompt="task", ctx=_DummyContext(), client_request_id="a"),
        server.hermes_submit(prompt="task", ctx=_DummyContext(), client_request_id="b"),
    )
    assert {result["execution_id"] for result in results} == {"run-1", "run-2"}


@pytest.mark.asyncio
async def test_same_key_same_fingerprint_concurrent_one_post(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _make_server_module(monkeypatch, tmp_path)
    db_path = str(tmp_path / "state.sqlite3")
    registry = RunRegistry(db_path)
    registry.initialize()
    monkeypatch.setattr("hermes_mcp_bridge.server.registry", registry)

    post_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        if request.method == "POST" and request.url.path == "/api/sessions":
            return httpx.Response(
                201, json={"object": "hermes.session", "session": {"id": "session-1"}}
            )
        if request.method == "POST" and request.url.path == "/v1/runs":
            post_count += 1
            return httpx.Response(
                202, json={"run_id": f"run-{post_count}", "status": "started"}
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
            prompt="concurrent task",
            ctx=_DummyContext(),
            client_request_id="shared",
        )

    results = await asyncio.gather(*[submit() for _ in range(5)])
    assert post_count == 1
    execution_ids = {result["execution_id"] for result in results}
    assert len(execution_ids) == 1
    assert all(result["execution_id"] == next(iter(execution_ids)) for result in results)


@pytest.mark.asyncio
async def test_hermes_submit_blocks_denied_action(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _make_server_module(monkeypatch, tmp_path)
    db_path = str(tmp_path / "state.sqlite3")
    registry = RunRegistry(db_path)
    registry.initialize()
    monkeypatch.setattr("hermes_mcp_bridge.server.registry", registry)

    settings = Settings(
        hermes_api_key=SecretStr("test-key"),
        hermes_api_base_url="http://hermes.test",
        bridge_state_db_path=db_path,
    )
    message = "Unexpected request: {} {}".format
    client = HermesClient(
        settings,
        transport_factory=lambda: httpx.MockTransport(
            lambda req: (_ for _ in ()).throw(
                AssertionError(message(req.method, req.url))
            )
        ),
    )
    monkeypatch.setattr("hermes_mcp_bridge.server.client", client)

    result = await server.hermes_submit(
        prompt="concurrent task",
        ctx=_DummyContext(),
        client_request_id="shared",
        trust_labels=["untrusted_content"],
    )
    assert result["execution_id"] == "not-created"
    error_text = result.get("error") or ""
    policy_message = (result.get("metadata") or {}).get("policy", {}).get("message") or ""
    assert (
        "policy denied" in error_text
        or "policy requires approval" in error_text
        or "policy denied" in policy_message
        or "policy requires approval" in policy_message
    )


@pytest.mark.asyncio
async def test_mapping_persisted_before_wait(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _make_server_module(monkeypatch, tmp_path)
    db_path = str(tmp_path / "state.sqlite3")
    registry = RunRegistry(db_path)
    registry.initialize()
    monkeypatch.setattr("hermes_mcp_bridge.server.registry", registry)

    async def fake_submit_prompt(
        prompt: str,
        **kwargs: object,
    ) -> HermesPromptResult:
        return HermesPromptResult(execution_id="run-1", status=RunStatus.QUEUED)

    async def fake_wait(execution_id: str, **kwargs: object) -> HermesPromptResult:
        return HermesPromptResult(execution_id=execution_id, status=RunStatus.COMPLETED)

    monkeypatch.setattr(server.client, "submit_prompt", fake_submit_prompt)
    monkeypatch.setattr(server.client, "wait_for_run", fake_wait)
    result = await server.hermes_prompt(
        "task", ctx=_DummyContext(), client_request_id="id-1", wait_seconds=0
    )
    assert result["execution_id"] == "run-1"
    mapping = registry.get("id-1")
    assert mapping is not None
    assert "created_at" in mapping


@pytest.mark.asyncio
async def test_registry_failure_returns_execution_id_with_sanitized_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _make_server_module(monkeypatch, tmp_path)
    db_path = str(tmp_path / "state.sqlite3")
    registry = RunRegistry(db_path)
    registry.initialize()
    monkeypatch.setattr("hermes_mcp_bridge.server.registry", registry)

    class FailRegistry(RunRegistry):
        def record(self, **kwargs: object) -> dict[str, object]:
            raise RuntimeError("registry unavailable")

    failing = FailRegistry(db_path)
    failing.initialize()
    monkeypatch.setattr("hermes_mcp_bridge.server.registry", failing)

    settings = Settings(
        hermes_api_key=SecretStr("test-key"),
        hermes_api_base_url="http://hermes.test",
        bridge_state_db_path=db_path,
    )
    client = HermesClient(settings, transport_factory=lambda: httpx.MockTransport(lambda req: (
        httpx.Response(201, json={"object": "hermes.session", "session": {"id": "session-1"}})
        if req.method == "POST" and req.url.path == "/api/sessions"
        else httpx.Response(202, json={"run_id": "created-run", "status": "started"})
        if req.method == "POST" and req.url.path == "/v1/runs"
        else (_ for _ in ()).throw(AssertionError(f"Unexpected request: {req.method} {req.url}"))
    )))
    monkeypatch.setattr("hermes_mcp_bridge.server.client", client)

    result = await server.hermes_submit(
        prompt="recoverable",
        ctx=_DummyContext(),
        client_request_id="recover",
    )
    assert result["execution_id"] == "created-run"
    warning = result.get("metadata", {}).get("warning", "")
    assert "registry record failed after run creation" in warning


@pytest.mark.asyncio
async def test_fallback_registry_returns_unknown_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _make_server_module(monkeypatch, tmp_path)
    db_path = str(tmp_path / "state.sqlite3")
    registry = RunRegistry(db_path)
    registry.initialize()
    registry.record(
        client_request_id="unknown-run",
        fingerprint="fp",
        execution_id="unknown-run",
        session_id="session",
        last_status="queued",
    )
    monkeypatch.setattr("hermes_mcp_bridge.server.registry", registry)

    async def fake_get_run(execution_id: str, **kwargs: object) -> HermesPromptResult:
        raise HermesAPIError("upstream down")

    monkeypatch.setattr(server.client, "get_run", fake_get_run)
    result = await server.hermes_status("unknown-run")
    assert result["execution_id"] == "unknown-run"
    assert result["status"] == "queued"
    assert result["metadata"].get("bridge_recovery_source") == "registry"


@pytest.mark.asyncio
async def test_health_preserves_upstream_and_adds_bridge_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _make_server_module(monkeypatch, tmp_path)
    db_path = str(tmp_path / "state.sqlite3")
    registry = RunRegistry(db_path)
    registry.initialize()
    monkeypatch.setattr("hermes_mcp_bridge.server.registry", registry)

    async def fake_health(detailed: bool = False) -> dict[str, Any]:
        return {"status": "healthy", "authenticated": True}

    monkeypatch.setattr(server.client, "health", fake_health)
    result = await server.hermes_health()
    assert result["upstream"] == {"status": "healthy", "authenticated": True}
    assert "path" not in result["bridge"]
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
        return HermesPromptResult(
            execution_id="run-1", status=RunStatus.COMPLETED
        )

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
        return HermesPromptResult(
            execution_id="run-1", status=RunStatus.COMPLETED
        )

    monkeypatch.setattr(server.client, "submit_prompt", fake_submit_prompt)

    result = await server.hermes_prompt(
        "task", ctx=_DummyContext(), stop_on_disconnect=True
    )
    assert result["execution_id"] == "run-1"
    assert captured.get("stop_on_cancel") is True
