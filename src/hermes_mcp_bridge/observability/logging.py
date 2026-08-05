"""Deterministic, secret-safe structured logging for the bridge.

Design constraints:

* JSON is the default in production/container; ``BRIDGE_LOG_FORMAT=text`` opts
  into a human format for local debugging only.
* Every payload passes through :mod:`~hermes_mcp_bridge.observability.redaction`
  before serialization (fail closed).
* Logging must never break execution: formatter/sink failures are swallowed.
* No stack traces are emitted verbatim; exceptions become ``{type, message}``.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from typing import Any

from .context import get_context
from .quiet import apply_quiet_policy, quiet_status
from .redaction import enforce_total_size, sanitize

ENV_FORMAT = "BRIDGE_LOG_FORMAT"
ENV_LEVEL = "BRIDGE_LOG_LEVEL"

_RESERVED = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)

_configured = False
_quiet_summary: dict[str, Any] = {}


def log_mode() -> str:
    """Return the effective logging mode: ``json`` (default) or ``text``."""

    value = os.environ.get(ENV_FORMAT, "").strip().lower()
    return "text" if value == "text" else "json"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class JsonFormatter(logging.Formatter):
    """Deterministic JSON formatter (sorted keys, UTC timestamps)."""

    def format(self, record: logging.LogRecord) -> str:
        try:
            payload = self._build(record)
            return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:  # pragma: no cover - never break the caller
            return json.dumps(
                {"ts": _utc_now(), "level": "ERROR", "event": "log.format_failed"},
                sort_keys=True,
            )

    def _build(self, record: logging.LogRecord) -> dict[str, Any]:
        event = getattr(record, "event", None) or record.getMessage()
        payload: dict[str, Any] = {
            "ts": _utc_now(),
            "level": record.levelname,
            "event": sanitize(str(event)),
            "logger": record.name,
        }
        for key, value in get_context().items():
            payload[key] = sanitize(value, _key=key)
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_") or key == "event":
                continue
            payload[key] = sanitize(value, _key=key)
        if record.exc_info and record.exc_info[1] is not None:
            payload["error"] = sanitize(record.exc_info[1])
        return enforce_total_size(payload)


class TextFormatter(logging.Formatter):
    """Compact human formatter; still fully redacted."""

    def format(self, record: logging.LogRecord) -> str:
        try:
            event = getattr(record, "event", None) or record.getMessage()
            ctx = " ".join(f"{k}={v}" for k, v in sorted(get_context().items()))
            extras = {
                k: sanitize(v, _key=k)
                for k, v in record.__dict__.items()
                if k not in _RESERVED and not k.startswith("_") and k != "event"
            }
            rendered = " ".join(f"{k}={v}" for k, v in sorted(extras.items()))
            parts = [_utc_now(), record.levelname, sanitize(str(event)), ctx, rendered]
            return " ".join(p for p in parts if p)
        except Exception:  # pragma: no cover
            return f"{_utc_now()} ERROR log.format_failed"


class SafeHandler(logging.StreamHandler):
    """Stream handler that never propagates sink/formatter failures."""

    def emit(self, record: logging.LogRecord) -> None:
        with suppress(Exception):  # pragma: no cover - fail open for telemetry
            super().emit(record)

    def handleError(self, record: logging.LogRecord) -> None:
        return None


def configure_logging(*, force: bool = False) -> logging.Logger:
    """Install the bridge root handler once (idempotent)."""

    global _configured, _quiet_summary
    root = logging.getLogger("hermes_mcp_bridge")
    if _configured and not force:
        return root
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = SafeHandler(stream=sys.stderr)
    handler.setFormatter(JsonFormatter() if log_mode() == "json" else TextFormatter())
    root.addHandler(handler)
    level = os.environ.get(ENV_LEVEL, os.environ.get("LOG_LEVEL", "INFO")).upper()
    root.setLevel(getattr(logging, level, logging.INFO))
    # Propagation stays enabled so embedding applications (and pytest's caplog)
    # can still observe bridge records; the handler above owns formatting.
    root.propagate = True
    # Single-stream hygiene: third-party records go through the same redacting
    # formatter, and the root handler filters out the bridge tree so a bridge
    # record is never written twice. Idempotent and never raises.
    _quiet_summary = apply_quiet_policy(handler)
    _configured = True
    return root


def get_logger(name: str = "hermes_mcp_bridge") -> logging.Logger:
    configure_logging()
    return logging.getLogger(name)


def log_event(event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    """Emit a structured event; never raises."""

    try:
        logger = get_logger()
        logger.log(level, event, extra={"event": event, **fields})
    except Exception:  # pragma: no cover - logging must not break execution
        pass


@contextmanager
def timed_event(event: str, **fields: Any) -> Iterator[dict[str, Any]]:
    """Log ``event`` with ``duration_ms`` and ``outcome`` on exit."""

    started = time.perf_counter()
    state: dict[str, Any] = dict(fields)
    try:
        yield state
    except BaseException as exc:
        error_type = type(exc).__name__
        state["outcome"] = "cancelled" if error_type == "CancelledError" else "error"
        state["error_type"] = error_type
        log_event(
            event,
            level=logging.WARNING,
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
            **state,
        )
        raise
    else:
        state.setdefault("outcome", "success")
        log_event(
            event,
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
            **state,
        )


def observability_status() -> dict[str, Any]:
    """Non-sensitive logging status for health output."""

    return {
        "logging_mode": log_mode(),
        "logging_configured": _configured,
        "level": logging.getLogger("hermes_mcp_bridge").getEffectiveLevel(),
        "redaction": "fail-closed",
        "hygiene": quiet_status(),
    }
