#!/usr/bin/env python3
"""OUTER final out-of-band acceptance runner for V2 Phase 2.

Shape
-----

The runner is designed to be executed by a **transient user-systemd one-shot**
scheduled with a delayed start, so it begins only after the scheduling Hermes
control run has fully ended. It then:

1. guards: aborts *before* the PRE measurement when active API runs or
   delegations indicate the control run is still alive;
2. PRE-measures the **real** Hermes state database strictly read-only;
3. invokes the existing inner connected launcher unchanged (the inner
   semantic/economics gate — this runner never reimplements or relaxes it);
4. POST-measures the real state database the same way;
5. requires absolute zero delta on the tracked real tables, stable schema/user
   versions, stable size/mtime and equal non-empty per-run salted fingerprints,
   with no exclusions or allowlists for any control session;
6. records that the disposable **shadow** state database was positively active,
   as a boolean plus a row-count delta captured before inner cleanup — never
   row contents;
7. writes an atomic ``0600`` sanitized result/status document.

Modes
-----

``plan``     print the sanitized transient unit plan (default; runs nothing).
``execute``  perform the guarded PRE/inner/POST acceptance. Requires the
             internal ``--i-understand-this-runs-a-real-acceptance`` flag *and*
             ``HERMES_V2_FINAL_EXECUTE=YES``.

Nothing here is scheduled by the repository. No path, row content, salt or
session id ever reaches the emitted documents.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:  # pragma: no cover - path shim
    sys.path.insert(0, str(_SRC))

from hermes_mcp_bridge.v2.control_activity import (  # noqa: E402
    STATUS_ACTIVE,
    STATUS_QUIET,
    STATUS_UNMEASURABLE,
    evaluate_control_activity,
)
from hermes_mcp_bridge.v2.final_gate import (  # noqa: E402
    REQUIRED_ZERO_DELTA_TABLES,
    STATE_INTEGRITY_DOC_SCHEMA,
)
from hermes_mcp_bridge.v2.out_of_band import (  # noqa: E402
    MARKER_COMPLETED,
    MARKER_FAILED,
    OutOfBandError,
    build_transient_unit_plan,
    dry_run_report,
    write_terminal_marker,
)
from hermes_mcp_bridge.v2.shadow_witness import (  # noqa: E402
    WITNESS_ENV,
    consume_witness,
)
from hermes_mcp_bridge.v2.state_integrity import (  # noqa: E402
    StateIntegrityError,
    capture_state_snapshot,
    compare_snapshots,
    new_run_salt,
    read_state_metadata,
)

EXECUTE_ENV = "HERMES_V2_FINAL_EXECUTE"
EXECUTE_ENV_VALUE = "YES"

#: Stable, secret-free blocker codes emitted by this runner.
REASONS = frozenset(
    {
        "FINAL_EXECUTION_NOT_PERMITTED",
        "FINAL_CONTROL_ACTIVITY_DETECTED",
        "FINAL_CONTROL_GUARD_UNAVAILABLE",
        "FINAL_STATE_MEASUREMENT_FAILED",
        "FINAL_STATE_DELTA_DETECTED",
        "FINAL_INNER_LAUNCHER_FAILED",
        "FINAL_INNER_LAUNCHER_MISSING",
        "FINAL_SHADOW_ACTIVITY_NOT_OBSERVED",
        "FINAL_SHADOW_WITNESS_INVALID",
        "FINAL_RESULT_WRITE_FAILED",
        "FINAL_BACKGROUND_WRITER_UNCONTROLLED",
    }
)

#: Blocker D. An idle real ``hermes-gateway.service`` is NOT provably
#: write-free against the tracked source tables: the gateway housekeeping loop
#: can flip ``sessions.archived`` (``maybe_auto_archive``) and the session
#: expiry watcher can write ``sessions.expiry_finalized`` without any user
#: interaction. Both are periodic timers, not request-driven, so a short
#: "no-op" measurement window does not exclude them. The runner therefore
#: refuses to start unless the operator asserts, out of band, that the
#: background writer was controlled for the whole window. Zero absolute delta
#: is NEVER relaxed; this precondition only prevents a false ACCEPTED or a
#: spurious FINAL_STATE_DELTA_DETECTED caused by an uncontrolled writer.
#: See docs/v2/phase2-final-outer-gate.md for what must be controlled.
BACKGROUND_WRITER_CONTROLLED_FLAG: Final[str] = "--background-writer-controlled"


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _now() -> str:
    return datetime.now(UTC).isoformat()


# --------------------------------------------------------------------------
# control-activity guard
# --------------------------------------------------------------------------


def control_activity_detected(state_db: str) -> bool | None:
    """Backwards-compatible boolean view over :func:`evaluate_control_activity`.

    ``True``  live control activity (ACTIVE).
    ``False`` no live indicator (QUIET).
    ``None``  the guard could not answer (UNMEASURABLE) — always fatal.
    """
    report = evaluate_control_activity(state_db)
    if report.status == STATUS_ACTIVE:
        return True
    if report.status == STATUS_QUIET:
        return False
    return None


# --------------------------------------------------------------------------
# shadow activity (positive proof, no row contents)
# --------------------------------------------------------------------------


def shadow_row_count(shadow_state_db: str) -> int | None:
    """Total tracked-table row count in the disposable shadow database.

    Only used for the pre-run baseline the outer runner can still take while
    the shadow home exists. The post-run count is never read here: the inner
    launcher deletes its shadow home in its own EXIT trap, so the positive
    control arrives through the sanitized witness handoff instead.
    """
    path = Path(shadow_state_db)
    if not path.is_file():
        return None
    uri = f"file:{path.as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        connection.execute("PRAGMA query_only = ON")
    except sqlite3.Error:
        return None
    total = 0
    try:
        present = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        for table in REQUIRED_ZERO_DELTA_TABLES:
            if table not in present:
                continue
            row = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
            if row is not None:
                total += int(row[0])
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    return total


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------


def _cmd_plan(args: argparse.Namespace) -> int:
    witness = args.shadow_witness or str(
        Path(args.result).absolute().parent / "final-shadow-witness.json"
    )
    plan = build_transient_unit_plan(
        unit_name=args.unit_name,
        probe_argv=(
            str(Path(sys.executable).absolute()),
            str(Path(__file__).absolute()),
            "execute",
            "--state-db",
            args.state_db,
            "--shadow-state-db",
            args.shadow_state_db,
            "--inner-launcher",
            args.inner_launcher,
            "--result",
            args.result,
            "--source-commit",
            args.source_commit,
            "--shadow-witness",
            witness,
            "--i-understand-this-runs-a-real-acceptance",
            BACKGROUND_WRITER_CONTROLLED_FLAG,
        ),
        working_directory=args.working_directory,
        result_path=args.result,
        delay_seconds=args.delay_seconds,
        timeout_seconds=args.timeout_seconds,
        # ProtectHome stays read-only. These are the only writable grants: the
        # canonical acceptance work directory and the directory that receives
        # the sanitized result marker. Both are non-secret by contract.
        writable_paths=_sandbox_writable_paths(args),
        # The credential source (the operator's real Hermes home) is mounted
        # read-only and must never appear as a writable grant.
        read_only_paths=(str(Path(args.state_db).absolute().parent),),
    )
    report = dry_run_report(plan)
    report["command_line"] = plan.command_line()
    report["execution_env_required"] = f"{EXECUTE_ENV}={EXECUTE_ENV_VALUE}"
    report["background_writer_precondition"] = "FINAL_BACKGROUND_WRITER_UNCONTROLLED"
    _emit(report)
    return 0


def _sandbox_writable_paths(args: argparse.Namespace) -> tuple[str, ...]:
    """Minimal writable set: the acceptance workdir and the result directory."""
    candidates = (
        Path(args.working_directory).absolute(),
        Path(args.result).absolute().parent,
    )
    ordered: list[str] = []
    for item in candidates:
        text = str(item)
        if text not in ordered:
            ordered.append(text)
    return tuple(ordered)


# --------------------------------------------------------------------------
# execute
# --------------------------------------------------------------------------


def _fail(result_path: str, reason: str) -> int:
    with contextlib.suppress(OutOfBandError):
        write_terminal_marker(
            str(Path(result_path).absolute()),
            state=MARKER_FAILED,
            payload={"reason": reason},
        )
    _emit({"state": MARKER_FAILED, "reason": reason})
    return 2


def _cmd_execute(args: argparse.Namespace) -> int:
    result_path = str(Path(args.result).absolute())
    if not args.confirmed or os.environ.get(EXECUTE_ENV) != EXECUTE_ENV_VALUE:
        return _fail(result_path, "FINAL_EXECUTION_NOT_PERMITTED")
    # Blocker D: never proceed silently when the real gateway could write to
    # the tracked tables during the window. Zero absolute delta is unchanged.
    if not args.background_writer_controlled:
        return _fail(result_path, "FINAL_BACKGROUND_WRITER_UNCONTROLLED")

    state_db = str(Path(args.state_db).absolute())
    launcher = Path(args.inner_launcher).absolute()
    if not launcher.is_file():
        return _fail(result_path, "FINAL_INNER_LAUNCHER_MISSING")

    guard = evaluate_control_activity(state_db)
    if guard.status == STATUS_UNMEASURABLE:
        return _fail(result_path, "FINAL_CONTROL_GUARD_UNAVAILABLE")
    if guard.status != STATUS_QUIET:
        return _fail(result_path, "FINAL_CONTROL_ACTIVITY_DETECTED")

    salt = new_run_salt()
    try:
        before_meta = read_state_metadata(state_db)
        before = capture_state_snapshot(state_db, metadata=before_meta, salt=salt)
    except StateIntegrityError:
        return _fail(result_path, "FINAL_STATE_MEASUREMENT_FAILED")
    measured_before_at = _now()

    # Acceptance-only private handoff: the inner launcher writes the sanitized
    # shadow-activity witness here, before its own cleanup destroys the shadow
    # home. The path is outside SHADOW_HOME and is consumed exactly once.
    handoff = Path(args.shadow_witness).absolute()
    with contextlib.suppress(OSError):
        handoff.unlink()
    launcher_env = dict(os.environ)
    launcher_env[WITNESS_ENV] = str(handoff)

    inner_started_at = _now()
    completed = subprocess.run(
        [str(launcher)],
        capture_output=True,
        text=True,
        check=False,
        timeout=args.inner_timeout_seconds,
        env=launcher_env,
    )
    inner_finished_at = _now()

    witness = consume_witness(handoff, expected_commit=args.source_commit)

    try:
        after_meta = read_state_metadata(state_db)
        after = capture_state_snapshot(state_db, metadata=after_meta, salt=salt)
        comparison = compare_snapshots(before, after)
    except StateIntegrityError:
        return _fail(result_path, "FINAL_STATE_MEASUREMENT_FAILED")
    measured_after_at = _now()

    if completed.returncode != 0:
        return _fail(result_path, "FINAL_INNER_LAUNCHER_FAILED")
    if not comparison.unchanged:
        return _fail(result_path, "FINAL_STATE_DELTA_DETECTED")
    if witness is None:
        return _fail(result_path, "FINAL_SHADOW_WITNESS_INVALID")
    shadow_delta = int(witness["sessions_row_delta"]) + int(
        witness["session_model_usage_row_delta"]
    )
    if shadow_delta <= 0:
        return _fail(result_path, "FINAL_SHADOW_ACTIVITY_NOT_OBSERVED")

    document = build_state_integrity_document(
        source_commit=args.source_commit,
        comparison=comparison,
        before_digest=before.digest,
        after_digest=after.digest,
        measured_before_at=measured_before_at,
        measured_after_at=measured_after_at,
        inner_started_at=inner_started_at,
        inner_finished_at=inner_finished_at,
        shadow_row_count_delta=shadow_delta,
        witness=witness,
    )
    try:
        write_terminal_marker(
            result_path,
            state=MARKER_COMPLETED,
            payload={"state_integrity": document},
        )
    except OutOfBandError:
        return _fail(result_path, "FINAL_RESULT_WRITE_FAILED")
    _emit({"state": MARKER_COMPLETED, "state_integrity": document})
    return 0


def build_state_integrity_document(
    *,
    source_commit: str,
    comparison: Any,
    before_digest: str,
    after_digest: str,
    measured_before_at: str,
    measured_after_at: str,
    inner_started_at: str,
    inner_finished_at: str,
    shadow_row_count_delta: int,
    witness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the sanitized state-integrity document consumed by the final gate."""
    canonical = comparison.as_canonical()
    row_deltas = {
        str(item["name"]): int(item["row_count_delta"] or 0)
        for item in canonical["tables"]
        if str(item["name"]) in REQUIRED_ZERO_DELTA_TABLES
    }
    return {
        "schema": STATE_INTEGRITY_DOC_SCHEMA,
        "source_commit": source_commit,
        "measured_out_of_band": True,
        "read_only": True,
        "measurement_self_write_observed": False,
        "control_activity_detected": False,
        "exclusions_applied": False,
        "fingerprint_before": before_digest,
        "fingerprint_after": after_digest,
        "user_version_changed": bool(canonical["user_version_changed"]),
        "sqlite_schema_version_changed": bool(canonical["sqlite_schema_version_changed"]),
        "size_changed": bool(canonical["size_changed"]),
        "mtime_changed": bool(canonical["mtime_changed"]),
        "row_deltas": row_deltas,
        "shadow_state_activity_observed": shadow_row_count_delta > 0,
        "shadow_row_count_delta": int(shadow_row_count_delta),
        "shadow_db_distinct_from_source": bool(
            (witness or {}).get("shadow_db_distinct_from_source", False)
        ),
        "shadow_db_disposable": bool(
            (witness or {}).get("shadow_db_inside_disposable_home", False)
        ),
        "background_writer_controlled": True,
        "measured_before_at": measured_before_at,
        "measured_after_at": measured_after_at,
        "inner_started_at": inner_started_at,
        "inner_finished_at": inner_finished_at,
        "paths_stored": False,
        "row_contents_stored": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="print the sanitized transient unit plan")
    plan.add_argument("--unit-name", default="hermes-v2-final-oob-acceptance")
    plan.add_argument("--state-db", required=True)
    plan.add_argument("--shadow-state-db", required=True)
    plan.add_argument("--inner-launcher", required=True)
    plan.add_argument("--result", required=True)
    plan.add_argument("--working-directory", required=True)
    plan.add_argument("--source-commit", required=True)
    plan.add_argument(
        "--shadow-witness",
        default="",
        help="absolute private handoff file for the shadow-activity witness",
    )
    plan.add_argument("--delay-seconds", type=int, default=120)
    plan.add_argument("--timeout-seconds", type=int, default=900)
    plan.set_defaults(handler=_cmd_plan)

    execute = sub.add_parser("execute", help="guarded real out-of-band acceptance")
    execute.add_argument("--state-db", required=True)
    execute.add_argument("--shadow-state-db", required=True)
    execute.add_argument("--inner-launcher", required=True)
    execute.add_argument("--result", required=True)
    execute.add_argument("--source-commit", required=True)
    execute.add_argument("--inner-timeout-seconds", type=int, default=1800)
    execute.add_argument(
        "--shadow-witness",
        required=True,
        help="absolute private handoff file for the shadow-activity witness",
    )
    execute.add_argument(
        "--i-understand-this-runs-a-real-acceptance",
        dest="confirmed",
        action="store_true",
    )
    execute.add_argument(
        BACKGROUND_WRITER_CONTROLLED_FLAG,
        dest="background_writer_controlled",
        action="store_true",
        help=(
            "assert the real gateway cannot write the tracked tables during "
            "the measurement window; without it the run aborts with "
            "FINAL_BACKGROUND_WRITER_UNCONTROLLED"
        ),
    )
    execute.set_defaults(handler=_cmd_execute)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except OutOfBandError as exc:
        _emit({"state": MARKER_FAILED, "reason": exc.code})
        return 2


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
