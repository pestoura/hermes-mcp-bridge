"""Centralised log hygiene: one event per fact, one JSON stream.

Problem this module solves
--------------------------

Two independent failure modes break machine-readable logs:

1. **Duplication** — a bridge record is emitted once by the bridge handler and
   again by an ancestor handler (``logging.lastResort`` or a handler installed
   by an embedding application), so a single fact appears twice on the stream.
2. **Third-party plain text** — libraries such as ``httpx``, ``uvicorn`` or
   ``mcp`` install (or inherit) their own handlers and print unstructured,
   *unredacted* lines next to the JSON events. A consumer doing
   ``json.loads`` per line then fails, and the lines themselves may carry URLs
   with identifiers or query strings.

The policy implemented here is deliberately simple and centralised:

* the bridge logger owns exactly **one** handler, and the root handler we
  install *filters out* the ``hermes_mcp_bridge`` tree, so a bridge record can
  never be written twice even though propagation stays enabled (propagation is
  what lets embedding applications and ``pytest``'s ``caplog`` observe records);
* the **root** logger gets the same JSON/text formatter, so any third-party
  record is re-emitted through the redacting pipeline instead of raw;
* third-party loggers lose their own handlers, propagate to root, and are
  raised to ``BRIDGE_LOG_THIRD_PARTY_LEVEL`` (default ``WARNING``) so useful
  warnings and errors survive while per-request noise disappears;
* ``warnings`` are captured into logging rather than written to stderr.

Nothing here decides authorization or policy, and every operation is
best-effort: a failure to quiet a library must never break the bridge.
Applying the policy twice is a no-op (idempotent).
"""

from __future__ import annotations

import logging
import os
from typing import Any

ENV_THIRD_PARTY_LEVEL = "BRIDGE_LOG_THIRD_PARTY_LEVEL"
ENV_CAPTURE_THIRD_PARTY = "BRIDGE_LOG_CAPTURE_THIRD_PARTY"

DEFAULT_THIRD_PARTY_LEVEL = "WARNING"

BRIDGE_LOGGER_NAME = "hermes_mcp_bridge"

#: Marker attribute used to recognise handlers this module installed.
HANDLER_MARKER = "_hermes_bridge_root_handler"

#: Loggers known to emit unstructured or high-volume output next to ours.
THIRD_PARTY_LOGGERS: tuple[str, ...] = (
    "anyio",
    "asyncio",
    "concurrent.futures",
    "httpcore",
    "httpx",
    "hypercorn",
    "mcp",
    "sse_starlette",
    "starlette",
    "urllib3",
    "uvicorn",
    "uvicorn.access",
    "uvicorn.error",
    "watchfiles",
)


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def capture_third_party() -> bool:
    """Whether third-party records are routed through the bridge formatter.

    Enabled by default; ``BRIDGE_LOG_CAPTURE_THIRD_PARTY=0`` opts out (useful
    when an embedding application owns the root logger).
    """

    raw = os.environ.get(ENV_CAPTURE_THIRD_PARTY, "").strip()
    return True if raw == "" else _truthy(raw)


def third_party_level() -> int:
    name = os.environ.get(ENV_THIRD_PARTY_LEVEL, DEFAULT_THIRD_PARTY_LEVEL).strip().upper()
    level = getattr(logging, name, None)
    return level if isinstance(level, int) else logging.WARNING


class BridgeTreeFilter(logging.Filter):
    """Drop records originating from the bridge logger tree.

    Installed on the *root* handler so a bridge record handled by the bridge's
    own handler is not written a second time while propagation stays on.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        name = record.name or ""
        return not (name == BRIDGE_LOGGER_NAME or name.startswith(BRIDGE_LOGGER_NAME + "."))


def _detach_handlers(logger: logging.Logger) -> int:
    removed = 0
    for handler in list(logger.handlers):
        try:
            logger.removeHandler(handler)
            removed += 1
        except Exception:  # pragma: no cover - defensive
            pass
    return removed


def remove_root_handlers() -> int:
    """Remove root handlers previously installed by this module."""

    root = logging.getLogger()
    removed = 0
    for handler in list(root.handlers):
        if getattr(handler, HANDLER_MARKER, False):
            root.removeHandler(handler)
            removed += 1
    return removed


def apply_quiet_policy(handler: logging.Handler) -> dict[str, Any]:
    """Install the single-stream policy around ``handler``.

    ``handler`` is the bridge's own (already redacting) handler. Returns a
    non-sensitive summary suitable for health output. Never raises. Repeated
    calls converge on the same state (idempotent).
    """

    summary: dict[str, Any] = {
        "third_party_captured": False,
        "third_party_level": logging.getLevelName(third_party_level()),
        "quieted_loggers": 0,
        "detached_handlers": 0,
        "root_handler_installed": False,
        "duplicate_suppression": "root_handler_filters_bridge_tree",
    }
    try:
        # Propagation stays enabled: embedding apps and pytest's caplog must be
        # able to observe bridge records. Duplication is prevented by the
        # BridgeTreeFilter on the root handler instead.
        logging.getLogger(BRIDGE_LOGGER_NAME).propagate = True

        detached = remove_root_handlers()
        if not capture_third_party():
            summary["detached_handlers"] = detached
            return summary

        root = logging.getLogger()
        level = third_party_level()
        root_handler = logging.StreamHandler(stream=getattr(handler, "stream", None))
        root_handler.setFormatter(handler.formatter)
        root_handler.setLevel(level)
        root_handler.addFilter(BridgeTreeFilter())
        setattr(root_handler, HANDLER_MARKER, True)
        root.addHandler(root_handler)
        summary["root_handler_installed"] = True
        if root.level == logging.NOTSET or root.level > level:
            root.setLevel(level)

        quieted = 0
        for name in THIRD_PARTY_LOGGERS:
            logger = logging.getLogger(name)
            detached += _detach_handlers(logger)
            logger.propagate = True
            # Only set a level where none was configured. A library (or an
            # operator running a support session at DEBUG) that set its own
            # level explicitly keeps it: hygiene must not override deliberate
            # verbosity, and must never hide warnings/errors.
            if logger.level == logging.NOTSET:
                logger.setLevel(level)
            quieted += 1
        summary["third_party_captured"] = True
        summary["quieted_loggers"] = quieted
        summary["detached_handlers"] = detached

        # Route warnings.warn() into logging instead of raw stderr text.
        logging.captureWarnings(True)
        warnings_logger = logging.getLogger("py.warnings")
        _detach_handlers(warnings_logger)
        warnings_logger.propagate = True
    except Exception:  # pragma: no cover - hygiene must never break the bridge
        pass
    return summary


def quiet_status() -> dict[str, Any]:
    """Non-sensitive view of the current hygiene configuration."""

    root = logging.getLogger()
    bridge = logging.getLogger(BRIDGE_LOGGER_NAME)
    return {
        "third_party_captured": capture_third_party(),
        "third_party_level": logging.getLevelName(third_party_level()),
        "bridge_handlers": len(bridge.handlers),
        "bridge_propagates": bridge.propagate,
        "root_bridge_handlers": sum(
            1 for h in root.handlers if getattr(h, HANDLER_MARKER, False)
        ),
        "managed_loggers": len(THIRD_PARTY_LOGGERS),
        "duplicate_suppression": "root_handler_filters_bridge_tree",
    }
