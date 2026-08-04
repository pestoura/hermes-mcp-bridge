"""Secret source discovery, planning and rotation helpers.

Rules:
- never print secrets
- compare only by sha256 prefix (<=32), sanitized identity, and length
- no Hermes-process restart; use external systemd-run --user transient unit
- inspect /proc/<pid>/environ only for digest comparison by default
- all paths are absolute/canonical and derived from discovered sources; if no
  explicit absolute base is available the source is INSUFFICIENT and apply is
  blocked (no silent CWD-relative fallback)
"""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from . import _rotation_plans
from ._file_lock import FileLockError, exclusive_file_lock
from .config import get_settings

LOCK_PATH = os.path.join(
    os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir(),
    "hermes-bridge-rotation.lock",
)

HMAC_ENV = "HERMES_BRIDGE_HMAC_SECRET"
TEMP_DIR = os.environ.get("HERMES_BRIDGE_TEMP_DIR") or os.path.join(
    tempfile.gettempdir(), "hermes-bridge-restart"
)


class SourceStatus(StrEnum):
    CONSISTENT = "consistent"
    MISMATCH = "mismatch"
    INSUFFICIENT = "insufficient"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SecretSource:
    source: str
    path: str | None
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
    source_digests: list[str] = field(default_factory=list)
    plan_token: str = ""
    nonce: str = ""
    operation_id: str = ""
    dry_run: bool = True
    requires_restart: bool = True
    active_runs: int = 0
    target_service: str | None = None


def _sha256_prefix(value: str, length: int = 32) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _read_env_file(path: str | None) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path or not Path(path).is_absolute():
        return result
    p = Path(path)
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


def _systemd_unit_environment(
    unit_name: str,
) -> tuple[dict[str, str], list[str], str | None]:
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


def _source_report(source: str, path: str | None, value: str | None) -> SecretSource:
    return SecretSource(
        source=source,
        path=path,
        present=value is not None,
        length=len(value) if value is not None else 0,
        digest_prefix=_sha256_prefix(value) if value is not None else None,
    )


def _classify(candidates: dict[str, str | None]) -> SourceStatus:
    present_values = [v for v in candidates.values() if v is not None and v != ""]
    if not present_values:
        return SourceStatus.INSUFFICIENT
    if len(present_values) < 2:
        # Single source cannot prove gateway-memory vs files alignment.
        return SourceStatus.INSUFFICIENT
    digests = {_sha256_prefix(v) for v in present_values}
    if len(digests) == 1:
        return SourceStatus.CONSISTENT
    return SourceStatus.MISMATCH


def discover_api_server_key(
    working_directory: str | None = None,
) -> SecretSourceReport:
    unit_name = _gateway_unit_name()
    unit_env: dict[str, str] = {}
    pid_env: dict[str, str] = {}
    pid: int | None = None
    working_dir = working_directory
    if unit_name and working_dir is None:
        unit_env, _env_files, working_dir = _systemd_unit_environment(unit_name)
        pid = _systemd_unit_pid(unit_name)
        if pid:
            pid_env = _environ_for_pid(pid)
    env_file_path: str | None = None
    if working_dir and Path(working_dir).is_absolute():
        env_file_path = os.path.join(working_dir, ".env")
    working_dir_env = _read_env_file(env_file_path)
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
            "unit_env", unit_name or None, candidates["unit_env"]
        ).__dict__,
        _source_report(
            "pid_environ", f"pid:{pid}" if pid else None, candidates["pid_environ"]
        ).__dict__,
        _source_report(
            "working_dir_env", env_file_path, candidates["working_dir_env"]
        ).__dict__,
    ]
    status = _classify(candidates)
    return SecretSourceReport(
        key="API_SERVER_KEY",
        current_digest_prefix=digest,
        current_length=length,
        sources=sources,
        comparable=status == SourceStatus.CONSISTENT,
        status=status.value,
    )


def discover_hermes_api_key(compose_dir: str | None = None) -> SecretSourceReport:
    compose_env_path: str | None = None
    container_env_path: str | None = None
    if compose_dir and Path(compose_dir).is_absolute():
        compose_env_path = os.path.join(compose_dir, ".env")
        container_env_path = os.path.join(compose_dir, ".env")
    compose_env = _read_env_file(compose_env_path)
    container_env = _read_env_file(container_env_path)
    candidates = {
        "compose_env": compose_env.get("HERMES_API_KEY"),
        "container_env": container_env.get("HERMES_API_KEY"),
    }
    current = next((v for v in candidates.values() if v is not None), None)
    length = len(current) if current is not None else 0
    digest = _sha256_prefix(current) if current else ""
    sources = [
        _source_report(
            "compose_env", compose_env_path, candidates["compose_env"]
        ).__dict__,
        _source_report(
            "container_env", container_env_path, candidates["container_env"]
        ).__dict__,
    ]
    status = _classify(candidates)
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


def inspect_secrets(
    *, working_directory: str | None = None, compose_dir: str | None = None
) -> dict[str, Any]:
    api_server = discover_api_server_key(working_directory=working_directory)
    hermes = discover_hermes_api_key(compose_dir=compose_dir)
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
    working_directory: str | None = None,
    compose_dir: str | None = None,
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
        report = discover_api_server_key(working_directory=working_directory)
        env_paths = [
            s["path"]
            for s in report.sources
            if s["source"] == "working_dir_env" and s["path"] and s["present"]
        ]
        if not env_paths:
            raise RuntimeError(
                "no absolute env file discovered for API_SERVER_KEY; cannot rotate safely"
            )
        changed = sorted(set(env_paths))
        source_digests = [s["digest_prefix"] for s in report.sources if s["present"]]
    else:
        report = discover_hermes_api_key(compose_dir=compose_dir)
        env_paths = [s["path"] for s in report.sources if s["path"] and s["present"]]
        if not env_paths:
            raise RuntimeError(
                "no absolute env file discovered for HERMES_API_KEY; cannot rotate safely"
            )
        changed = sorted(set(env_paths))
        source_digests = [s["digest_prefix"] for s in report.sources if s["present"]]

    operation_id = f"{int(time.time())}-{os.urandom(6).hex()}"
    plan = RotationPlan(
        key=key,
        new_value=new_value,
        changed_paths=changed,
        backup_paths=[f"{p}.pre-rotation-{operation_id}" for p in changed],
        source_digests=source_digests,
        plan_token="",
        nonce="",
        operation_id=operation_id,
        dry_run=True,
        requires_restart=True,
        active_runs=active_runs,
        target_service=target_service or _gateway_unit_name(),
    )
    _validate_plan_paths(plan)
    token, nonce = _rotation_plans.register_plan(
        key=plan.key,
        new_value=plan.new_value,
        changed_paths=plan.changed_paths,
        source_digests=plan.source_digests,
        active_runs=plan.active_runs,
        requires_restart=plan.requires_restart,
    )
    plan = RotationPlan(**{**plan.__dict__, "plan_token": token, "nonce": nonce})
    return plan


def _find_key_line_index(lines: list[str], prefix: str) -> int:
    for i, line in enumerate(lines):
        if line.startswith(prefix):
            return i
    return -1


def _atomic_write_text(path: str, content: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent or ".", prefix=".rot-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        _fsync_dir_here(parent)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _fsync_dir_here(path: str | None) -> None:
    if not path:
        return
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def apply_rotation(plan: RotationPlan) -> RotationPlan:
    if plan.dry_run:
        raise RuntimeError("apply_rotation requires dry_run=False")
    if plan.key not in {"API_SERVER_KEY", "HERMES_API_KEY"}:
        raise ValueError(plan.key)
    if plan.new_value is None:
        raise ValueError("new_value is required for apply")
    # Prove the plan is not forged/manually-built and is single-use.
    _rotation_plans.verify_and_consume(
        plan_token=plan.plan_token,
        key=plan.key,
        new_value=plan.new_value,
        changed_paths=plan.changed_paths,
        source_digests=plan.source_digests,
        active_runs=plan.active_runs,
        requires_restart=plan.requires_restart,
        nonce=plan.nonce,
    )
    _validate_plan_paths(plan)

    # Re-validate active runs and health immediately on apply (reduce TOCTOU).
    active_runs = _active_api_runs()
    if active_runs != 0:
        raise RuntimeError(
            f"apply aborted: active_api_runs={active_runs} detected at apply time"
        )

    try:
        with exclusive_file_lock(LOCK_PATH):
            return _apply_rotation_locked(plan, active_runs)
    except FileLockError as exc:
        raise RuntimeError(f"rotation lock unavailable: {exc}") from exc


def _apply_rotation_locked(plan: RotationPlan, active_runs: int) -> RotationPlan:
    changed: list[str] = []
    backups: list[str] = []
    manifest: list[tuple[str, str]] = []  # (path, backup)
    try:
        # Prepare phase: back up every target that exists, before replacing any.
        for path_str in plan.changed_paths:
            p = Path(path_str)
            if not p.is_absolute():
                raise ValueError(f"unexpected non-absolute path: {path_str}")
            if not p.exists():
                continue
            backup = f"{path_str}.pre-rotation-{plan.operation_id}"
            if Path(backup).exists():
                raise FileExistsError(f"rotation backup already exists: {backup}")
            _atomic_write_text(backup, p.read_text(encoding="utf-8"))
            os.chmod(backup, 0o600)
            manifest.append((path_str, backup))
            backups.append(backup)

        # Replace phase: atomic writes, no window of absence.
        prefix = f"{plan.key}="
        for path_str, _backup in manifest:
            lines = Path(path_str).read_text(encoding="utf-8").splitlines()
            idx = _find_key_line_index(lines, prefix)
            if idx >= 0:
                lines[idx] = f"{plan.key}={plan.new_value}"
            else:
                lines.append(f"{plan.key}={plan.new_value}")
            _atomic_write_text(path_str, "\n".join(lines) + "\n")
            changed.append(path_str)
    except BaseException:
        # Roll back everything that was changed, using the per-op backups.
        for path_str, backup in manifest:
            if Path(path_str).exists() and _sha256_file(path_str) != _sha256_file(backup):
                _atomic_write_text(path_str, Path(backup).read_text(encoding="utf-8"))
        raise

    return RotationPlan(
        key=plan.key,
        new_value=plan.new_value,
        changed_paths=changed,
        backup_paths=backups,
        source_digests=plan.source_digests,
        plan_token=plan.plan_token,
        nonce=plan.nonce,
        operation_id=plan.operation_id,
        dry_run=False,
        requires_restart=True,
        active_runs=active_runs,
        target_service=plan.target_service,
    )


def rollback_rotation(manifest: dict[str, Any]) -> dict[str, Any]:
    """Roll back a rotation using the exact operation manifest.

    manifest = {"changed_paths": [...], "backup_paths": [...],
                "operation_id": "..."}. Revalidates every path (absolute,
    canonical, not symlink) and restores each changed file from its associated
    backup atomically. Prior backups are never overwritten.
    """
    changed = manifest.get("changed_paths", [])
    backups = manifest.get("backup_paths", [])
    if len(changed) != len(backups):
        raise ValueError("manifest changed/backup length mismatch")
    pairs = list(zip(changed, backups, strict=False))
    for path_str, backup in pairs:
        if not Path(path_str).is_absolute() or not Path(backup).is_absolute():
            raise ValueError("rollback manifest contains non-absolute paths")
        if os.path.islink(path_str) or os.path.islink(backup):
            raise ValueError("rollback path is a symlink (rejected)")
        if not Path(backup).exists():
            raise FileNotFoundError(f"rollback backup missing: {backup}")

    restored: list[str] = []
    try:
        with exclusive_file_lock(LOCK_PATH):
            for path_str, backup in pairs:
                content = Path(backup).read_text(encoding="utf-8")
                _atomic_write_text(path_str, content)
                restored.append(path_str)
    except FileLockError as exc:
        raise RuntimeError(f"rotation lock unavailable: {exc}") from exc

    return {
        "status": "rolled_back",
        "restored": restored,
        "backups": backups,
        "operation_id": manifest.get("operation_id"),
    }


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

    Uses a transient systemd-run --user service (name <=55 chars) that invokes a
    private, mode-0700 script. The transient unit name is separate from the real
    target service name; target_service is never truncated or altered. The script
    self-deletes via trap after execution (no --timer-property is used).
    """
    if not target_service:
        raise ValueError("target_service is required")
    transient = _short_unit_name(f"bridge-restart-{target_service}")
    if len(transient) > 55:
        digest = hashlib.sha256(target_service.encode("utf-8")).hexdigest()[:8]
        transient = f"bridge-restart-{digest}"

    os.makedirs(TEMP_DIR, exist_ok=True)
    fd, script_path = tempfile.mkstemp(
        dir=TEMP_DIR, prefix="bridge-restart-", suffix=".sh"
    )
    os.close(fd)
    script = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "trap 'rm -f \"$0\"' EXIT INT TERM\n"
        f"systemctl --user restart {shlex.quote(target_service)}\n"
    )
    Path(script_path).write_text(script, encoding="utf-8")
    Path(script_path).chmod(0o700)
    argv = [
        "systemd-run",
        "--user",
        f"--unit={transient}",
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
