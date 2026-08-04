"""Optional tracing: traceparent parsing, no-op spans and fail-open behaviour."""

from __future__ import annotations

import pytest

from hermes_mcp_bridge.observability import context as ctx
from hermes_mcp_bridge.observability import tracing as tr

VALID = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(tr.ENV_TRACING_ENABLED, raising=False)
    monkeypatch.delenv(tr.ENV_TRACING_EXPORT, raising=False)
    ctx.clear_context()
    yield
    ctx.clear_context()


def test_valid_traceparent_is_parsed() -> None:
    parsed = tr.parse_traceparent(VALID)
    assert parsed is not None
    assert parsed["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert parsed["span_id"] == "00f067aa0ba902b7"
    assert parsed["trace_flags"] == "01"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "garbage",
        None,
        123,
        "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7",  # missing flags
        "00-4bf92f-00f067aa0ba902b7-01",  # short trace id
        "ff-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",  # forbidden version
        "00-" + "0" * 32 + "-00f067aa0ba902b7-01",  # all-zero trace id
        "00-4bf92f3577b34da6a3ce929d0e0e4736-" + "0" * 16 + "-01",  # all-zero span id
        "00-4BF92F3577B34DA6A3CE929D0E0E4736-00f067aa0ba902b7-01",  # uppercase
    ],
)
def test_invalid_traceparent_returns_none(value: object) -> None:
    assert tr.parse_traceparent(value) is None


def test_span_is_noop_by_default() -> None:
    assert tr.tracing_enabled() is False
    assert tr.export_enabled() is False
    with tr.start_span("tool.demo", attr="x") as span:
        assert isinstance(span, tr.NoOpSpan)
        assert span.name == "tool.demo"
        assert len(span.trace_id) == 32
        assert len(span.span_id) == 16
        assert tr.parse_traceparent(span.traceparent()) is not None


def test_span_inherits_parent_traceparent() -> None:
    with tr.start_span("tool.demo", traceparent=VALID) as span:
        assert span.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
        assert span.span_id != "00f067aa0ba902b7"


def test_invalid_traceparent_starts_a_new_trace() -> None:
    with tr.start_span("tool.demo", traceparent="not-valid") as span:
        assert len(span.trace_id) == 32


def test_span_binds_and_clears_correlation_context() -> None:
    with tr.start_span("tool.demo") as span:
        assert ctx.get_field("trace_id") == span.trace_id
        assert ctx.get_field("span_id") == span.span_id
    assert ctx.get_field("trace_id") is None


def test_set_attribute_never_raises() -> None:
    with tr.start_span("tool.demo") as span:
        span.set_attribute("outcome", "success")
        span.set_attribute("bad", object())
        assert span.attributes["outcome"] == "success"


def test_tracing_status_shape() -> None:
    status = tr.tracing_status()
    assert status["enabled"] is False
    assert status["export_enabled"] is False
    assert status["implementation"] == "noop"
    assert status["propagation"] == "w3c-traceparent"


def test_enabling_tracing_does_not_require_otel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(tr.ENV_TRACING_ENABLED, "1")
    with tr.start_span("tool.demo") as span:
        assert isinstance(span, tr.NoOpSpan)


def test_format_and_roundtrip() -> None:
    header = tr.format_traceparent(tr.new_trace_id(), tr.new_span_id())
    assert tr.parse_traceparent(header) is not None
