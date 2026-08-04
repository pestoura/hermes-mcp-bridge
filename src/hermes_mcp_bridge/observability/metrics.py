"""Thread-safe, dependency-free metrics registry with Prometheus text output.

Cardinality policy (enforced, not advisory):

* Only label names in :data:`ALLOWED_LABELS` may be used.
* Identifiers such as ``run_id``, ``session_id``, ``execution_id``,
  ``client_request_id``, paths and secrets are rejected as label names, and
  label *values* are bounded per metric to avoid unbounded series growth.
"""

from __future__ import annotations

import threading
from contextlib import suppress
from typing import Any

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

#: Low-cardinality label names allowed anywhere in the bridge.
ALLOWED_LABELS: frozenset[str] = frozenset(
    {
        "tool",
        "outcome",
        "endpoint_class",
        "status_class",
        "decision",
        "reason",
        "mode",
        "version",
        "kind",
    }
)

#: Label names that must never appear (high cardinality / privacy).
FORBIDDEN_LABELS: frozenset[str] = frozenset(
    {
        "run_id",
        "session_id",
        "execution_id",
        "client_request_id",
        "approval_id",
        "correlation_id",
        "trace_id",
        "user",
        "prompt",
        "output",
        "path",
        "url",
        "token",
        "api_key",
    }
)

MAX_SERIES_PER_METRIC = 200
DEFAULT_BUCKETS: tuple[float, ...] = (
    0.005,
    0.025,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    300.0,
)


class CardinalityError(ValueError):
    """Raised when a metric would introduce forbidden/high-cardinality labels."""


def _validate_labels(labels: dict[str, str] | None) -> tuple[tuple[str, str], ...]:
    if not labels:
        return ()
    normalized: list[tuple[str, str]] = []
    for raw_key, raw_value in labels.items():
        key = str(raw_key).strip().lower()
        if key in FORBIDDEN_LABELS:
            raise CardinalityError(f"forbidden label: {key}")
        if key not in ALLOWED_LABELS:
            raise CardinalityError(f"label not allow-listed: {key}")
        value = str(raw_value)
        if len(value) > 64:
            value = value[:64]
        normalized.append((key, value))
    return tuple(sorted(normalized))


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _render_labels(
    labels: tuple[tuple[str, str], ...], extra: tuple[str, str] | None = None
) -> str:
    items = list(labels)
    if extra:
        items.append(extra)
    if not items:
        return ""
    body = ",".join(f'{k}="{_escape(v)}"' for k, v in items)
    return "{" + body + "}"


class _Metric:
    def __init__(self, name: str, help_text: str, mtype: str) -> None:
        self.name = name
        self.help = help_text
        self.type = mtype
        self._lock = threading.Lock()

    def render(self) -> list[str]:  # pragma: no cover - overridden
        raise NotImplementedError

    def _guard_series(self, store: dict[Any, Any], key: Any) -> bool:
        if key in store:
            return True
        return len(store) < MAX_SERIES_PER_METRIC


class Counter(_Metric):
    def __init__(self, name: str, help_text: str) -> None:
        super().__init__(name, help_text, "counter")
        self._values: dict[tuple[tuple[str, str], ...], float] = {}

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        key = _validate_labels(labels)
        with self._lock:
            if not self._guard_series(self._values, key):
                return
            self._values[key] = self._values.get(key, 0.0) + float(amount)

    def value(self, **labels: str) -> float:
        key = _validate_labels(labels)
        with self._lock:
            return self._values.get(key, 0.0)

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} counter"]
        with self._lock:
            items = sorted(self._values.items())
        for key, val in items:
            lines.append(f"{self.name}{_render_labels(key)} {val!r}")
        return lines


class Gauge(_Metric):
    def __init__(self, name: str, help_text: str) -> None:
        super().__init__(name, help_text, "gauge")
        self._values: dict[tuple[tuple[str, str], ...], float] = {}

    def set(self, value: float, **labels: str) -> None:
        key = _validate_labels(labels)
        with self._lock:
            if not self._guard_series(self._values, key):
                return
            self._values[key] = float(value)

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        key = _validate_labels(labels)
        with self._lock:
            if not self._guard_series(self._values, key):
                return
            self._values[key] = self._values.get(key, 0.0) + float(amount)

    def dec(self, amount: float = 1.0, **labels: str) -> None:
        self.inc(-amount, **labels)

    def value(self, **labels: str) -> float:
        key = _validate_labels(labels)
        with self._lock:
            return self._values.get(key, 0.0)

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} gauge"]
        with self._lock:
            items = sorted(self._values.items())
        for key, val in items:
            lines.append(f"{self.name}{_render_labels(key)} {val!r}")
        return lines


class Histogram(_Metric):
    def __init__(
        self, name: str, help_text: str, buckets: tuple[float, ...] = DEFAULT_BUCKETS
    ) -> None:
        super().__init__(name, help_text, "histogram")
        self.buckets = tuple(sorted(buckets))
        self._counts: dict[tuple[tuple[str, str], ...], list[int]] = {}
        self._sums: dict[tuple[tuple[str, str], ...], float] = {}
        self._totals: dict[tuple[tuple[str, str], ...], int] = {}

    def observe(self, value: float, **labels: str) -> None:
        key = _validate_labels(labels)
        amount = float(value)
        with self._lock:
            if not self._guard_series(self._counts, key):
                return
            counts = self._counts.setdefault(key, [0] * len(self.buckets))
            for index, bound in enumerate(self.buckets):
                if amount <= bound:
                    counts[index] += 1
            self._sums[key] = self._sums.get(key, 0.0) + amount
            self._totals[key] = self._totals.get(key, 0) + 1

    def snapshot(self, **labels: str) -> dict[str, Any]:
        key = _validate_labels(labels)
        with self._lock:
            return {
                "count": self._totals.get(key, 0),
                "sum": self._sums.get(key, 0.0),
            }

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} histogram"]
        with self._lock:
            keys = sorted(self._counts)
            snapshot = {
                k: (list(self._counts[k]), self._sums.get(k, 0.0), self._totals.get(k, 0))
                for k in keys
            }
        for key, (counts, total_sum, total) in snapshot.items():
            for index, bound in enumerate(self.buckets):
                lines.append(
                    f"{self.name}_bucket"
                    f"{_render_labels(key, ('le', repr(bound)))} {counts[index]}"
                )
            lines.append(f"{self.name}_bucket{_render_labels(key, ('le', '+Inf'))} {total}")
            lines.append(f"{self.name}_sum{_render_labels(key)} {total_sum!r}")
            lines.append(f"{self.name}_count{_render_labels(key)} {total}")
        return lines


class MetricsRegistry:
    """Thread-safe registry of bridge metrics."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._metrics: dict[str, _Metric] = {}

    def counter(self, name: str, help_text: str) -> Counter:
        return self._get_or_create(name, help_text, Counter)

    def gauge(self, name: str, help_text: str) -> Gauge:
        return self._get_or_create(name, help_text, Gauge)

    def histogram(
        self, name: str, help_text: str, buckets: tuple[float, ...] = DEFAULT_BUCKETS
    ) -> Histogram:
        with self._lock:
            existing = self._metrics.get(name)
            if isinstance(existing, Histogram):
                return existing
            metric = Histogram(name, help_text, buckets)
            self._metrics[name] = metric
            return metric

    def _get_or_create(self, name: str, help_text: str, cls: type) -> Any:
        with self._lock:
            existing = self._metrics.get(name)
            if isinstance(existing, cls):
                return existing
            metric = cls(name, help_text)
            self._metrics[name] = metric
            return metric

    def label_names(self) -> set[str]:
        names: set[str] = set()
        with self._lock:
            metrics = list(self._metrics.values())
        for metric in metrics:
            for attr in ("_values", "_counts"):
                store = getattr(metric, attr, None)
                if isinstance(store, dict):
                    for key in store:
                        names.update(k for k, _ in key)
        return names

    def render(self) -> str:
        with self._lock:
            metrics = [self._metrics[name] for name in sorted(self._metrics)]
        lines: list[str] = []
        for metric in metrics:
            lines.extend(metric.render())
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self._metrics.clear()
        _install_defaults(self)

    def health(self) -> dict[str, Any]:
        with self._lock:
            count = len(self._metrics)
        return {"status": "up", "metrics": count, "max_series_per_metric": MAX_SERIES_PER_METRIC}


_registry = MetricsRegistry()


class _Metrics:
    """Namespace of the bridge's declared metrics."""

    def __init__(self, registry: MetricsRegistry) -> None:
        self.tool_calls_total = registry.counter(
            "bridge_tool_calls_total", "MCP tool invocations by tool and outcome."
        )
        self.tool_duration_seconds = registry.histogram(
            "bridge_tool_duration_seconds", "MCP tool duration in seconds."
        )
        self.tool_inflight = registry.gauge(
            "bridge_tool_inflight", "In-flight MCP tool invocations."
        )
        self.upstream_requests_total = registry.counter(
            "bridge_upstream_requests_total",
            "Upstream Hermes API requests by endpoint class and status class.",
        )
        self.upstream_duration_seconds = registry.histogram(
            "bridge_upstream_duration_seconds", "Upstream Hermes API request duration in seconds."
        )
        self.sse_connections_total = registry.counter(
            "bridge_sse_connections_total", "SSE event-stream connection attempts by outcome."
        )
        self.sse_fallbacks_total = registry.counter(
            "bridge_sse_fallbacks_total", "SSE to polling fallbacks by reason."
        )
        self.polling_iterations_total = registry.counter(
            "bridge_polling_iterations_total", "Run polling iterations."
        )
        self.active_runs = registry.gauge(
            "bridge_active_runs", "Runs observed as active by the bridge."
        )
        self.approvals_total = registry.counter(
            "bridge_approvals_total", "Approval outcomes by decision."
        )
        self.sqlite_errors_total = registry.counter(
            "bridge_sqlite_errors_total", "SQLite errors by kind."
        )
        self.sqlite_lock_contention_total = registry.counter(
            "bridge_sqlite_lock_contention_total", "SQLite lock contention events."
        )
        self.migrations_version = registry.gauge(
            "bridge_migrations_version", "Applied state schema migration version."
        )
        self.bridge_info = registry.gauge(
            "bridge_info", "Bridge build info (always 1); version carried as a label."
        )
        self.observability_errors_total = registry.counter(
            "bridge_observability_errors_total", "Observability internal failures by kind."
        )


def _install_defaults(registry: MetricsRegistry) -> _Metrics:
    global metrics
    metrics = _Metrics(registry)
    return metrics


metrics = _Metrics(_registry)


def get_registry() -> MetricsRegistry:
    return _registry


def get_metrics() -> _Metrics:
    return metrics


def set_bridge_info(version: str) -> None:
    with suppress(CardinalityError):  # pragma: no cover - defensive
        metrics.bridge_info.set(1.0, version=str(version)[:64])


def render_prometheus() -> str:
    return _registry.render()
