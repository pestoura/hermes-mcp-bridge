#!/usr/bin/env python3
"""Run the 1.0.0 candidate in an isolated Docker acceptance stack.

The harness creates uniquely named containers, network and volumes. It never
uses host networking, production container names, production paths or RITMO.
Only read-only MCP tools are called. All resources are removed before exit.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from hermes_mcp_bridge.contracts import (
    CURRENT_CONTRACT_VERSION,
    SCHEMA_VERSION,
    expected_tool_count,
    validate_tools,
)

API_KEY = "isolated-api-key-not-secret-0123456789"
HMAC_KEY = "isolated-hmac-key-not-secret-0123456789"
HMAC_KEY_ID = "isolated-current-key"
EXPECTED_VERSION = "1.0.0"
EXPECTED_SCHEMA = "0.6.1"


class AcceptanceError(RuntimeError):
    """The isolated candidate failed one acceptance invariant."""


def _redact(text: str) -> str:
    safe = text.replace(API_KEY, "[REDACTED]").replace(HMAC_KEY, "[REDACTED]")
    return safe[:2000]


def _run(
    args: list[str],
    *,
    check: bool = True,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args,
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise AcceptanceError(
            f"command failed rc={result.returncode}: {_redact(result.stderr)}"
        )
    return result


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _run(["docker", *args], check=check)


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_container_health(name: str, *, timeout_seconds: float = 90.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    last = "unknown"
    while time.monotonic() < deadline:
        result = _docker(
            "inspect",
            "--format",
            "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
            name,
            check=False,
        )
        if result.returncode == 0:
            last = result.stdout.strip() or "none"
            if last == "healthy":
                return
            if last == "unhealthy":
                raise AcceptanceError("candidate container became unhealthy")
        time.sleep(1.0)
    raise AcceptanceError(f"candidate health timeout; final_status={last}")


def _wait_mock(name: str, *, timeout_seconds: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    probe = (
        "import urllib.request; "
        "urllib.request.urlopen('http://127.0.0.1:8642/health', timeout=2).read()"
    )
    while time.monotonic() < deadline:
        result = _docker("exec", name, "python", "-c", probe, check=False)
        if result.returncode == 0:
            return
        time.sleep(0.5)
    raise AcceptanceError("isolated Hermes mock did not become ready")


def _tool_payload(result: Any) -> Any:
    structured = getattr(result, "structuredContent", None)
    if structured is None:
        structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    parts = [
        item.text
        for item in getattr(result, "content", [])
        if isinstance(getattr(item, "text", None), str)
    ]
    if not parts:
        return None
    combined = "\n".join(parts)
    try:
        return json.loads(combined)
    except json.JSONDecodeError:
        return combined


async def _probe_mcp(url: str) -> dict[str, Any]:
    validation_passed = False
    streamable = streamable_http_client(url)
    try:
        read_stream, write_stream, _ = await streamable.__aenter__()
        session = ClientSession(read_stream, write_stream)
        await session.__aenter__()
        try:
            await session.initialize()
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            contract = validate_tools(names, version=CURRENT_CONTRACT_VERSION)
            if not contract["ok"] or contract["extra"]:
                raise AcceptanceError("candidate tool contract mismatch")
            if contract["count"] != expected_tool_count(CURRENT_CONTRACT_VERSION):
                raise AcceptanceError("candidate tool count mismatch")

            health = _tool_payload(
                await session.call_tool("hermes_health", arguments={"detailed": False})
            )
            readiness = _tool_payload(
                await session.call_tool("hermes_readiness", arguments={})
            )
            capabilities = _tool_payload(
                await session.call_tool("hermes_capabilities", arguments={})
            )
            agent_card = _tool_payload(
                await session.call_tool("hermes_agent_card", arguments={})
            )
            for name, payload in (
                ("health", health),
                ("readiness", readiness),
                ("capabilities", capabilities),
                ("agent_card", agent_card),
            ):
                if not isinstance(payload, dict):
                    raise AcceptanceError(f"{name} did not return an object")

            bridge = health.get("bridge") or {}
            upstream = health.get("upstream") or {}
            observability = bridge.get("observability") or {}
            security = bridge.get("security_posture") or {}
            if upstream.get("status") not in {"ok", "healthy"}:
                raise AcceptanceError("isolated upstream is not healthy")
            if bridge.get("bridge_version") != EXPECTED_VERSION:
                raise AcceptanceError("bridge version mismatch")
            if bridge.get("manifest_version") != EXPECTED_VERSION:
                raise AcceptanceError("manifest version mismatch")
            if bridge.get("schema_version") != EXPECTED_SCHEMA:
                raise AcceptanceError("schema version mismatch")
            if bridge.get("unsupported_tools"):
                raise AcceptanceError("candidate exposes unsupported tools")
            if security.get("status") != "ready":
                raise AcceptanceError("security posture is not ready")

            if readiness.get("status") != "ready":
                raise AcceptanceError("candidate readiness is not ready")
            if readiness.get("bridge_version") != EXPECTED_VERSION:
                raise AcceptanceError("readiness bridge version mismatch")
            if readiness.get("contract_version") != EXPECTED_VERSION:
                raise AcceptanceError("readiness contract version mismatch")
            if readiness.get("schema_version") != EXPECTED_SCHEMA:
                raise AcceptanceError("readiness schema version mismatch")

            components = readiness.get("components") or {}
            posture = components.get("security_posture") or {}
            policy = posture.get("policy") or {}
            hmac_posture = posture.get("hmac") or {}
            tool_contract = components.get("tool_contract") or {}
            if policy.get("valid") is not True or policy.get("source") != "file":
                raise AcceptanceError("policy is not valid and file-backed")
            if hmac_posture.get("required") is not True:
                raise AcceptanceError("HMAC is not required")
            if hmac_posture.get("configured") is not True:
                raise AcceptanceError("HMAC is not configured")
            if hmac_posture.get("source_type") != "file":
                raise AcceptanceError("HMAC is not file-backed")
            if hmac_posture.get("key_id") != HMAC_KEY_ID:
                raise AcceptanceError("HMAC key ID mismatch")
            if hmac_posture.get("previous_configured") is not False:
                raise AcceptanceError("previous HMAC key unexpectedly configured")
            if tool_contract.get("count") != expected_tool_count(EXPECTED_VERSION):
                raise AcceptanceError("readiness tool count mismatch")

            metrics = observability.get("metrics") or {}
            tracing = observability.get("tracing") or {}
            retry = observability.get("retry") or {}
            circuit = observability.get("circuit_breaker") or {}
            if metrics.get("enabled") is not False:
                raise AcceptanceError("metrics exporter unexpectedly enabled")
            if tracing.get("export_enabled") is not False:
                raise AcceptanceError("tracing export unexpectedly enabled")
            if retry.get("enabled") is not False:
                raise AcceptanceError("retry unexpectedly enabled")
            if circuit.get("enabled") is not False:
                raise AcceptanceError("circuit breaker unexpectedly enabled")
            if retry.get("mutations_retryable") is not False:
                raise AcceptanceError("mutation retry posture is unsafe")
            if circuit.get("mutations_protected") is not False:
                raise AcceptanceError("mutation circuit posture is unsafe")

            if capabilities.get("bridge_version") != EXPECTED_VERSION:
                raise AcceptanceError("capability version mismatch")
            if capabilities.get("schema_version") != EXPECTED_SCHEMA:
                raise AcceptanceError("capability schema mismatch")
            if capabilities.get("upstream_capabilities_source") != "upstream":
                raise AcceptanceError("capability source is not the isolated upstream")
            if agent_card.get("version") != EXPECTED_VERSION:
                raise AcceptanceError("agent-card version mismatch")
            if agent_card.get("schema_version") != EXPECTED_SCHEMA:
                raise AcceptanceError("agent-card schema mismatch")

            validation_passed = True
            return {
                "bridge_version": bridge.get("bridge_version"),
                "schema_version": bridge.get("schema_version"),
                "tool_count": contract["count"],
                "readiness": readiness.get("status"),
                "policy_source": policy.get("source"),
                "hmac_source": hmac_posture.get("source_type"),
                "optional_features": {
                    "metrics": metrics.get("enabled"),
                    "tracing_export": tracing.get("export_enabled"),
                    "retry": retry.get("enabled"),
                    "circuit": circuit.get("enabled"),
                },
            }
        finally:
            with suppress(Exception):
                await session.__aexit__(None, None, None)
    finally:
        try:
            await streamable.__aexit__(None, None, None)
        except Exception:
            if not validation_passed:
                raise


def _inspect_security(container: str, host_port: int) -> dict[str, Any]:
    payload = json.loads(_docker("inspect", container).stdout)[0]
    config = payload.get("Config") or {}
    host = payload.get("HostConfig") or {}
    network = host.get("NetworkMode")
    if config.get("User") not in {"bridge:bridge", "1000:1000", "1000"}:
        raise AcceptanceError("candidate is not running as the bridge user")
    if host.get("ReadonlyRootfs") is not True:
        raise AcceptanceError("candidate root filesystem is not read-only")
    if "ALL" not in (host.get("CapDrop") or []):
        raise AcceptanceError("candidate capabilities are not fully dropped")
    security_options = host.get("SecurityOpt") or []
    if not any("no-new-privileges" in item for item in security_options):
        raise AcceptanceError("candidate lacks no-new-privileges")
    if network == "host":
        raise AcceptanceError("isolated candidate unexpectedly uses host networking")

    bindings = (host.get("PortBindings") or {}).get("8765/tcp") or []
    if not any(
        item.get("HostIp") == "127.0.0.1"
        and int(item.get("HostPort") or 0) == host_port
        for item in bindings
    ):
        raise AcceptanceError("candidate MCP port is not bound to loopback")

    destinations = {mount.get("Destination") for mount in payload.get("Mounts") or []}
    if "/var/run/docker.sock" in destinations:
        raise AcceptanceError("candidate has access to the Docker socket")
    required = {
        "/var/lib/hermes-mcp-bridge",
        "/run/secrets",
        "/etc/hermes-mcp-bridge/policies/production.json",
    }
    if not required.issubset(destinations):
        raise AcceptanceError("candidate mount contract is incomplete")

    environment = "\n".join(config.get("Env") or [])
    if API_KEY in environment or HMAC_KEY in environment:
        raise AcceptanceError("secret material leaked into container environment")
    return {
        "user": config.get("User"),
        "read_only_root": host.get("ReadonlyRootfs"),
        "cap_drop_all": True,
        "no_new_privileges": True,
        "network_isolated": True,
        "loopback_publish": True,
    }


def _state_integrity(container: str) -> dict[str, Any]:
    code = """
import json
import sqlite3

connection = sqlite3.connect(
    'file:/var/lib/hermes-mcp-bridge/state.sqlite3?mode=ro',
    uri=True,
)
try:
    quick = connection.execute('PRAGMA quick_check').fetchone()[0]
    integrity = connection.execute('PRAGMA integrity_check').fetchone()[0]
    version = connection.execute(
        'SELECT MAX(version) FROM schema_migrations'
    ).fetchone()[0]
finally:
    connection.close()
print(json.dumps({
    'quick_check': quick,
    'integrity_check': integrity,
    'migration_version': version,
}))
"""
    result = _docker("exec", container, "python", "-c", code)
    payload = json.loads(result.stdout)
    if payload.get("quick_check") != "ok":
        raise AcceptanceError("isolated state quick_check failed")
    if payload.get("integrity_check") != "ok":
        raise AcceptanceError("isolated state integrity_check failed")
    if not isinstance(payload.get("migration_version"), int):
        raise AcceptanceError("isolated state migration version missing")
    return payload


def _parse_json_logs(container: str) -> list[dict[str, Any]]:
    result = _docker("logs", container)
    lines = [*result.stdout.splitlines(), *result.stderr.splitlines()]
    records: list[dict[str, Any]] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AcceptanceError(
                f"container {container} emitted non-JSON log line {number}"
            ) from exc
        if not isinstance(payload, dict):
            raise AcceptanceError("container emitted a non-object JSON log")
        records.append(payload)
    serialized = json.dumps(records, sort_keys=True)
    for value in (API_KEY, HMAC_KEY, "/run/secrets", "Authorization: Bearer"):
        if value in serialized:
            raise AcceptanceError("sensitive material appeared in container logs")
    return records


def _validate_mock_logs(container: str) -> dict[str, Any]:
    records = _parse_json_logs(container)
    request_records = [item for item in records if item.get("event") == "mock.request"]
    if not request_records:
        raise AcceptanceError("isolated mock observed no upstream requests")
    if any(item.get("method") != "GET" for item in request_records):
        raise AcceptanceError("isolated acceptance attempted an upstream mutation")
    if any(item.get("event") == "mock.mutation_rejected" for item in records):
        raise AcceptanceError("isolated mock rejected a mutation attempt")
    return {
        "request_count": len(request_records),
        "methods": sorted({str(item.get("method")) for item in request_records}),
        "path_classes": sorted(
            {str(item.get("path_class")) for item in request_records}
        ),
    }


def _cleanup(prefix: str, resources: dict[str, str]) -> None:
    for key in ("bridge", "mock"):
        name = resources.get(key)
        if name:
            _docker("rm", "-f", name, check=False)
    network = resources.get("network")
    if network:
        _docker("network", "rm", network, check=False)
    for key in ("state_volume", "secrets_volume"):
        name = resources.get(key)
        if name:
            _docker("volume", "rm", "-f", name, check=False)
    remaining = _docker(
        "ps",
        "-a",
        "--filter",
        f"name={prefix}",
        "--format",
        "{{.Names}}",
        check=False,
    ).stdout.strip()
    if remaining:
        raise AcceptanceError("isolated acceptance container cleanup failed")


def _accept(image: str, repo_root: Path) -> dict[str, Any]:
    if CURRENT_CONTRACT_VERSION != EXPECTED_VERSION:
        raise AcceptanceError("acceptance harness contract version mismatch")
    if SCHEMA_VERSION != EXPECTED_SCHEMA:
        raise AcceptanceError("acceptance harness schema version mismatch")
    _docker("image", "inspect", image)

    token = uuid.uuid4().hex[:10]
    prefix = f"hermes-1-0-accept-{token}"
    resources = {
        "network": f"{prefix}-net",
        "state_volume": f"{prefix}-state",
        "secrets_volume": f"{prefix}-secrets",
        "mock": f"{prefix}-mock",
        "bridge": f"{prefix}-bridge",
    }
    port = _free_loopback_port()
    policy = repo_root / "config" / "policies" / "production.json"
    mock_script = repo_root / "tests" / "isolated" / "mock_hermes.py"
    state_mount = (
        f"type=volume,src={resources['state_volume']},"
        "dst=/var/lib/hermes-mcp-bridge"
    )
    secrets_mount = (
        f"type=volume,src={resources['secrets_volume']},"
        "dst=/run/secrets,readonly"
    )
    policy_mount = (
        f"type=bind,src={policy},"
        "dst=/etc/hermes-mcp-bridge/policies/production.json,readonly"
    )
    result: dict[str, Any] | None = None

    try:
        _docker("network", "create", resources["network"])
        _docker("volume", "create", resources["state_volume"])
        _docker("volume", "create", resources["secrets_volume"])

        _docker(
            "run",
            "--rm",
            "--user",
            "0:0",
            "--mount",
            f"type=volume,src={resources['state_volume']},dst=/state",
            image,
            "sh",
            "-c",
            "chown 1000:1000 /state && chmod 700 /state",
        )
        secret_writer = """
import os
from pathlib import Path

root = Path('/secrets')
root.mkdir(parents=True, exist_ok=True)
for name, env_name in (
    ('hermes_api_key', 'ACCEPTANCE_API_KEY'),
    ('hermes_bridge_hmac_secret', 'ACCEPTANCE_HMAC_KEY'),
):
    path = root / name
    path.write_text(os.environ[env_name] + '\\n', encoding='utf-8')
    os.chown(path, 1000, 1000)
    path.chmod(0o400)
os.chown(root, 1000, 1000)
root.chmod(0o700)
"""
        _docker(
            "run",
            "--rm",
            "--user",
            "0:0",
            "--env",
            f"ACCEPTANCE_API_KEY={API_KEY}",
            "--env",
            f"ACCEPTANCE_HMAC_KEY={HMAC_KEY}",
            "--mount",
            f"type=volume,src={resources['secrets_volume']},dst=/secrets",
            image,
            "python",
            "-c",
            secret_writer,
        )

        _docker(
            "run",
            "--detach",
            "--name",
            resources["mock"],
            "--network",
            resources["network"],
            "--network-alias",
            "hermes-mock",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--tmpfs",
            "/tmp:size=16m,mode=1777",
            "--env",
            f"MOCK_HERMES_TOKEN={API_KEY}",
            "--mount",
            f"type=bind,src={mock_script},dst=/acceptance/mock_hermes.py,readonly",
            image,
            "python",
            "/acceptance/mock_hermes.py",
        )
        _wait_mock(resources["mock"])

        _docker(
            "run",
            "--detach",
            "--name",
            resources["bridge"],
            "--network",
            resources["network"],
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--tmpfs",
            "/tmp:size=32m,mode=1777",
            "--publish",
            f"127.0.0.1:{port}:8765",
            "--health-cmd",
            "python -m hermes_mcp_bridge.healthcheck",
            "--health-interval",
            "2s",
            "--health-timeout",
            "3s",
            "--health-retries",
            "30",
            "--health-start-period",
            "2s",
            "--env",
            "HERMES_API_BASE_URL=http://hermes-mock:8642",
            "--env",
            "HERMES_API_KEY=",
            "--env",
            "HERMES_API_KEY_FILE=/run/secrets/hermes_api_key",
            "--env",
            "MCP_HOST=0.0.0.0",
            "--env",
            "MCP_PORT=8765",
            "--env",
            "BRIDGE_STATE_DB_PATH=/var/lib/hermes-mcp-bridge/state.sqlite3",
            "--env",
            "BRIDGE_SECURITY_MODE=production",
            "--env",
            "BRIDGE_POLICY_PATH=/etc/hermes-mcp-bridge/policies/production.json",
            "--env",
            "BRIDGE_POLICY_JSON=",
            "--env",
            "HERMES_BRIDGE_HMAC_SECRET=",
            "--env",
            "HERMES_BRIDGE_HMAC_SECRET_FILE=/run/secrets/hermes_bridge_hmac_secret",
            "--env",
            f"HERMES_BRIDGE_HMAC_KEY_ID={HMAC_KEY_ID}",
            "--env",
            "HERMES_BRIDGE_HMAC_SECRET_PREVIOUS=",
            "--env",
            "HERMES_BRIDGE_HMAC_SECRET_PREVIOUS_FILE=",
            "--env",
            "HERMES_BRIDGE_HMAC_PREVIOUS_KEY_ID=",
            "--env",
            "HERMES_BRIDGE_HMAC_PREVIOUS_VALID_FROM=",
            "--env",
            "HERMES_BRIDGE_HMAC_PREVIOUS_VALID_UNTIL=",
            "--env",
            "HERMES_AGENT_CARD_VERSION=1.0.0",
            "--env",
            "BRIDGE_LOG_FORMAT=json",
            "--env",
            "BRIDGE_LOG_LEVEL=INFO",
            "--env",
            "BRIDGE_LOG_CAPTURE_THIRD_PARTY=1",
            "--env",
            "BRIDGE_LOG_THIRD_PARTY_LEVEL=WARNING",
            "--env",
            "BRIDGE_METRICS_ENABLED=0",
            "--env",
            "BRIDGE_TRACING_ENABLED=0",
            "--env",
            "BRIDGE_TRACING_EXPORT=0",
            "--env",
            "BRIDGE_RETRY_ENABLED=false",
            "--env",
            "BRIDGE_CIRCUIT_ENABLED=false",
            "--mount",
            state_mount,
            "--mount",
            secrets_mount,
            "--mount",
            policy_mount,
            image,
        )
        _wait_container_health(resources["bridge"])

        url = f"http://127.0.0.1:{port}/mcp"
        first_probe = asyncio.run(_probe_mcp(url))
        security = _inspect_security(resources["bridge"], port)
        state_before = _state_integrity(resources["bridge"])
        bridge_logs_before = _parse_json_logs(resources["bridge"])

        before_state = json.loads(
            _docker("inspect", resources["bridge"]).stdout
        )[0]["State"]
        before_pid = int(before_state.get("Pid") or 0)
        before_started_at = str(before_state.get("StartedAt") or "")
        _docker("restart", resources["bridge"])
        _wait_container_health(resources["bridge"])
        after_state = json.loads(
            _docker("inspect", resources["bridge"]).stdout
        )[0]["State"]
        after_pid = int(after_state.get("Pid") or 0)
        after_started_at = str(after_state.get("StartedAt") or "")
        if (
            before_pid <= 0
            or after_pid <= 0
            or before_pid == after_pid
            or not before_started_at
            or before_started_at == after_started_at
        ):
            raise AcceptanceError(
                "authorized restart did not replace the container process exactly once"
            )

        second_probe = asyncio.run(_probe_mcp(url))
        if first_probe != second_probe:
            raise AcceptanceError("candidate posture changed after restart")
        state_after = _state_integrity(resources["bridge"])
        if state_before != state_after:
            raise AcceptanceError("candidate state integrity changed after restart")

        bridge_logs = _parse_json_logs(resources["bridge"])
        if len(bridge_logs) < len(bridge_logs_before):
            raise AcceptanceError("candidate logs regressed after restart")
        mock_evidence = _validate_mock_logs(resources["mock"])
        image_id = _docker(
            "image",
            "inspect",
            "--format",
            "{{.Id}}",
            image,
        ).stdout.strip()

        result = {
            "decision": "HERMES_BRIDGE_1_0_0_ISOLATED_ACCEPTANCE_PASS",
            "image_id": image_id,
            "contract": first_probe,
            "container_security": security,
            "state": state_after,
            "restart_evidence": {
                "pid_changed": before_pid != after_pid,
                "started_at_changed": before_started_at != after_started_at,
            },
            "authorized_restarts": 1,
            "bridge_json_log_lines": len(bridge_logs),
            "upstream_mock": mock_evidence,
            "production_touched": False,
            "ritmo_used": False,
        }
    finally:
        _cleanup(prefix, resources)

    if result is None:
        raise AcceptanceError("isolated acceptance produced no result")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--image",
        default="hermes-mcp-bridge:ci",
        help="Already-built candidate image to accept",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root containing policy and mock assets",
    )
    args = parser.parse_args()

    if not shutil_which("docker"):
        raise SystemExit("docker is required")
    try:
        result = _accept(args.image, args.repo_root.resolve())
    except Exception as exc:
        print(
            json.dumps(
                {
                    "decision": "HERMES_BRIDGE_1_0_0_ISOLATED_ACCEPTANCE_FAIL",
                    "error": _redact(str(exc)),
                    "production_touched": False,
                    "ritmo_used": False,
                },
                sort_keys=True,
            )
        )
        raise SystemExit(1) from exc
    print(json.dumps(result, sort_keys=True, indent=2))


def shutil_which(command: str) -> str | None:
    """Local wrapper to keep the import surface explicit in tests."""

    from shutil import which

    return which(command)


if __name__ == "__main__":
    main()
