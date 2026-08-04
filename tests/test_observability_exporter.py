"""Prometheus exporter endpoint: content type, format, auth and disabled mode."""

from __future__ import annotations

import urllib.error
import urllib.request

import pytest

from hermes_mcp_bridge.observability import exporter as exp
from hermes_mcp_bridge.observability.metrics import CONTENT_TYPE, get_metrics, get_registry


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch):
    for var in (
        exp.ENV_ENABLED,
        exp.ENV_HOST,
        exp.ENV_PORT,
        exp.ENV_ALLOW_REMOTE,
        exp.ENV_TOKEN,
    ):
        monkeypatch.delenv(var, raising=False)
    get_registry().reset()
    exp._exporter = None
    yield
    exp._exporter = None
    get_registry().reset()


def _get(url: str, headers: dict[str, str] | None = None):
    request = urllib.request.Request(url, headers=headers or {})
    return urllib.request.urlopen(request, timeout=5)


def test_disabled_by_default() -> None:
    assert exp.metrics_enabled() is False
    assert exp.start_exporter_if_enabled() is None
    assert exp.exporter_status()["enabled"] is False
    assert exp.exporter_status()["running"] is False


def test_metrics_endpoint_serves_prometheus(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(exp.ENV_ENABLED, "1")
    get_metrics().tool_calls_total.inc(tool="hermes_prompt", outcome="success")
    exporter = exp.MetricsExporter(host="127.0.0.1", port=0).start()
    try:
        response = _get(f"http://127.0.0.1:{exporter.port}/metrics")
        body = response.read().decode()
        assert response.status == 200
        assert response.headers["Content-Type"] == CONTENT_TYPE
        assert response.headers["Cache-Control"] == "no-store"
        assert "# TYPE bridge_tool_calls_total counter" in body
        assert 'bridge_tool_calls_total{outcome="success",tool="hermes_prompt"} 1.0' in body
    finally:
        exporter.stop()


def test_unknown_paths_are_404_and_healthz_is_cheap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(exp.ENV_ENABLED, "1")
    exporter = exp.MetricsExporter(host="127.0.0.1", port=0).start()
    try:
        assert _get(f"http://127.0.0.1:{exporter.port}/healthz").status == 200
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _get(f"http://127.0.0.1:{exporter.port}/secrets")
        assert excinfo.value.code == 404
    finally:
        exporter.stop()


def test_remote_binding_refused_without_explicit_opt_in() -> None:
    with pytest.raises(exp.MetricsExporterError):
        exp.validate_binding("0.0.0.0")


def test_remote_binding_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(exp.ENV_ALLOW_REMOTE, "1")
    with pytest.raises(exp.MetricsExporterError):
        exp.validate_binding("0.0.0.0")
    monkeypatch.setenv(exp.ENV_TOKEN, "a-token")
    exp.validate_binding("0.0.0.0")


def test_loopback_binding_is_allowed() -> None:
    exp.validate_binding("127.0.0.1")
    exp.validate_binding("::1")
    assert exp.is_loopback("localhost") is True
    assert exp.is_loopback("10.0.0.5") is False


def test_token_enforced_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(exp.ENV_ENABLED, "1")
    monkeypatch.setenv(exp.ENV_TOKEN, "s3cret-token")
    exporter = exp.MetricsExporter(host="127.0.0.1", port=0).start()
    try:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _get(f"http://127.0.0.1:{exporter.port}/metrics")
        assert excinfo.value.code == 401
        response = _get(
            f"http://127.0.0.1:{exporter.port}/metrics",
            headers={"Authorization": "Bearer s3cret-token"},
        )
        assert response.status == 200
    finally:
        exporter.stop()


def test_exporter_status_has_no_secret_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(exp.ENV_TOKEN, "super-secret-value")
    status = exp.exporter_status()
    assert "super-secret-value" not in str(status)
    assert status["auth_required"] is True
    assert status["bind_scope"] == "loopback"


def test_start_exporter_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(exp.ENV_ENABLED, "1")
    monkeypatch.setenv(exp.ENV_HOST, "203.0.113.1")  # unroutable + not loopback
    assert exp.start_exporter_if_enabled() is None
