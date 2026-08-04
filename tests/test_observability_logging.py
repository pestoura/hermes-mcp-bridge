"""Correlation context isolation and structured logging behaviour."""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from hermes_mcp_bridge.observability import context as ctx
from hermes_mcp_bridge.observability.logging import (
    JsonFormatter,
    configure_logging,
    log_event,
    log_mode,
    observability_status,
    timed_event,
)

SECRET = "sk-live-ABCDEF1234567890abcdef"


@pytest.fixture(autouse=True)
def _clean_context():
    ctx.clear_context()
    yield
    ctx.clear_context()


def test_scope_sets_and_restores_fields() -> None:
    assert ctx.get_context() == {}
    with ctx.correlation_scope(execution_id="run-1", tool_name="hermes_prompt"):
        current = ctx.get_context()
        assert current["execution_id"] == "run-1"
        assert current["tool_name"] == "hermes_prompt"
        assert current["correlation_id"]
    assert ctx.get_context() == {}


def test_scope_restored_on_exception() -> None:
    with pytest.raises(RuntimeError), ctx.correlation_scope(run_id="r"):
        raise RuntimeError("boom")
    assert ctx.get_context() == {}


async def test_context_isolated_between_asyncio_tasks() -> None:
    observed: dict[str, str | None] = {}

    async def worker(name: str) -> None:
        with ctx.correlation_scope(execution_id=name):
            await asyncio.sleep(0.01)
            observed[name] = ctx.get_field("execution_id")

    await asyncio.gather(worker("a"), worker("b"), worker("c"))
    assert observed == {"a": "a", "b": "b", "c": "c"}
    assert ctx.get_field("execution_id") is None


async def test_child_task_does_not_leak_into_parent() -> None:
    async def child() -> None:
        with ctx.correlation_scope(run_id="child-run"):
            await asyncio.sleep(0)

    with ctx.correlation_scope(run_id="parent-run"):
        await asyncio.create_task(child())
        assert ctx.get_field("run_id") == "parent-run"


def _record(**fields) -> dict:
    logger = logging.getLogger("hermes_mcp_bridge.test")
    record = logger.makeRecord(
        "hermes_mcp_bridge.test", logging.INFO, __file__, 1, "msg", None, None
    )
    for key, value in fields.items():
        setattr(record, key, value)
    return json.loads(JsonFormatter().format(record))


def test_json_formatter_is_deterministic_and_sorted() -> None:
    payload = _record(event="bridge.tool.call", tool="hermes_prompt", outcome="success")
    assert payload["event"] == "bridge.tool.call"
    assert payload["level"] == "INFO"
    assert payload["ts"].endswith("Z")
    assert list(payload) == sorted(payload)


def test_json_formatter_includes_correlation_context() -> None:
    with ctx.correlation_scope(execution_id="exec-1", tool_name="hermes_status"):
        payload = _record(event="bridge.tool.call")
    assert payload["execution_id"] == "exec-1"
    assert payload["tool_name"] == "hermes_status"


def test_json_formatter_redacts_secret_extras() -> None:
    payload = _record(event="e", authorization=f"Bearer {SECRET}", prompt="do the thing")
    dumped = json.dumps(payload)
    assert SECRET not in dumped
    assert "do the thing" not in dumped


def test_json_formatter_never_emits_stack_traces() -> None:
    try:
        raise ValueError(f"boom with Bearer {SECRET}")
    except ValueError:
        import sys

        logger = logging.getLogger("hermes_mcp_bridge.test")
        record = logger.makeRecord(
            "hermes_mcp_bridge.test",
            logging.ERROR,
            __file__,
            1,
            "msg",
            None,
            sys.exc_info(),
        )
        record.event = "bridge.tool.call"
        payload = json.loads(JsonFormatter().format(record))
    assert payload["error"]["type"] == "ValueError"
    assert SECRET not in json.dumps(payload)
    assert "Traceback" not in json.dumps(payload)


def test_log_mode_default_is_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BRIDGE_LOG_FORMAT", raising=False)
    assert log_mode() == "json"
    monkeypatch.setenv("BRIDGE_LOG_FORMAT", "text")
    assert log_mode() == "text"
    monkeypatch.setenv("BRIDGE_LOG_FORMAT", "nonsense")
    assert log_mode() == "json"


def test_logging_failure_does_not_break_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    configure_logging(force=True)

    def explode(*_args, **_kwargs):
        raise RuntimeError("sink is gone")

    monkeypatch.setattr(logging.Logger, "log", explode)
    log_event("bridge.tool.call", outcome="success")  # must not raise


def test_timed_event_reports_duration_and_outcome(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("hermes_mcp_bridge")
    logger.propagate = True
    try:
        with caplog.at_level(logging.INFO, logger="hermes_mcp_bridge"), timed_event(
            "bridge.op", tool="x"
        ):
            pass
    finally:
        logger.propagate = False
    record = next(r for r in caplog.records if getattr(r, "event", "") == "bridge.op")
    assert record.outcome == "success"
    assert record.duration_ms >= 0


def test_timed_event_marks_error_and_reraises() -> None:
    with pytest.raises(ValueError), timed_event("bridge.op"):
        raise ValueError("nope")


def test_observability_status_has_no_secrets() -> None:
    status = observability_status()
    assert status["redaction"] == "fail-closed"
    assert "key" not in json.dumps(status).lower().replace("logging", "")
