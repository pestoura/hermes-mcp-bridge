"""Edge coverage for the 1.0.0 selective upstream retry policy."""

from __future__ import annotations

import random
from collections.abc import Callable

import httpx
import pytest
from pydantic import SecretStr

from hermes_mcp_bridge.client import HermesClient
from hermes_mcp_bridge.config import Settings


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "hermes_api_key": SecretStr("test-key"),
        "hermes_api_base_url": "http://hermes.test",
        "hermes_request_timeout_seconds": 1.0,
        "bridge_retry_enabled": True,
        "bridge_retry_max_attempts": 2,
        "bridge_retry_base_seconds": 0.25,
        "bridge_retry_max_seconds": 1.0,
        "bridge_retry_jitter_ratio": 0.0,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    delays: list[float],
) -> HermesClient:
    async def sleep(delay: float) -> None:
        delays.append(delay)

    return HermesClient(
        _settings(),
        transport_factory=lambda: httpx.MockTransport(handler),
        retry_sleep=sleep,
        retry_rng=random.Random(1),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [429, 500, 502, 503, 504])
async def test_every_allowed_transient_status_retries_once_then_succeeds(
    status_code: int,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                status_code,
                json={"error": {"message": "temporary"}},
            )
        return httpx.Response(200, json={"status": "healthy"})

    delays: list[float] = []
    result = await _client(handler, delays).health()

    assert result == {"status": "healthy"}
    assert calls == 2
    assert delays == [0.25]


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_after", ["not-a-delay", "-1", "301"])
async def test_invalid_or_excessive_retry_after_uses_configured_backoff(
    retry_after: str,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": retry_after},
                json={"error": {"message": "busy"}},
            )
        return httpx.Response(200, json={"status": "healthy"})

    delays: list[float] = []
    result = await _client(handler, delays).health()

    assert result["status"] == "healthy"
    assert calls == 2
    assert delays == [0.25]


@pytest.mark.asyncio
async def test_valid_maximum_retry_after_is_globally_bounded() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "300"},
                json={"error": {"message": "busy"}},
            )
        return httpx.Response(200, json={"status": "healthy"})

    delays: list[float] = []
    result = await _client(handler, delays).health()

    assert result["status"] == "healthy"
    assert calls == 2
    assert delays == [300.0]
