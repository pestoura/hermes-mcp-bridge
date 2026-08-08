#!/usr/bin/env python3
"""Collect sanitized connected evidence for Hermes MCP Bridge v2 Phase 0.

The harness executes explicitly supplied v1 agentic scenarios sequentially through
``hermes_prompt`` and records only aggregate timing/call-count/token metadata.
Prompts and outputs are never written to the evidence file.

A scenario file is JSON:

{
  "schema_version": "1",
  "scenarios": [
    {"id": "github_read", "category": "read", "prompt": "...", "repetitions": 3},
    {"id": "safe_mutation", "category": "mutation", "prompt": "...", "repetitions": 3},
    {"id": "agentic_reasoning", "category": "agentic", "prompt": "...", "repetitions": 3}
  ]
}

Token usage is extracted from the Hermes result when available. If the runtime does
not expose it, pass ``--token-usage-file`` with records keyed by scenario/run:

{
  "github_read:1": {"input_tokens": 123, "output_tokens": 45, "source": "provider"},
  ...
}

The resulting JSON is suitable for ``validate_v2_phase0_evidence.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import statistics
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

EVIDENCE_SCHEMA = "hermes-v2-phase0-benchmark/1"
SCENARIO_SCHEMA = "1"
ALLOWED_CATEGORIES = frozenset({"read", "mutation", "agentic"})
TERMINAL_FAILURES = frozenset({"failed", "error", "cancelled", "canceled", "stopped", "rejected"})
_METRIC_BASES = (
    "bridge_execution_terminal_total",
    "bridge_execution_tool_calls_sum",
    "bridge_execution_upstream_calls_sum",
    "bridge_execution_poll_iterations_sum",
    "bridge_execution_retries_sum",
    "bridge_execution_recoveries_sum",
)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _payload(result: Any) -> Any:
    structured = getattr(result, "structuredContent", None)
    if structured is None:
        structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    parts: list[str] = []
    for item in getattr(result, "content", []):
        text = getattr(item, "text", None)
        if isinstance(text, str):
            parts.append(text)
    if not parts:
        return None
    combined = "\n".join(parts)
    try:
        return json.loads(combined)
    except json.JSONDecodeError:
        return combined


def _result_success(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return payload is not None
    status = payload.get("status")
    if isinstance(status, str) and status.lower() in TERMINAL_FAILURES:
        return False
    if payload.get("ok") is False or payload.get("success") is False:
        return False
    return True


def _safe_output_bytes(payload: Any) -> int:
    try:
        return len(json.dumps(payload, default=str, sort_keys=True).encode("utf-8"))
    except Exception:
        return 0


def _extract_tokens(value: Any) -> dict[str, int] | None:
    """Return the first recognizable token-usage object without retaining payload text."""
    if isinstance(value, dict):
        keysets = (
            ("input_tokens", "output_tokens", "total_tokens"),
            ("prompt_tokens", "completion_tokens", "total_tokens"),
        )
        for input_key, output_key, total_key in keysets:
            if input_key in value and output_key in value:
                try:
                    input_tokens = int(value[input_key])
                    output_tokens = int(value[output_key])
                    total_tokens = int(value.get(total_key, input_tokens + output_tokens))
                except (TypeError, ValueError):
                    continue
                if input_tokens >= 0 and output_tokens >= 0 and total_tokens >= input_tokens + output_tokens:
                    return {
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "total_tokens": total_tokens,
                    }
        for child in value.values():
            found = _extract_tokens(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _extract_tokens(child)
            if found is not None:
                return found
    return None


def _read_json(path: str | None) -> dict[str, Any]:
    if path is None:
        return {}
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _load_scenarios(path: str) -> list[dict[str, Any]]:
    payload = _read_json(path)
    if payload.get("schema_version") != SCENARIO_SCHEMA:
        raise ValueError(f"scenario schema_version must be {SCENARIO_SCHEMA!r}")
    raw = payload.get("scenarios")
    if not isinstance(raw, list) or not raw:
        raise ValueError("scenario file must contain a non-empty scenarios list")

    seen: set[str] = set()
    scenarios: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each scenario must be an object")
        scenario_id = item.get("id")
        category = item.get("category")
        prompt = item.get("prompt")
        repetitions = item.get("repetitions", 3)
        if not isinstance(scenario_id, str) or not scenario_id or len(scenario_id) > 64:
            raise ValueError("scenario id must be a non-empty string <= 64 chars")
        if scenario_id in seen:
            raise ValueError(f"duplicate scenario id: {scenario_id}")
        if category not in ALLOWED_CATEGORIES:
            raise ValueError(f"scenario {scenario_id}: invalid category")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError(f"scenario {scenario_id}: prompt is required")
        if not isinstance(repetitions, int) or not 1 <= repetitions <= 20:
            raise ValueError(f"scenario {scenario_id}: repetitions must be 1..20")
        seen.add(scenario_id)
        scenarios.append(
            {
                "id": scenario_id,
                "category": category,
                "prompt": prompt,
                "repetitions": repetitions,
            }
        )
    return scenarios


def _parse_prometheus(text: str) -> dict[str, float]:
    values: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        metric, raw_value = parts[0], parts[1]
        try:
            value = float(raw_value)
        except (ValueError, TypeError):
            continue
        base = metric.split("{", 1)[0]
        if base in _METRIC_BASES and math.isfinite(value):
            values[base] = values.get(base, 0.0) + value
    return values


def _scrape_metrics(url: str | None, timeout: float) -> dict[str, float]:
    if not url:
        return {}
    request = urllib.request.Request(url, headers={"Accept": "text/plain"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        text = response.read().decode("utf-8", errors="replace")
    return _parse_prometheus(text)


def _metric_delta(before: dict[str, float], after: dict[str, float]) -> dict[str, float | None]:
    result: dict[str, float | None] = {}
    for metric in _METRIC_BASES:
        if metric not in after:
            result[metric] = None
        else:
            result[metric] = round(after[metric] - before.get(metric, 0.0), 6)
    return result


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def _summary(samples: list[dict[str, Any]]) -> dict[str, Any]:
    durations = [float(sample["duration_seconds"]) for sample in samples]
    successes = sum(1 for sample in samples if sample["success"])
    token_rows = [sample["tokens"] for sample in samples if sample.get("tokens") is not None]
    return {
        "runs": len(samples),
        "successes": successes,
        "failures": len(samples) - successes,
        "duration_seconds": {
            "median": round(statistics.median(durations), 6),
            "p95": round(_percentile(durations, 0.95), 6),
            "min": round(min(durations), 6),
            "max": round(max(durations), 6),
        },
        "tokens": {
            "complete_runs": len(token_rows),
            "input_total": sum(int(row["input_tokens"]) for row in token_rows),
            "output_total": sum(int(row["output_tokens"]) for row in token_rows),
            "total": sum(int(row["total_tokens"]) for row in token_rows),
        },
    }


@dataclass(frozen=True)
class RuntimeIdentity:
    bridge_version: str | None
    schema_version: str | None
    manifest_hash: str | None
    upstream_status: str | None


async def _identity(session: ClientSession) -> RuntimeIdentity:
    result = await session.call_tool("hermes_health", arguments={"detailed": False})
    payload = _payload(result)
    if not isinstance(payload, dict):
        return RuntimeIdentity(None, None, None, None)
    bridge = payload.get("bridge") if isinstance(payload.get("bridge"), dict) else {}
    upstream = payload.get("upstream") if isinstance(payload.get("upstream"), dict) else {}
    return RuntimeIdentity(
        str(bridge.get("manifest_version")) if bridge.get("manifest_version") is not None else None,
        str(bridge.get("schema_version")) if bridge.get("schema_version") is not None else None,
        str(bridge.get("manifest_hash")) if bridge.get("manifest_hash") is not None else None,
        str(upstream.get("status")) if upstream.get("status") is not None else None,
    )


def _sidecar_tokens(sidecar: dict[str, Any], scenario_id: str, run_number: int) -> dict[str, Any] | None:
    row = sidecar.get(f"{scenario_id}:{run_number}")
    if not isinstance(row, dict):
        return None
    try:
        input_tokens = int(row["input_tokens"])
        output_tokens = int(row["output_tokens"])
        total_tokens = int(row.get("total_tokens", input_tokens + output_tokens))
    except (KeyError, TypeError, ValueError):
        return None
    if min(input_tokens, output_tokens, total_tokens) < 0 or total_tokens < input_tokens + output_tokens:
        return None
    source = row.get("source", "sidecar")
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "source": str(source)[:64],
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    scenarios = _load_scenarios(args.scenarios)
    token_sidecar = _read_json(args.token_usage_file)
    started_at = datetime.now(timezone.utc)
    evidence: list[dict[str, Any]] = []

    streamable = streamable_http_client(args.url)
    read_stream, write_stream, _ = await streamable.__aenter__()
    try:
        session = ClientSession(read_stream, write_stream)
        await session.__aenter__()
        try:
            await session.initialize()
            identity = await _identity(session)

            for scenario in scenarios:
                samples: list[dict[str, Any]] = []
                prompt_hash = _sha256_text(scenario["prompt"])
                for run_number in range(1, scenario["repetitions"] + 1):
                    before_metrics = _scrape_metrics(args.metrics_url, args.http_timeout)
                    started = time.monotonic()
                    error_type: str | None = None
                    payload: Any = None
                    try:
                        result = await session.call_tool(
                            "hermes_prompt",
                            arguments={
                                "prompt": scenario["prompt"],
                                "wait_seconds": args.wait_seconds,
                            },
                            read_timeout_seconds=timedelta(
                                seconds=max(120.0, args.wait_seconds + 60.0)
                            ),
                        )
                        payload = _payload(result)
                        success = _result_success(payload)
                    except Exception as error:
                        success = False
                        error_type = type(error).__name__
                    duration = time.monotonic() - started
                    after_metrics = _scrape_metrics(args.metrics_url, args.http_timeout)

                    tokens = _extract_tokens(payload)
                    if tokens is not None:
                        tokens = {**tokens, "source": "hermes_result"}
                    else:
                        tokens = _sidecar_tokens(token_sidecar, scenario["id"], run_number)

                    deltas = _metric_delta(before_metrics, after_metrics)
                    terminal_delta = deltas.get("bridge_execution_terminal_total")
                    contaminated = terminal_delta is not None and terminal_delta != 1.0

                    samples.append(
                        {
                            "run": run_number,
                            "success": bool(success),
                            "duration_seconds": round(duration, 6),
                            "output_bytes": _safe_output_bytes(payload),
                            "error_type": error_type,
                            "metrics_delta": deltas,
                            "contaminated_window": bool(contaminated),
                            "tokens": tokens,
                        }
                    )

                evidence.append(
                    {
                        "id": scenario["id"],
                        "category": scenario["category"],
                        "prompt_sha256": prompt_hash,
                        "repetitions": scenario["repetitions"],
                        "samples": samples,
                        "summary": _summary(samples),
                    }
                )
        finally:
            await session.__aexit__(None, None, None)
    finally:
        await streamable.__aexit__(None, None, None)

    finished_at = datetime.now(timezone.utc)
    return {
        "schema": EVIDENCE_SCHEMA,
        "gate": "BASELINE_EVIDENCE_COLLECTED",
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "runtime": {
            "bridge_version": identity.bridge_version,
            "schema_version": identity.schema_version,
            "manifest_hash": identity.manifest_hash,
            "upstream_status": identity.upstream_status,
        },
        "collection": {
            "mcp_url_scope": "loopback" if "127.0.0.1" in args.url or "localhost" in args.url else "non_loopback",
            "metrics_enabled": bool(args.metrics_url),
            "token_sidecar_used": bool(args.token_usage_file),
        },
        "scenarios": evidence,
        "privacy": {
            "prompts_stored": False,
            "outputs_stored": False,
            "secrets_stored": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8765/mcp")
    parser.add_argument("--metrics-url", default="http://127.0.0.1:9464/metrics")
    parser.add_argument("--scenarios", required=True)
    parser.add_argument("--token-usage-file")
    parser.add_argument(
        "--ack-mutation-sandbox",
        action="store_true",
        help="confirm mutation scenarios target an isolated/disposable resource",
    )
    parser.add_argument("--json-out", required=True)
    parser.add_argument("--wait-seconds", type=float, default=180.0)
    parser.add_argument("--http-timeout", type=float, default=5.0)
    args = parser.parse_args()

    if args.wait_seconds < 0 or args.wait_seconds > 900:
        parser.error("--wait-seconds must be in [0, 900]")
    if args.http_timeout <= 0 or args.http_timeout > 60:
        parser.error("--http-timeout must be in (0, 60]")

    scenarios = _load_scenarios(args.scenarios)
    if any(item["category"] == "mutation" for item in scenarios) and not args.ack_mutation_sandbox:
        parser.error(
            "mutation scenarios require --ack-mutation-sandbox and MUST target an isolated/disposable resource"
        )

    report = asyncio.run(_run(args))
    text = json.dumps(report, indent=2, sort_keys=True)
    Path(args.json_out).write_text(text + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "gate": report["gate"],
                "scenarios": len(report["scenarios"]),
                "evidence_sha256": _sha256_text(text),
                "output": args.json_out,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
