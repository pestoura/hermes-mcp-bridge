"""Out-of-band orchestration template for the V2 Phase 2 state-integrity probe.

Repo-side foundation only. Nothing here is wired into the canonical connected
gate, and nothing here executes a real out-of-band acceptance: in repo tests the
planner runs exclusively in **dry-run / fixture** mode.

Why out-of-band
---------------

While the Hermes control run is alive it keeps writing to its own state
database (sessions, messages, token accounting). A measurement taken from
inside that run can therefore never prove *absolute zero delta*. The only sound
shape is a transient, delayed one-shot that starts after the control run has
ended, takes the before/after snapshots itself, and writes a sanitized terminal
marker the operator reads later.

Safety contract enforced by :func:`build_transient_unit_plan`
-------------------------------------------------------------

* transient **user** scope (``systemd-run --user``), ``Type=oneshot``;
* ``Restart=no`` — a probe must never be resurrected;
* ``UMask=0077`` — marker/result files are operator-private;
* an explicit ``RuntimeMaxSec`` timeout, always > 0 and bounded;
* delayed start via ``--on-active`` so the control run can end first;
* **no secret material in ``ExecStart`` argv and no ``Environment=``
  assignments at all**; the probe reads what it needs from the filesystem under
  the operator's own credentials;
* an atomic sanitized terminal marker: the result document is written to a
  temporary file in the same directory and ``os.replace``-d into place, so a
  reader never observes a partial document;
* :func:`cleanup_run_artifacts` is idempotent — running it twice, or before the
  probe ever ran, is a no-op.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

OUT_OF_BAND_SCHEMA: Final[str] = "hermes-v2-phase2-out-of-band-state-integrity/1"

#: Terminal marker states. ``PENDING`` is never written as a terminal marker.
MARKER_COMPLETED: Final[str] = "COMPLETED"
MARKER_FAILED: Final[str] = "FAILED"
TERMINAL_MARKERS: Final[frozenset[str]] = frozenset({MARKER_COMPLETED, MARKER_FAILED})

#: Bounds for the delayed start and the probe timeout, in seconds.
MIN_DELAY_SECONDS: Final[int] = 1
MAX_DELAY_SECONDS: Final[int] = 3600
MIN_TIMEOUT_SECONDS: Final[int] = 1
MAX_TIMEOUT_SECONDS: Final[int] = 900

_UNIT_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

#: Argument/identifier tokens that must never appear in a probe invocation.
FORBIDDEN_ARGV_TOKENS: Final[tuple[str, ...]] = (
    "token",
    "secret",
    "password",
    "passwd",
    "apikey",
    "api_key",
    "api-key",
    "bearer",
    "authorization",
    "private_key",
    "privatekey",
    "credential",
    "pem",
)

REASON_CODES: Final[frozenset[str]] = frozenset(
    {
        "OOB_UNIT_NAME_INVALID",
        "OOB_DELAY_OUT_OF_RANGE",
        "OOB_TIMEOUT_OUT_OF_RANGE",
        "OOB_PATH_NOT_ABSOLUTE",
        "OOB_SECRET_BEARING_ARGUMENT",
        "OOB_ENVIRONMENT_NOT_EMPTY",
        "OOB_EXECUTION_NOT_PERMITTED",
        "OOB_MARKER_STATE_INVALID",
        "OOB_MARKER_WRITE_FAILED",
    }
)


class OutOfBandError(RuntimeError):
    """Fail-closed orchestration failure identified only by a stable code."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if code not in REASON_CODES:  # pragma: no cover - defensive
            code = "OOB_EXECUTION_NOT_PERMITTED"
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"OutOfBandError({self.code!r})"


def _contains_secret_token(value: str) -> bool:
    lowered = value.lower()
    return any(token in lowered for token in FORBIDDEN_ARGV_TOKENS)


@dataclass(frozen=True, slots=True)
class TransientUnitPlan:
    """Fully-described transient one-shot. Building it never runs anything."""

    unit_name: str
    delay_seconds: int
    timeout_seconds: int
    working_directory: str
    result_path: str
    argv: tuple[str, ...]
    properties: tuple[tuple[str, str], ...]
    environment: tuple[tuple[str, str], ...] = field(default=())

    def property_map(self) -> dict[str, str]:
        return dict(self.properties)

    def systemd_run_argv(self) -> tuple[str, ...]:
        """Return the exact ``systemd-run`` argv this plan would execute."""
        command: list[str] = [
            "systemd-run",
            "--user",
            f"--unit={self.unit_name}",
            "--collect",
            f"--on-active={self.delay_seconds}s",
            "--timer-property=AccuracySec=1s",
        ]
        for key, value in self.properties:
            command.append(f"--property={key}={value}")
        command.extend(self.argv)
        return tuple(command)

    def command_line(self) -> str:
        """Human-readable, shell-quoted rendering for dry-run output."""
        return " ".join(shlex.quote(item) for item in self.systemd_run_argv())

    def as_canonical(self) -> dict[str, Any]:
        return {
            "schema": OUT_OF_BAND_SCHEMA,
            "unit_name": self.unit_name,
            "delay_seconds": self.delay_seconds,
            "timeout_seconds": self.timeout_seconds,
            "properties": [list(item) for item in self.properties],
            "environment_count": len(self.environment),
            "argv_length": len(self.argv),
        }


def build_transient_unit_plan(
    *,
    unit_name: str,
    probe_argv: tuple[str, ...] | list[str],
    working_directory: str | os.PathLike[str],
    result_path: str | os.PathLike[str],
    delay_seconds: int,
    timeout_seconds: int,
) -> TransientUnitPlan:
    """Validate and build a transient one-shot plan. Executes nothing."""
    if not isinstance(unit_name, str) or _UNIT_NAME_RE.fullmatch(unit_name) is None:
        raise OutOfBandError("OOB_UNIT_NAME_INVALID")
    if (
        isinstance(delay_seconds, bool)
        or not isinstance(delay_seconds, int)
        or not MIN_DELAY_SECONDS <= delay_seconds <= MAX_DELAY_SECONDS
    ):
        raise OutOfBandError("OOB_DELAY_OUT_OF_RANGE")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or not MIN_TIMEOUT_SECONDS <= timeout_seconds <= MAX_TIMEOUT_SECONDS
    ):
        raise OutOfBandError("OOB_TIMEOUT_OUT_OF_RANGE")

    workdir = Path(working_directory)
    result = Path(result_path)
    if not workdir.is_absolute() or not result.is_absolute():
        raise OutOfBandError("OOB_PATH_NOT_ABSOLUTE")

    argv = tuple(str(item) for item in probe_argv)
    if not argv:
        raise OutOfBandError("OOB_EXECUTION_NOT_PERMITTED")
    if not Path(argv[0]).is_absolute():
        raise OutOfBandError("OOB_PATH_NOT_ABSOLUTE")
    for item in argv:
        if _contains_secret_token(item):
            raise OutOfBandError("OOB_SECRET_BEARING_ARGUMENT")

    properties: tuple[tuple[str, str], ...] = (
        ("Type", "oneshot"),
        ("Restart", "no"),
        ("UMask", "0077"),
        ("RuntimeMaxSec", str(timeout_seconds)),
        ("WorkingDirectory", str(workdir)),
        ("PrivateTmp", "yes"),
        ("NoNewPrivileges", "yes"),
        ("ProtectSystem", "strict"),
        ("ProtectHome", "read-only"),
        ("StandardOutput", "null"),
        ("StandardError", "journal"),
    )
    return TransientUnitPlan(
        unit_name=unit_name,
        delay_seconds=delay_seconds,
        timeout_seconds=timeout_seconds,
        working_directory=str(workdir),
        result_path=str(result),
        argv=argv,
        properties=properties,
        environment=(),
    )


def assert_plan_is_secret_free(plan: TransientUnitPlan) -> None:
    """Fail closed if a plan carries environment assignments or secret argv."""
    if plan.environment:
        raise OutOfBandError("OOB_ENVIRONMENT_NOT_EMPTY")
    for item in plan.systemd_run_argv():
        if item.startswith("--setenv") or item.startswith("--property=Environment"):
            raise OutOfBandError("OOB_ENVIRONMENT_NOT_EMPTY")
        if _contains_secret_token(item):
            raise OutOfBandError("OOB_SECRET_BEARING_ARGUMENT")


def dry_run_report(plan: TransientUnitPlan) -> dict[str, Any]:
    """Return the sanitized dry-run description of a plan. Runs nothing."""
    assert_plan_is_secret_free(plan)
    report = dict(plan.as_canonical())
    report["mode"] = "DRY_RUN"
    report["executed"] = False
    return report


def schedule_transient_unit(
    plan: TransientUnitPlan,
    *,
    execute: bool = False,
    runner: Any = None,
) -> dict[str, Any]:
    """Dry-run by default. Real execution requires ``execute=True`` and a runner.

    The repo test-suite never passes ``execute=True``; the dual gate exists so a
    future operator-driven acceptance can reuse this code path unchanged.
    """
    assert_plan_is_secret_free(plan)
    if not execute:
        return dry_run_report(plan)
    if runner is None:
        raise OutOfBandError("OOB_EXECUTION_NOT_PERMITTED")
    result = runner(plan.systemd_run_argv())
    report = dict(plan.as_canonical())
    report["mode"] = "EXECUTED"
    report["executed"] = True
    report["runner_result"] = result
    return report


def write_terminal_marker(
    result_path: str | os.PathLike[str],
    *,
    state: str,
    payload: dict[str, Any],
) -> Path:
    """Atomically write the sanitized terminal marker document.

    The document is written to a temporary file in the destination directory
    with mode ``0o600`` and then ``os.replace``-d, so readers observe either the
    previous document or the complete new one, never a partial write.
    """
    if state not in TERMINAL_MARKERS:
        raise OutOfBandError("OOB_MARKER_STATE_INVALID")
    if not isinstance(payload, dict):
        raise OutOfBandError("OOB_MARKER_STATE_INVALID")
    for key in payload:
        if _contains_secret_token(str(key)):
            raise OutOfBandError("OOB_SECRET_BEARING_ARGUMENT")

    destination = Path(result_path)
    if not destination.is_absolute():
        raise OutOfBandError("OOB_PATH_NOT_ABSOLUTE")
    document = {
        "schema": OUT_OF_BAND_SCHEMA,
        "state": state,
        **payload,
    }
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=str(destination.parent),
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(document, stream, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, destination)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise
    except OSError as exc:
        raise OutOfBandError("OOB_MARKER_WRITE_FAILED") from exc
    return destination


def read_terminal_marker(result_path: str | os.PathLike[str]) -> dict[str, Any] | None:
    """Return the terminal marker document, or ``None`` when absent/partial."""
    path = Path(result_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("schema") != OUT_OF_BAND_SCHEMA:
        return None
    if payload.get("state") not in TERMINAL_MARKERS:
        return None
    return payload


def cleanup_run_artifacts(
    *paths: str | os.PathLike[str],
) -> tuple[str, ...]:
    """Remove probe artifacts. Idempotent: missing paths are not an error.

    Returns the tuple of artifact **basenames** that existed and were removed,
    so a caller can log progress without leaking directories.
    """
    removed: list[str] = []
    for item in paths:
        path = Path(item)
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except IsADirectoryError:  # pragma: no cover - defensive
            continue
        except OSError:  # pragma: no cover - best effort
            continue
        removed.append(path.name)
    return tuple(removed)


__all__ = [
    "FORBIDDEN_ARGV_TOKENS",
    "MARKER_COMPLETED",
    "MARKER_FAILED",
    "MAX_DELAY_SECONDS",
    "MAX_TIMEOUT_SECONDS",
    "MIN_DELAY_SECONDS",
    "MIN_TIMEOUT_SECONDS",
    "OUT_OF_BAND_SCHEMA",
    "REASON_CODES",
    "TERMINAL_MARKERS",
    "OutOfBandError",
    "TransientUnitPlan",
    "assert_plan_is_secret_free",
    "build_transient_unit_plan",
    "cleanup_run_artifacts",
    "dry_run_report",
    "read_terminal_marker",
    "schedule_transient_unit",
    "write_terminal_marker",
]
