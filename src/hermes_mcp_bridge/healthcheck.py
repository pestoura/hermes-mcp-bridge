"""Healthcheck CLI for container/compose.

Exit codes:
  0 - OK
  1 - FAIL: <category> <reason>

This module intentionally does not log secrets, request bodies, or paths.
"""

from __future__ import annotations

import socket
from urllib.parse import urljoin

import httpx

from .config import get_settings
from .registry import RunRegistry


def _fail(reason: str) -> None:
    print(f"FAIL: {reason}", flush=True)
    raise SystemExit(1)


def _ok() -> None:
    print("OK", flush=True)
    raise SystemExit(0)


def _tcp_mcp(host: str, port: int, timeout: float) -> None:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return None
    except OSError:
        _fail("MCP endpoint unreachable")


def _http_health(base_url: str, api_key: str, timeout: float) -> dict[str, object]:
    url = urljoin(base_url.rstrip("/") + "/", "health")
    headers = {"Authorization": f"Bearer {api_key}"}
    response: httpx.Response | None = None
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(url, headers=headers)
    except (httpx.HTTPError, OSError):
        _fail("Hermes API health unreachable")
    if response.status_code >= 400:
        _fail("Hermes API health unreachable")
    assert response is not None
    return response.json()


def _registry_status(db_path: str) -> dict[str, object]:
    registry = RunRegistry(db_path)
    try:
        registry.initialize()
    except Exception:
        _fail("state registry unavailable")
    return registry.health()


def main() -> None:
    settings = get_settings()
    timeout = min(3.0, float(settings.hermes_request_timeout_seconds))
    _tcp_mcp(settings.mcp_host, settings.mcp_port, timeout)
    api_key = str(settings.hermes_api_key.get_secret_value())
    _http_health(settings.hermes_api_base_url, api_key, timeout)
    registry_health = _registry_status(settings.bridge_state_db_path)
    if str(registry_health.get("status", "down")) != "up":
        _fail("state registry unavailable")
    _ok()


if __name__ == "__main__":
    main()
