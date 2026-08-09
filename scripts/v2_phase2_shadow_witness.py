#!/usr/bin/env python3
"""Acceptance-only shadow-state activity handoff helper.

Invoked by the inner connected launcher **only** when the OUTER final
out-of-band runner enabled the handoff by exporting
``HERMES_V2_FINAL_SHADOW_WITNESS_FILE``. Two subcommands:

``capture``  read bounded row counts from the disposable shadow state DB and
             store them in a private baseline file (mode ``0600``);
``emit``     re-read the counts, build the sanitized witness against the
             baseline and write it atomically to the handoff file.

Nothing here prints a path, a row value, a session id or any credential; on
failure only a stable ``reason`` code is emitted and the exit status is 2.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:  # pragma: no cover - path shim
    sys.path.insert(0, str(_SRC))

from hermes_mcp_bridge.v2.shadow_witness import (  # noqa: E402
    ShadowCounts,
    ShadowWitnessError,
    build_witness,
    read_shadow_counts,
    write_witness,
)


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _write_private_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, sort_keys=True, separators=(",", ":"))


def _cmd_capture(args: argparse.Namespace) -> int:
    try:
        counts = read_shadow_counts(args.shadow_state_db)
        _write_private_json(Path(args.baseline).absolute(), counts.as_canonical())
    except (ShadowWitnessError, OSError) as exc:
        code = getattr(exc, "code", "SHADOW_WITNESS_WRITE_FAILED")
        _emit({"status": "SHADOW_WITNESS_BLOCKED", "reason": code})
        return 2
    _emit({"status": "SHADOW_WITNESS_BASELINE_CAPTURED"})
    return 0


def _load_baseline(path: Path) -> ShadowCounts:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("baseline_invalid")
    return ShadowCounts(
        sessions=int(payload["sessions"]),
        session_model_usage=int(payload["session_model_usage"]),
    )


def _cmd_emit(args: argparse.Namespace) -> int:
    baseline_path = Path(args.baseline).absolute()
    try:
        before = _load_baseline(baseline_path)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        before = ShadowCounts(sessions=0, session_model_usage=0)
    try:
        after = read_shadow_counts(args.shadow_state_db)
        document = build_witness(
            source_commit=args.source_commit,
            before=before,
            after=after,
            shadow_state_db=args.shadow_state_db,
            source_state_db=args.source_state_db,
            shadow_home=args.shadow_home,
            handoff_path=args.handoff,
        )
        write_witness(args.handoff, document)
    except ShadowWitnessError as exc:
        _emit({"status": "SHADOW_WITNESS_BLOCKED", "reason": exc.code})
        return 2
    except OSError:
        _emit(
            {
                "status": "SHADOW_WITNESS_BLOCKED",
                "reason": "SHADOW_WITNESS_WRITE_FAILED",
            }
        )
        return 2
    _emit({"status": "SHADOW_WITNESS_WRITTEN"})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    capture = sub.add_parser("capture", help="store the pre-run baseline counts")
    capture.add_argument("--shadow-state-db", required=True)
    capture.add_argument("--baseline", required=True)
    capture.set_defaults(handler=_cmd_capture)

    emit = sub.add_parser("emit", help="write the sanitized activity witness")
    emit.add_argument("--shadow-state-db", required=True)
    emit.add_argument("--source-state-db", required=True)
    emit.add_argument("--shadow-home", required=True)
    emit.add_argument("--baseline", required=True)
    emit.add_argument("--handoff", required=True)
    emit.add_argument("--source-commit", required=True)
    emit.set_defaults(handler=_cmd_emit)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
