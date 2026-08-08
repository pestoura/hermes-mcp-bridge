from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.replace(".py", ""), path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


benchmark = _load_script("v2_phase0_benchmark.py")
validator = _load_script("validate_v2_phase0_evidence.py")


def _sample() -> dict:
    return {
        "run": 1,
        "success": True,
        "duration_seconds": 1.25,
        "output_bytes": 42,
        "error_type": None,
        "metrics_delta": {
            "bridge_execution_terminal_total": 1.0,
            "bridge_execution_tool_calls_sum": 2.0,
            "bridge_execution_upstream_calls_sum": 3.0,
            "bridge_execution_poll_iterations_sum": 1.0,
            "bridge_execution_retries_sum": 0.0,
            "bridge_execution_recoveries_sum": 0.0,
        },
        "contaminated_window": False,
        "tokens": {
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "source": "provider",
        },
    }


def _valid_evidence() -> dict:
    scenarios = []
    for category in ("read", "mutation", "agentic"):
        samples = []
        for run_number in (1, 2, 3):
            sample = _sample()
            sample["run"] = run_number
            samples.append(sample)
        scenarios.append(
            {
                "id": f"{category}_case",
                "category": category,
                "prompt_sha256": "a" * 64,
                "repetitions": 3,
                "samples": samples,
                "summary": {},
            }
        )
    return {
        "schema": "hermes-v2-phase0-benchmark/1",
        "gate": "BASELINE_EVIDENCE_COLLECTED",
        "started_at": "2026-08-08T00:00:00+00:00",
        "finished_at": "2026-08-08T00:01:00+00:00",
        "runtime": {
            "bridge_version": "1.0.0",
            "schema_version": "0.6.1",
            "manifest_hash": "abc123",
            "upstream_status": "ok",
        },
        "collection": {
            "mcp_url_scope": "loopback",
            "metrics_enabled": True,
            "token_sidecar_used": True,
        },
        "scenarios": scenarios,
        "privacy": {
            "prompts_stored": False,
            "outputs_stored": False,
            "secrets_stored": False,
        },
    }


def test_extract_tokens_from_nested_usage() -> None:
    payload = {
        "result": {
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            }
        }
    }
    assert benchmark._extract_tokens(payload) == {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
    }


def test_parse_prometheus_aggregates_bounded_labels() -> None:
    text = """
# HELP bridge_execution_terminal_total terminal
bridge_execution_terminal_total{outcome="success"} 2
bridge_execution_terminal_total{outcome="failed"} 1
bridge_execution_tool_calls_sum{outcome="success"} 6
bridge_execution_upstream_calls_sum{outcome="success"} 4
"""
    parsed = benchmark._parse_prometheus(text)
    assert parsed["bridge_execution_terminal_total"] == 3.0
    assert parsed["bridge_execution_tool_calls_sum"] == 6.0
    assert parsed["bridge_execution_upstream_calls_sum"] == 4.0


def test_metric_delta_allows_first_observation() -> None:
    after = {
        "bridge_execution_terminal_total": 1.0,
        "bridge_execution_tool_calls_sum": 2.0,
        "bridge_execution_upstream_calls_sum": 3.0,
    }
    delta = benchmark._metric_delta({}, after)
    assert delta["bridge_execution_terminal_total"] == 1.0
    assert delta["bridge_execution_tool_calls_sum"] == 2.0
    assert delta["bridge_execution_upstream_calls_sum"] == 3.0


def test_load_scenarios_rejects_duplicate_ids(tmp_path: Path) -> None:
    path = tmp_path / "scenarios.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "scenarios": [
                    {
                        "id": "same",
                        "category": "read",
                        "prompt": "one",
                        "repetitions": 3,
                    },
                    {
                        "id": "same",
                        "category": "agentic",
                        "prompt": "two",
                        "repetitions": 3,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    try:
        benchmark._load_scenarios(str(path))
    except ValueError as error:
        assert "duplicate scenario id" in str(error)
    else:
        raise AssertionError("duplicate scenario id was accepted")


def test_valid_phase0_evidence_passes() -> None:
    assert validator.validate_evidence(_valid_evidence()) == []


def test_prompt_or_missing_tokens_blocks_gate() -> None:
    payload = _valid_evidence()
    payload["prompt"] = "must never be persisted"
    payload["scenarios"][0]["samples"][0]["tokens"] = None
    failures = validator.validate_evidence(payload)
    assert "forbidden_evidence_keys:prompt" in failures
    assert "token_usage_missing:read_case" in failures


def test_contaminated_window_blocks_gate() -> None:
    payload = _valid_evidence()
    payload["scenarios"][1]["samples"][1]["contaminated_window"] = True
    failures = validator.validate_evidence(payload)
    assert "contaminated_metrics_window:mutation_case" in failures
