#!/usr/bin/env python3
"""Prepare a minimal, private Hermes home for the Phase 2 read-only V1 shadow.

Run this helper with the Python interpreter belonging to the installed Hermes
runtime. It copies only model-provider material needed for inference, never
messaging/integration credentials, and writes no secret value to stdout.

The launcher historically guessed that interpreter as ``$(dirname hermes)/python``.
That is not reliable for console-script shims.  If this helper starts under a
Python that cannot import the same Hermes modules as the gateway executable, it
locates the console script's real interpreter and re-execs itself before any
shadow configuration is written.
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

from hermes_mcp_bridge.v2.hermes_runtime import (  # noqa: E402
    HermesRuntimeError,
    absolute_invocation_path,
    resolve_hermes_python,
    validate_hermes_python_hint,
)
from hermes_mcp_bridge.v2.shadow_isolation import (  # noqa: E402
    SHADOW_MCP_SERVER,
    SHADOW_MCP_TOOL_NAMES,
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


def _runtime_resolver_available() -> bool:
    try:
        from hermes_cli.tools_config import _get_platform_tools
    except Exception:
        return False
    return callable(_get_platform_tools)


def _ensure_hermes_runtime_python(args: argparse.Namespace, argv: list[str]) -> None:
    """Re-exec under the Python proven to own the running Hermes console script.

    When the launcher supplies an explicit ``--hermes-python`` hint it is
    validated fail-closed against the *real* Hermes roots before any shadow
    configuration is written. The hint is only ever an argument: it is never
    exported to the environment of this process or of any child.
    """
    hint = getattr(args, "hermes_python", None)
    if hint:
        try:
            resolved_hint = validate_hermes_python_hint(
                hint,
                probe_home=os.environ.get("HOME", str(Path.home())),
                probe_hermes_home=args.source_home,
                path_env=os.environ.get("PATH", ""),
            )
        except HermesRuntimeError as exc:
            raise ShadowHomeError(exc.code) from exc
        if _runtime_resolver_available():
            return
        _reexec(resolved_hint, args, argv)

    if _runtime_resolver_available():
        return
    hermes_bin = shutil.which("hermes")
    if not hermes_bin:
        raise ShadowHomeError("HERMES_RUNTIME_EXECUTABLE_MISSING")
    try:
        resolved = resolve_hermes_python(
            hermes_bin,
            home=os.environ.get("HOME", str(Path.home())),
            hermes_home=args.source_home,
            path_env=os.environ.get("PATH", ""),
        )
    except HermesRuntimeError as exc:
        raise ShadowHomeError(exc.code) from exc

    try:
        current = Path(sys.executable).resolve(strict=True)
    except OSError as exc:
        raise ShadowHomeError("HERMES_RUNTIME_PYTHON_UNRESOLVED") from exc
    if resolved == current:
        raise ShadowHomeError("HERMES_TOOLSET_RESOLVER_UNAVAILABLE")
    _reexec(resolved, args, argv)


def _reexec(resolved: Path, args: argparse.Namespace, argv: list[str]) -> None:
    """Re-exec this helper under ``resolved`` with a minimal environment."""
    # All provider material required by the shadow is read selectively from
    # source_home/.env below; broad caller credentials must not become part of
    # the compatibility handoff, and the interpreter hint is never exported.
    env = {
        "HOME": os.environ.get("HOME", str(Path.home())),
        "HERMES_HOME": str(Path(args.source_home).expanduser().resolve()),
        "PATH": os.environ.get("PATH", ""),
        "USER": os.environ.get("USER", "estourpm"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    os.execve(
        str(resolved),
        [str(resolved), str(Path(__file__).resolve()), *argv],
        env,
    )


def _constrain_platform_to_shadow_mcp(target: dict[str, Any]) -> int:
    """Use the installed Hermes resolver itself to suppress every non-MCP toolset."""
    try:
        from hermes_cli.tools_config import _get_platform_tools
    except Exception as exc:
        raise ShadowHomeError("HERMES_TOOLSET_RESOLVER_UNAVAILABLE") from exc

    try:
        preliminary = set(_get_platform_tools(target, "api_server"))
    except Exception as exc:
        raise ShadowHomeError("HERMES_TOOLSET_RESOLUTION_FAILED") from exc
    if SHADOW_MCP_SERVER not in preliminary:
        raise ShadowHomeError("SHADOW_MCP_SERVER_NOT_RESOLVED")

    disabled = sorted(preliminary - {SHADOW_MCP_SERVER})
    target.setdefault("agent", {})["disabled_toolsets"] = disabled

    try:
        final = set(_get_platform_tools(target, "api_server"))
    except Exception as exc:
        raise ShadowHomeError("HERMES_TOOLSET_RESOLUTION_FAILED") from exc
    if final != {SHADOW_MCP_SERVER}:
        raise ShadowHomeError("SHADOW_PLATFORM_TOOLSETS_NOT_EXACT")
    return len(disabled)


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

    # The shadow MCP interpreter is normally a virtualenv ``bin/python``, which
    # is a symlink to the base interpreter. Resolving it here would drop the
    # venv site-packages from the launched MCP server, so the absolute
    # invocation path is preserved and validated without dereferencing.
    mcp_python = absolute_invocation_path(args.mcp_python)
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

    # Current Hermes treats the MCP server key itself as the platform toolset
    # allowlist entry. It is not a /v1/toolsets configurable/native toolset.
    target["platform_toolsets"] = {"api_server": [SHADOW_MCP_SERVER]}
    target["mcp_servers"] = {
        SHADOW_MCP_SERVER: {
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
    target["agent"] = {"disabled_toolsets": []}

    # Hermes can auto-enable recently shipped, plugin and recovered native
    # toolsets even when an MCP server is explicitly listed. Derive the exact
    # suppression set using the installed resolver, then verify the result is
    # mechanically reduced to this one MCP server before persisting config.
    disabled_toolset_count = _constrain_platform_to_shadow_mcp(target)

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
        "platform_toolset": SHADOW_MCP_SERVER,
        "mcp_tool_count": len(SHADOW_MCP_TOOL_NAMES),
        "disabled_non_mcp_toolset_count": disabled_toolset_count,
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
    # Optional explicit interpreter hint resolved by the launcher against the
    # real Hermes runtime roots. Omitted, the legacy console-script resolution
    # path is used unchanged.
    parser.add_argument("--hermes-python", required=False, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(raw_argv)
    try:
        _ensure_hermes_runtime_python(args, raw_argv)
        result = prepare(args)
    except ShadowHomeError as exc:
        print(json.dumps({"status": "SHADOW_HOME_BLOCKED", "reason": exc.code}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
