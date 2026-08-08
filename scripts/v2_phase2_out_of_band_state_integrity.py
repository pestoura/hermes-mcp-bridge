#!/usr/bin/env python3
"""Dry-run planner for the V2 Phase 2 out-of-band state-integrity probe.

Repo-side foundation only. This CLI **never** performs a real out-of-band
acceptance: without ``--execute`` (which is intentionally not implemented here)
it only prints the sanitized plan that an operator would schedule, or performs a
purely local fixture measurement against a database the operator supplies.

Modes
-----

``plan``     print the sanitized transient one-shot plan (no execution).
``measure``  take a before/after read-only measurement of a supplied database
             and write the atomic sanitized terminal marker. Intended to be the
             body executed by the transient unit; safe to run locally against a
             fixture because it only ever opens the database read-only.

Absolute zero delta is only meaningful when ``measure`` runs out-of-band, after
the Hermes control run has ended. Running it inside a live control run proves
nothing; see ``docs/v2/phase2-out-of-band-state-integrity.md``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:  # pragma: no cover - path shim
    sys.path.insert(0, str(_SRC))

from hermes_mcp_bridge.v2.out_of_band import (  # noqa: E402
    MARKER_COMPLETED,
    MARKER_FAILED,
    OutOfBandError,
    build_transient_unit_plan,
    dry_run_report,
    write_terminal_marker,
)
from hermes_mcp_bridge.v2.state_integrity import (  # noqa: E402
    StateIntegrityError,
    capture_state_snapshot,
    compare_snapshots,
    new_run_salt,
    read_state_metadata,
)


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _cmd_plan(args: argparse.Namespace) -> int:
    plan = build_transient_unit_plan(
        unit_name=args.unit_name,
        probe_argv=(
            str(Path(sys.executable).absolute()),
            str(Path(__file__).absolute()),
            "measure",
            "--state-db",
            args.state_db,
            "--result",
            args.result,
        ),
        working_directory=args.working_directory,
        result_path=args.result,
        delay_seconds=args.delay_seconds,
        timeout_seconds=args.timeout_seconds,
    )
    report = dry_run_report(plan)
    report["command_line"] = plan.command_line()
    _emit(report)
    return 0


def _cmd_measure(args: argparse.Namespace) -> int:
    salt = new_run_salt()
    state_db = str(Path(args.state_db).absolute())
    try:
        before_meta = read_state_metadata(state_db)
        before = capture_state_snapshot(state_db, metadata=before_meta, salt=salt)
        after_meta = read_state_metadata(state_db)
        after = capture_state_snapshot(state_db, metadata=after_meta, salt=salt)
        comparison = compare_snapshots(before, after)
    except StateIntegrityError as exc:
        write_terminal_marker(
            str(Path(args.result).absolute()),
            state=MARKER_FAILED,
            payload={"reason": exc.code},
        )
        _emit({"state": MARKER_FAILED, "reason": exc.code})
        return 2
    write_terminal_marker(
        str(Path(args.result).absolute()),
        state=MARKER_COMPLETED,
        payload={"comparison": comparison.as_canonical()},
    )
    _emit({"state": MARKER_COMPLETED, "comparison": comparison.as_canonical()})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="print the sanitized dry-run plan")
    plan.add_argument("--unit-name", default="hermes-v2-oob-state-integrity")
    plan.add_argument("--state-db", required=True)
    plan.add_argument("--result", required=True)
    plan.add_argument("--working-directory", required=True)
    plan.add_argument("--delay-seconds", type=int, default=60)
    plan.add_argument("--timeout-seconds", type=int, default=120)
    plan.set_defaults(handler=_cmd_plan)

    measure = sub.add_parser("measure", help="read-only before/after measurement")
    measure.add_argument("--state-db", required=True)
    measure.add_argument("--result", required=True)
    measure.set_defaults(handler=_cmd_measure)
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
