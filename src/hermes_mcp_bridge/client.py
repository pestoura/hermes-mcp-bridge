"""Hermes API client with opt-in, fail-closed resilience.

The pre-resilience implementation is preserved in :mod:`.client_base`. This
module wraps the common request path with two disabled-by-default mechanisms:

* selective retry for explicitly safe GET operations;
* one circuit breaker per safe endpoint class, evaluated once per logical call.

Mutations, SSE and unknown endpoints remain outside both mechanisms.
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
from .observability.execution import (
    observe_poll_wait,
    observe_retry,
    observe_serialization,
    observe_sse_wait,
)
from .resilience.circuit import CircuitOpenError, Clock, get_breaker
from .resilience.http_circuit import (
    circuit_posture,
    circuit_target,
    is_circuit_failure,
)
from .resilience.http_circuit import (
    policy_from_settings as circuit_policy_from_settings,
)
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
        observe_retry()
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
    """Legacy client plus bounded retry, circuit breaking and 1.x latency evidence."""

    def __init__(
        self,
        settings: Any,
        *,
        transport_factory: TransportFactory | None = None,
        retry_sleep: RetrySleep | None = None,
        retry_rng: random.Random | None = None,
        circuit_clock: Clock | None = None,
    ) -> None:
        super().__init__(settings, transport_factory=transport_factory)
        self._retry_sleep = retry_sleep or asyncio.sleep
        self._retry_rng = retry_rng
        self._circuit_clock = circuit_clock

    def retry_posture(self) -> dict[str, object]:
        """Return the non-sensitive effective retry configuration."""

        return retry_posture(self._settings)

    def circuit_posture(self) -> dict[str, object]:
        """Return the non-sensitive effective circuit configuration and state."""

        return circuit_posture(self._settings)

    @staticmethod
    def _decode(response: httpx.Response, *, expected: set[int]) -> dict[str, Any]:
        """Measure the real JSON decoding/validation boundary, fail-open."""

        started = time.perf_counter()
        try:
            return _base.HermesClient._decode(response, expected=expected)
        finally:
            with suppress(Exception):
                observe_serialization(time.perf_counter() - started)

    async def _wait_for_run_connected(
        self,
        execution_id: str,
        *,
        max_wait_seconds: float,
        fallback_session_id: str | None,
        agent: str | None,
        subagents: list[str] | None,
        progress_callback: ProgressCallback,
    ) -> Any:
        """Preserve connected wait semantics while measuring actual SSE wait time."""

        loop = asyncio.get_running_loop()
        started_at = loop.time()
        deadline = started_at + max_wait_seconds
        queue: asyncio.Queue[dict[str, Any] | _base._EventStreamEnd] = asyncio.Queue()
        reader_task = asyncio.create_task(self._read_run_events(execution_id, queue))
        terminal_event_seen = False
        stream_error: str | None = None
        next_heartbeat_at = started_at + self._settings.hermes_progress_interval_seconds

        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                timeout = min(self._settings.hermes_progress_interval_seconds, remaining)
                wait_started = time.perf_counter()
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=timeout)
                except TimeoutError:
                    observe_sse_wait(time.perf_counter() - wait_started)
                    latest = await self.get_run(
                        execution_id,
                        fallback_session_id=fallback_session_id,
                        agent=agent,
                        subagents=subagents,
                    )
                    await self._notify_progress(
                        progress_callback,
                        {
                            "event": "bridge.heartbeat",
                            "run_id": execution_id,
                            "status": latest.status.value,
                            "elapsed_seconds": round(loop.time() - started_at, 1),
                        },
                    )
                    next_heartbeat_at = (
                        loop.time() + self._settings.hermes_progress_interval_seconds
                    )
                    if latest.status in _base.TERMINAL_STATUSES:
                        return latest
                    continue
                else:
                    observe_sse_wait(time.perf_counter() - wait_started)

                if isinstance(item, _base._EventStreamEnd):
                    stream_error = item.error
                    break

                await self._notify_progress(progress_callback, item)
                event_type = str(item.get("event") or "")
                if event_type in _base._TERMINAL_EVENT_TYPES:
                    terminal_event_seen = True
                    break
                if loop.time() >= next_heartbeat_at:
                    await self._notify_progress(
                        progress_callback,
                        {
                            "event": "bridge.heartbeat",
                            "run_id": execution_id,
                            "status": "running",
                            "elapsed_seconds": round(loop.time() - started_at, 1),
                        },
                    )
                    next_heartbeat_at = (
                        loop.time() + self._settings.hermes_progress_interval_seconds
                    )
        finally:
            if not reader_task.done():
                reader_task.cancel()
            with suppress(asyncio.CancelledError):
                await reader_task

        if terminal_event_seen:
            return await self.get_run(
                execution_id,
                fallback_session_id=fallback_session_id,
                agent=agent,
                subagents=subagents,
            )

        remaining = max(0.0, deadline - loop.time())
        _base.record_sse_fallback(stream_error or "stream_ended")
        await self._notify_progress(
            progress_callback,
            {
                "event": "bridge.event_stream_fallback",
                "run_id": execution_id,
                "error": stream_error,
                "remaining_seconds": round(remaining, 1),
            },
        )
        return await self._wait_for_run_polling(
            execution_id,
            max_wait_seconds=remaining,
            fallback_session_id=fallback_session_id,
            agent=agent,
            subagents=subagents,
            progress_callback=progress_callback,
            started_at=started_at,
        )

    async def _wait_for_run_polling(
        self,
        execution_id: str,
        *,
        max_wait_seconds: float,
        fallback_session_id: str | None = None,
        agent: str | None = None,
        subagents: list[str] | None = None,
        progress_callback: ProgressCallback | None = None,
        started_at: float | None = None,
    ) -> Any:
        """Preserve polling semantics while measuring actual inter-poll wait time."""

        loop = asyncio.get_running_loop()
        started_at = loop.time() if started_at is None else started_at
        deadline = loop.time() + max_wait_seconds
        next_progress_at = loop.time() + self._settings.hermes_progress_interval_seconds
        latest = await self.get_run(
            execution_id,
            fallback_session_id=fallback_session_id,
            agent=agent,
            subagents=subagents,
        )
        while latest.status not in _base.TERMINAL_STATUSES:
            remaining = deadline - loop.time()
            if remaining <= 0:
                await self._notify_progress(
                    progress_callback,
                    {
                        "event": "bridge.wait_expired",
                        "run_id": execution_id,
                        "status": latest.status.value,
                        "elapsed_seconds": round(loop.time() - started_at, 1),
                    },
                )
                break
            sleep_for = min(self._settings.hermes_run_poll_interval_seconds, remaining)
            wait_started = time.perf_counter()
            await asyncio.sleep(sleep_for)
            observe_poll_wait(time.perf_counter() - wait_started)
            _base.record_polling_iteration()
            latest = await self.get_run(
                execution_id,
                fallback_session_id=fallback_session_id,
                agent=agent,
                subagents=subagents,
            )
            if loop.time() >= next_progress_at and latest.status not in _base.TERMINAL_STATUSES:
                await self._notify_progress(
                    progress_callback,
                    {
                        "event": "bridge.heartbeat",
                        "run_id": execution_id,
                        "status": latest.status.value,
                        "elapsed_seconds": round(loop.time() - started_at, 1),
                    },
                )
                next_progress_at = (
                    loop.time() + self._settings.hermes_progress_interval_seconds
                )
        return latest

    async def _send(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> httpx.Response:
        breaker_name = circuit_target(method, path)
        circuit_policy = circuit_policy_from_settings(self._settings)

        if breaker_name is None or not circuit_policy.enabled:
            return await self._send_with_retry(client, method, path, **kwargs)

        breaker = get_breaker(
            breaker_name,
            config=circuit_policy.config,
            clock=self._circuit_clock,
        )
        try:
            breaker.acquire()
        except CircuitOpenError as exc:
            raise HermesAPIError("Hermes API temporarily unavailable (circuit open)") from exc

        try:
            response = await self._send_with_retry(client, method, path, **kwargs)
        except HermesAPIError as exc:
            if is_circuit_failure(error=exc.__cause__ or exc):
                breaker.record_failure()
            else:
                breaker.record_success()
            raise

        if is_circuit_failure(response=response):
            breaker.record_failure()
        else:
            # Permanent 4xx/501 prove reachability and must not poison availability.
            breaker.record_success()
        return response

    async def _send_with_retry(
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
