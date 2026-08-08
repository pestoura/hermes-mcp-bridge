#!/usr/bin/env python3
"""Prepare a minimal, private Hermes home for the Phase 2 read-only V1 shadow.

Run this helper with the Python interpreter belonging to the installed Hermes
runtime. It copies only model-provider material needed for inference, never
messaging/integration credentials, and writes no secret value to stdout.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import json
import os
import re
import secrets
import shutil
import stat
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from hermes_mcp_bridge.v2.shadow_isolation import (  # noqa: E402
    SHADOW_MCP_TOOL_NAMES,
    SHADOW_TOOLSET,
)

_SENSITIVE_KEY_RE = re.compile(r"(?:api[_-]?key|authorization|bearer|password|secret|token)$", re.I)


class ShadowHomeError(RuntimeError):
    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode != 0o700:
        raise ShadowHomeError("SHADOW_HOME_PERMISSIONS_INVALID")


def _atomic_private_write(path: Path, content: str) -> None:
    _private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        path.chmod(0o600)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def _sanitize_config_value(value: Any) -> Any:
    """Copy config while refusing secret-bearing literal fields."""
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, child in value.items():
            key = str(raw_key)
            if _SENSITIVE_KEY_RE.search(key):
                # Environment-variable *names* are configuration, values are not.
                if key.lower().endswith(("_env", "_env_var", "_env_vars")):
                    result[key] = _sanitize_config_value(child)
                continue
            result[key] = _sanitize_config_value(child)
        return result
    if isinstance(value, list):
        return [_sanitize_config_value(item) for item in value]
    return copy.deepcopy(value)


def _provider_env_names(provider: str) -> tuple[str, ...]:
    try:
        from providers import get_provider_profile

        profile = get_provider_profile(provider)
    except Exception:
        profile = None
    raw = getattr(profile, "env_vars", ()) if profile is not None else ()
    if isinstance(raw, str):
        raw = (raw,)
    names: list[str] = []
    for item in raw or ():
        name = str(item).strip()
        if re.fullmatch(r"[A-Z][A-Z0-9_]{1,127}", name):
            names.append(name)
    return tuple(dict.fromkeys(names))


def _dotenv_values(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        from dotenv import dotenv_values

        raw = dotenv_values(path)
    except Exception as exc:
        raise ShadowHomeError("SOURCE_ENV_UNREADABLE") from exc
    return {
        str(key): str(value)
        for key, value in raw.items()
        if isinstance(key, str) and value is not None
    }


def _dotenv_line(name: str, value: str) -> str:
    # JSON string quoting is accepted by python-dotenv and protects whitespace,
    # comments and shell metacharacters without ever invoking a shell.
    return f"{name}={json.dumps(value)}\n"


def _matching_custom_providers(source: dict[str, Any], provider: str) -> list[Any]:
    raw = source.get("custom_providers")
    if not isinstance(raw, list):
        return []
    matches: list[Any] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        identity = str(item.get("name") or item.get("id") or item.get("provider") or "")
        if identity == provider:
            matches.append(_sanitize_config_value(item))
    return matches


def prepare(args: argparse.Namespace) -> dict[str, Any]:
    source_home = Path(args.source_home).expanduser().resolve()
    shadow_home = Path(args.shadow_home).expanduser().resolve()
    config_path = source_home / "config.yaml"
    if not config_path.is_file():
        raise ShadowHomeError("SOURCE_CONFIG_MISSING")
    try:
        source = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ShadowHomeError("SOURCE_CONFIG_INVALID") from exc
    if not isinstance(source, dict):
        raise ShadowHomeError("SOURCE_CONFIG_INVALID")

    model = source.get("model")
    if not isinstance(model, dict):
        raise ShadowHomeError("SOURCE_MODEL_CONFIG_MISSING")
    provider = str(model.get("provider") or "").strip()
    default_model = str(model.get("default") or "").strip()
    if not provider or not default_model:
        raise ShadowHomeError("SOURCE_MODEL_CONFIG_INCOMPLETE")

    mcp_python = Path(args.mcp_python).expanduser().resolve()
    mcp_script = Path(args.mcp_script).expanduser().resolve()
    token_file = Path(args.token_file).expanduser().resolve()
    if not mcp_python.is_file() or not os.access(mcp_python, os.X_OK):
        raise ShadowHomeError("MCP_PYTHON_INVALID")
    if not mcp_script.is_file():
        raise ShadowHomeError("MCP_SERVER_SCRIPT_INVALID")
    if not token_file.is_file() or stat.S_IMODE(token_file.stat().st_mode) != 0o600:
        raise ShadowHomeError("DIRECT_TOKEN_FILE_INVALID")

    if shadow_home == source_home or source_home in shadow_home.parents:
        # Avoid ever nesting the disposable shadow under the live Hermes home.
        raise ShadowHomeError("SHADOW_HOME_NOT_ISOLATED")
    if shadow_home.exists():
        shutil.rmtree(shadow_home)
    _private_dir(shadow_home)

    target: dict[str, Any] = {}
    if "_config_version" in source:
        target["_config_version"] = source["_config_version"]
    target["model"] = _sanitize_config_value(model)
    custom = _matching_custom_providers(source, provider)
    if custom:
        target["custom_providers"] = custom
    target["platform_toolsets"] = {"api_server": [SHADOW_TOOLSET]}
    target["mcp_servers"] = {
        "phase2-read": {
            "command": str(mcp_python),
            "args": [str(mcp_script)],
            "env": {
                "HERMES_V2_SHADOW_REPOSITORY": args.repository,
                "BRIDGE_V2_GITHUB_DIRECT_READ_TOKEN_FILE": str(token_file),
            },
            "tools": {
                "include": list(SHADOW_MCP_TOOL_NAMES),
                "resources": False,
                "prompts": False,
            },
            "supports_parallel_tool_calls": False,
        }
    }
    target["terminal"] = {"home_mode": "profile"}
    target["agent"] = {
        # Defense-in-depth. The platform allow-list and live /v1/toolsets probe
        # are the authoritative boundary; this list prevents accidental future
        # fallback to broad built-ins during config migrations.
        "disabled_toolsets": [
            "browser",
            "code_execution",
            "computer_use",
            "file",
            "homeassistant",
            "image_gen",
            "kanban",
            "memory",
            "messaging",
            "skills",
            "terminal",
            "todo",
            "web",
        ]
    }

    _atomic_private_write(
        shadow_home / "config.yaml",
        yaml.safe_dump(target, sort_keys=True, allow_unicode=True),
    )

    source_env = _dotenv_values(source_home / ".env")
    provider_env = _provider_env_names(provider)
    api_key = secrets.token_urlsafe(48)
    env_lines = [
        _dotenv_line("API_SERVER_ENABLED", "true"),
        _dotenv_line("API_SERVER_HOST", "127.0.0.1"),
        _dotenv_line("API_SERVER_PORT", str(args.api_port)),
        _dotenv_line("API_SERVER_KEY", api_key),
        _dotenv_line("API_SERVER_MODEL_NAME", "phase2-shadow"),
    ]
    copied_provider_env: list[str] = []
    for name in provider_env:
        value = source_env.get(name)
        if value:
            env_lines.append(_dotenv_line(name, value))
            copied_provider_env.append(name)
    _atomic_private_write(shadow_home / ".env", "".join(env_lines))
    _atomic_private_write(Path(args.api_key_out), api_key + "\n")

    source_auth = source_home / "auth.json"
    auth_copied = False
    if source_auth.is_file():
        destination = shadow_home / "auth.json"
        shutil.copyfile(source_auth, destination)
        destination.chmod(0o600)
        auth_copied = True

    return {
        "status": "SHADOW_HOME_PREPARED",
        "model_provider": provider,
        "model_configured": True,
        "provider_env_names_copied": sorted(copied_provider_env),
        "oauth_auth_store_copied": auth_copied,
        "platform_toolset": SHADOW_TOOLSET,
        "mcp_tool_count": len(SHADOW_MCP_TOOL_NAMES),
        "messaging_credentials_copied": False,
        "integration_credentials_copied": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-home", required=True)
    parser.add_argument("--shadow-home", required=True)
    parser.add_argument("--mcp-python", required=True)
    parser.add_argument("--mcp-script", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--api-port", required=True, type=int)
    parser.add_argument("--api-key-out", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = prepare(args)
    except ShadowHomeError as exc:
        print(json.dumps({"status": "SHADOW_HOME_BLOCKED", "reason": exc.code}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
