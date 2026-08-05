"""Directed 1.0.0 observability production-gate tests."""

from __future__ import annotations

import json
import time
from pathlib import Path

import yaml

from hermes_mcp_bridge.observability.metrics import (
    ALLOWED_LABELS,
    BOUNDED_LABEL_VALUES,
    FORBIDDEN_LABELS,
    get_metrics,
    get_registry,
    render_prometheus,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ALLOY_PROFILE = REPO_ROOT / "deploy" / "observability" / "grafana-cloud-loopback.alloy"
RULES_FILE = REPO_ROOT / "deploy" / "observability" / "hermes-bridge.rules.yml"
RUNBOOK = REPO_ROOT / "docs" / "observability-production-1.0.0.md"


def setup_function() -> None:
    get_registry().reset()


def teardown_function() -> None:
    get_registry().reset()


def test_every_allowed_label_has_a_finite_domain() -> None:
    assert set(BOUNDED_LABEL_VALUES) == set(ALLOWED_LABELS)
    for label, values in BOUNDED_LABEL_VALUES.items():
        assert values, label
        assert "other" in values, label
        assert len(values) <= 40, label


def test_unknown_label_values_fold_to_other_without_leaking_content() -> None:
    secret_marker = "Bearer-private-value-123456789"
    counter = get_metrics().tool_calls_total

    for index in range(500):
        counter.inc(
            tool=f"untrusted-tool-{index}-{secret_marker}",
            outcome=f"untrusted-outcome-{index}-{secret_marker}",
        )

    rendered = render_prometheus()
    assert secret_marker not in rendered
    assert 'tool="other"' in rendered
    assert 'outcome="other"' in rendered
    assert len(counter._values) == 1


def test_metric_health_reports_zero_unbounded_labels() -> None:
    health = get_registry().health()
    assert health["status"] == "up"
    assert health["label_domains"] == len(ALLOWED_LABELS)
    assert health["unbounded_labels"] == []


def test_process_start_time_metric_is_present_and_sane() -> None:
    started = get_metrics().process_start_time_seconds.value()
    assert started > 0
    assert started <= time.time()
    assert time.time() - started < 60
    assert "bridge_process_start_time_seconds" in render_prometheus()


def test_forbidden_labels_cover_authentication_and_cookie_material() -> None:
    assert {
        "authorization",
        "api_key",
        "token",
        "password",
        "cookie",
        "prompt",
        "output",
    } <= FORBIDDEN_LABELS


def test_grafana_cloud_profile_is_loopback_only_and_secret_free() -> None:
    text = ALLOY_PROFILE.read_text(encoding="utf-8")

    assert '"127.0.0.1:9464"' in text
    assert "172.17.0.1" not in text
    assert '"0.0.0.0:9464"' not in text
    assert 'regex         = "bridge_.*"' in text
    assert "prometheus.remote_write" in text
    assert "prometheus.scrape" in text
    assert "prometheus.relabel" in text

    for variable in (
        "GRAFANA_CLOUD_PROMETHEUS_URL",
        "GRAFANA_CLOUD_PROMETHEUS_USERNAME",
        "GRAFANA_CLOUD_PROMETHEUS_PASSWORD",
        "HERMES_ENVIRONMENT",
    ):
        assert f'sys.env("{variable}")' in text

    lowered = text.lower()
    assert "glc_" not in lowered
    assert "glsa_" not in lowered
    assert "bearer " not in lowered
    assert "otelcol." not in lowered


def test_alert_rules_use_real_restart_signal_and_have_runbooks() -> None:
    text = RULES_FILE.read_text(encoding="utf-8")
    document = yaml.safe_load(text)
    alerts = [
        rule
        for group in document["groups"]
        for rule in group.get("rules", [])
    ]
    names = {rule["alert"] for rule in alerts}

    assert "HermesBridgeRecentlyStarted" in names
    assert "HermesBridgeShortReadOnlyLatencyHigh" in names
    assert "bridge_process_start_time_seconds" in text
    assert "changes(bridge_migrations_version" not in text
    assert "bridge_tool_duration_seconds_bucket" in text

    for rule in alerts:
        assert rule.get("expr")
        assert rule.get("annotations", {}).get("runbook")


def test_runbook_defines_security_invariants_slos_and_lease_boundary() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "BRIDGE_METRICS_HOST=127.0.0.1" in text
    assert "BRIDGE_TRACING_EXPORT=0" in text
    assert "HERMES_BRIDGE_1_0_0_METRICS_GATE_PASS" in text
    assert "Minimum SLOs and indicators" in text
    assert "RITMO lease table" in text
    assert "cannot prove the lease SLO" in text

    # Documentation and deploy assets must remain safe to serialize as evidence.
    json.dumps({"runbook": text, "alloy": ALLOY_PROFILE.read_text(encoding="utf-8")})
