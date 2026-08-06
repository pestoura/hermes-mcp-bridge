"""Hermes API client with opt-in, fail-closed upstream retry.

The pre-retry implementation is preserved in :mod:`.client_base`. This module
subclasses it only to wrap the common request path. Retry remains disabled by
default and is admitted solely for operations classified as safe by
``resilience.http_retry``.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

import httpx

from . import client_base as _base
from .client_base import HermesAPIError
from .observability import get_registry, log_event, record_upstream
from .resilience.http_retry import (
    classify_retry_target,
    is_transient_status,
    policy_from_settings,
    retry_after_seconds,
    retry_posture,
    retry_reason,
)

# Compatibility for existing tests and callers importing the historical module
# symbols. These aliases point at the same objects used by client_base.
uuid = _base.uuid
TransportFactory = _base.TransportFactory
ProgressCallback = _base.ProgressCallback

RetrySleep = Callable[[float], Awaitable[None]]


def _record_retry(
    *,
    endpoint_class: str,
    reason: str,
    attempt: int,
    delay_seconds: float,
) -> None:
    """Emit bounded retry evidence without affecting request execution."""

    try:
        registry = get_registry()
        registry.counter(
            "bridge_upstream_retries_total",
            "Safe upstream retries scheduled by endpoint class and reason.",
        ).inc(endpoint_class=endpoint_class, reason=reason)
        registry.histogram(
            "bridge_upstream_retry_delay_seconds",
            "Backoff delay before a safe upstream retry.",
        ).observe(
            float(delay_seconds),
            endpoint_class=endpoint_class,
            reason=reason,
        )
        log_event(
            "bridge.upstream.retry",
            endpoint_class=endpoint_class,
            reason=reason,
            outcome="retry",
            attempt=attempt,
            delay_ms=round(float(delay_seconds) * 1000, 3),
        )
    except Exception:
        # Telemetry is fail-open and must never alter retry or request outcome.
        with suppress(Exception):
            get_registry().counter(
                "bridge_observability_errors_total",
                "Observability internal failures by kind.",
            ).inc(kind="instrumentation")


class HermesClient(_base.HermesClient):
    """Legacy client plus bounded retry for explicitly safe GET operations."""

    def __init__(
        self,
        settings: Any,
        *,
        transport_factory: TransportFactory | None = None,
        retry_sleep: RetrySleep | None = None,
        retry_rng: random.Random | None = None,
    ) -> None:
        super().__init__(settings, transport_factory=transport_factory)
        self._retry_sleep = retry_sleep or asyncio.sleep
        self._retry_rng = retry_rng

    def retry_posture(self) -> dict[str, object]:
        """Return the non-sensitive effective retry configuration."""

        return retry_posture(self._settings)

    async def _send(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> httpx.Response:
        target = classify_retry_target(method, path)
        policy = policy_from_settings(self._settings)
        attempts = policy.max_attempts if target is not None else 1

        for attempt in range(1, attempts + 1):
            started = time.perf_counter()
            status_code: int | None = None
            outcome = "success"

            try:
                response = await client.request(method, path, **kwargs)
                status_code = response.status_code
                if status_code >= 500:
                    outcome = "upstream_error"
                elif status_code >= 400:
                    outcome = "client_error"
            except httpx.TimeoutException as exc:
                outcome = "timeout"
                record_upstream(
                    path=path,
                    status_code=None,
                    duration_seconds=time.perf_counter() - started,
                    outcome=outcome,
                )
                if target is not None and attempt < attempts:
                    reason = retry_reason(error=exc)
                    delay = policy.backoff.delay(attempt, rng=self._retry_rng)
                    _record_retry(
                        endpoint_class=target.endpoint_class,
                        reason=reason,
                        attempt=attempt,
                        delay_seconds=delay,
                    )
                    await self._retry_sleep(delay)
                    continue
                raise HermesAPIError("Hermes API request timed out") from exc
            except httpx.RequestError as exc:
                outcome = "unreachable"
                record_upstream(
                    path=path,
                    status_code=None,
                    duration_seconds=time.perf_counter() - started,
                    outcome=outcome,
                )
                if target is not None and attempt < attempts:
                    reason = retry_reason(error=exc)
                    delay = policy.backoff.delay(attempt, rng=self._retry_rng)
                    _record_retry(
                        endpoint_class=target.endpoint_class,
                        reason=reason,
                        attempt=attempt,
                        delay_seconds=delay,
                    )
                    await self._retry_sleep(delay)
                    continue
                raise HermesAPIError("Unable to reach the Hermes API server") from exc

            record_upstream(
                path=path,
                status_code=status_code,
                duration_seconds=time.perf_counter() - started,
                outcome=outcome,
            )

            if (
                target is not None
                and attempt < attempts
                and is_transient_status(response.status_code)
            ):
                reason = retry_reason(status_code=response.status_code)
                delay = policy.backoff.delay(
                    attempt,
                    rng=self._retry_rng,
                    retry_after=retry_after_seconds(response),
                )
                _record_retry(
                    endpoint_class=target.endpoint_class,
                    reason=reason,
                    attempt=attempt,
                    delay_seconds=delay,
                )
                await self._retry_sleep(delay)
                continue

            return response

        raise HermesAPIError("Safe upstream retry exhausted without a response")


def __getattr__(name: str) -> Any:
    """Delegate legacy, non-overridden module attributes to client_base."""

    return getattr(_base, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_base)))


__all__ = [
    "HermesAPIError",
    "HermesClient",
    "ProgressCallback",
    "TransportFactory",
]
