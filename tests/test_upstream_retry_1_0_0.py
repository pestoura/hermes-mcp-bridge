"""Directed tests for the 1.0.0 selective upstream retry gate."""

from __future__ import annotations

import json
import random
from collections.abc import Callable

import httpx
import pytest
from pydantic import SecretStr

from hermes_mcp_bridge.client import HermesAPIError, HermesClient
from hermes_mcp_bridge.config import Settings
from hermes_mcp_bridge.observability.metrics import get_registry, render_prometheus
from hermes_mcp_bridge.resilience.http_retry import (
    classify_retry_target,
    is_transient_status,
    policy_from_settings,
    retry_posture,
)


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "hermes_api_key": SecretStr("test-key"),
        "hermes_api_base_url": "http://hermes.test",
        "hermes_request_timeout_seconds": 1.0,
        "hermes_run_poll_interval_seconds": 0.001,
        "hermes_run_default_wait_seconds": 0.0,
        "hermes_run_max_wait_seconds": 1.0,
        "bridge_retry_enabled": True,
        "bridge_retry_max_attempts": 3,
        "bridge_retry_base_seconds": 0.01,
        "bridge_retry_max_seconds": 1.0,
        "bridge_retry_jitter_ratio": 0.0,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def client_for(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    configured: Settings | None = None,
    delays: list[float] | None = None,
) -> HermesClient:
    recorded = delays if delays is not None else []

    async def sleep(delay: float) -> None:
        recorded.append(delay)

    return HermesClient(
        configured or settings(),
        transport_factory=lambda: httpx.MockTransport(handler),
        retry_sleep=sleep,
        retry_rng=random.Random(1),
    )


def setup_function() -> None:
    get_registry().reset()


def teardown_function() -> None:
    get_registry().reset()


def test_retry_classifier_is_exact_and_fail_closed() -> None:
    assert classify_retry_target("GET", "/health").endpoint_class == "health"
    assert classify_retry_target("GET", "/health/detailed").endpoint_class == "health"
    assert classify_retry_target("GET", "/v1/runs/run-1").endpoint_class == "runs"
    assert (
        classify_retry_target("GET", "/api/sessions/session-1/messages").endpoint_class
        == "sessions"
    )

    for method, path in (
        ("POST", "/api/sessions"),
        ("POST", "/v1/runs"),
        ("POST", "/v1/runs/run-1/stop"),
        ("GET", "/v1/runs/run-1/events"),
        ("GET", "/v1/runs"),
        ("GET", "/api/sessions/session-1"),
        ("DELETE", "/v1/runs/run-1"),
        ("GET", "/unknown"),
    ):
        assert classify_retry_target(method, path) is None


def test_transient_status_allow_list_is_bounded() -> None:
    for status in (429, 500, 502, 503, 504):
        assert is_transient_status(status)
    for status in (200, 400, 401, 403, 404, 409, 422, 501):
        assert not is_transient_status(status)


@pytest.mark.asyncio
async def test_retry_disabled_keeps_exactly_one_request() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("temporary", request=request)

    delays: list[float] = []
    client = client_for(
        handler,
        configured=settings(bridge_retry_enabled=False),
        delays=delays,
    )

    with pytest.raises(HermesAPIError, match="timed out"):
        await client.health()

    assert calls == 1
    assert delays == []


@pytest.mark.asyncio
async def test_safe_health_get_retries_timeout_then_succeeds() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("temporary", request=request)
        return httpx.Response(200, json={"status": "healthy"})

    delays: list[float] = []
    client = client_for(handler, delays=delays)
    result = await client.health()

    assert result == {"status": "healthy"}
    assert calls == 2
    assert delays == [0.01]
    metrics = get_registry().render()
    assert 'bridge_upstream_retries_total{endpoint_class="health",reason="timeout"} 1' in metrics


@pytest.mark.asyncio
async def test_safe_get_honours_bounded_retry_after() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, json={"error": "busy"})
        return httpx.Response(200, json={"status": "healthy"})

    delays: list[float] = []
    client = client_for(handler, delays=delays)
    result = await client.health()

    assert result["status"] == "healthy"
    assert calls == 2
    assert delays == [2.0]


@pytest.mark.asyncio
async def test_safe_run_get_retries_503_then_succeeds_without_identifier_metrics() -> None:
    calls = 0
    execution_id = "run-private-123"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, json={"error": {"message": "temporarily unavailable"}})
        return httpx.Response(
            200,
            json={"run_id": execution_id, "status": "completed", "output": "ok"},
        )

    client = client_for(handler)
    result = await client.get_run(execution_id)

    assert result.execution_id == execution_id
    assert result.status.value == "completed"
    rendered = render_prometheus()
    assert execution_id not in rendered
    assert 'endpoint_class="runs"' in rendered
    assert 'reason="http_error"' in rendered


@pytest.mark.asyncio
async def test_404_is_not_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404, json={"error": {"message": "not found"}})

    client = client_for(handler)
    with pytest.raises(HermesAPIError, match="HTTP 404"):
        await client.get_run("run-404")
    assert calls == 1


@pytest.mark.asyncio
async def test_session_creation_post_is_never_retried_after_transport_error() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.method == "POST"
        assert request.url.path == "/api/sessions"
        raise httpx.ConnectError("ambiguous", request=request)

    client = client_for(handler)
    with pytest.raises(HermesAPIError, match="Unable to reach"):
        await client.submit_prompt(prompt="task", wait_seconds=0)
    assert calls == 1


@pytest.mark.asyncio
async def test_run_submission_post_is_never_retried_after_503() -> None:
    run_posts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal run_posts
        if request.method == "GET" and request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"session_id": "session-1", "data": []})
        if request.method == "POST" and request.url.path == "/v1/runs":
            run_posts += 1
            return httpx.Response(503, json={"error": {"message": "ambiguous"}})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    client = client_for(handler)
    with pytest.raises(HermesAPIError, match="HTTP 503"):
        await client.submit_prompt(
            prompt="task",
            session_id="session-1",
            wait_seconds=0,
        )
    assert run_posts == 1


@pytest.mark.asyncio
async def test_stop_post_is_never_retried_after_timeout() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.method == "POST"
        assert request.url.path == "/v1/runs/run-1/stop"
        raise httpx.ReadTimeout("ambiguous", request=request)

    client = client_for(handler)
    with pytest.raises(HermesAPIError, match="timed out"):
        await client.stop_run("run-1")
    assert calls == 1


@pytest.mark.asyncio
async def test_retry_attempts_are_strictly_bounded() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"error": {"message": "down"}})

    delays: list[float] = []
    client = client_for(
        handler,
        configured=settings(bridge_retry_max_attempts=3),
        delays=delays,
    )

    with pytest.raises(HermesAPIError, match="HTTP 503"):
        await client.health()

    assert calls == 3
    assert delays == [0.01, 0.02]


def test_retry_posture_is_secret_free_and_mutations_remain_disabled() -> None:
    configured = settings()
    policy = policy_from_settings(configured)
    posture = retry_posture(configured)

    assert policy.enabled is True
    assert policy.max_attempts == 3
    assert posture == {
        "enabled": True,
        "max_attempts": 3,
        "safe_endpoint_classes": ["health", "runs", "sessions"],
        "mutations_retryable": False,
        "sse_retryable": False,
    }
    serialized = json.dumps(posture)
    assert "test-key" not in serialized
    assert "http://hermes.test" not in serialized
