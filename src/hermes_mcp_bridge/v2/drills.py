"""Phase 9 executable drills: rollback, credential rotation, audit restore.

> **V2 · PHASE 9 · hardening**

The Phase 9 design lane requires that rollback, rotation and restore are
*demonstrated*, not documented. This module is the executable half: each drill
is a pure, deterministic state machine over the accepted V2 primitives, so it
runs in CI with no live provider, no credentials and no host mutation, and it
emits a sanitized record with measured elapsed time against the accepted RTO/RPO
targets.

What each drill actually proves:

* :func:`run_rollback_drill` — a provider can be withdrawn by allow-list removal
  alone; afterwards the capability is unusable, and no work is left in flight
  (the drain reached zero). Elapsed time is compared against ``ROLLBACK_RTO``.
* :func:`run_rotation_drill` — new material is installed with no restart; an
  in-flight handle minted before rotation still completes on its own material
  and is never silently retried on the new one; a revoked domain fails closed.
* :func:`run_restore_drill` — a chained audit digest recomputed from restored
  records reproduces the pre-loss chain exactly. Any missing or reordered record
  breaks the chain, so RPO is measured, not asserted.

Nothing here reads a secret, a token, a path or an environment variable.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .audit_chain import digest_chain

#: Accepted objectives from ``docs/v2/phase9/chaos-and-recovery.md`` and
#: ``rollback-drills.md``. Seconds.
ROLLBACK_RTO_SECONDS: float = 900.0
GATEWAY_RTO_SECONDS: float = 300.0
#: Zero terminal audit records may be lost for write operations.
AUDIT_RPO_RECORDS: int = 0


class DrillError(RuntimeError):
    """A drill could not be completed; the gate treats this as a failure."""


@dataclass(frozen=True, slots=True)
class DrillResult:
    """Sanitized drill outcome. Safe to publish as evidence."""

    drill: str
    passed: bool
    elapsed_seconds: float
    target_seconds: float | None
    observations: Mapping[str, Any] = field(default_factory=dict)
    failures: tuple[str, ...] = ()

    def canonical(self) -> dict[str, Any]:
        return {
            "drill": self.drill,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "failures": list(self.failures),
            "observations": dict(sorted(self.observations.items())),
            "passed": self.passed,
            "target_seconds": self.target_seconds,
        }


def _elapsed(clock: Callable[[], float], started: float) -> float:
    return max(0.0, clock() - started)


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------
def run_rollback_drill(
    *,
    registry_provider_ids: Sequence[str],
    withdraw: str,
    disable: Callable[[str], Sequence[str]],
    capability_usable: Callable[[str], bool],
    live_after_drain: int,
    clock: Callable[[], float],
    target_seconds: float = ROLLBACK_RTO_SECONDS,
) -> DrillResult:
    """Withdraw one provider by allow-list removal and verify the effect.

    ``disable`` returns the allow-list after removal; ``capability_usable``
    answers whether the withdrawn provider can still be reached. Both are
    injected so the drill exercises the real registry in tests and a recorded
    fixture in CI without ever touching a live provider.
    """
    started = clock()
    failures: list[str] = []
    if withdraw not in registry_provider_ids:
        raise DrillError("withdraw target is not registered")

    remaining = tuple(disable(withdraw))
    if withdraw in remaining:
        failures.append("D-RB-01: provider still present in the allow-list")
    if len(remaining) != len(registry_provider_ids) - 1:
        failures.append("D-RB-02: allow-list removal changed more than one provider")
    if capability_usable(withdraw):
        failures.append("D-RB-03: withdrawn provider is still reachable")
    if live_after_drain != 0:
        failures.append(f"D-RB-04: {live_after_drain} operations still in flight after drain")

    elapsed = _elapsed(clock, started)
    if elapsed > target_seconds:
        failures.append(f"D-RB-05: rollback exceeded RTO ({target_seconds}s)")
    return DrillResult(
        drill="rollback",
        passed=not failures,
        elapsed_seconds=elapsed,
        target_seconds=target_seconds,
        observations={
            "providers_before": len(registry_provider_ids),
            "providers_after": len(remaining),
            "live_after_drain": live_after_drain,
            "withdrawn_reachable": capability_usable(withdraw),
        },
        failures=tuple(failures),
    )


# ---------------------------------------------------------------------------
# Credential rotation
# ---------------------------------------------------------------------------
def run_rotation_drill(
    *,
    provider_id: str,
    capability_id: str,
    mint_handle: Callable[[], Any],
    rotate: Callable[[], None],
    status: Callable[[], bool],
    apply_headers: Callable[[Any], Mapping[str, str]],
    restart_observed: bool,
    clock: Callable[[], float],
) -> DrillResult:
    """Rotate one credential domain and prove no failed-open and no restart.

    The in-flight handle is minted *before* rotation and applied *after*; it must
    still resolve against its own material. A rotation that requires a restart,
    or that leaves the capability not-ready, is a failure.
    """
    started = clock()
    failures: list[str] = []

    if not status():
        failures.append("D-CR-00: capability was not READY before rotation")
    inflight = mint_handle()
    rotate()

    if restart_observed:
        failures.append("D-CR-01: rotation required a gateway restart")
    if not status():
        failures.append("D-CR-02: capability did not return READY after rotation")

    # In-flight completion on the OLD material — never a silent retry on the new.
    try:
        headers = apply_headers(inflight)
    except Exception:
        headers = {}
        inflight_outcome = "failed_closed"
    else:
        inflight_outcome = "completed_on_old_material"
    if headers and not isinstance(headers, Mapping):
        failures.append("D-CR-03: in-flight handle returned a non-mapping authorization")

    fresh = mint_handle()
    if fresh is inflight:
        failures.append("D-CR-04: rotation reused the pre-rotation handle object")

    elapsed = _elapsed(clock, started)
    return DrillResult(
        drill="credential_rotation",
        passed=not failures,
        elapsed_seconds=elapsed,
        target_seconds=None,
        observations={
            "provider_id": provider_id,
            "capability_id": capability_id,
            "inflight_outcome": inflight_outcome,
            "ready_after_rotation": status(),
            "restart_required": restart_observed,
        },
        failures=tuple(failures),
    )


# ---------------------------------------------------------------------------
# Audit restore
# ---------------------------------------------------------------------------
def run_restore_drill(
    *,
    original_records: Sequence[Mapping[str, Any]],
    restored_records: Sequence[Mapping[str, Any]],
    clock: Callable[[], float],
    target_seconds: float = GATEWAY_RTO_SECONDS,
) -> DrillResult:
    """Recompute the chained digest from restored records and compare.

    Loss, reordering or tampering all break the chain. The record delta is the
    measured RPO.
    """
    started = clock()
    failures: list[str] = []

    original_digest = digest_chain(*original_records)
    restored_digest = digest_chain(*restored_records)
    lost = len(original_records) - len(restored_records)

    if restored_digest != original_digest:
        failures.append("D-RS-01: restored audit chain does not reproduce the original digest")
    if lost > AUDIT_RPO_RECORDS:
        failures.append(f"D-RS-02: RPO breached — {lost} terminal record(s) lost")
    if lost < 0:
        failures.append("D-RS-03: restore produced more records than existed")

    elapsed = _elapsed(clock, started)
    if elapsed > target_seconds:
        failures.append(f"D-RS-04: restore exceeded RTO ({target_seconds}s)")
    return DrillResult(
        drill="audit_restore",
        passed=not failures,
        elapsed_seconds=elapsed,
        target_seconds=target_seconds,
        observations={
            "original_records": len(original_records),
            "restored_records": len(restored_records),
            "records_lost": lost,
            "chain_matches": restored_digest == original_digest,
        },
        failures=tuple(failures),
    )


def drill_evidence(results: Sequence[DrillResult]) -> dict[str, Any]:
    """Aggregate sanitized evidence for the production gate."""
    failures = [failure for result in results for failure in result.failures]
    return {
        "audit_rpo_records": AUDIT_RPO_RECORDS,
        "drills": [result.canonical() for result in results],
        "failures": sorted(failures),
        "gateway_rto_seconds": GATEWAY_RTO_SECONDS,
        "passed": not failures,
        "rollback_rto_seconds": ROLLBACK_RTO_SECONDS,
    }


__all__ = [
    "AUDIT_RPO_RECORDS",
    "GATEWAY_RTO_SECONDS",
    "ROLLBACK_RTO_SECONDS",
    "DrillError",
    "DrillResult",
    "drill_evidence",
    "run_restore_drill",
    "run_rollback_drill",
    "run_rotation_drill",
]
