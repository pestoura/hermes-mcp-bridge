"""Secret source discovery, planning and rotation helpers.

Rules:
- never print secrets
- compare only by sha256 prefix, sanitized identity, and length
- no Hermes-process restart; use external systemd-run --user unit
- inspect /proc/<pid>/environ only for digest comparison by default
- all paths are absolute/canonical and derived from discovered sources
"""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
import tempfile
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .config import get_settings


class SourceStatus(StrEnum):
    CONSISTENT = "consistent"
    MISMATCH = "mismatch"
    INSUFFICIENT = "insufficient"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SecretSource:
    source: str
    path: str
    present: bool
    length: int
    digest_prefix: str | None


@dataclass(frozen=True)
class SecretSourceReport:
    key: str
    current_digest_prefix: str
    current_length: int
    sources: list[dict[str, Any]]
    comparable: bool
    status: str


@dataclass(frozen=True)
class RotationPlan:
    key: str
    new_value: str | None = None
    changed_paths: list[str] = field(default_factory=list)
    backup_paths: list[str] = field(default_factory=list)
    plan_token: str = ""
    dry_run: bool = True
    requires_restart: bool = True
    active_runs: int = 0
    target_service: str | None = None


def _sha256_prefix(value: str, length: int = 16) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _plan_token(new_value: str) -> str:
    # Commit-time fingerprint of the intended change (no secret exposure).
    return hashlib.sha256(("plan:" + new_value).encode("utf-8")).hexdigest()[:16]


def _read_env_file(path: str) -> dict[str, str]:
    result: dict[str, str] = {}
    p = Path(path)
    if not p.is_absolute():
        return result
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip().strip("\"'")
    except FileNotFoundError:
        pass
    except OSError:
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


def _systemd_unit_environment(unit_name: str) -> tuple[dict[str, str], list[str], str | None]:
    props = "Environment,EnvironmentFiles,WorkingDirectory"
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
        return {}, [], None
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
    return env, env_files, working_dir


def _gateway_unit_name() -> str | None:
    candidates = [
        "hermes-gateway.service",
        "hermes-agent-gateway.service",
        "hermes.service",
    ]
    for unit in candidates:
        try:
            subprocess.check_output(
                [
                    "systemctl",
                    "--user",
                    "list-unit-files",
                    unit,
                    "--no-pager",
                    "--no-legend",
                ],
                text=True,
            )
            return unit
        except subprocess.CalledProcessError:
            continue
    return None


def _source_report(
    source: str, path: str, value: str | None
) -> SecretSource:
    return SecretSource(
        source=source,
        path=path,
        present=value is not None,
        length=len(value) if value is not None else 0,
        digest_prefix=_sha256_prefix(value) if value is not None else None,
    )


def _classify(candidates: dict[str, str | None], digest: str) -> SourceStatus:
    present_values = [
        v for v in candidates.values() if v is not None and v != ""
    ]
    if not present_values:
        return SourceStatus.INSUFFICIENT
    if len(present_values) < 2:
        # Single source is not sufficient for gateway-memory vs files comparison.
        return SourceStatus.INSUFFICIENT
    digests = {_sha256_prefix(v) for v in present_values}
    if len(digests) == 1:
        return SourceStatus.CONSISTENT
    return SourceStatus.MISMATCH


def discover_api_server_key() -> SecretSourceReport:
    unit_name = _gateway_unit_name()
    unit_env: dict[str, str] = {}
    pid_env: dict[str, str] = {}
    pid: int | None = None
    working_dir: str | None = None
    if unit_name:
        unit_env, _env_files, working_dir = _systemd_unit_environment(unit_name)
        pid = _systemd_unit_pid(unit_name)
        if pid:
            pid_env = _environ_for_pid(pid)
    working_dir_env_path = (
        os.path.join(working_dir, ".env") if working_dir else ".env"
    )
    working_dir_env = _read_env_file(working_dir_env_path)
    candidates = {
        "unit_env": unit_env.get("API_SERVER_KEY"),
        "pid_environ": pid_env.get("API_SERVER_KEY"),
        "working_dir_env": working_dir_env.get("API_SERVER_KEY"),
    }
    current = next((v for v in candidates.values() if v is not None), None)
    length = len(current) if current is not None else 0
    digest = _sha256_prefix(current) if current else ""
    sources = [
        _source_report(
            "unit_env",
            unit_name or "",
            candidates["unit_env"],
        ).__dict__,
        _source_report(
            "pid_environ",
            f"pid:{pid}" if pid else "",
            candidates["pid_environ"],
        ).__dict__,
        _source_report(
            "working_dir_env",
            working_dir_env_path,
            candidates["working_dir_env"],
        ).__dict__,
    ]
    status = _classify(candidates, digest)
    return SecretSourceReport(
        key="API_SERVER_KEY",
        current_digest_prefix=digest,
        current_length=length,
        sources=sources,
        comparable=status == SourceStatus.CONSISTENT,
        status=status.value,
    )


def discover_hermes_api_key() -> SecretSourceReport:
    compose_env_path = os.path.abspath(os.path.join("compose", ".env"))
    container_env_path = os.path.abspath(".env")
    compose_env = _read_env_file(compose_env_path)
    container_env = _read_env_file(container_env_path)
    candidates = {
        "compose_env": compose_env.get("HERMES_API_KEY"),
        "working_dir_env": container_env.get("HERMES_API_KEY"),
    }
    current = next((v for v in candidates.values() if v is not None), None)
    length = len(current) if current is not None else 0
    digest = _sha256_prefix(current) if current else ""
    sources = [
        _source_report(
            name,
            compose_env_path if name == "compose_env" else container_env_path,
            value,
        ).__dict__
        for name, value in candidates.items()
    ]
    status = _classify(candidates, digest)
    return SecretSourceReport(
        key="HERMES_API_KEY",
        current_digest_prefix=digest,
        current_length=length,
        sources=sources,
        comparable=status == SourceStatus.CONSISTENT,
        status=status.value,
    )


def _active_api_runs() -> int:
    try:
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
            "status": api_server.status,
            "sources": api_server.sources,
        },
        "HERMES_API_KEY": {
            "digest_prefix": hermes.current_digest_prefix,
            "length": hermes.current_length,
            "comparable": hermes.comparable,
            "status": hermes.status,
            "sources": hermes.sources,
        },
    }


def _validate_plan_paths(plan: RotationPlan) -> None:
    for p in plan.changed_paths + plan.backup_paths:
        if not Path(p).is_absolute():
            raise ValueError(f"plan path is not absolute: {p}")
        real = os.path.realpath(p)
        if os.path.islink(p):
            raise ValueError(f"plan path is a symlink (rejected): {p}")
        if real != p:
            raise ValueError(f"plan path is not canonical: {p} -> {real}")


def plan_rotation(
    key: str,
    new_value: str | None = None,
    *,
    force: bool = False,
    target_service: str | None = None,
) -> RotationPlan:
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
        report = discover_api_server_key()
        env_path = next(
            (s["path"] for s in report.sources if s["present"] and s["path"].startswith("/")),
            os.path.abspath(".env"),
        )
        changed = [env_path]
        backups = [env_path + ".pre-rotation"]
    else:
        changed = [os.path.abspath(os.path.join("compose", ".env")), os.path.abspath(".env")]
        backups = [c + ".pre-rotation" for c in changed]
    if target_service is None:
        target_service = _gateway_unit_name()
    plan = RotationPlan(
        key=key,
        new_value=new_value,
        changed_paths=changed,
        backup_paths=backups,
        plan_token=_plan_token(new_value) if new_value else "",
        dry_run=True,
        requires_restart=True,
        active_runs=active_runs,
        target_service=target_service,
    )
    _validate_plan_paths(plan)
    return plan


def apply_rotation(plan: RotationPlan) -> RotationPlan:
    if plan.dry_run:
        raise RuntimeError("apply_rotation requires dry_run=False")
    if plan.key not in {"API_SERVER_KEY", "HERMES_API_KEY"}:
        raise ValueError(plan.key)
    if plan.new_value is None:
        raise ValueError("new_value is required for apply")
    if not plan.plan_token or plan.plan_token != _plan_token(plan.new_value):
        raise ValueError("plan token mismatch: plan was tampered or manually built")
    _validate_plan_paths(plan)

    # Re-validate active runs and health immediately on apply (reduce TOCTOU).
    active_runs = _active_api_runs()
    if active_runs != 0:
        raise RuntimeError(
            f"apply aborted: active_api_runs={active_runs} detected at apply time"
        )

    backups: list[str] = []
    changed: list[str] = []
    for path_str in plan.changed_paths:
        p = Path(path_str)
        if not p.is_absolute():
            raise ValueError(f"unexpected non-absolute path: {path_str}")
        if not p.exists():
            continue
        lines = p.read_text(encoding="utf-8").splitlines()
        backup = Path(path_str + ".pre-rotation")
        p.replace(backup)
        backup.chmod(0o600)
        backups.append(str(backup))
        updated: list[str] = []
        replaced = False
        prefix = f"{plan.key}="
        for line in lines:
            if line.startswith(prefix):
                updated.append(f"{plan.key}={plan.new_value}")
                replaced = True
            else:
                updated.append(line)
        if not replaced:
            updated.append(f"{plan.key}={plan.new_value}")
        p.write_text("\n".join(updated) + "\n", encoding="utf-8")
        p.chmod(0o600)
        changed.append(path_str)

    return RotationPlan(
        key=plan.key,
        new_value=plan.new_value,
        changed_paths=changed,
        backup_paths=backups,
        plan_token=plan.plan_token,
        dry_run=False,
        requires_restart=True,
        active_runs=active_runs,
        target_service=plan.target_service,
    )


def _short_unit_name(value: str, max_length: int = 55) -> str:
    value = value.strip()
    if len(value) <= max_length:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return f"rotation-{digest}"


def schedule_external_restart(
    target_service: str, timeout_seconds: int = 180
) -> dict[str, Any]:
    """Schedule an external restart of the REAL target service.

    Uses a transient systemd-run --user unit (name <=55 chars) that invokes a
    private, mode-0700 script. The transient unit name is separate from the real
    target service name; target_service is never truncated or altered.
    """
    transient = _short_unit_name(f"bridge-restart-{target_service}")
    if len(transient) > 55:
        digest = hashlib.sha256(target_service.encode("utf-8")).hexdigest()[:8]
        transient = f"bridge-restart-{digest}"
    fd, script_path = tempfile.mkstemp(prefix="bridge-restart-", suffix=".sh")
    os.close(fd)
    Path(script_path).write_text(
        f"#!/usr/bin/env bash\nset -euo pipefail\n"
        f"systemctl --user restart {shlex.quote(target_service)}\n",
        encoding="utf-8",
    )
    Path(script_path).chmod(0o700)
    argv = [
        "systemd-run",
        "--user",
        f"--unit={transient}",
        "--timer-property=AccuracySec=1s",
        f"--property=TimeoutStopSec={timeout_seconds}",
        "/bin/bash",
        script_path,
    ]
    result: dict[str, Any] = {
        "target_service": target_service,
        "transient_unit": transient,
        "script_path": script_path,
        "argv": argv,
    }
    try:
        out = subprocess.check_output(argv, text=True)
        result["output"] = out.strip()
        result["status"] = "scheduled"
    except subprocess.CalledProcessError as exc:
        result["status"] = "failed"
        result["error"] = exc.stderr or exc.output
    return result


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
        dst_name = name[: -len(".pre-rotation")] if name.endswith(".pre-rotation") else name
        dst = src.with_name(dst_name)
        if not dst.is_absolute():
            continue
        if dst.exists():
            dst.unlink(missing_ok=True)
        src.replace(dst)
        dst.chmod(0o600)
        restored.append(str(dst))
    return {
        "status": "ok",
        "restored": restored,
        "next": "recreate bridge/compose service externally if needed; then run verify_rotation",
        "note": "operational reconciliation (restart/verify) is external and not declared here",
    }
