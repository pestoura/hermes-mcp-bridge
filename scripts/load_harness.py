#!/usr/bin/env python3
"""Deterministic concurrency load harness for the bridge state layer.

CI-safe by default: it never touches the Hermes API, never opens a socket and
writes only inside a temporary directory (or ``--db`` when given). Work is
purely SQLite contention against the real registries, which is exactly the
surface Block 3 hardens.

Usage (CI profile, a few seconds):

    python scripts/load_harness.py --profile ci --json-out report.json

Usage (soak, run OUTSIDE CI):

    python scripts/load_harness.py --profile soak-30m
    python scripts/load_harness.py --profile soak-60m
    python scripts/load_harness.py --profile soak-2h

Exit criteria (non-zero exit means FAIL):

* zero unexpected errors (only declared, expected contention outcomes allowed);
* ``PRAGMA integrity_check`` returns ``ok``;
* no duplicated run mappings and no double-consumed approvals;
* observed error ratio at or below ``--max-error-ratio``.

The JSON report is sanitized: aggregated counters, durations and truncated
fingerprints only. No prompts, outputs, secrets or full identifiers.
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("HERMES_API_KEY", "load-harness")
# Expected contention (busy/locked/approval reuse) is *the* workload here, so the
# per-event bridge logs would emit tens of thousands of lines on soak profiles.
# Keep stderr bounded; the JSON report carries the aggregated counters.
os.environ.setdefault("LOG_LEVEL", "WARNING")

from hermes_mcp_bridge.approvals import (
    ApprovalConsumedError,
    ApprovalRegistry,
    ApprovalStatusError,
)
from hermes_mcp_bridge.locks import LockError, LockRegistry
from hermes_mcp_bridge.migrations import apply_migrations
from hermes_mcp_bridge.models import LockType, ResourceLock
from hermes_mcp_bridge.protocol import ApprovalRecord, ApprovalStatus
from hermes_mcp_bridge.registry import (
    FingerprintConflictError,
    RunRegistry,
    compute_fingerprint,
)
from hermes_mcp_bridge.resilience.recovery import recover_state

PROFILES: dict[str, dict[str, float]] = {
    "ci": {"duration_seconds": 5.0, "workers": 8, "checkpoint_seconds": 5.0},
    "soak-30m": {"duration_seconds": 1800.0, "workers": 8, "checkpoint_seconds": 300.0},
    "soak-60m": {"duration_seconds": 3600.0, "workers": 8, "checkpoint_seconds": 300.0},
    "soak-2h": {"duration_seconds": 7200.0, "workers": 8, "checkpoint_seconds": 600.0},
}

#: Hard ceiling so a mis-typed --duration cannot run forever.
MAX_DURATION_SECONDS = 8 * 3600


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


@dataclass
class Counters:
    run_records: int = 0
    run_updates: int = 0
    fingerprint_conflicts: int = 0
    lock_acquired: int = 0
    lock_conflicts: int = 0
    approvals_created: int = 0
    approvals_consumed: int = 0
    approvals_rejected_second_use: int = 0
    contention_errors: int = 0
    unexpected_errors: int = 0
    error_samples: list[str] = field(default_factory=list)

    def merge(self, other: Counters) -> None:
        for key, value in vars(other).items():
            if key == "error_samples":
                self.error_samples.extend(value)
                del self.error_samples[8:]
            else:
                setattr(self, key, getattr(self, key) + value)

    def total_operations(self) -> int:
        return (
            self.run_records
            + self.run_updates
            + self.lock_acquired
            + self.lock_conflicts
            + self.approvals_created
            + self.approvals_consumed
        )


def _worker(
    db_path: str, worker_id: int, deadline: float, iterations_cap: int
) -> Counters:
    counters = Counters()
    runs = RunRegistry(db_path)
    locks = LockRegistry(db_path)
    approvals = ApprovalRegistry(db_path)
    iteration = 0

    while time.monotonic() < deadline and iteration < iterations_cap:
        iteration += 1
        key = f"w{worker_id:02d}-i{iteration:06d}"
        try:
            runs.record(
                client_request_id=key,
                fingerprint=compute_fingerprint(prompt=key),
                execution_id=f"exec-{worker_id}-{iteration}",
            )
            counters.run_records += 1
            runs.update_status(client_request_id=key, last_status="running")
            runs.update_status(client_request_id=key, last_status="completed")
            counters.run_updates += 2
        except FingerprintConflictError:
            counters.fingerprint_conflicts += 1
        except sqlite3.OperationalError as error:
            counters.contention_errors += 1
            _sample(counters, error)
        except Exception as error:
            counters.unexpected_errors += 1
            _sample(counters, error)

        shared_key = f"lock-{iteration % 4}"
        owner = f"owner-{worker_id}"
        try:
            locks.acquire(
                ResourceLock(
                    lock_key=shared_key,
                    lock_type=LockType.WRITE_EXCLUSIVE,
                    owner=owner,
                    ttl_seconds=30,
                )
            )
            counters.lock_acquired += 1
            locks.release(shared_key, owner)
        except LockError:
            counters.lock_conflicts += 1
        except sqlite3.OperationalError as error:
            counters.contention_errors += 1
            _sample(counters, error)
        except Exception as error:
            counters.unexpected_errors += 1
            _sample(counters, error)

        approval_id = f"appr-{worker_id}-{iteration}"
        try:
            approvals.create(
                ApprovalRecord(
                    approval_id=approval_id,
                    action="write",
                    resource="res",
                    resource_fingerprint="fp",
                    created_at="2026-01-01T00:00:00+00:00",
                )
            )
            counters.approvals_created += 1
            approvals.respond(approval_id, ApprovalStatus.APPROVED)
            approvals.consume(approval_id, "fp")
            counters.approvals_consumed += 1
            try:
                approvals.consume(approval_id, "fp")
                counters.unexpected_errors += 1
                counters.error_samples.append("double_consume_allowed")
            except (ApprovalConsumedError, ApprovalStatusError):
                counters.approvals_rejected_second_use += 1
        except sqlite3.OperationalError as error:
            counters.contention_errors += 1
            _sample(counters, error)
        except Exception as error:
            counters.unexpected_errors += 1
            _sample(counters, error)

    return counters


def _sample(counters: Counters, error: BaseException) -> None:
    if len(counters.error_samples) < 8:
        counters.error_samples.append(type(error).__name__)


def _verify(db_path: str) -> dict[str, object]:
    connection = sqlite3.connect(db_path)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        mappings, distinct = connection.execute(
            "SELECT COUNT(*), COUNT(DISTINCT client_request_id) FROM run_mappings"
        ).fetchone()
        double_consumed = connection.execute(
            "SELECT COUNT(*) FROM approvals WHERE consumed_at IS NOT NULL"
            " AND decision <> 'consumed'"
        ).fetchone()[0]
        active_locks = connection.execute(
            "SELECT COUNT(*) FROM resource_locks WHERE status = 'active'"
        ).fetchone()[0]
        return {
            "integrity_check": str(integrity),
            "run_mappings": int(mappings),
            "distinct_run_mappings": int(distinct),
            "duplicate_mappings": int(mappings) - int(distinct),
            "double_consumed_approvals": int(double_consumed),
            "active_locks_left": int(active_locks),
        }
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=sorted(PROFILES), default="ci")
    parser.add_argument("--duration", type=float, default=None, help="override seconds")
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--iterations-cap", type=int, default=100_000)
    parser.add_argument("--db", default=None, help="state db path (default: temp dir)")
    parser.add_argument("--json-out", default=None)
    parser.add_argument("--max-error-ratio", type=float, default=0.0)
    parser.add_argument("--keep-db", action="store_true")
    args = parser.parse_args(argv)

    profile = PROFILES[args.profile]
    duration = float(args.duration if args.duration is not None else profile["duration_seconds"])
    workers = int(args.workers if args.workers is not None else profile["workers"])
    if duration <= 0 or duration > MAX_DURATION_SECONDS:
        parser.error(f"duration must be in (0, {MAX_DURATION_SECONDS}]")
    if not (1 <= workers <= 64):
        parser.error("workers must be in [1, 64]")

    workdir = tempfile.mkdtemp(prefix="bridge-load-")
    db_path = args.db or os.path.join(workdir, "state.sqlite3")
    apply_migrations(db_path)

    started = time.monotonic()
    deadline = started + duration
    totals = Counters()
    checkpoints: list[dict[str, object]] = []
    checkpoint_every = float(profile["checkpoint_seconds"])
    next_checkpoint = started + checkpoint_every

    with futures.ThreadPoolExecutor(max_workers=workers) as pool:
        pending = [
            pool.submit(_worker, db_path, worker_id, deadline, args.iterations_cap)
            for worker_id in range(workers)
        ]
        while any(not future.done() for future in pending):
            time.sleep(0.2)
            if time.monotonic() >= next_checkpoint:
                checkpoints.append(
                    {
                        "elapsed_seconds": round(time.monotonic() - started, 1),
                        "run_mappings": _verify(db_path)["run_mappings"],
                    }
                )
                next_checkpoint += checkpoint_every
        for future in pending:
            totals.merge(future.result())

    elapsed = time.monotonic() - started
    verification = _verify(db_path)
    recovery = recover_state(db_path, reap_locks=False)
    operations = totals.total_operations()
    error_ratio = (totals.unexpected_errors / operations) if operations else 0.0

    failures: list[str] = []
    if verification["integrity_check"] != "ok":
        failures.append("integrity_check_failed")
    if verification["duplicate_mappings"] != 0:
        failures.append("duplicate_run_mappings")
    if verification["double_consumed_approvals"] != 0:
        failures.append("double_consumed_approvals")
    if totals.unexpected_errors:
        failures.append("unexpected_errors")
    if error_ratio > args.max_error_ratio:
        failures.append("error_ratio_exceeded")

    report = {
        "profile": args.profile,
        "workers": workers,
        "requested_duration_seconds": duration,
        "elapsed_seconds": round(elapsed, 2),
        "operations": operations,
        "throughput_ops_per_second": round(operations / elapsed, 2) if elapsed else 0.0,
        "counters": {
            key: value for key, value in vars(totals).items() if key != "error_samples"
        },
        "error_samples": totals.error_samples,
        "error_ratio": round(error_ratio, 6),
        "verification": verification,
        "recovery": {
            "recoverable_runs": recovery.recoverable_runs,
            "terminal_runs": recovery.terminal_runs,
        },
        "checkpoints": checkpoints,
        "db_fingerprint": fingerprint(db_path),
        "result": "PASS" if not failures else "FAIL",
        "failures": failures,
    }

    text = json.dumps(report, indent=2, sort_keys=True)
    print(text)
    if args.json_out:
        Path(args.json_out).write_text(text + "\n", encoding="utf-8")

    if not args.keep_db and args.db is None:
        import shutil

        shutil.rmtree(workdir, ignore_errors=True)

    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
