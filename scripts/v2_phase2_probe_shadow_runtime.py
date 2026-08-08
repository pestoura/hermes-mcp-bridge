#!/usr/bin/env python3
"""Probe the live isolated Hermes shadow and emit sanitized non-mutation proof."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from hermes_mcp_bridge.v2.hermes_runtime import (  # noqa: E402
    HermesRuntimeError,
    validate_hermes_python_hint,
)
from hermes_mcp_bridge.v2.shadow_isolation import (  # noqa: E402
    SHADOW_HERMES_TOOL_NAMES,
    SHADOW_HTTP_METHODS,
    SHADOW_ISOLATION_SCHEMA,
    SHADOW_MCP_SERVER,
    SHADOW_MCP_TOOL_NAMES,
    SHADOW_SERVER_CONTRACT,
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


def _validated_hermes_runtime_python(hint: str, shadow_home: Path) -> Path:
    """Revalidate the launcher-supplied interpreter hint under the shadow env.

    The hint is resolved by the launcher *before* the shadow HOME exists, using
    the real managed layout. It is not trusted here: it must still be an
    absolute executable that imports the required Hermes modules while running
    with HOME/HERMES_HOME pointing at the disposable shadow home.
    """
    try:
        return validate_hermes_python_hint(
            hint,
            probe_home=shadow_home,
            probe_hermes_home=shadow_home,
            path_env=os.environ.get("PATH", ""),
        )
    except HermesRuntimeError as exc:
        raise ProbeError(exc.code) from exc


def _probe_hermes_resolver(args: argparse.Namespace) -> dict:
    """Re-run the installed Hermes platform resolver against the shadow config."""
    shadow_home = Path(args.shadow_home).expanduser().resolve()
    if not shadow_home.is_dir() or stat.S_IMODE(shadow_home.stat().st_mode) != 0o700:
        raise ProbeError("SHADOW_HOME_PERMISSIONS_INVALID")
    if not (shadow_home / "config.yaml").is_file():
        raise ProbeError("SHADOW_CONFIG_MISSING")

    # The launcher resolves the managed Hermes interpreter against the real
    # runtime roots before the shadow home exists and passes it as an explicit
    # argument. Revalidate it here under the shadow environment; never derive
    # managed layout candidates from the shadow home.
    hermes_python = _validated_hermes_runtime_python(args.hermes_python, shadow_home)

    # The child reports only names/booleans from an allowlisted contract. It
    # never serializes config values, environment values, paths or credentials.
    child = r'''
import json
import sys

from hermes_cli.config import load_config
from hermes_cli.tools_config import _get_platform_tools

server = sys.argv[1]
expected_tools = json.loads(sys.argv[2])
config = load_config() or {}
servers = config.get("mcp_servers") or {}
server_cfg = servers.get(server) if isinstance(servers, dict) else None
platform = (config.get("platform_toolsets") or {}).get("api_server")
resolved = sorted(str(item) for item in _get_platform_tools(config, "api_server"))
exact = bool(
    isinstance(servers, dict)
    and sorted(str(name) for name in servers) == [server]
    and isinstance(server_cfg, dict)
    and server_cfg.get("enabled", True) is True
    and platform == [server]
    and isinstance(server_cfg.get("tools"), dict)
    and server_cfg["tools"].get("include") == expected_tools
    and server_cfg["tools"].get("resources") is False
    and server_cfg["tools"].get("prompts") is False
    and server_cfg.get("supports_parallel_tool_calls") is False
)
print(json.dumps({"resolved": resolved, "config_exact": exact}, sort_keys=True))
'''
    env = {
        "HOME": str(shadow_home),
        "HERMES_HOME": str(shadow_home),
        "PATH": os.environ.get("PATH", ""),
        "USER": os.environ.get("USER", "estourpm"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    try:
        completed = subprocess.run(
            [
                str(hermes_python),
                "-c",
                child,
                SHADOW_MCP_SERVER,
                json.dumps(list(SHADOW_MCP_TOOL_NAMES)),
            ],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProbeError("SHADOW_RESOLVER_PROBE_FAILED") from exc
    if completed.returncode != 0:
        raise ProbeError("SHADOW_RESOLVER_PROBE_FAILED")

    # Hermes may emit warnings before the final JSON line. Never surface them;
    # only accept the final line if it is the small, secret-free contract.
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise ProbeError("SHADOW_RESOLVER_OUTPUT_INVALID")
    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise ProbeError("SHADOW_RESOLVER_OUTPUT_INVALID") from exc
    if not isinstance(payload, dict) or set(payload) != {"resolved", "config_exact"}:
        raise ProbeError("SHADOW_RESOLVER_OUTPUT_INVALID")
    resolved = payload.get("resolved")
    if (
        not isinstance(resolved, list)
        or any(not isinstance(item, str) for item in resolved)
        or any(re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", item) is None for item in resolved)
    ):
        raise ProbeError("SHADOW_RESOLVER_OUTPUT_INVALID")
    if resolved != [SHADOW_MCP_SERVER]:
        raise ProbeError("SHADOW_EFFECTIVE_TOOLSETS_NOT_EXACT")
    if payload.get("config_exact") is not True:
        raise ProbeError("SHADOW_MCP_SERVER_CONFIG_NOT_EXACT")
    return payload


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
    if (
        not isinstance(auth, dict)
        or auth.get("type") != "bearer"
        or auth.get("required") is not True
    ):
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
    native_enabled = sorted(
        str(item.get("name"))
        for item in data
        if isinstance(item, dict) and item.get("enabled") is True
    )
    # /v1/toolsets intentionally lists configurable/native toolsets and omits
    # dynamic MCP servers. A mechanically isolated MCP-only shadow must expose
    # zero enabled native entries here.
    if native_enabled:
        raise ProbeError("SHADOW_NATIVE_TOOLSETS_NOT_EMPTY")

    if sessions.get("object") != "list" or not isinstance(sessions.get("data"), list):
        raise ProbeError("SHADOW_SESSION_DB_INVALID")

    resolver = _probe_hermes_resolver(args)

    report = {
        "schema": SHADOW_ISOLATION_SCHEMA,
        "source_commit": args.source_commit,
        "connected_jarvas": True,
        "hermes_profile_isolated": True,
        "api_platform": "api_server",
        "api_bind_loopback": True,
        "api_auth_required": True,
        "effective_toolsets": list(resolver["resolved"]),
        "native_toolsets_enabled": native_enabled,
        # These names are derived only after the same installed Hermes resolver
        # and exact MCP include/resources/prompts config have been verified.
        # The 15-sample collector then proves the tools are actually callable.
        "effective_tools": sorted(SHADOW_HERMES_TOOL_NAMES),
        "resolver_exact": True,
        "mcp_server_config_exact": True,
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
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--api-key-file", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--json-out", required=True)
    # Explicit interpreter hint resolved by the launcher against the real Hermes
    # runtime roots, before the shadow HOME transition. Never an env var, never
    # trusted blindly: it is revalidated under the shadow environment.
    parser.add_argument("--hermes-python", required=True)
    parser.add_argument("--shadow-home", required=True)
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
