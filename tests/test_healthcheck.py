"""Healthcheck contract tests."""

from __future__ import annotations

import sys

import pytest

MODULE_NAME = "hermes_mcp_bridge.healthcheck"


def _make_settings(**overrides):
    from pydantic import SecretStr

    from hermes_mcp_bridge.config import Settings

    values = {
        "hermes_api_base_url": "http://127.0.0.1:8642",
        "hermes_api_key": SecretStr("unit-test-key-0123456789"),
        "hermes_model": "hermes-agent",
        "hermes_request_timeout_seconds": 30.0,
        "hermes_run_poll_interval_seconds": 1.0,
        "hermes_run_max_wait_seconds": 7200.0,
        "hermes_progress_interval_seconds": 15.0,
        "hermes_event_stream_connect_timeout_seconds": 30.0,
        "mcp_host": "127.0.0.1",
        "mcp_port": 8765,
        "mcp_path": "/mcp",
        "log_level": "INFO",
        "bridge_state_db_path": "/tmp/hermes-mcp-bridge-test-state.sqlite3",
    }
    values.update(overrides)
    return Settings(**values)


def _load_module():
    if MODULE_NAME in sys.modules:
        del sys.modules[MODULE_NAME]
    return __import__(MODULE_NAME, fromlist=["main"])


def test_success_path(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    settings = _make_settings(
        bridge_state_db_path="/tmp/hermes-mcp-bridge-state-test-success.sqlite3"
    )
    module = _load_module()
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(module, "_tcp_mcp", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "_http_health", lambda *args, **kwargs: {"status": "ok"})
    monkeypatch.setattr(module, "_registry_status", lambda *args, **kwargs: {"status": "up"})
    with pytest.raises(SystemExit) as exc_info:
        module.main()
    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == "OK"


def test_tcp_failure_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    settings = _make_settings(bridge_state_db_path="/tmp/hermes-mcp-bridge-state-test-tcp.sqlite3")
    module = _load_module()
    monkeypatch.setattr(module, "get_settings", lambda: settings)

    def _tcp(*args, **kwargs):
        module._fail("MCP endpoint unreachable")

    monkeypatch.setattr(module, "_tcp_mcp", _tcp)
    with pytest.raises(SystemExit) as exc_info:
        module.main()
    assert exc_info.value.code == 1
    assert capsys.readouterr().out.strip() == "FAIL: MCP endpoint unreachable"


def test_http_failure_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    settings = _make_settings(bridge_state_db_path="/tmp/hermes-mcp-bridge-state-test-http.sqlite3")
    module = _load_module()
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(module, "_tcp_mcp", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        module,
        "_http_health",
        lambda *args, **kwargs: module._fail("Hermes API health unreachable"),
    )
    with pytest.raises(SystemExit) as exc_info:
        module.main()
    assert exc_info.value.code == 1
    assert capsys.readouterr().out.strip() == "FAIL: Hermes API health unreachable"


def test_registry_failure_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    settings = _make_settings(
        bridge_state_db_path="/tmp/hermes-mcp-bridge-state-test-registry.sqlite3"
    )
    module = _load_module()
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(module, "_tcp_mcp", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "_http_health", lambda *args, **kwargs: {"status": "ok"})
    monkeypatch.setattr(
        module,
        "_registry_status",
        lambda *args, **kwargs: module._fail("state registry unavailable"),
    )
    with pytest.raises(SystemExit) as exc_info:
        module.main()
    assert exc_info.value.code == 1
    assert capsys.readouterr().out.strip() == "FAIL: state registry unavailable"


def test_timeout_capped_to_three_seconds(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    settings = _make_settings(
        hermes_request_timeout_seconds=60,
        bridge_state_db_path="/tmp/hermes-mcp-bridge-state-test-timeout.sqlite3",
    )
    module = _load_module()
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    captured: dict[str, float] = {}

    def fake_tcp(host: str, port: int, timeout: float) -> None:
        captured["tcp"] = float(timeout)

    monkeypatch.setattr(module, "_tcp_mcp", fake_tcp)

    def fake_http(base_url, api_key, timeout, **kwargs):
        captured["http"] = float(timeout)
        return {"status": "ok"}

    monkeypatch.setattr(module, "_http_health", fake_http)
    monkeypatch.setattr(module, "_registry_status", lambda *args, **kwargs: {"status": "up"})
    with pytest.raises(SystemExit) as exc_info:
        module.main()
    assert exc_info.value.code == 0
    assert captured["tcp"] <= 3.0
    assert captured["http"] <= 3.0
    assert capsys.readouterr().out.strip() == "OK"


def test_sanitized_output_contains_no_secrets(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    settings = _make_settings(
        bridge_state_db_path="/tmp/hermes-mcp-bridge-state-test-sanitized.sqlite3"
    )
    module = _load_module()
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(module, "_tcp_mcp", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "_http_health", lambda *args, **kwargs: {"status": "ok"})
    monkeypatch.setattr(module, "_registry_status", lambda *args, **kwargs: {"status": "down"})
    with pytest.raises(SystemExit):
        module.main()
    out = capsys.readouterr().out
    assert "unit-test-key-0123456789" not in out
    assert "/tmp/hermes-mcp-bridge-state-test-sanitized.sqlite3" not in out
