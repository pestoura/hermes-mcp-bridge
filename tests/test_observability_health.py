"""Health and readiness must expose observability status without secrets."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import httpx
import pytest

SECRET_KEY = "sk-live-SUPERSECRET-0123456789"


def _make_server_module(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("HERMES_API_KEY", SECRET_KEY)
    monkeypatch.setenv("BRIDGE_STATE_DB_PATH", str(tmp_path / "state.sqlite3"))
    from hermes_mcp_bridge.config import get_settings

    get_settings.cache_clear()
    server = importlib.import_module("hermes_mcp_bridge.server")
    return importlib.reload(server)


def _mock_transport(status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/health"):
            return httpx.Response(status, json={"status": "ok"})
        return httpx.Response(404, json={"error": "unexpected"})

    return httpx.MockTransport(handler)


async def test_health_reports_observability_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _make_server_module(monkeypatch, tmp_path)
    server.client._transport_factory = lambda: _mock_transport()
    result = await server.hermes_health()
    observability = result["bridge"]["observability"]
    assert observability["logging"]["logging_mode"] in {"json", "text"}
    assert observability["metrics"]["enabled"] in {True, False}
    assert observability["metrics"]["exporter"] == "prometheus-text"
    assert observability["tracing"]["propagation"] == "w3c-traceparent"
    assert observability["metrics_registry"]["status"] == "up"


async def test_health_contains_no_secret_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _make_server_module(monkeypatch, tmp_path)
    server.client._transport_factory = lambda: _mock_transport()
    dumped = json.dumps(await server.hermes_health(), default=str)
    assert SECRET_KEY not in dumped
    assert "Bearer" not in dumped
    assert "/var/lib" not in dumped
    assert str(tmp_path) not in dumped


async def test_readiness_distinguishes_components(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _make_server_module(monkeypatch, tmp_path)
    server.client._transport_factory = lambda: _mock_transport()
    result = await server.hermes_readiness()
    components = result["components"]
    for name in (
        "upstream",
        "state_db",
        "approval_registry",
        "metrics_registry",
        "logging",
        "tracing",
        "config",
    ):
        assert name in components, name
        assert components[name]["status"] in {"ready", "not_ready"}
    assert result["status"] in {"ready", "degraded", "not_ready"}


async def test_readiness_marks_upstream_degraded_when_unreachable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _make_server_module(monkeypatch, tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    server.client._transport_factory = lambda: httpx.MockTransport(handler)
    result = await server.hermes_readiness()
    assert result["components"]["upstream"]["status"] == "not_ready"
    assert result["components"]["upstream"]["reason"] == "unreachable"
    assert result["components"]["state_db"]["status"] == "ready"
    assert result["status"] == "degraded"


async def test_readiness_contains_no_secrets_or_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BRIDGE_METRICS_TOKEN", "metrics-token-value")
    server = _make_server_module(monkeypatch, tmp_path)
    server.client._transport_factory = lambda: _mock_transport()
    dumped = json.dumps(await server.hermes_readiness(), default=str)
    assert SECRET_KEY not in dumped
    assert "metrics-token-value" not in dumped
    assert str(tmp_path) not in dumped
    assert "sqlite3" not in dumped
    assert '"api_key_configured": true' in dumped.lower()


async def test_readiness_does_not_run_integrity_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Readiness must stay cheap: no PRAGMA integrity_check anywhere in the path."""

    import inspect

    server = _make_server_module(monkeypatch, tmp_path)
    server.client._transport_factory = lambda: _mock_transport()
    await server.hermes_readiness()

    from hermes_mcp_bridge import registry

    source = inspect.getsource(server.hermes_readiness)
    assert "PRAGMA integrity_check" not in source
    assert "integrity_check" not in inspect.getsource(registry.RunRegistry.health)
