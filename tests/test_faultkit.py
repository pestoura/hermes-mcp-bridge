"""Tests for the test-only fault injection framework itself.

A fault framework that silently stops injecting faults is worse than none, so
its determinism and isolation properties are asserted explicitly.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
import pytest
from faultkit import (
    FaultProfile,
    FaultySqlite,
    FaultyTransport,
    ScriptedResponse,
    disk_full_connection,
    duplicated_events,
    flaky_connection,
    invalid_event_stream,
    out_of_order_events,
    truncated_stream,
)
from faultkit.sse import SSEScript

RUN_ID = "run-fk"


def _request() -> httpx.Request:
    return httpx.Request("GET", "http://127.0.0.1:9/v1/runs/run-fk")


# -- HTTP ---------------------------------------------------------------


def test_scripted_timeout_and_reset_raise_httpx_errors() -> None:
    with pytest.raises(httpx.ReadTimeout):
        ScriptedResponse(failure="timeout").build(_request())
    with pytest.raises(httpx.ReadError):
        ScriptedResponse(failure="reset").build(_request())
    with pytest.raises(httpx.ConnectError):
        ScriptedResponse(failure="connect").build(_request())


@pytest.mark.parametrize("code", [429, 500, 502, 503])
def test_profile_can_emit_each_error_status(code: int) -> None:
    rates = {
        429: {"status_429_rate": 1.0},
        500: {"status_500_rate": 1.0},
        502: {"status_502_rate": 1.0},
        503: {"status_503_rate": 1.0},
    }[code]
    profile = FaultProfile(seed=1, retry_after_seconds=3, **rates)  # type: ignore[arg-type]
    response = profile.next_fault()
    assert response is not None
    assert response.status_code == code
    if code in {429, 503}:
        assert response.headers["Retry-After"] == "3"


def test_profile_with_zero_rates_never_injects() -> None:
    profile = FaultProfile(seed=5)
    assert all(profile.next_fault() is None for _ in range(50))


async def test_transport_replays_script_then_default() -> None:
    transport = FaultyTransport(
        script=[ScriptedResponse(status_code=201, json_body={"a": 1})],
        default=ScriptedResponse(status_code=200, json_body={"b": 2}),
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:9"
    ) as client:
        first = await client.get("/v1/runs/x")
        second = await client.get("/v1/runs/x")
    assert first.status_code == 201
    assert second.json() == {"b": 2}
    assert transport.call_count == 2
    assert transport.calls_to("/v1/runs") == 2


async def test_transport_profile_takes_precedence_over_script() -> None:
    transport = FaultyTransport(
        script=[ScriptedResponse(status_code=200, json_body={"ok": True})],
        profile=FaultProfile(seed=3, status_500_rate=1.0),
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:9"
    ) as client:
        response = await client.get("/v1/runs/x")
    assert response.status_code == 500


# -- SQLite -------------------------------------------------------------


def test_faulty_sqlite_injects_exactly_n_failures() -> None:
    faults = FaultySqlite(failures=3)
    outcomes = [faults.should_fail() for _ in range(6)]
    assert outcomes == [True, True, True, False, False, False]
    assert faults.injected == 3
    assert faults.calls == 6


def test_faulty_sqlite_rate_mode_is_seeded_and_reproducible() -> None:
    first = [FaultySqlite(rate=0.5, seed=11).should_fail() for _ in range(1)]
    a = FaultySqlite(rate=0.5, seed=11)
    b = FaultySqlite(rate=0.5, seed=11)
    assert [a.should_fail() for _ in range(20)] == [b.should_fail() for _ in range(20)]
    assert isinstance(first[0], bool)


def test_faulty_sqlite_rejects_invalid_rate() -> None:
    with pytest.raises(ValueError):
        FaultySqlite(rate=1.5)


def test_flaky_connection_only_affects_its_own_handle(tmp_path: Path) -> None:
    path = str(tmp_path / "db.sqlite3")
    setup = sqlite3.connect(path)
    setup.execute("CREATE TABLE t (v INTEGER)")
    setup.commit()
    setup.close()

    flaky = flaky_connection(path, FaultySqlite(failures=1))
    with pytest.raises(sqlite3.OperationalError):
        flaky.execute("SELECT 1")
    assert flaky.execute("SELECT 1").fetchone() == (1,)

    clean = sqlite3.connect(path)
    assert clean.execute("SELECT 1").fetchone() == (1,)
    clean.close()


def test_disk_full_connection_fails_writes_but_not_reads(tmp_path: Path) -> None:
    path = str(tmp_path / "db.sqlite3")
    setup = sqlite3.connect(path)
    setup.execute("CREATE TABLE t (v INTEGER)")
    setup.commit()
    setup.close()

    connection = disk_full_connection(path, fail_after=1)
    connection.execute("INSERT INTO t VALUES (1)")
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        connection.execute("INSERT INTO t VALUES (2)")
    assert connection.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
    connection.close()


# -- SSE ----------------------------------------------------------------


def test_truncated_stream_cuts_the_body() -> None:
    body = truncated_stream(RUN_ID, keep_chars=25)
    assert len(body) == 25
    assert not body.endswith("\n\n")


def test_invalid_stream_contains_malformed_and_valid_frames() -> None:
    body = invalid_event_stream(RUN_ID)
    assert "data: {not json" in body
    assert "run.completed" in body


def test_duplicated_events_repeat_the_terminal_frame() -> None:
    assert duplicated_events(RUN_ID, repeats=4).count("run.completed") == 4


def test_out_of_order_events_place_terminal_first() -> None:
    body = out_of_order_events(RUN_ID)
    assert body.index("run.completed") < body.index("run.started")


def test_sse_script_frames_are_valid_json_events() -> None:
    body = SSEScript().event("run.started", RUN_ID, extra="x").build()
    payload = json.loads(body.split("data: ", 1)[1].strip())
    assert payload == {"event": "run.started", "run_id": RUN_ID, "extra": "x"}


def test_faultkit_is_not_imported_by_runtime_package() -> None:
    src = Path(__file__).resolve().parents[1] / "src"
    offenders = [
        path.name
        for path in src.rglob("*.py")
        if "faultkit" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
