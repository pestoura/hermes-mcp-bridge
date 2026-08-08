"""1.x operational-intelligence wrappers for admission and coverage.

This module is deliberately additive and contract-preserving. It does not add MCP
Tools or change the execution plane. It augments the existing 1.x readiness result
with an admission dimension and publishes low-cardinality coverage/admission
metrics.

Telemetry remains fail-open. Admission probing is read-only and uses the existing
authenticated Hermes client. No prompt, output, arguments, identifiers or secrets
are emitted as metrics labels.
"""

from __future__ import annotations

import asyncio
import functools
import importlib
from contextlib import suppress
from typing import Any

from .instrumentation import instrument_all_tools as _base_instrument_all_tools
from .logging import log_event
from .metrics import BOUNDED_LABEL_VALUES, get_registry

_ADMISSION_REASONS = frozenset({"draining", "unavailable", "timeout", "rejected", "other"})

# Keep the reason label finite while extending the pre-existing SSE reason domain.
BOUNDED_LABEL_VALUES["reason"] = frozenset(BOUNDED_LABEL_VALUES["reason"] | _ADMISSION_REASONS)


def _metric_gauge(name: str, help_text: str) -> Any:
    return get_registry().gauge(name, help_text)


def _metric_counter(name: str, help_text: str) -> Any:
    return get_registry().counter(name, help_text)


def _set_coverage(*, expected: int, instrumented: int) -> None:
    """Publish FastMCP instrumentation coverage without affecting startup."""

    try:
        _metric_gauge(
            "bridge_expected_tools",
            "Expected MCP tools registered in the active 1.x server.",
        ).set(float(expected))
        _metric_gauge(
            "bridge_instrumented_tools",
            "MCP tools covered by central bridge instrumentation.",
        ).set(float(instrumented))
        ratio = 1.0 if expected == 0 else instrumented / expected
        _metric_gauge(
            "bridge_instrumentation_coverage_ratio",
            "Ratio of instrumented MCP tools to expected registered tools.",
        ).set(float(ratio))
    except Exception:
        return


def _set_admission_ready(value: bool) -> None:
    try:
        _metric_gauge(
            "bridge_upstream_admission_ready",
            "Whether upstream Hermes is accepting new work (1=yes, 0=no).",
        ).set(1.0 if value else 0.0)
    except Exception:
        return


def _record_admission_failure(reason: str) -> None:
    normalized = str(reason or "other").strip().lower().replace("-", "_")
    if normalized not in _ADMISSION_REASONS:
        normalized = "other"
    try:
        _metric_counter(
            "bridge_upstream_admission_failures_total",
            "New-work admission failures by bounded reason.",
        ).inc(reason=normalized)
    except Exception:
        return


def _gateway_state(payload: dict[str, Any]) -> str:
    """Extract a bounded gateway state from known detailed-health shapes."""

    candidates: list[Any] = [payload.get("gateway_state")]
    gateway = payload.get("gateway")
    if isinstance(gateway, dict):
        candidates.extend((gateway.get("state"), gateway.get("gateway_state")))
    readiness = payload.get("readiness")
    if isinstance(readiness, dict):
        candidates.append(readiness.get("gateway_state"))
        checks = readiness.get("checks")
        if isinstance(checks, dict):
            gateway_check = checks.get("gateway")
            if isinstance(gateway_check, dict):
                candidates.extend(
                    (gateway_check.get("state"), gateway_check.get("gateway_state"))
                )
    for raw in candidates:
        state = str(raw or "").strip().lower().replace("-", "_")
        if state:
            return state
    return "unknown"


def classify_admission(payload: dict[str, Any]) -> dict[str, Any]:
    """Classify authenticated detailed health into admission semantics."""

    state = _gateway_state(payload)
    top_status = str(payload.get("status") or "").strip().lower()
    if state in {"running", "ready"}:
        accepting = True
        reason: str | None = None
    elif state == "draining":
        accepting = False
        reason = "draining"
    elif state in {"unavailable", "down", "failed", "stopped"}:
        accepting = False
        reason = "unavailable"
    elif top_status in {"ok", "healthy", "ready", "running"} and state == "unknown":
        # Fail closed for admission: a healthy-looking response that does not
        # expose the gateway state is not evidence that new work is accepted.
        accepting = False
        reason = "other"
    else:
        accepting = False
        reason = "unavailable"
    return {
        "status": "ready" if accepting else "not_ready",
        "accepting_new_work": accepting,
        "gateway_state": state,
        "reason": reason,
    }


async def _probe_admission() -> dict[str, Any]:
    """Read authenticated detailed health without exposing sensitive fields."""

    try:
        server = importlib.import_module("hermes_mcp_bridge.server")
        payload = await server.client.health(detailed=True)
        if not isinstance(payload, dict):
            raise TypeError("detailed health was not an object")
        admission = classify_admission(payload)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        name = exc.__class__.__name__.lower()
        reason = "timeout" if "timeout" in name else "unavailable"
        admission = {
            "status": "not_ready",
            "accepting_new_work": False,
            "gateway_state": "unknown",
            "reason": reason,
        }
    _set_admission_ready(bool(admission["accepting_new_work"]))
    return admission


def _returned_failure(result: Any) -> tuple[bool, bool]:
    if not isinstance(result, dict):
        return False, False
    status = str(result.get("status") or "").strip().lower()
    error = str(result.get("error") or result.get("message") or "")
    failed = status in {"failed", "error", "rejected"} or bool(error)
    return failed, "503" in error


async def _augment_readiness(result: Any) -> Any:
    if not isinstance(result, dict):
        return result
    admission = await _probe_admission()
    augmented = dict(result)
    components = dict(augmented.get("components") or {})
    components["admission"] = admission
    augmented["components"] = components
    augmented["alive"] = True
    augmented["ready"] = str(augmented.get("status") or "") == "ready"
    augmented["accepting_new_work"] = bool(admission["accepting_new_work"])
    return augmented


async def _observe_503_admission() -> None:
    admission = await _probe_admission()
    reason = str(admission.get("reason") or "rejected")
    _record_admission_failure(reason)
    with suppress(Exception):
        log_event(
            "bridge.upstream.admission_failure",
            outcome="rejected",
            reason=reason,
            gateway_state=str(admission.get("gateway_state") or "unknown")[:32],
        )


def _operational_wrapper(tool_name: str, fn: Any) -> Any:
    if not asyncio.iscoroutinefunction(fn):
        return fn

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        result = await fn(*args, **kwargs)
        if tool_name == "hermes_readiness":
            return await _augment_readiness(result)
        if tool_name in {"hermes_prompt", "hermes_submit"}:
            _failed, is_503 = _returned_failure(result)
            if is_503:
                await _observe_503_admission()
        return result

    wrapper.__bridge_operational_intelligence__ = True  # type: ignore[attr-defined]
    return wrapper


def instrument_all_tools(mcp_server: Any) -> int:
    """Instrument all tools, then add 1.x operational-intelligence wrappers.

    The returned count remains the base instrumentation count so existing startup
    semantics are unchanged. Coverage is exported separately and CI can require
    it to equal the registered tool count.
    """

    instrumented = _base_instrument_all_tools(mcp_server)
    expected = 0
    try:
        manager = mcp_server._tool_manager
        tools = getattr(manager, "_tools", None)
        if not isinstance(tools, dict):
            _set_coverage(expected=0, instrumented=instrumented)
            return instrumented
        expected = len(tools)
        for tool_name in ("hermes_readiness", "hermes_prompt", "hermes_submit"):
            tool = tools.get(tool_name)
            fn = getattr(tool, "fn", None) if tool is not None else None
            if fn is None or getattr(fn, "__bridge_operational_intelligence__", False):
                continue
            wrapped = _operational_wrapper(tool_name, fn)
            try:
                object.__setattr__(tool, "fn", wrapped)
            except Exception:
                continue
            # Keep the module-level callable aligned for direct tests/callers.
            with suppress(Exception):
                server = importlib.import_module("hermes_mcp_bridge.server")
                setattr(server, tool_name, wrapped)
    finally:
        _set_coverage(expected=expected, instrumented=instrumented)
    return instrumented
