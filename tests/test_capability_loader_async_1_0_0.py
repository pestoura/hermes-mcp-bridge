"""Regression tests for the async upstream-capability loader contract."""

from __future__ import annotations

import asyncio
import inspect
import threading

import hermes_mcp_bridge as package
from hermes_mcp_bridge import protocol


def test_protocol_capability_loader_is_awaitable_after_package_bootstrap() -> None:
    assert inspect.iscoroutinefunction(protocol.load_upstream_capabilities)


def test_async_adapter_preserves_payload_and_uses_worker_thread(monkeypatch) -> None:
    caller_thread = threading.get_ident()
    observed: dict[str, object] = {}

    def fake_sync_loader(
        *,
        base_url: str,
        api_key: str,
        timeout: float,
    ) -> dict[str, object]:
        observed.update(
            {
                "base_url": base_url,
                "api_key": api_key,
                "timeout": timeout,
                "thread": threading.get_ident(),
            }
        )
        return {"status": "ok", "canonical": {"source": "unit-test"}}

    monkeypatch.setattr(package, "_sync_load_upstream_capabilities", fake_sync_loader)

    result = asyncio.run(
        protocol.load_upstream_capabilities(
            base_url="http://hermes.test",
            api_key="test-key",
            timeout=2.5,
        )
    )

    assert result == {"status": "ok", "canonical": {"source": "unit-test"}}
    assert observed == {
        "base_url": "http://hermes.test",
        "api_key": "test-key",
        "timeout": 2.5,
        "thread": observed["thread"],
    }
    assert observed["thread"] != caller_thread
