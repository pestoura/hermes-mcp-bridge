"""Fail-closed classification for selective upstream HTTP retry.

Only a small, explicit set of GET operations is retryable. A path that does not
match exactly is unsafe by default. Mutating POST operations, SSE streams and
unknown endpoints are never admitted by this policy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

import httpx

from .backoff import BackoffPolicy, parse_retry_after

_IDENTIFIER = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"
_RUN_STATUS_RE = re.compile(rf"^/v1/runs/{_IDENTIFIER}$")
_SESSION_MESSAGES_RE = re.compile(
    rf"^/api/sessions/{_IDENTIFIER}/messages$"
)
_TRANSIENT_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


class RetrySettings(Protocol):
    """Minimal settings shape used to construct a retry policy."""

    bridge_retry_enabled: bool
    bridge_retry_max_attempts: int
    bridge_retry_base_seconds: float
    bridge_retry_max_seconds: float
    bridge_retry_jitter_ratio: float


@dataclass(frozen=True)
class RetryTarget:
    """A low-cardinality, explicitly retry-safe upstream operation."""

    endpoint_class: str


@dataclass(frozen=True)
class UpstreamRetryPolicy:
    """Resolved retry policy for one bridge process."""

    enabled: bool
    backoff: BackoffPolicy

    @property
    def max_attempts(self) -> int:
        """Total attempts, including the initial request."""

        return self.backoff.max_attempts if self.enabled else 1


def classify_retry_target(method: str, path: str) -> RetryTarget | None:
    """Return the safe endpoint class, or ``None`` to forbid retry.

    Safe operations:

    * ``GET /health`` and ``GET /health/detailed``;
    * ``GET /v1/runs/{execution_id}``;
    * ``GET /api/sessions/{session_id}/messages``.

    The SSE events endpoint is intentionally absent. It already converges to
    polling and must not create an independent reconnect loop here.
    """

    if str(method).upper() != "GET":
        return None
    normalized = str(path or "").split("?", 1)[0]
    if normalized in {"/health", "/health/detailed"}:
        return RetryTarget(endpoint_class="health")
    if _RUN_STATUS_RE.fullmatch(normalized):
        return RetryTarget(endpoint_class="runs")
    if _SESSION_MESSAGES_RE.fullmatch(normalized):
        return RetryTarget(endpoint_class="sessions")
    return None


def is_transient_status(status_code: int) -> bool:
    """Whether a response status is eligible for a bounded safe retry."""

    return int(status_code) in _TRANSIENT_STATUS_CODES


def retry_reason(
    *,
    status_code: int | None = None,
    error: BaseException | None = None,
) -> str:
    """Map a retry cause to a bounded, non-sensitive reason label."""

    if isinstance(error, httpx.TimeoutException):
        return "timeout"
    if isinstance(error, httpx.RequestError):
        return "connect_error"
    if status_code in _TRANSIENT_STATUS_CODES:
        return "http_error"
    return "other"


def retry_after_seconds(response: httpx.Response) -> float | None:
    """Read and validate a bounded ``Retry-After`` response header."""

    return parse_retry_after(response.headers.get("Retry-After"))


def policy_from_settings(settings: RetrySettings) -> UpstreamRetryPolicy:
    """Build the immutable policy from validated bridge settings."""

    return UpstreamRetryPolicy(
        enabled=bool(settings.bridge_retry_enabled),
        backoff=BackoffPolicy(
            base_seconds=float(settings.bridge_retry_base_seconds),
            multiplier=2.0,
            max_seconds=float(settings.bridge_retry_max_seconds),
            max_attempts=int(settings.bridge_retry_max_attempts),
            jitter_ratio=float(settings.bridge_retry_jitter_ratio),
        ),
    )


def retry_posture(settings: RetrySettings) -> dict[str, object]:
    """Return a secret-free summary suitable for health evidence."""

    policy = policy_from_settings(settings)
    return {
        "enabled": policy.enabled,
        "max_attempts": policy.max_attempts,
        "safe_endpoint_classes": ["health", "runs", "sessions"],
        "mutations_retryable": False,
        "sse_retryable": False,
    }
