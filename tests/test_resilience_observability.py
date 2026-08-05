"""Observability contract for the Block 3 resilience metrics and the harness."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_mcp_bridge.observability import instrumentation as inst
from hermes_mcp_bridge.observability.metrics import (
    ALLOWED_LABELS,
    BOUNDED_LABEL_VALUES,
    FORBIDDEN_LABELS,
    MAX_SERIES_PER_METRIC,
    CardinalityError,
    get_metrics,
    get_registry,
    render_prometheus,
)

NEW_METRICS = (
    "bridge_sqlite_retries_total",
    "bridge_circuit_transitions_total",
    "bridge_circuit_rejections_total",
    "bridge_duplicate_events_total",
    "bridge_out_of_order_events_total",
    "bridge_recovery_runs_total",
    "bridge_backoff_sleep_seconds",
)


@pytest.fixture(autouse=True)
def _fresh_registry() -> None:
    get_registry().reset()


def test_new_metrics_are_declared_and_rendered() -> None:
    inst.record_sqlite_retry(kind="state")
    inst.record_circuit_transition(name="runs", state="open")
    inst.record_circuit_rejection(name="runs")
    inst.record_duplicate_event(source="sse")
    inst.record_out_of_order_event(source="polling")
    inst.record_recovery(outcome="recovered", count=2)
    inst.record_backoff_sleep(0.25, source="polling")

    text = render_prometheus()
    for name in NEW_METRICS:
        assert f"# TYPE {name}" in text


def test_new_labels_are_allow_listed_and_not_forbidden() -> None:
    for label in ("state", "source", "upstream"):
        assert label in ALLOWED_LABELS
        assert label not in FORBIDDEN_LABELS


def test_identifier_labels_remain_rejected() -> None:
    for label in ("run_id", "execution_id", "session_id", "correlation_id"):
        with pytest.raises(CardinalityError):
            get_metrics().duplicate_events_total.inc(**{label: "x"})


def test_bounded_label_values_fold_unknown_input_into_other() -> None:
    inst.record_duplicate_event(source="totally-unknown-source")
    inst.record_circuit_transition(name="a" * 200, state="weird")
    metrics = get_metrics()
    assert metrics.duplicate_events_total.value(source="other") == 1.0
    assert metrics.circuit_transitions_total.value(upstream="other", state="other") == 1.0


def test_resilience_label_cardinality_is_provably_finite() -> None:
    for index in range(500):
        inst.record_duplicate_event(source=f"src-{index}")
        inst.record_circuit_rejection(name=f"up-{index}")
    names = get_registry().label_names()
    assert names <= ALLOWED_LABELS

    text = render_prometheus()
    duplicate_series = [
        line for line in text.splitlines() if line.startswith("bridge_duplicate_events_total{")
    ]
    assert len(duplicate_series) <= len(BOUNDED_LABEL_VALUES["source"])
    assert len(duplicate_series) < MAX_SERIES_PER_METRIC


def test_metrics_output_contains_no_identifier_like_values() -> None:
    inst.record_duplicate_event(source="sse")
    inst.record_circuit_transition(name="run_events", state="half_open")
    text = render_prometheus()
    for forbidden in ("prompt", "secret", "token", "api_key", "Bearer"):
        assert forbidden not in text


def test_recording_helpers_never_raise_on_bad_input() -> None:
    inst.record_sqlite_retry(kind="")
    inst.record_circuit_transition(name="", state="")
    inst.record_circuit_rejection(name="")
    inst.record_duplicate_event(source="")
    inst.record_out_of_order_event(source="")
    inst.record_recovery(outcome="", count=-5)
    inst.record_backoff_sleep(-1.0, source="")


def test_structured_logs_from_resilience_paths_are_sanitized(capsys) -> None:  # type: ignore[no-untyped-def]
    from hermes_mcp_bridge.observability.logging import configure_logging

    configure_logging(force=True)
    inst.record_circuit_transition(name="runs", state="open")
    inst.record_sqlite_retry(kind="state")
    captured = capsys.readouterr()
    payloads = [
        json.loads(line)
        for line in (captured.out + captured.err).splitlines()
        if line.strip().startswith("{")
    ]
    assert payloads
    for payload in payloads:
        assert "prompt" not in payload
        assert "output" not in payload
        assert not any(
            key in payload for key in ("run_id", "execution_id", "session_id", "token")
        )


# -- load harness -------------------------------------------------------


def test_load_harness_ci_profile_passes_and_reports_sanitized_json(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[1]
    out = tmp_path / "report.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(repo / "scripts" / "load_harness.py"),
            "--profile",
            "ci",
            "--duration",
            "2",
            "--workers",
            "4",
            "--json-out",
            str(out),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        cwd=str(repo),
    )
    assert completed.returncode == 0, completed.stderr[-2000:]

    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["result"] == "PASS"
    assert report["failures"] == []
    assert report["verification"]["integrity_check"] == "ok"
    assert report["verification"]["duplicate_mappings"] == 0
    assert report["verification"]["double_consumed_approvals"] == 0
    assert report["counters"]["unexpected_errors"] == 0
    assert report["operations"] > 0

    text = json.dumps(report)
    for forbidden in ("prompt", "HERMES_API_KEY", "Bearer", "/home/"):
        assert forbidden not in text
    assert len(report["db_fingerprint"]) == 12


def test_load_harness_rejects_out_of_range_arguments() -> None:
    repo = Path(__file__).resolve().parents[1]
    for args in (["--duration", "0"], ["--workers", "0"], ["--duration", "999999"]):
        completed = subprocess.run(
            [sys.executable, str(repo / "scripts" / "load_harness.py"), *args],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            cwd=str(repo),
        )
        assert completed.returncode != 0


def test_load_harness_declares_every_documented_profile() -> None:
    import importlib.util

    repo = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "load_harness", repo / "scripts" / "load_harness.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve types via sys.modules[cls.__module__]; register the
    # module before executing it or @dataclass raises AttributeError.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    assert set(module.PROFILES) == {"ci", "soak-30m", "soak-60m", "soak-2h"}
    assert module.PROFILES["soak-2h"]["duration_seconds"] == 7200.0
    assert module.MAX_DURATION_SECONDS == 8 * 3600
