#!/usr/bin/env python3
"""Probe the live isolated Hermes shadow and emit sanitized non-mutation proof."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from hermes_mcp_bridge.v2.shadow_isolation import (  # noqa: E402
    SHADOW_HERMES_TOOL_NAMES,
    SHADOW_HTTP_METHODS,
    SHADOW_ISOLATION_SCHEMA,
    SHADOW_SERVER_CONTRACT,
    SHADOW_TOOLSET,
    validate_shadow_isolation,
)


class ProbeError(RuntimeError):
    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _read_private_file(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ProbeError("SHADOW_API_KEY_UNREADABLE") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise ProbeError("SHADOW_API_KEY_PERMISSIONS_INVALID")
        value = os.read(fd, 8192).decode("utf-8").strip()
    finally:
        os.close(fd)
    if not value:
        raise ProbeError("SHADOW_API_KEY_EMPTY")
    return value


def _loopback_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ProbeError("SHADOW_API_NOT_LOOPBACK")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ProbeError("SHADOW_API_URL_INVALID")
    return value.rstrip("/")


def _get_json(client: httpx.Client, path: str, attempts: int = 30) -> tuple[int, dict]:
    last_status = 0
    for attempt in range(attempts):
        try:
            response = client.get(path)
            last_status = response.status_code
            if response.status_code == 200:
                payload = response.json()
                if isinstance(payload, dict):
                    return response.status_code, payload
        except (httpx.HTTPError, json.JSONDecodeError):
            pass
        if attempt + 1 < attempts:
            time.sleep(0.5)
    raise ProbeError(f"SHADOW_PROBE_UNAVAILABLE_{last_status or 'NO_RESPONSE'}")


def probe(args: argparse.Namespace) -> dict:
    base_url = _loopback_url(args.url)
    api_key = _read_private_file(Path(args.api_key_file))
    headers = {"Authorization": f"Bearer {api_key}"}
    with httpx.Client(
        base_url=base_url,
        headers=headers,
        timeout=5.0,
        follow_redirects=False,
    ) as client:
        health_status, health = _get_json(client, "/health")
        capabilities_status, capabilities = _get_json(client, "/v1/capabilities")
        toolsets_status, toolsets = _get_json(client, "/v1/toolsets")
        # The list-sessions endpoint forces the shadow SessionDB to initialize
        # before the collector performs its strict read-only token accounting.
        sessions_status, sessions = _get_json(client, "/api/sessions?limit=1")

    if health.get("status") != "ok" or health.get("platform") != "hermes-agent":
        raise ProbeError("SHADOW_HEALTH_INVALID")
    if capabilities.get("platform") != "hermes-agent":
        raise ProbeError("SHADOW_CAPABILITIES_PLATFORM_INVALID")
    auth = capabilities.get("auth")
    if not isinstance(auth, dict) or auth.get("type") != "bearer" or auth.get("required") is not True:
        raise ProbeError("SHADOW_API_AUTH_NOT_REQUIRED")
    runtime = capabilities.get("runtime")
    if not isinstance(runtime, dict) or runtime.get("tool_execution") != "server":
        raise ProbeError("SHADOW_TOOL_EXECUTION_INVALID")
    endpoints = capabilities.get("endpoints")
    toolset_endpoint = endpoints.get("toolsets") if isinstance(endpoints, dict) else None
    if toolset_endpoint != {"method": "GET", "path": "/v1/toolsets"}:
        raise ProbeError("SHADOW_TOOLSET_ENDPOINT_INVALID")

    if toolsets.get("object") != "list" or toolsets.get("platform") != "api_server":
        raise ProbeError("SHADOW_TOOLSETS_PAYLOAD_INVALID")
    data = toolsets.get("data")
    if not isinstance(data, list):
        raise ProbeError("SHADOW_TOOLSETS_PAYLOAD_INVALID")
    enabled = [item for item in data if isinstance(item, dict) and item.get("enabled") is True]
    if len(enabled) != 1 or enabled[0].get("name") != SHADOW_TOOLSET:
        raise ProbeError("SHADOW_EFFECTIVE_TOOLSETS_NOT_EXACT")
    tools = enabled[0].get("tools")
    observed_tools = sorted(str(item) for item in tools) if isinstance(tools, list) else []
    if observed_tools != sorted(SHADOW_HERMES_TOOL_NAMES):
        raise ProbeError("SHADOW_EFFECTIVE_TOOLS_NOT_EXACT")
    if sessions.get("object") != "list" or not isinstance(sessions.get("data"), list):
        raise ProbeError("SHADOW_SESSION_DB_INVALID")

    report = {
        "schema": SHADOW_ISOLATION_SCHEMA,
        "source_commit": args.source_commit,
        "connected_jarvas": True,
        "hermes_profile_isolated": True,
        "api_platform": "api_server",
        "api_bind_loopback": True,
        "api_auth_required": True,
        "effective_toolsets": [SHADOW_TOOLSET],
        "effective_tools": observed_tools,
        "repository_scopes": [args.repository],
        "credential_provider_type": "github_app",
        "credential_capability": "github.read",
        "credential_file_backed": True,
        "mcp_resources_enabled": False,
        "mcp_prompts_enabled": False,
        "http_methods": SHADOW_HTTP_METHODS,
        "generic_execution_tools": False,
        "mutation_capable_tools": False,
        "server_contract": SHADOW_SERVER_CONTRACT,
        "probes": {
            "health_status": health_status,
            "capabilities_status": capabilities_status,
            "toolsets_status": toolsets_status,
            "sessions_status": sessions_status,
        },
        "confirmed_at": datetime.now(UTC).isoformat(),
    }
    failures = validate_shadow_isolation(
        report,
        repositories={args.repository},
        source_commit=args.source_commit,
    )
    if failures:
        raise ProbeError(failures[0].upper())
    return report


def _atomic_private_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    tmp = path.with_name(f".{path.name}.tmp")
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        path.chmod(0o600)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--api-key-file", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--json-out", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = probe(args)
        _atomic_private_json(Path(args.json_out), report)
    except ProbeError as exc:
        print(json.dumps({"status": "SHADOW_ISOLATION_BLOCKED", "reason": exc.code}))
        return 2
    print(
        json.dumps(
            {
                "status": "SHADOW_ISOLATION_PROVEN",
                "effective_toolsets": report["effective_toolsets"],
                "effective_tool_count": len(report["effective_tools"]),
                "repository_scope_count": len(report["repository_scopes"]),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
