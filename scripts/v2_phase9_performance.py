#!/usr/bin/env python3
"""Phase 9 performance sampling for the deterministic hot path.

The Phase 9 design lane requires *measured distributions with recorded sample
counts*, not stated targets. The one component whose latency the bridge fully
owns — and whose target (`resolver decision p99 <= 5 ms`) is therefore
meaningful without a provider — is the mode resolver. Provider-bound latency is
explicitly out of scope here and is reported by connected evidence instead; this
script never contacts a provider, so its numbers are not polluted by network.

Output: per-scenario p50/p95/p99/max with the sample count and the warm-up
policy, plus a pass/fail against the accepted target.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Accepted target from ``docs/v2/phase9/performance-targets.md``.
RESOLVER_P99_TARGET_MS = 5.0
DEFAULT_SAMPLES = 2_000
WARMUP_SAMPLES = 200


def _import() -> None:
    path = str(REPO_ROOT / "src")
    if path not in sys.path:
        sys.path.insert(0, path)


def _build():
    _import()
    from hermes_mcp_bridge.v2.enums import CapabilityState
    from hermes_mcp_bridge.v2.resolver import ModeResolver
    from hermes_mcp_bridge.v2.resolver_contract import (
        IntentOperation,
        ResolverBudget,
        ResolverIntent,
    )

    read, read2, write = "github.repo_read", "github.pr_read", "github.pr_create"
    snapshot = {
        read: CapabilityState.READY,
        read2: CapabilityState.READY,
        write: CapabilityState.READY,
    }

    def operation(capability_id=read, ref=""):
        return IntentOperation(
            capability_id=capability_id,
            target_scope_ref="pestoura/hermes-mcp-bridge",
            operation_ref=ref or capability_id,
        )

    def dependent(capability_id, ref, depends_on):
        return IntentOperation(
            capability_id=capability_id,
            target_scope_ref="pestoura/hermes-mcp-bridge",
            operation_ref=ref,
            depends_on=depends_on,
        )

    def intent(operations=(), **kwargs):
        return ResolverIntent(
            request_id="perf",
            principal_ref="perf",
            operations=tuple(operations),
            **kwargs,
        )

    resolver = ModeResolver(
        snapshot=snapshot,
        snapshot_digest="p" * 64,
        budget=ResolverBudget(agentic_token_budget=1_000),
        runbooks={"rb.perf": True},
        write_capabilities=frozenset({write}),
    )
    scenarios = {
        "direct_single_node": intent((operation(),)),
        "batch_16_nodes": intent([operation(read, ref=f"op-{i}") for i in range(16)]),
        "dag_8_nodes": intent(
            (
                operation(read, ref="n0"),
                *[dependent(read2, f"n{i}", (f"n{i - 1}",)) for i in range(1, 8)],
            )
        ),
        "refusal_no_coverage": intent((), no_contract_coverage=True),
    }
    return resolver, scenarios


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def sample(resolver, intent, repetitions: int) -> list[float]:
    _import()
    from hermes_mcp_bridge.v2.enums import PolicyDecision

    durations: list[float] = []
    for _ in range(WARMUP_SAMPLES):
        resolver.resolve(intent, policy=PolicyDecision.ALLOW)
    for _ in range(repetitions):
        started = time.perf_counter_ns()
        resolver.resolve(intent, policy=PolicyDecision.ALLOW)
        durations.append((time.perf_counter_ns() - started) / 1_000_000)
    return durations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=DEFAULT_SAMPLES)
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args()

    if args.samples < 500:
        print(json.dumps({"error": "a distribution needs at least 500 samples"}))
        return 2

    resolver, scenarios = _build()
    distributions: dict[str, Any] = {}
    failures: list[str] = []
    for name, intent in scenarios.items():
        durations = sample(resolver, intent, args.samples)
        record = {
            "samples": len(durations),
            "warmup_samples": WARMUP_SAMPLES,
            "p50_ms": round(statistics.median(durations), 4),
            "p95_ms": round(_percentile(durations, 0.95), 4),
            "p99_ms": round(_percentile(durations, 0.99), 4),
            "max_ms": round(max(durations), 4),
            "mean_ms": round(statistics.fmean(durations), 4),
        }
        distributions[name] = record
        if record["p99_ms"] > RESOLVER_P99_TARGET_MS:
            failures.append(
                f"PERF-01: {name} p99 {record['p99_ms']}ms exceeds {RESOLVER_P99_TARGET_MS}ms"
            )

    report = {
        "schema": "hermes-v2-phase9-performance/1",
        "distributions": distributions,
        "failures": sorted(failures),
        "gate": "PERFORMANCE_OK" if not failures else "PERFORMANCE_BLOCKED",
        "scope": "resolver decision path only; provider latency is reported separately",
        "target_resolver_p99_ms": RESOLVER_P99_TARGET_MS,
    }
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    print(
        json.dumps(
            {"gate": report["gate"], "failures": report["failures"]}, indent=2, sort_keys=True
        )
    )
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
