"""Fail-closed classification of Hermes upstream operations.

Retry policy must be derived from the semantic operation, never just from the
HTTP verb. In particular, POST requests that can create sessions, runs or
mutations are ambiguous after a transport failure and must not be replayed
unless the upstream exposes a proven idempotency contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RetryClass(StrEnum):
    """Retry safety classification for one upstream operation."""

    SAFE_READ = "safe_read"
    IDEMPOTENT_WRITE = "idempotent_write"
    AMBIGUOUS_MUTATION = "ambiguous_mutation"
    STREAM = "stream"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class OperationPolicy:
    """Static retry policy for a normalized upstream operation."""

    name: str
    method: str
    path_template: str
    retry_class: RetryClass
    retry_transport_errors: bool
    retry_statuses: frozenset[int]

    @property
    def automatic_retry_allowed(self) -> bool:
        return self.retry_class in {
            RetryClass.SAFE_READ,
            RetryClass.IDEMPOTENT_WRITE,
        }


_RETRYABLE_READ_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


_POLICIES: tuple[OperationPolicy, ...] = (
    OperationPolicy(
        name="health",
        method="GET",
        path_template="/health",
        retry_class=RetryClass.SAFE_READ,
        retry_transport_errors=True,
        retry_statuses=_RETRYABLE_READ_STATUSES,
    ),
    OperationPolicy(
        name="capabilities",
        method="GET",
        path_template="/v1/capabilities",
        retry_class=RetryClass.SAFE_READ,
        retry_transport_errors=True,
        retry_statuses=_RETRYABLE_READ_STATUSES,
    ),
    OperationPolicy(
        name="session_messages",
        method="GET",
        path_template="/api/sessions/{session_id}/messages",
        retry_class=RetryClass.SAFE_READ,
        retry_transport_errors=True,
        retry_statuses=_RETRYABLE_READ_STATUSES,
    ),
    OperationPolicy(
        name="run_status",
        method="GET",
        path_template="/v1/runs/{execution_id}",
        retry_class=RetryClass.SAFE_READ,
        retry_transport_errors=True,
        retry_statuses=_RETRYABLE_READ_STATUSES,
    ),
    OperationPolicy(
        name="run_events",
        method="GET",
        path_template="/v1/runs/{execution_id}/events",
        retry_class=RetryClass.STREAM,
        retry_transport_errors=False,
        retry_statuses=frozenset(),
    ),
    OperationPolicy(
        name="create_session",
        method="POST",
        path_template="/api/sessions",
        retry_class=RetryClass.AMBIGUOUS_MUTATION,
        retry_transport_errors=False,
        retry_statuses=frozenset(),
    ),
    OperationPolicy(
        name="create_run",
        method="POST",
        path_template="/v1/runs",
        retry_class=RetryClass.AMBIGUOUS_MUTATION,
        retry_transport_errors=False,
        retry_statuses=frozenset(),
    ),
    OperationPolicy(
        name="stop_run",
        method="POST",
        path_template="/v1/runs/{execution_id}/stop",
        retry_class=RetryClass.AMBIGUOUS_MUTATION,
        retry_transport_errors=False,
        retry_statuses=frozenset(),
    ),
)


def operation_policies() -> tuple[OperationPolicy, ...]:
    """Return the immutable canonical upstream-operation policy."""

    return _POLICIES


def _segments(path: str) -> tuple[str, ...]:
    normalized = "/" + path.strip().strip("/")
    return tuple(part for part in normalized.split("/") if part)


def _matches(template: str, path: str) -> bool:
    expected = _segments(template)
    observed = _segments(path.split("?", 1)[0])
    if len(expected) != len(observed):
        return False
    return all(
        expected_part == observed_part
        or (expected_part.startswith("{") and expected_part.endswith("}"))
        for expected_part, observed_part in zip(expected, observed, strict=True)
    )


def classify_operation(method: str, path: str) -> OperationPolicy:
    """Classify an operation, returning a fail-closed UNKNOWN policy."""

    normalized_method = method.strip().upper()
    for policy in _POLICIES:
        if policy.method == normalized_method and _matches(policy.path_template, path):
            return policy
    return OperationPolicy(
        name="unknown",
        method=normalized_method,
        path_template=path.split("?", 1)[0],
        retry_class=RetryClass.UNKNOWN,
        retry_transport_errors=False,
        retry_statuses=frozenset(),
    )


def should_retry(
    policy: OperationPolicy,
    *,
    attempt: int,
    max_attempts: int,
    status_code: int | None = None,
    transport_error: bool = False,
) -> bool:
    """Return whether another attempt is allowed under the operation policy."""

    if attempt < 1 or max_attempts < 1 or attempt >= max_attempts:
        return False
    if not policy.automatic_retry_allowed:
        return False
    if transport_error:
        return policy.retry_transport_errors
    if status_code is None:
        return False
    return status_code in policy.retry_statuses
