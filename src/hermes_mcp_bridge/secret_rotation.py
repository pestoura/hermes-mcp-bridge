"""Secret source discovery, planning and rotation helpers.

Rules:
- never print secrets
- compare only by sha256 prefix, sanitized identity, and length
- no Hermes-process restart; use external systemd-run --user unit
- inspect /proc/<pid>/environ only for digest comparison by default
"""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import get_settings

_SETTINGS = get_settings()


@dataclass(frozen=True)
class SecretSourceReport:
    key: str
    current_digest_prefix: str
    current_length: int
    sources: list[dict[str, Any]]
    comparable: bool


@dataclass(frozen=True)
class RotationPlan:
    key: str
    new_value: str | None = None
    changed_paths: list[str] = field(default_factory=list)
    backup_paths: list[str] = field(default_factory=list)
    dry_run: bool = True
    requires_restart: bool = True
    active_runs: int = 0


def _sha256_prefix(value: str, length: int = 16) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _read_env_file(path: str) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip().strip("\"'")
    except FileNotFoundError:
        pass
    return result


def _environ_for_pid(pid: int) -> dict[str, str]:
    env_path = f"/proc/{pid}/environ"
    try:
        data = Path(env_path).read_bytes()
    except FileNotFoundError:
        return {}
    result: dict[str, str] = {}
    for entry in data.split(b"\x00"):
        if not entry:
            continue
        text = entry.decode("utf-8", errors="ignore")
        if "=" not in text:
            continue
        key, _, value = text.partition("=")
        result[key.strip()] = value.strip()
    return result


def _systemd_unit_pid(unit_name: str) -> int | None:
    try:
        out = subprocess.check_output(
            ["systemctl", "--user", "show", "--property=MainPID", "--value", unit_name],
            text=True,
        ).strip()
        if out.isdigit():
            return int(out)
    except Exception:
        pass
    return None


def _systemd_unit_environment(unit_name: str) -> dict[str, str]:
    props = (
        "Environment,EnvironmentFiles,WorkingDirectory"
    )
    try:
        out = subprocess.check_output(
            [
                "systemctl",
                "--user",
                "show",
                f"--property={props}",
                "--value",
                unit_name,
            ],
            text=True,
        )
    except Exception:
        return {}
    env: dict[str, str] = {}
    env_files: list[str] = []
    working_dir: str | None = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Environment="):
            assignment = line[len("Environment=") :].strip()
            key, _, value = assignment.partition("=")
            env[key.strip()] = value.strip()
        elif line.startswith("EnvironmentFiles="):
            raw = line[len("EnvironmentFiles=") :].strip()
            env_files.extend(
                part.strip().strip("\"'") for part in raw.split(" ") if part.strip()
            )
        elif line.startswith("WorkingDirectory="):
            working_dir = line[len("WorkingDirectory=") :].strip().strip("\"'")
    if working_dir:
        env.update(_read_env_file(os.path.join(working_dir, ".env")))
    for env_file in env_files:
        env.update(_read_env_file(env_file))
    return env


def discover_api_server_key() -> SecretSourceReport:
    unit_name = _gateway_unit_name()
    unit_env: dict[str, str] = {}
    pid_env: dict[str, str] = {}
    if unit_name:
        unit_env = _systemd_unit_environment(unit_name)
        pid = _systemd_unit_pid(unit_name)
        if pid:
            pid_env = _environ_for_pid(pid)
    working_dir_env = _read_env_file(".env")
    candidates = {
        "unit_env": unit_env.get("API_SERVER_KEY"),
        "pid_environ": pid_env.get("API_SERVER_KEY"),
        "working_dir_env": working_dir_env.get("API_SERVER_KEY"),
    }
    current = next((v for v in candidates.values() if v is not None), None)
    length = len(current) if current is not None else 0
    digest = _sha256_prefix(current) if current else ""
    sources = [
        {
            "source": name,
            "present": value is not None,
            "length": len(value) if value is not None else 0,
            "digest_prefix": _sha256_prefix(value) if value is not None else None,
        }
        for name, value in candidates.items()
    ]
    present = [s for s in sources if s["present"]]
    comparable = length > 0 and len(present) > 0 and all(
        s["digest_prefix"] == digest for s in present
    )
    return SecretSourceReport(
        key="API_SERVER_KEY",
        current_digest_prefix=digest,
        current_length=length,
        sources=sources,
        comparable=comparable,
    )


def _gateway_unit_name() -> str | None:
    candidates = [
        "hermes-gateway.service",
        "hermes-agent-gateway.service",
        "hermes.service",
    ]
    for unit in candidates:
        try:
            subprocess.check_output(
                ["systemctl", "--user", "list-unit-files", unit, "--no-pager", "--no-legend"],
                text=True,
            )
            return unit
        except subprocess.CalledProcessError:
            continue
    return None


def discover_hermes_api_key() -> SecretSourceReport:
    compose_env = _read_env_file(os.path.join("compose", ".env"))
    container_env = _read_env_file(os.path.join(".env"))
    current = next(
        (
            v
            for v in (
                compose_env.get("HERMES_API_KEY"),
                container_env.get("HERMES_API_KEY"),
            )
            if v is not None
        ),
        None,
    )
    length = len(current) if current is not None else 0
    digest = _sha256_prefix(current) if current else ""
    sources = [
        _source_report(
            "compose_env",
            os.path.join("compose", ".env"),
            compose_env.get("HERMES_API_KEY"),
        ),
        _source_report(
            "working_dir_env", ".env", container_env.get("HERMES_API_KEY")
        ),
    ]
    present = [s for s in sources if s["present"]]
    comparable = length > 0 and len(present) > 0 and all(
        s["digest_prefix"] == digest for s in present
    )
    return SecretSourceReport(
        key="HERMES_API_KEY",
        current_digest_prefix=digest,
        current_length=length,
        sources=sources,
        comparable=comparable,
    )


def _source_report(source: str, path: str, value: str | None) -> dict[str, Any]:
    return {
        "source": source,
        "path": path,
        "present": value is not None,
        "length": len(value) if value is not None else 0,
        "digest_prefix": _sha256_prefix(value) if value is not None else None,
    }


def _active_api_runs() -> int:
    try:
        from hermes_mcp_bridge.config import get_settings
        from hermes_mcp_bridge.healthcheck import _http_health

        settings = get_settings()
        payload = _http_health(
            f"{settings.hermes_api_base_url}/health",
            settings.hermes_api_key.get_secret_value(),
            min(3.0, settings.hermes_request_timeout_seconds),
        )
        raw = payload.get("active_api_runs", 0) if isinstance(payload, dict) else 0
        if isinstance(raw, (int, float)) or (
            isinstance(raw, str) and raw.replace(".", "", 1).isdigit()
        ):
            return int(float(raw))
        return 0
    except BaseException:
        # health unreachable or unexpected error: treat as unknown/unsafe
        return -1


def inspect_secrets() -> dict[str, Any]:
    api_server = discover_api_server_key()
    hermes = discover_hermes_api_key()
    return {
        "API_SERVER_KEY": {
            "digest_prefix": api_server.current_digest_prefix,
            "length": api_server.current_length,
            "comparable": api_server.comparable,
            "sources": api_server.sources,
        },
        "HERMES_API_KEY": {
            "digest_prefix": hermes.current_digest_prefix,
            "length": hermes.current_length,
            "comparable": hermes.comparable,
            "sources": hermes.sources,
        },
    }


def plan_rotation(key: str, new_value: str | None = None, *, force: bool = False) -> RotationPlan:
    if key not in {"API_SERVER_KEY", "HERMES_API_KEY"}:
        raise ValueError(f"unsupported secret key {key}")
    active_runs = _active_api_runs()
    if active_runs != 0 and not force:
        # fail-closed: abort on any active runs OR unknown health state
        raise RuntimeError(
            f"active_api_runs={active_runs}; gateway health unknown or busy; "
            f"use force=True to override"
        )
    if key == "API_SERVER_KEY":
        changed_paths = ["working-dir .env"]
        backup_paths = [".env.pre-rotation"]
    else:
        changed_paths = ["compose/.env", ".env"]
        backup_paths = ["compose/.env.pre-rotation", ".env.pre-rotation"]
    return RotationPlan(
        key=key,
        new_value=new_value,
        changed_paths=changed_paths,
        backup_paths=backup_paths,
        dry_run=True,
        requires_restart=True,
        active_runs=active_runs,
    )


def apply_rotation(plan: RotationPlan) -> RotationPlan:
    if plan.dry_run:
        raise RuntimeError("apply_rotation requires dry_run=False")
    if plan.key not in {"API_SERVER_KEY", "HERMES_API_KEY"}:
        raise ValueError(plan.key)
    if plan.new_value is None:
        raise ValueError("new_value is required for apply")
    backups: list[str] = []
    changed: list[str] = []
    if plan.key == "API_SERVER_KEY":
        dotenv = Path(".env")
        if dotenv.exists():
            lines = dotenv.read_text(encoding="utf-8").splitlines()
            backup = Path(".env.pre-rotation")
            dotenv.replace(backup)
            backup.chmod(0o600)
            backups.append(str(backup))
            updated = []
            replaced = False
            for line in lines:
                if line.startswith("API_SERVER_KEY="):
                    updated.append(f"API_SERVER_KEY={plan.new_value}")
                    replaced = True
                else:
                    updated.append(line)
            if not replaced:
                updated.append(f"API_SERVER_KEY={plan.new_value}")
            dotenv.write_text("\n".join(updated) + "\n", encoding="utf-8")
            dotenv.chmod(0o600)
            changed.append(".env")
    else:
        for rel in ("compose/.env", ".env"):
            p = Path(rel)
            if not p.exists():
                continue
            lines = p.read_text(encoding="utf-8").splitlines()
            backup = Path(rel + ".pre-rotation")
            p.replace(backup)
            backup.chmod(0o600)
            backups.append(str(backup))
            updated = []
            replaced = False
            for line in lines:
                if line.startswith("HERMES_API_KEY="):
                    updated.append(f"HERMES_API_KEY={plan.new_value}")
                    replaced = True
                else:
                    updated.append(line)
            if not replaced:
                updated.append(f"HERMES_API_KEY={plan.new_value}")
            p.write_text("\n".join(updated) + "\n", encoding="utf-8")
            p.chmod(0o600)
            changed.append(rel)
    return RotationPlan(
        key=plan.key,
        new_value=plan.new_value,
        changed_paths=changed,
        backup_paths=backups,
        dry_run=False,
        requires_restart=True,
        active_runs=plan.active_runs,
    )


def _short_unit_name(value: str, max_length: int = 55) -> str:
    value = value.strip()
    if len(value) <= max_length:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return f"rotation-{digest}"


def schedule_external_restart(unit_name: str, timeout_seconds: int = 180) -> str:
    safe_name = _short_unit_name(unit_name)
    script = f"""#!/usr/bin/env bash
set -euo pipefail
systemctl --user restart {shlex.quote(safe_name)}
"""
    script_path = f"/tmp/{safe_name}-restart.sh"
    Path(script_path).write_text(script, encoding="utf-8")
    Path(script_path).chmod(0o700)
    unit = f"{safe_name}-restart.service"
    run_cmd = (
        f"systemd-run --user --unit={unit} --timer-property=AccuracySec=1s "
        f"--property=TimeoutStopSec={timeout_seconds} /bin/bash {script_path}"
    )
    try:
        out = subprocess.check_output(run_cmd.split(), text=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"systemd-run failed: {exc.stderr or exc.output}") from exc
    return f"{unit}|script={script_path}|output={out.strip()}"


def finalize_rotation(key: str) -> dict[str, Any]:
    return {
        "key": key,
        "next": "verify secret outside Hermes gateway; do not restart Hermes from inside",
        "note": "use scheduled external systemd-run restart if needed",
    }


def verify_rotation(key: str) -> dict[str, Any]:
    current = inspect_secrets()[key]
    return {
        "key": key,
        "current_digest_prefix": current["digest_prefix"],
        "comparable": current["comparable"],
        "status": "verified" if current["comparable"] else "inconclusive",
    }


def rollback_rotation(plan: RotationPlan) -> dict[str, Any]:
    restored: list[str] = []
    for backup in plan.backup_paths:
        src = Path(backup)
        if not src.exists():
            continue
        name = src.name
        if name.endswith(".pre-rotation"):
            dst_name = name[: -len(".pre-rotation")]
        elif name.endswith("-pre-rotation"):
            dst_name = name[: -len("-pre-rotation")]
        else:
            dst_name = name
        dst = src.with_name(dst_name)
        if dst.exists():
            dst.unlink(missing_ok=True)
        src.replace(dst)
        dst.chmod(0o600)
        restored.append(str(dst))
    return {"status": "ok", "restored": restored, "note": "recreate bridge if needed externally"}
