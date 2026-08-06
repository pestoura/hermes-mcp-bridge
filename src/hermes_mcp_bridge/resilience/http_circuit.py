"""Fail-closed circuit-breaker policy for safe upstream GET operations.

The breaker shares the exact allow-list used by selective retry. Mutations, SSE
and unknown endpoints are never protected by this circuit because rejecting or
replaying them could hide an accepted state transition.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx

from .circuit import CircuitBreakerConfig, breaker_snapshots
from .http_retry import classify_retry_target, is_transient_status

SAFE_CIRCUIT_CLASSES: tuple[str, ...] = ("health", "runs", "sessions")


class CircuitSettings(Protocol):
    bridge_circuit_enabled: bool
    bridge_circuit_failure_threshold: int
    bridge_circuit_recovery_seconds: float
    bridge_circuit_half_open_max_calls: int
    bridge_circuit_success_threshold: int


@dataclass(frozen=True)
class UpstreamCircuitPolicy:
    enabled: bool
    config: CircuitBreakerConfig


def policy_from_settings(settings: CircuitSettings) -> UpstreamCircuitPolicy:
    return UpstreamCircuitPolicy(
        enabled=bool(settings.bridge_circuit_enabled),
        config=CircuitBreakerConfig(
            failure_threshold=int(settings.bridge_circuit_failure_threshold),
            recovery_seconds=float(settings.bridge_circuit_recovery_seconds),
            half_open_max_calls=int(settings.bridge_circuit_half_open_max_calls),
            success_threshold=int(settings.bridge_circuit_success_threshold),
        ),
    )


def circuit_target(method: str, path: str) -> str | None:
    """Return a finite breaker name for a safe GET, otherwise ``None``."""

    target = classify_retry_target(method, path)
    if target is None or target.endpoint_class not in SAFE_CIRCUIT_CLASSES:
        return None
    return target.endpoint_class


def is_circuit_failure(
    *,
    response: httpx.Response | None = None,
    error: BaseException | None = None,
) -> bool:
    """Classify only availability/transient failures as breaker failures."""

    if isinstance(error, httpx.RequestError):
        return True
    return response is not None and is_transient_status(response.status_code)


def circuit_posture(settings: CircuitSettings) -> dict[str, object]:
    """Return a secret-free config and live-state summary."""

    policy = policy_from_settings(settings)
    return {
        "enabled": policy.enabled,
        "safe_endpoint_classes": list(SAFE_CIRCUIT_CLASSES),
        "failure_threshold": policy.config.failure_threshold,
        "recovery_seconds": policy.config.recovery_seconds,
        "half_open_max_calls": policy.config.half_open_max_calls,
        "success_threshold": policy.config.success_threshold,
        "mutations_protected": False,
        "sse_protected": False,
        "breakers": breaker_snapshots() if policy.enabled else [],
    }
