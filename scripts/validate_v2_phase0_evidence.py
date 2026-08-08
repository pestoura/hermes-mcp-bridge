#!/usr/bin/env python3
"""Validate connected evidence for the Hermes MCP Bridge v2 Phase 0 gate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

EVIDENCE_SCHEMA = "hermes-v2-phase0-benchmark/1"
REQUIRED_CATEGORIES = frozenset({"read", "mutation", "agentic"})
FORBIDDEN_KEYS = frozenset(
    {
        "prompt",
        "prompt_text",
        "output",
        "response",
        "secret",
        "token",
        "password",
        "authorization",
        "api_key",
    }
)


def _walk_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            keys.add(str(key).lower())
            keys.update(_walk_keys(child))
    elif isinstance(value, list):
        for child in value:
            keys.update(_walk_keys(child))
    return keys


def validate_evidence(payload: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if payload.get("schema") != EVIDENCE_SCHEMA:
        failures.append("invalid_schema")
    if payload.get("gate") != "BASELINE_EVIDENCE_COLLECTED":
        failures.append("invalid_collection_gate")

    privacy = payload.get("privacy")
    if privacy != {"prompts_stored": False, "outputs_stored": False, "secrets_stored": False}:
        failures.append("privacy_contract_not_met")

    collection = payload.get("collection")
    if not isinstance(collection, dict) or collection.get("metrics_enabled") is not True:
        failures.append("metrics_collection_not_enabled")

    runtime = payload.get("runtime")
    if not isinstance(runtime, dict):
        failures.append("runtime_identity_missing")
    else:
        if runtime.get("bridge_version") != "1.0.0":
            failures.append("unexpected_bridge_version")
        if runtime.get("schema_version") != "0.6.1":
            failures.append("unexpected_schema_version")
        if not runtime.get("manifest_hash"):
            failures.append("manifest_hash_missing")
        if runtime.get("upstream_status") not in {"ok", "healthy"}:
            failures.append("upstream_not_healthy")

    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        failures.append("scenarios_missing")
        return failures

    categories: set[str] = set()
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            failures.append("scenario_not_object")
            continue
        scenario_id = scenario.get("id")
        category = scenario.get("category")
        if not isinstance(scenario_id, str) or not scenario_id:
            failures.append("scenario_id_missing")
        if category in REQUIRED_CATEGORIES:
            categories.add(category)
        else:
            failures.append(f"scenario_category_invalid:{scenario_id}")

        digest = scenario.get("prompt_sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            failures.append(f"prompt_digest_invalid:{scenario_id}")

        samples = scenario.get("samples")
        if not isinstance(samples, list) or len(samples) < 3:
            failures.append(f"insufficient_repetitions:{scenario_id}")
            continue

        for sample in samples:
            if not isinstance(sample, dict):
                failures.append(f"sample_not_object:{scenario_id}")
                continue
            if sample.get("success") is not True:
                failures.append(f"unsuccessful_sample:{scenario_id}")
            try:
                if float(sample.get("duration_seconds", 0)) <= 0:
                    failures.append(f"duration_invalid:{scenario_id}")
            except (TypeError, ValueError):
                failures.append(f"duration_invalid:{scenario_id}")

            if sample.get("contaminated_window") is True:
                failures.append(f"contaminated_metrics_window:{scenario_id}")

            tokens = sample.get("tokens")
            if not isinstance(tokens, dict):
                failures.append(f"token_usage_missing:{scenario_id}")
            else:
                try:
                    input_tokens = int(tokens["input_tokens"])
                    output_tokens = int(tokens["output_tokens"])
                    total_tokens = int(tokens["total_tokens"])
                except (KeyError, TypeError, ValueError):
                    failures.append(f"token_usage_invalid:{scenario_id}")
                else:
                    if min(input_tokens, output_tokens, total_tokens) < 0:
                        failures.append(f"token_usage_invalid:{scenario_id}")
                    if total_tokens < input_tokens + output_tokens:
                        failures.append(f"token_total_invalid:{scenario_id}")
                    if not tokens.get("source"):
                        failures.append(f"token_source_missing:{scenario_id}")

            deltas = sample.get("metrics_delta")
            if not isinstance(deltas, dict):
                failures.append(f"metrics_delta_missing:{scenario_id}")
            else:
                required_metrics = (
                    "bridge_execution_terminal_total",
                    "bridge_execution_tool_calls_sum",
                    "bridge_execution_upstream_calls_sum",
                )
                for name in required_metrics:
                    value = deltas.get(name)
                    if value is None:
                        failures.append(f"metric_missing:{scenario_id}:{name}")
                    else:
                        try:
                            numeric = float(value)
                        except (TypeError, ValueError):
                            failures.append(f"metric_invalid:{scenario_id}:{name}")
                        else:
                            if numeric < 0:
                                failures.append(f"metric_negative:{scenario_id}:{name}")
                terminal = deltas.get("bridge_execution_terminal_total")
                if terminal is not None:
                    try:
                        terminal_value = float(terminal)
                    except (TypeError, ValueError):
                        pass
                    else:
                        if terminal_value != 1.0:
                            failures.append(f"terminal_delta_not_one:{scenario_id}")

    missing = REQUIRED_CATEGORIES - categories
    if missing:
        failures.append("missing_categories:" + ",".join(sorted(missing)))

    keys = _walk_keys(payload)
    forbidden_present = sorted(FORBIDDEN_KEYS & keys)
    if forbidden_present:
        failures.append("forbidden_evidence_keys:" + ",".join(forbidden_present))

    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence")
    parser.add_argument("--json-out")
    args = parser.parse_args()

    payload = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit("evidence must be a JSON object")

    failures = validate_evidence(payload)
    result = {
        "gate": "BASELINE_ACCEPTED" if not failures else "BASELINE_BLOCKED",
        "failures": sorted(set(failures)),
    }
    text = json.dumps(result, indent=2, sort_keys=True)
    print(text)
    if args.json_out:
        Path(args.json_out).write_text(text + "\n", encoding="utf-8")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
