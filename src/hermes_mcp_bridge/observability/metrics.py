"""Thread-safe, dependency-free metrics registry with Prometheus text output.

Cardinality policy (enforced, not advisory):

* Only label names in :data:`ALLOWED_LABELS` may be used.
* Every allowed label has an explicit finite value domain.
* Unknown values are folded into ``other`` before a series is created.
* Identifiers, paths, prompts, output and secret-bearing labels are rejected.
"""

from __future__ import annotations

import threading
import time
from contextlib import suppress
from typing import Any

from ..build_metadata import UNKNOWN as _BUILD_UNKNOWN
from ..build_metadata import get_build_metadata
from ..contracts import CURRENT_CONTRACT_VERSION, TOOL_CONTRACTS, required_tools

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
        "state",
        "source",
        "upstream",
        "release",
        "revision",
        "contract_version",
        "schema_version",
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
        "password",
        "cookie",
        "authorization",
    }
)

MAX_SERIES_PER_METRIC = 200

_TOOL_VALUES = frozenset(required_tools(CURRENT_CONTRACT_VERSION)) | {"other"}
_VERSION_VALUES = frozenset(TOOL_CONTRACTS) | {"other"}

# Build identity domains are derived from the canonical build metadata of this
# artifact, so a new release updates them automatically while the domain stays
# closed at three values: the resolved one, ``unknown`` (metadata absent or
# malformed) and the registry-wide ``other`` sentinel. A label value that does
# not match this build can therefore never open a new series.
_BUILD_METADATA = get_build_metadata()
_BUILD_SENTINELS = frozenset({_BUILD_UNKNOWN, "other"})
_RELEASE_VALUES = frozenset({_BUILD_METADATA.release}) | _BUILD_SENTINELS
_REVISION_VALUES = frozenset({_BUILD_METADATA.revision}) | _BUILD_SENTINELS
_CONTRACT_VERSION_VALUES = frozenset(TOOL_CONTRACTS) | _BUILD_SENTINELS
_SCHEMA_VERSION_VALUES = frozenset({_BUILD_METADATA.schema_version}) | _BUILD_SENTINELS

#: Every label value is normalized into one of these finite domains. This is
#: deliberately stricter than a global series cap: user-controlled strings can
#: never create a new time series merely by changing their content.
BOUNDED_LABEL_VALUES: dict[str, frozenset[str]] = {
    "tool": _TOOL_VALUES,
    "outcome": frozenset(
        {
            "success",
            "ok",
            "error",
            "upstream_error",
            "cancelled",
            "timeout",
            "timed_out",
            "open",
            "closed",
            "connected",
            "disconnected",
            "fallback",
            "retry",
            "rejected",
            "transition",
            "approved",
            "denied",
            "expired",
            "consumed",
            "pending",
            "partial",
            "failed",
            "blocked",
            "skipped",
            "stopped",
            "unknown",
            "other",
        }
    ),
    "endpoint_class": frozenset(
        {"runs", "run_events", "run_stop", "sessions", "health", "other"}
    ),
    "status_class": frozenset({"1xx", "2xx", "3xx", "4xx", "5xx", "error", "other"}),
    "decision": frozenset(
        {
            "allow",
            "deny",
            "require_approval",
            "pending",
            "approved",
            "rejected",
            "expired",
            "consumed",
            "unknown",
            "other",
        }
    ),
    "reason": frozenset(
        {
            "stream_ended",
            "stream_closed",
            "connect_error",
            "read_error",
            "http_error",
            "parse_error",
            "timeout",
            "cancelled",
            "unknown",
            "other",
        }
    ),
    "mode": frozenset(
        {
            "json",
            "text",
            "noop",
            "otel",
            "auto",
            "explicit",
            "advisory",
            "production",
            "development",
            "test",
            "unknown",
            "other",
        }
    ),
    "version": _VERSION_VALUES,
    "kind": frozenset(
        {
            "state",
            "approvals",
            "locks",
            "migrations",
            "quota",
            "sagas",
            "checkpoints",
            "registry",
            "contention",
            "instrumentation",
            "logging",
            "metrics",
            "tracing",
            "exporter",
            "unknown",
            "other",
        }
    ),
    "state": frozenset({"closed", "open", "half_open", "other"}),
    "source": frozenset({"sse", "polling", "recovery", "unknown", "other"}),
    "upstream": frozenset(
        {"runs", "run_events", "run_stop", "sessions", "health", "other"}
    ),
    "release": _RELEASE_VALUES,
    "revision": _REVISION_VALUES,
    "contract_version": _CONTRACT_VERSION_VALUES,
    "schema_version": _SCHEMA_VERSION_VALUES,
}

#: Labels whose out-of-domain fallback is ``unknown`` instead of ``other``.
#: Build identity is either exactly what this artifact reports or unknown;
#: ``other`` would be a meaningless value for a version string.
_UNKNOWN_FALLBACK_LABELS: frozenset[str] = frozenset(
    {"release", "revision", "contract_version", "schema_version"}
)

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


def _normalize_label_value(key: str, raw_value: object) -> str:
    value = str(raw_value).strip().lower()
    if key in {"reason", "kind", "state", "source", "upstream"}:
        value = value.replace("-", "_").replace(" ", "_")
    allowed_values = BOUNDED_LABEL_VALUES[key]
    if value in allowed_values:
        return value
    return _BUILD_UNKNOWN if key in _UNKNOWN_FALLBACK_LABELS else "other"


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
        normalized.append((key, _normalize_label_value(key, raw_value)))
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


def _render_value(value: float) -> str:
    if value != value:  # NaN
        return "NaN"
    if value == float("inf"):
        return "+Inf"
    if value == float("-inf"):
        return "-Inf"
    if value == int(value):
        return str(int(value))
    return repr(value)


def _escape_help(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


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
        lines = [
            f"# HELP {self.name} {_escape_help(self.help)}",
            f"# TYPE {self.name} counter",
        ]
        with self._lock:
            items = sorted(self._values.items())
        for key, val in items:
            lines.append(f"{self.name}{_render_labels(key)} {_render_value(val)}")
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
        lines = [
            f"# HELP {self.name} {_escape_help(self.help)}",
            f"# TYPE {self.name} gauge",
        ]
        with self._lock:
            items = sorted(self._values.items())
        for key, val in items:
            lines.append(f"{self.name}{_render_labels(key)} {_render_value(val)}")
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
        lines = [
            f"# HELP {self.name} {_escape_help(self.help)}",
            f"# TYPE {self.name} histogram",
        ]
        with self._lock:
            keys = sorted(self._counts)
            snapshot = {
                key: (
                    list(self._counts[key]),
                    self._sums.get(key, 0.0),
                    self._totals.get(key, 0),
                )
                for key in keys
            }
        for key, (counts, total_sum, total) in snapshot.items():
            for index, bound in enumerate(self.buckets):
                lines.append(
                    f"{self.name}_bucket"
                    f"{_render_labels(key, ('le', _render_value(bound)))} {counts[index]}"
                )
            lines.append(
                f"{self.name}_bucket{_render_labels(key, ('le', '+Inf'))} {total}"
            )
            lines.append(
                f"{self.name}_sum{_render_labels(key)} {_render_value(total_sum)}"
            )
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
                        names.update(label for label, _ in key)
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
        return {
            "status": "up",
            "metrics": count,
            "max_series_per_metric": MAX_SERIES_PER_METRIC,
            "label_domains": len(BOUNDED_LABEL_VALUES),
            "unbounded_labels": sorted(ALLOWED_LABELS - BOUNDED_LABEL_VALUES.keys()),
        }


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
            "bridge_upstream_duration_seconds",
            "Upstream Hermes API request duration in seconds.",
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
            "bridge_active_runs",
            "Runs observed as active upstream (last observed via health; not authoritative).",
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
        self.build_info = registry.gauge(
            "bridge_build_info",
            (
                "Product build identity (always 1): release train and source "
                "revision of the running artifact, alongside the public "
                "contract and schema versions it serves."
            ),
        )
        self.process_start_time_seconds = registry.gauge(
            "bridge_process_start_time_seconds",
            "Unix timestamp when the bridge process initialized its metrics registry.",
        )
        self.process_start_time_seconds.set(time.time())
        self.observability_errors_total = registry.counter(
            "bridge_observability_errors_total", "Observability internal failures by kind."
        )
        self.sqlite_retries_total = registry.counter(
            "bridge_sqlite_retries_total",
            "Bounded SQLite retries after transient contention, by operation kind.",
        )
        self.circuit_transitions_total = registry.counter(
            "bridge_circuit_transitions_total",
            "Circuit breaker state transitions by upstream and target state.",
        )
        self.circuit_rejections_total = registry.counter(
            "bridge_circuit_rejections_total",
            "Calls rejected because the circuit was open, by upstream.",
        )
        self.duplicate_events_total = registry.counter(
            "bridge_duplicate_events_total",
            "Duplicate run events ignored idempotently, by source.",
        )
        self.out_of_order_events_total = registry.counter(
            "bridge_out_of_order_events_total",
            "Out-of-order or regressing run events ignored, by source.",
        )
        self.recovery_runs_total = registry.counter(
            "bridge_recovery_runs_total",
            "Runs recovered from persisted state after a restart, by outcome.",
        )
        self.backoff_sleep_seconds = registry.histogram(
            "bridge_backoff_sleep_seconds", "Bounded backoff sleep durations in seconds."
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
        metrics.bridge_info.set(1.0, version=str(version))


def set_build_info(metadata: Any | None = None) -> None:
    """Publish ``bridge_build_info`` from canonical build metadata.

    ``metadata`` defaults to the process-wide resolved build identity. Every
    label goes through the bounded-domain normalizer, so an unexpected value
    degrades to ``unknown`` instead of creating a new series.
    """

    resolved = get_build_metadata() if metadata is None else metadata
    with suppress(CardinalityError):  # pragma: no cover - defensive
        metrics.build_info.set(1.0, **resolved.as_labels())


def render_prometheus() -> str:
    return _registry.render()
