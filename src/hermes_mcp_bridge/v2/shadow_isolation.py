"""Fail-closed evidence contract for the Phase 2 isolated V1 shadow runtime."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

SHADOW_ISOLATION_SCHEMA = "hermes-v2-phase2-shadow-isolation/2"
SHADOW_SERVER_CONTRACT = "github-direct-read-fixed-repository/1"
SHADOW_MCP_SERVER = "phase2-read"
# Compatibility alias for the evidence vocabulary used by the Phase 2 gate.
# In current Hermes this value is an MCP server/toolset name resolved by
# _get_platform_tools(), not an entry returned by GET /v1/toolsets.
SHADOW_TOOLSET = SHADOW_MCP_SERVER
SHADOW_MCP_TOOL_NAMES = (
    "github_get_checks",
    "github_get_issue",
    "github_get_pr",
    "github_get_repo",
    "github_search",
)
_SHADOW_MCP_SERVER_SAFE = re.sub(r"[^A-Za-z0-9_]", "_", SHADOW_MCP_SERVER)
SHADOW_HERMES_TOOL_NAMES = tuple(
    f"mcp__{_SHADOW_MCP_SERVER_SAFE}__{name}" for name in SHADOW_MCP_TOOL_NAMES
)
SHADOW_HTTP_METHODS = ["GET"]

_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")

_ALLOWED_KEYS = frozenset(
    {
        "schema",
        "source_commit",
        "connected_jarvas",
        "hermes_profile_isolated",
        "api_platform",
        "api_bind_loopback",
        "api_auth_required",
        "effective_toolsets",
        "native_toolsets_enabled",
        "effective_tools",
        "resolver_exact",
        "mcp_server_config_exact",
        "repository_scopes",
        "credential_provider_type",
        "credential_capability",
        "credential_file_backed",
        "mcp_resources_enabled",
        "mcp_prompts_enabled",
        "http_methods",
        "generic_execution_tools",
        "mutation_capable_tools",
        "server_contract",
        "probes",
        "confirmed_at",
    }
)
_ALLOWED_PROBE_KEYS = frozenset(
    {"health_status", "capabilities_status", "toolsets_status", "sessions_status"}
)


class ShadowIsolationError(RuntimeError):
    """Stable, secret-free shadow isolation validation failure."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _repository_ok(value: Any) -> bool:
    return (
        isinstance(value, str)
        and _REPOSITORY_RE.fullmatch(value) is not None
        and not any(token in value for token in ("*", "?", "[", "]", "\\"))
    )


def _timestamp_ok(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def validate_shadow_isolation(
    payload: Any,
    *,
    repositories: set[str] | frozenset[str] | list[str] | tuple[str, ...],
    source_commit: str,
) -> list[str]:
    """Return stable failure codes; an empty list proves read-only isolation."""
    failures: list[str] = []
    if not isinstance(payload, dict):
        return ["shadow_isolation_invalid"]

    if set(payload) != _ALLOWED_KEYS:
        failures.append("shadow_isolation_fields_invalid")
    if payload.get("schema") != SHADOW_ISOLATION_SCHEMA:
        failures.append("shadow_isolation_schema_invalid")
    if payload.get("source_commit") != source_commit or _SHA40_RE.fullmatch(
        str(payload.get("source_commit", ""))
    ) is None:
        failures.append("shadow_isolation_source_commit_invalid")

    expected_scalars = {
        "connected_jarvas": True,
        "hermes_profile_isolated": True,
        "api_platform": "api_server",
        "api_bind_loopback": True,
        "api_auth_required": True,
        "resolver_exact": True,
        "mcp_server_config_exact": True,
        "credential_provider_type": "github_app",
        "credential_capability": "github.read",
        "credential_file_backed": True,
        "mcp_resources_enabled": False,
        "mcp_prompts_enabled": False,
        "generic_execution_tools": False,
        "mutation_capable_tools": False,
        "server_contract": SHADOW_SERVER_CONTRACT,
    }
    for key, expected in expected_scalars.items():
        if payload.get(key) != expected:
            failures.append(f"shadow_isolation_invalid:{key}")

    if payload.get("effective_toolsets") != [SHADOW_MCP_SERVER]:
        failures.append("shadow_isolation_toolsets_not_exact")
    # GET /v1/toolsets intentionally reports configurable/native toolsets only;
    # a correct MCP-only shadow must therefore expose no enabled entry there.
    if payload.get("native_toolsets_enabled") != []:
        failures.append("shadow_isolation_native_toolsets_not_empty")
    if payload.get("effective_tools") != sorted(SHADOW_HERMES_TOOL_NAMES):
        failures.append("shadow_isolation_tools_not_exact")
    if payload.get("http_methods") != SHADOW_HTTP_METHODS:
        failures.append("shadow_isolation_http_methods_not_get_only")

    requested = {str(item).lower() for item in repositories if _repository_ok(item)}
    scopes = payload.get("repository_scopes")
    if not isinstance(scopes, list) or not scopes:
        failures.append("shadow_isolation_repository_scopes_missing")
    else:
        observed = {str(item).lower() for item in scopes if _repository_ok(item)}
        if len(observed) != len(scopes) or observed != requested:
            failures.append("shadow_isolation_repository_scopes_not_exact")

    probes = payload.get("probes")
    if not isinstance(probes, dict) or set(probes) != _ALLOWED_PROBE_KEYS:
        failures.append("shadow_isolation_probes_invalid")
    else:
        for key in sorted(_ALLOWED_PROBE_KEYS):
            if probes.get(key) != 200:
                failures.append(f"shadow_isolation_probe_failed:{key}")

    if not _timestamp_ok(payload.get("confirmed_at")):
        failures.append("shadow_isolation_confirmed_at_invalid")
    return failures


def load_shadow_isolation(
    path: str | Path,
    *,
    repositories: set[str] | frozenset[str] | list[str] | tuple[str, ...],
    source_commit: str,
) -> dict[str, Any]:
    """Load and validate sanitized isolation evidence from a local JSON file."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ShadowIsolationError("SHADOW_ISOLATION_UNREADABLE") from exc
    failures = validate_shadow_isolation(
        payload,
        repositories=repositories,
        source_commit=source_commit,
    )
    if failures:
        raise ShadowIsolationError(failures[0].upper())
    return payload
