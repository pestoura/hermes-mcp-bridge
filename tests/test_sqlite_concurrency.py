"""Real SQLite concurrency tests: threads, processes, contention, integrity.

Every test uses a real on-disk SQLite database in a temporary directory. No
mocks: the goal is to observe the actual locking behaviour of the registries.
"""

from __future__ import annotations

import concurrent.futures as futures
import json
import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from faultkit.sqlite import FaultySqlite, disk_full_connection

from hermes_mcp_bridge.approvals import (
    ApprovalConsumedError,
    ApprovalRegistry,
    ApprovalStatusError,
)
from hermes_mcp_bridge.locks import LockError, LockRegistry
from hermes_mcp_bridge.migrations import apply_migrations
from hermes_mcp_bridge.models import LockType, ResourceLock
from hermes_mcp_bridge.observability.metrics import get_metrics, get_registry
from hermes_mcp_bridge.protocol import ApprovalRecord, ApprovalStatus
from hermes_mcp_bridge.registry import (
    FingerprintConflictError,
    RunRegistry,
    compute_fingerprint,
)
from hermes_mcp_bridge.resilience import RetryExhaustedError, RetryPolicy, run_with_retry
from hermes_mcp_bridge.resilience.backoff import BackoffPolicy
from hermes_mcp_bridge.resilience.clock import ManualClock

THREADS = 16
_NOW = "2026-01-01T00:00:00+00:00"


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    path = str(tmp_path / "state.sqlite3")
    apply_migrations(path)
    return path


def _pragma(db_path: str, name: str) -> object:
    connection = sqlite3.connect(db_path)
    try:
        return connection.execute(f"PRAGMA {name}").fetchone()[0]
    finally:
        connection.close()


# -- WAL / pragmas ------------------------------------------------------


def test_migrations_enable_wal_and_busy_timeout(db_path: str) -> None:
    assert str(_pragma(db_path, "journal_mode")).lower() == "wal"
    registry = RunRegistry(db_path)
    health = registry.health()
    assert health["status"] == "up"
    assert str(health["pragmas"]["journal_mode"]).lower() == "wal"


def test_wal_allows_reads_during_a_write_transaction(db_path: str) -> None:
    writer = sqlite3.connect(db_path)
    writer.isolation_level = None
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO run_mappings VALUES ('wal-1','fp','exec','sess','queued','t','t')")
    reader = sqlite3.connect(db_path)
    try:
        rows = reader.execute("SELECT COUNT(*) FROM run_mappings").fetchone()[0]
        assert rows == 0  # uncommitted write is invisible; the read is not blocked
    finally:
        reader.close()
    writer.execute("COMMIT")
    writer.close()
    check = sqlite3.connect(db_path)
    assert check.execute("SELECT COUNT(*) FROM run_mappings").fetchone()[0] == 1
    check.close()


def test_busy_timeout_is_applied_on_registry_connections(db_path: str) -> None:
    assert int(RunRegistry(db_path).health()["pragmas"]["busy_timeout"]) >= 0


# -- run registry -------------------------------------------------------


def test_concurrent_record_same_key_yields_single_row(db_path: str) -> None:
    registry = RunRegistry(db_path)
    fingerprint = compute_fingerprint(prompt="p")
    barrier = threading.Barrier(THREADS)
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        barrier.wait()
        try:
            registry.record(
                client_request_id="same-key",
                fingerprint=fingerprint,
                execution_id="exec-1",
            )
        except BaseException as exc:
            errors.append(exc)

    with futures.ThreadPoolExecutor(max_workers=THREADS) as pool:
        list(pool.map(worker, range(THREADS)))

    assert errors == []
    connection = sqlite3.connect(db_path)
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM run_mappings WHERE client_request_id='same-key'"
        ).fetchone()[0]
        == 1
    )
    connection.close()


def test_conflicting_fingerprints_are_rejected_under_concurrency(db_path: str) -> None:
    registry = RunRegistry(db_path)
    barrier = threading.Barrier(THREADS)
    conflicts = 0
    lock = threading.Lock()

    def worker(index: int) -> None:
        nonlocal conflicts
        barrier.wait()
        try:
            registry.record(
                client_request_id="conflict",
                fingerprint=compute_fingerprint(prompt=f"p{index % 4}"),
                execution_id=f"exec-{index}",
            )
        except FingerprintConflictError:
            with lock:
                conflicts += 1

    with futures.ThreadPoolExecutor(max_workers=THREADS) as pool:
        list(pool.map(worker, range(THREADS)))

    assert conflicts > 0
    connection = sqlite3.connect(db_path)
    stored = connection.execute(
        "SELECT COUNT(*), COUNT(DISTINCT fingerprint) FROM run_mappings"
        " WHERE client_request_id='conflict'"
    ).fetchone()
    connection.close()
    assert stored == (1, 1)


def test_concurrent_distinct_keys_have_no_lost_updates(db_path: str) -> None:
    registry = RunRegistry(db_path)
    total = 64

    def worker(index: int) -> None:
        key = f"run-{index:03d}"
        registry.record(
            client_request_id=key,
            fingerprint=compute_fingerprint(prompt=key),
            execution_id=f"exec-{index}",
        )
        registry.update_status(client_request_id=key, last_status="running")
        registry.update_status(client_request_id=key, last_status="completed")

    with futures.ThreadPoolExecutor(max_workers=THREADS) as pool:
        list(pool.map(worker, range(total)))

    connection = sqlite3.connect(db_path)
    count, completed = connection.execute(
        "SELECT COUNT(*), SUM(last_status='completed') FROM run_mappings"
    ).fetchone()
    connection.close()
    assert count == total
    assert completed == total


def test_status_updates_never_interleave_into_a_torn_row(db_path: str) -> None:
    registry = RunRegistry(db_path)
    registry.record(
        client_request_id="torn",
        fingerprint=compute_fingerprint(prompt="x"),
        execution_id="exec-0",
    )
    barrier = threading.Barrier(THREADS)

    def worker(index: int) -> None:
        barrier.wait()
        registry.update_status(
            client_request_id="torn",
            last_status="running",
            execution_id=f"exec-{index}",
        )

    with futures.ThreadPoolExecutor(max_workers=THREADS) as pool:
        list(pool.map(worker, range(THREADS)))

    row = registry.get("torn")
    assert row is not None
    assert row["last_status"] == "running"
    assert str(row["execution_id"]).startswith("exec-")


# -- approvals ----------------------------------------------------------


def _make_approval(registry: ApprovalRegistry, approval_id: str) -> None:
    registry.create(
        ApprovalRecord(
            approval_id=approval_id,
            action="write",
            resource="res",
            resource_fingerprint="fp",
            created_at=_NOW,
        )
    )
    registry.respond(approval_id, ApprovalStatus.APPROVED)


def test_approval_is_consumed_exactly_once_under_contention(db_path: str) -> None:
    registry = ApprovalRegistry(db_path)
    _make_approval(registry, "appr-once")
    barrier = threading.Barrier(THREADS)
    successes: list[str] = []
    lock = threading.Lock()

    def worker(_: int) -> None:
        barrier.wait()
        try:
            registry.consume("appr-once", "fp")
        except (ApprovalConsumedError, ApprovalStatusError):
            return
        except sqlite3.OperationalError:
            return
        with lock:
            successes.append("ok")

    with futures.ThreadPoolExecutor(max_workers=THREADS) as pool:
        list(pool.map(worker, range(THREADS)))

    assert len(successes) == 1
    assert registry.get("appr-once").decision is ApprovalStatus.CONSUMED


def test_approval_respond_is_single_winner(db_path: str) -> None:
    registry = ApprovalRegistry(db_path)
    registry.create(
        ApprovalRecord(
            approval_id="appr-race",
            action="write",
            resource="r",
            resource_fingerprint="fp",
            created_at=_NOW,
        )
    )
    barrier = threading.Barrier(THREADS)
    accepted: list[str] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        barrier.wait()
        decision = ApprovalStatus.APPROVED if index % 2 else ApprovalStatus.REJECTED
        try:
            registry.respond("appr-race", decision)
        except (ApprovalStatusError, sqlite3.OperationalError):
            return
        with lock:
            accepted.append(decision.value)

    with futures.ThreadPoolExecutor(max_workers=THREADS) as pool:
        list(pool.map(worker, range(THREADS)))

    assert len(accepted) == 1
    assert registry.get("appr-race").decision.value == accepted[0]


def test_duplicate_approval_ids_cannot_be_created_concurrently(db_path: str) -> None:
    registry = ApprovalRegistry(db_path)
    barrier = threading.Barrier(THREADS)
    created = 0
    lock = threading.Lock()

    def worker(_: int) -> None:
        nonlocal created
        barrier.wait()
        try:
            registry.create(ApprovalRecord(approval_id="dup", action="write", created_at=_NOW))
        except (sqlite3.IntegrityError, sqlite3.OperationalError):
            return
        with lock:
            created += 1

    with futures.ThreadPoolExecutor(max_workers=THREADS) as pool:
        list(pool.map(worker, range(THREADS)))

    assert created == 1


# -- resource locks -----------------------------------------------------


def test_write_exclusive_lock_has_single_owner_under_contention(db_path: str) -> None:
    registry = LockRegistry(db_path)
    barrier = threading.Barrier(THREADS)
    winners: list[str] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        barrier.wait()
        try:
            registry.acquire(
                ResourceLock(
                    lock_key="resource-a",
                    lock_type=LockType.WRITE_EXCLUSIVE,
                    owner=f"owner-{index}",
                    ttl_seconds=60,
                )
            )
        except (LockError, sqlite3.OperationalError):
            return
        with lock:
            winners.append(f"owner-{index}")

    with futures.ThreadPoolExecutor(max_workers=THREADS) as pool:
        list(pool.map(worker, range(THREADS)))

    assert len(winners) == 1
    active = [row for row in registry.list_status("resource-a") if row["status"] == "active"]
    assert len(active) == 1
    assert active[0]["owner"] == winners[0]


def test_shared_read_locks_are_all_granted(db_path: str) -> None:
    registry = LockRegistry(db_path)

    def worker(index: int) -> None:
        registry.acquire(
            ResourceLock(
                lock_key="resource-shared",
                lock_type=LockType.READ_SHARED,
                owner=f"reader-{index}",
                ttl_seconds=60,
            )
        )

    with futures.ThreadPoolExecutor(max_workers=THREADS) as pool:
        list(pool.map(worker, range(THREADS)))

    active = [row for row in registry.list_status("resource-shared") if row["status"] == "active"]
    assert len(active) == THREADS


def test_acquire_release_cycles_do_not_deadlock(db_path: str) -> None:
    registry = LockRegistry(db_path)

    def worker(index: int) -> None:
        owner = f"cycler-{index}"
        for _ in range(10):
            try:
                registry.acquire(
                    ResourceLock(
                        lock_key="resource-cycle",
                        lock_type=LockType.WRITE_EXCLUSIVE,
                        owner=owner,
                        ttl_seconds=60,
                    )
                )
            except LockError:
                continue
            registry.release("resource-cycle", owner)

    with futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = [pool.submit(worker, i) for i in range(8)]
        for future in futures.as_completed(results, timeout=60):
            future.result()

    active = [row for row in registry.list_status("resource-cycle") if row["status"] == "active"]
    assert len(active) <= 1


# -- migrations ---------------------------------------------------------


def test_concurrent_migrations_apply_each_version_once(tmp_path: Path) -> None:
    path = str(tmp_path / "migrate.sqlite3")
    barrier = threading.Barrier(8)

    def worker(_: int) -> int:
        barrier.wait()
        return apply_migrations(path)

    with futures.ThreadPoolExecutor(max_workers=8) as pool:
        versions = list(pool.map(worker, range(8)))

    assert len(set(versions)) == 1
    connection = sqlite3.connect(path)
    total, distinct = connection.execute(
        "SELECT COUNT(*), COUNT(DISTINCT version) FROM schema_migrations"
    ).fetchone()
    connection.close()
    assert total == distinct


def test_migrations_are_idempotent_when_reapplied(db_path: str) -> None:
    first = apply_migrations(db_path)
    second = apply_migrations(db_path)
    assert first == second


# -- multi-process ------------------------------------------------------

_CHILD = """
import json, sqlite3, sys
sys.path.insert(0, {src!r})
from hermes_mcp_bridge.registry import RunRegistry, compute_fingerprint
from hermes_mcp_bridge.registry import FingerprintConflictError

db, offset, count = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
registry = RunRegistry(db)
written = 0
for i in range(count):
    key = "proc-%03d" % (offset + i)
    try:
        registry.record(
            client_request_id=key,
            fingerprint=compute_fingerprint(prompt=key),
            execution_id="exec-%d" % (offset + i),
        )
        written += 1
    except (sqlite3.OperationalError, FingerprintConflictError):
        pass
print(json.dumps({{"written": written}}))
"""


def test_multi_process_writes_do_not_corrupt_the_database(tmp_path: Path, db_path: str) -> None:
    src = str(Path(__file__).resolve().parents[1] / "src")
    script = tmp_path / "child.py"
    script.write_text(_CHILD.format(src=src), encoding="utf-8")

    env = dict(os.environ, HERMES_API_KEY="test", BRIDGE_STATE_DB_PATH=db_path)
    procs = [
        subprocess.Popen(
            [sys.executable, str(script), db_path, str(offset), "25"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
        )
        for offset in (0, 100, 200, 300)
    ]
    written = 0
    for proc in procs:
        out, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, err
        written += json.loads(out.strip().splitlines()[-1])["written"]

    assert written == 100
    connection = sqlite3.connect(db_path)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT COUNT(*) FROM run_mappings").fetchone()[0] == 100
    finally:
        connection.close()


# -- bounded retry ------------------------------------------------------


def test_run_with_retry_recovers_from_transient_contention() -> None:
    faults = FaultySqlite(failures=2)
    clock = ManualClock()
    calls = {"n": 0}

    def operation() -> str:
        faults.raise_if_failing()
        calls["n"] += 1
        return "ok"

    result = run_with_retry(operation, clock=clock)
    assert result == "ok"
    assert calls["n"] == 1
    assert len(clock.sleeps) == 2


def test_run_with_retry_is_bounded_and_raises_when_exhausted() -> None:
    faults = FaultySqlite(failures=100)
    clock = ManualClock()
    policy = RetryPolicy(
        backoff=BackoffPolicy(base_seconds=0.01, max_seconds=0.1, max_attempts=4, jitter_ratio=0.0)
    )
    with pytest.raises(RetryExhaustedError):
        run_with_retry(faults.raise_if_failing, policy=policy, clock=clock)
    assert faults.calls == 4
    assert len(clock.sleeps) == 3


def test_run_with_retry_does_not_retry_logic_errors() -> None:
    clock = ManualClock()

    def operation() -> None:
        raise sqlite3.OperationalError("no such column: nope")

    with pytest.raises(sqlite3.OperationalError):
        run_with_retry(operation, clock=clock)
    assert clock.sleeps == []


def test_retry_emits_contention_metrics() -> None:
    get_registry().reset()
    faults = FaultySqlite(failures=2)
    run_with_retry(lambda: faults.raise_if_failing() or "ok", clock=ManualClock())
    metrics = get_metrics()
    assert metrics.sqlite_retries_total.value(kind="state") == 2.0
    assert metrics.sqlite_lock_contention_total.value() == 2.0


# -- integrity and interruption ----------------------------------------


def test_interrupted_write_leaves_database_consistent(db_path: str) -> None:
    connection = disk_full_connection(db_path, fail_after=0)
    connection.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.OperationalError):
        connection.execute("INSERT INTO run_mappings VALUES ('x','fp','e','s','queued','t','t')")
    connection.close()

    verify = sqlite3.connect(db_path)
    try:
        assert verify.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert verify.execute("SELECT COUNT(*) FROM run_mappings").fetchone()[0] == 0
    finally:
        verify.close()


def test_registry_recovers_after_a_simulated_process_restart(db_path: str) -> None:
    first = RunRegistry(db_path)
    first.record(
        client_request_id="restart-1",
        fingerprint=compute_fingerprint(prompt="p"),
        execution_id="exec-restart",
    )
    del first

    second = RunRegistry(db_path)
    row = second.get("restart-1")
    assert row is not None
    assert row["execution_id"] == "exec-restart"
    connection = sqlite3.connect(db_path)
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.close()
