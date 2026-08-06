"""1.0.0 retry-safety contract for Hermes upstream operations."""

from __future__ import annotations

import pytest

from hermes_mcp_bridge.resilience.operations import (
    RetryClass,
    classify_operation,
    operation_policies,
    should_retry,
)


@pytest.mark.parametrize(
    ("method", "path", "name"),
    [
        ("GET", "/health", "health"),
        ("GET", "/v1/capabilities", "capabilities"),
        ("GET", "/api/sessions/session-1/messages", "session_messages"),
        ("GET", "/v1/runs/run-1", "run_status"),
    ],
)
def test_reads_are_classified_as_safe(method: str, path: str, name: str) -> None:
    policy = classify_operation(method, path)
    assert policy.name == name
    assert policy.retry_class is RetryClass.SAFE_READ
    assert policy.automatic_retry_allowed is True


@pytest.mark.parametrize(
    ("method", "path", "name"),
    [
        ("POST", "/api/sessions", "create_session"),
        ("POST", "/v1/runs", "create_run"),
        ("POST", "/v1/runs/run-1/stop", "stop_run"),
    ],
)
def test_mutations_are_never_automatically_replayed(
    method: str, path: str, name: str
) -> None:
    policy = classify_operation(method, path)
    assert policy.name == name
    assert policy.retry_class is RetryClass.AMBIGUOUS_MUTATION
    assert policy.automatic_retry_allowed is False
    assert not should_retry(
        policy,
        attempt=1,
        max_attempts=3,
        transport_error=True,
    )
    assert not should_retry(
        policy,
        attempt=1,
        max_attempts=3,
        status_code=503,
    )


def test_sse_is_recovered_by_stream_fallback_not_request_retry() -> None:
    policy = classify_operation("GET", "/v1/runs/run-1/events")
    assert policy.retry_class is RetryClass.STREAM
    assert policy.automatic_retry_allowed is False


def test_unknown_operation_fails_closed() -> None:
    policy = classify_operation("PATCH", "/v1/future")
    assert policy.name == "unknown"
    assert policy.retry_class is RetryClass.UNKNOWN
    assert policy.automatic_retry_allowed is False
    assert policy.retry_statuses == frozenset()


def test_safe_read_retries_transport_and_allowlisted_statuses_only() -> None:
    policy = classify_operation("GET", "/v1/runs/run-1")
    assert should_retry(
        policy,
        attempt=1,
        max_attempts=3,
        transport_error=True,
    )
    assert should_retry(policy, attempt=1, max_attempts=3, status_code=429)
    assert should_retry(policy, attempt=2, max_attempts=3, status_code=503)
    assert not should_retry(policy, attempt=3, max_attempts=3, status_code=503)
    assert not should_retry(policy, attempt=1, max_attempts=3, status_code=404)
    assert not should_retry(policy, attempt=0, max_attempts=3, status_code=503)


def test_query_string_does_not_change_classification() -> None:
    policy = classify_operation("GET", "/v1/runs/run-1?include=summary")
    assert policy.name == "run_status"


def test_policy_catalog_has_unique_method_and_template_pairs() -> None:
    pairs = [(policy.method, policy.path_template) for policy in operation_policies()]
    assert len(pairs) == len(set(pairs))


def test_only_read_or_explicitly_idempotent_operations_can_retry() -> None:
    for policy in operation_policies():
        if policy.automatic_retry_allowed:
            assert policy.retry_class in {
                RetryClass.SAFE_READ,
                RetryClass.IDEMPOTENT_WRITE,
            }
        else:
            assert policy.retry_transport_errors is False
            assert policy.retry_statuses == frozenset()
