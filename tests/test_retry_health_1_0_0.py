"""Health evidence for the 1.0.0 selective retry gate."""

from __future__ import annotations

import json

from hermes_mcp_bridge.config import get_settings
from hermes_mcp_bridge.observability import observability_health


def test_retry_health_is_disabled_by_default_and_secret_free(monkeypatch) -> None:
    secret = "health-test-api-key-private-value"
    monkeypatch.setenv("HERMES_API_KEY", secret)
    monkeypatch.delenv("BRIDGE_RETRY_ENABLED", raising=False)
    get_settings.cache_clear()

    try:
        retry = observability_health()["retry"]
    finally:
        get_settings.cache_clear()

    assert retry == {
        "status": "ready",
        "enabled": False,
        "max_attempts": 1,
        "safe_endpoint_classes": ["health", "runs", "sessions"],
        "mutations_retryable": False,
        "sse_retryable": False,
    }
    dumped = json.dumps(retry)
    assert secret not in dumped
    assert "http://" not in dumped
    assert "/v1/" not in dumped


def test_retry_health_reports_enabled_without_exposing_settings(monkeypatch) -> None:
    monkeypatch.setenv("HERMES_API_KEY", "health-test-key")
    monkeypatch.setenv("BRIDGE_RETRY_ENABLED", "true")
    monkeypatch.setenv("BRIDGE_RETRY_MAX_ATTEMPTS", "4")
    get_settings.cache_clear()

    try:
        retry = observability_health()["retry"]
    finally:
        get_settings.cache_clear()

    assert retry["status"] == "ready"
    assert retry["enabled"] is True
    assert retry["max_attempts"] == 4
    assert retry["mutations_retryable"] is False
    assert retry["sse_retryable"] is False
    assert "base_seconds" not in retry
    assert "jitter" not in retry
