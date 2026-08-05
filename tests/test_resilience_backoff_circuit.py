"""Deterministic tests for bounded backoff and the circuit breaker (0.9)."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from hermes_mcp_bridge.observability.metrics import get_metrics, get_registry
from hermes_mcp_bridge.resilience import (
    BackoffPolicy,
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitOpenError,
    CircuitState,
    ManualClock,
    parse_retry_after,
)
from hermes_mcp_bridge.resilience.backoff import MAX_SLEEP_SECONDS
from hermes_mcp_bridge.resilience.circuit import get_breaker, reset_breakers


def test_backoff_schedule_is_deterministic_without_jitter() -> None:
    policy = BackoffPolicy(base_seconds=0.5, multiplier=2.0, max_seconds=4.0, max_attempts=5,
                           jitter_ratio=0.0)
    assert policy.schedule() == [0.5, 1.0, 2.0, 4.0, 4.0]


def test_backoff_is_bounded_by_max_seconds_and_global_cap() -> None:
    policy = BackoffPolicy(base_seconds=10.0, multiplier=10.0, max_seconds=60.0,
                           max_attempts=6, jitter_ratio=0.0)
    for attempt in range(1, 7):
        assert policy.delay(attempt) <= 60.0
        assert policy.delay(attempt) <= MAX_SLEEP_SECONDS


def test_backoff_jitter_is_seedable_and_reproducible() -> None:
    policy = BackoffPolicy(base_seconds=1.0, max_seconds=8.0, max_attempts=4, jitter_ratio=0.5)
    first = policy.schedule(rng=random.Random(7))
    second = policy.schedule(rng=random.Random(7))
    third = policy.schedule(rng=random.Random(8))
    assert first == second
    assert first != third
    for attempt, value in enumerate(first, start=1):
        base = policy.base_delay(attempt)
        assert base * 0.5 <= value <= base


def test_backoff_rejects_invalid_configuration() -> None:
    with pytest.raises(ValueError):
        BackoffPolicy(base_seconds=0)
    with pytest.raises(ValueError):
        BackoffPolicy(multiplier=0.5)
    with pytest.raises(ValueError):
        BackoffPolicy(max_seconds=MAX_SLEEP_SECONDS + 1)
    with pytest.raises(ValueError):
        BackoffPolicy(max_attempts=0)
    with pytest.raises(ValueError):
        BackoffPolicy(jitter_ratio=1.5)


def test_retry_after_seconds_form_is_honoured_and_bounded() -> None:
    policy = BackoffPolicy(base_seconds=0.5, max_attempts=3, jitter_ratio=0.0)
    assert parse_retry_after("7") == 7.0
    assert policy.delay(1, retry_after=7.0) == 7.0
    assert policy.delay(1, retry_after=10_000.0) == MAX_SLEEP_SECONDS


def test_retry_after_http_date_form() -> None:
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    later = (now + timedelta(seconds=30)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert parse_retry_after(later, now=now) == pytest.approx(30.0, abs=1.0)


def test_retry_after_invalid_values_are_rejected() -> None:
    assert parse_retry_after(None) is None
    assert parse_retry_after("") is None
    assert parse_retry_after("soon") is None
    assert parse_retry_after("-5") is None
    assert parse_retry_after("99999") is None


def test_circuit_opens_after_threshold_and_recovers_via_half_open() -> None:
    clock = ManualClock()
    breaker = CircuitBreaker(
        "runs",
        config=CircuitBreakerConfig(failure_threshold=3, recovery_seconds=10.0),
        clock=clock,
    )
    assert breaker.state is CircuitState.CLOSED
    for _ in range(3):
        breaker.record_failure()
    assert breaker.state is CircuitState.OPEN

    with pytest.raises(CircuitOpenError):
        breaker.acquire()
    assert breaker.rejections == 1

    clock.advance(10.0)
    assert breaker.state is CircuitState.HALF_OPEN
    breaker.acquire()
    breaker.record_success()
    assert breaker.state is CircuitState.CLOSED


def test_circuit_half_open_failure_reopens_immediately() -> None:
    clock = ManualClock()
    breaker = CircuitBreaker(
        "runs",
        config=CircuitBreakerConfig(failure_threshold=1, recovery_seconds=5.0),
        clock=clock,
    )
    breaker.record_failure()
    clock.advance(5.0)
    assert breaker.state is CircuitState.HALF_OPEN
    breaker.acquire()
    breaker.record_failure()
    assert breaker.state is CircuitState.OPEN
    with pytest.raises(CircuitOpenError):
        breaker.acquire()


def test_circuit_half_open_limits_concurrent_probes() -> None:
    clock = ManualClock()
    breaker = CircuitBreaker(
        "runs",
        config=CircuitBreakerConfig(
            failure_threshold=1, recovery_seconds=1.0, half_open_max_calls=1
        ),
        clock=clock,
    )
    breaker.record_failure()
    clock.advance(1.0)
    breaker.acquire()
    with pytest.raises(CircuitOpenError):
        breaker.acquire()


def test_circuit_success_in_closed_state_resets_failures() -> None:
    breaker = CircuitBreaker("runs", config=CircuitBreakerConfig(failure_threshold=3),
                             clock=ManualClock())
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state is CircuitState.CLOSED


def test_circuit_rejects_invalid_configuration() -> None:
    with pytest.raises(ValueError):
        CircuitBreakerConfig(failure_threshold=0)
    with pytest.raises(ValueError):
        CircuitBreakerConfig(recovery_seconds=0)
    with pytest.raises(ValueError):
        CircuitBreakerConfig(half_open_max_calls=0)
    with pytest.raises(ValueError):
        CircuitBreakerConfig(success_threshold=0)


def test_circuit_emits_bounded_transition_and_rejection_metrics() -> None:
    get_registry().reset()
    clock = ManualClock()
    breaker = CircuitBreaker(
        "runs",
        config=CircuitBreakerConfig(failure_threshold=1, recovery_seconds=5.0),
        clock=clock,
    )
    breaker.record_failure()
    with pytest.raises(CircuitOpenError):
        breaker.acquire()

    metrics = get_metrics()
    assert metrics.circuit_transitions_total.value(upstream="runs", state="open") == 1.0
    assert metrics.circuit_rejections_total.value(upstream="runs") == 1.0
    assert "upstream" in get_registry().label_names()


def test_circuit_metric_labels_are_folded_to_bounded_values() -> None:
    get_registry().reset()
    breaker = CircuitBreaker("some-unlisted-upstream", clock=ManualClock(),
                             config=CircuitBreakerConfig(failure_threshold=1))
    breaker.record_failure()
    metrics = get_metrics()
    assert metrics.circuit_transitions_total.value(upstream="other", state="open") == 1.0


def test_breaker_registry_is_memoized_and_resettable() -> None:
    reset_breakers()
    first = get_breaker("runs", clock=ManualClock())
    second = get_breaker("runs")
    assert first is second
    reset_breakers()
    assert get_breaker("runs") is not first


def test_breaker_snapshot_contains_no_identifiers() -> None:
    breaker = CircuitBreaker("runs", clock=ManualClock())
    snapshot = breaker.snapshot()
    assert set(snapshot) == {
        "name",
        "state",
        "failures",
        "successes",
        "transitions",
        "rejections",
    }
