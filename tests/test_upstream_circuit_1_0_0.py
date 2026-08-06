"""Directed tests for the 1.0.0 upstream circuit-breaker gate."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from pydantic import SecretStr

from hermes_mcp_bridge.client import HermesAPIError, HermesClient
from hermes_mcp_bridge.config import Settings, get_settings
from hermes_mcp_bridge.observability import observability_health
from hermes_mcp_bridge.resilience import ManualClock
from hermes_mcp_bridge.resilience.circuit import reset_breakers


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "hermes_api_key": SecretStr("test-key"),
        "hermes_api_base_url": "http://hermes.test",
        "hermes_request_timeout_seconds": 1.0,
        "bridge_retry_enabled": False,
        "bridge_circuit_enabled": True,
        "bridge_circuit_failure_threshold": 2,
        "bridge_circuit_recovery_seconds": 10.0,
        "bridge_circuit_half_open_max_calls": 1,
        "bridge_circuit_success_threshold": 1,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    configured: Settings | None = None,
    clock: ManualClock | None = None,
) -> HermesClient:
    return HermesClient(
        configured or _settings(),
        transport_factory=lambda: httpx.MockTransport(handler),
        circuit_clock=clock,
    )


def setup_function() -> None:
    reset_breakers()


def teardown_function() -> None:
    reset_breakers()
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_circuit_disabled_preserves_one_request_and_no_breaker() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"error": {"message": "down"}})

    client = _client(handler, configured=_settings(bridge_circuit_enabled=False))
    with pytest.raises(HermesAPIError, match="HTTP 503"):
        await client.health()

    assert calls == 1
    assert client.circuit_posture()["enabled"] is False
    assert client.circuit_posture()["breakers"] == []


@pytest.mark.asyncio
async def test_transient_failures_open_then_reject_without_network() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"error": {"message": "down"}})

    client = _client(handler)
    for _ in range(2):
        with pytest.raises(HermesAPIError, match="HTTP 503"):
            await client.health()

    with pytest.raises(HermesAPIError, match="circuit open"):
        await client.health()

    assert calls == 2
    posture = client.circuit_posture()
    assert posture["breakers"] == [
        {
            "name": "health",
            "state": "open",
            "failures": 0,
            "successes": 0,
            "transitions": 1,
            "rejections": 1,
        }
    ]


@pytest.mark.asyncio
async def test_retry_attempts_count_as_one_logical_circuit_failure() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"error": {"message": "down"}})

    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    configured = _settings(
        bridge_retry_enabled=True,
        bridge_retry_max_attempts=3,
        bridge_retry_base_seconds=0.01,
        bridge_retry_jitter_ratio=0.0,
        bridge_circuit_failure_threshold=1,
    )
    client = HermesClient(
        configured,
        transport_factory=lambda: httpx.MockTransport(handler),
        retry_sleep=sleep,
        circuit_clock=ManualClock(),
    )

    with pytest.raises(HermesAPIError, match="HTTP 503"):
        await client.health()
    with pytest.raises(HermesAPIError, match="circuit open"):
        await client.health()

    assert calls == 3
    assert delays == [0.01, 0.02]
    assert client.circuit_posture()["breakers"][0]["transitions"] == 1


@pytest.mark.asyncio
async def test_permanent_404_proves_reachability_and_does_not_open() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404, json={"error": {"message": "not found"}})

    client = _client(handler, configured=_settings(bridge_circuit_failure_threshold=1))
    for _ in range(3):
        with pytest.raises(HermesAPIError, match="HTTP 404"):
            await client.get_run("run-404")

    assert calls == 3
    assert client.circuit_posture()["breakers"][0]["state"] == "closed"


@pytest.mark.asyncio
async def test_mutating_post_is_never_circuit_protected() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.method == "POST"
        raise httpx.ConnectError("ambiguous", request=request)

    client = _client(handler, configured=_settings(bridge_circuit_failure_threshold=1))
    for _ in range(2):
        with pytest.raises(HermesAPIError, match="Unable to reach"):
            await client.submit_prompt(prompt="task", wait_seconds=0)

    assert calls == 2
    assert client.circuit_posture()["breakers"] == []


@pytest.mark.asyncio
async def test_half_open_probe_recovers_the_same_breaker() -> None:
    calls = 0
    clock = ManualClock()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, json={"error": {"message": "down"}})
        return httpx.Response(200, json={"status": "healthy"})

    client = _client(
        handler,
        configured=_settings(bridge_circuit_failure_threshold=1),
        clock=clock,
    )

    with pytest.raises(HermesAPIError, match="HTTP 503"):
        await client.health()
    with pytest.raises(HermesAPIError, match="circuit open"):
        await client.health()

    clock.advance(10.0)
    assert await client.health() == {"status": "healthy"}
    assert calls == 2
    snapshot = client.circuit_posture()["breakers"][0]
    assert snapshot["state"] == "closed"
    assert snapshot["transitions"] == 3


def test_observability_health_reports_sanitized_circuit_posture(monkeypatch) -> None:
    monkeypatch.setenv("HERMES_API_KEY", "private-health-key")
    monkeypatch.setenv("BRIDGE_CIRCUIT_ENABLED", "true")
    get_settings.cache_clear()

    circuit = observability_health()["circuit_breaker"]
    serialized = str(circuit)

    assert circuit["status"] == "ready"
    assert circuit["enabled"] is True
    assert circuit["safe_endpoint_classes"] == ["health", "runs", "sessions"]
    assert circuit["mutations_protected"] is False
    assert circuit["sse_protected"] is False
    assert "private-health-key" not in serialized
    assert "http://" not in serialized
