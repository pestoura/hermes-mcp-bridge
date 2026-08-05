"""BLOCO 6C phase 1: log hygiene, exporter bind scope, deploy assets, shim.

Every test here asserts a behaviour introduced or tightened in 0.9.0
observability hardening. Nothing touches the network beyond loopback and
nothing writes outside ``tmp_path``.
"""

from __future__ import annotations

import importlib
import io
import json
import logging
import re
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

from hermes_mcp_bridge.observability import exporter as exp
from hermes_mcp_bridge.observability import logging as obs_logging
from hermes_mcp_bridge.observability import quiet
from hermes_mcp_bridge.observability.metrics import ALLOWED_LABELS, FORBIDDEN_LABELS

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_DIR = REPO_ROOT / "deploy" / "observability"
SCRAPE_SNIPPET = DEPLOY_DIR / "prometheus-scrape.snippet.yml"
RULES_FILE = DEPLOY_DIR / "hermes-bridge.rules.yml"
ALERTMANAGER_FILE = DEPLOY_DIR / "alertmanager.example.yml"
COMPOSE_FILE = REPO_ROOT / "compose.yml"
SMOKE_SCRIPT = REPO_ROOT / "scripts" / "observability_smoke.py"

SECRET = "sk-live-0123456789abcdef"


yaml = pytest.importorskip("yaml")


# --------------------------------------------------------------------------
# 1. Log hygiene: one JSON stream, no duplicates, redaction everywhere
# --------------------------------------------------------------------------


@pytest.fixture
def log_stream(monkeypatch: pytest.MonkeyPatch):
    """Configure the real pipeline against an in-memory stream and restore it."""

    root = logging.getLogger()
    bridge = logging.getLogger(quiet.BRIDGE_LOGGER_NAME)
    saved_root_handlers = list(root.handlers)
    saved_bridge_handlers = list(bridge.handlers)
    saved_root_level = root.level
    saved_bridge_level = bridge.level
    saved_propagate = bridge.propagate
    saved_third_party = {
        name: (logging.getLogger(name).level, list(logging.getLogger(name).handlers))
        for name in quiet.THIRD_PARTY_LOGGERS
    }
    buffer = io.StringIO()

    monkeypatch.delenv("BRIDGE_LOG_FORMAT", raising=False)
    monkeypatch.delenv(quiet.ENV_CAPTURE_THIRD_PARTY, raising=False)
    monkeypatch.delenv(quiet.ENV_THIRD_PARTY_LEVEL, raising=False)

    obs_logging.configure_logging(force=True)
    for handler in bridge.handlers:
        if isinstance(handler, logging.StreamHandler):
            handler.setStream(buffer)
    for handler in root.handlers:
        if getattr(handler, quiet.HANDLER_MARKER, False) and isinstance(
            handler, logging.StreamHandler
        ):
            handler.setStream(buffer)

    yield buffer

    root.handlers[:] = saved_root_handlers
    bridge.handlers[:] = saved_bridge_handlers
    root.setLevel(saved_root_level)
    bridge.setLevel(saved_bridge_level)
    bridge.propagate = saved_propagate
    for name, (level, handlers) in saved_third_party.items():
        logger = logging.getLogger(name)
        logger.setLevel(level)
        logger.handlers[:] = handlers
    obs_logging._configured = False


def _lines(buffer: io.StringIO) -> list[dict]:
    payloads = []
    for line in buffer.getvalue().splitlines():
        if line.strip():
            payloads.append(json.loads(line))
    return payloads


def test_bridge_event_is_emitted_exactly_once(log_stream: io.StringIO) -> None:
    obs_logging.log_event("bridge.test.once", outcome="success")
    events = [p["event"] for p in _lines(log_stream)]
    assert events.count("bridge.test.once") == 1


def test_every_line_on_the_stream_is_valid_json(log_stream: io.StringIO) -> None:
    obs_logging.log_event("bridge.test.json", outcome="success")
    logging.getLogger("httpx").warning("HTTP Request: GET http://127.0.0.1:8642/v1/runs?x=1")
    logging.getLogger("uvicorn.error").error("boom")
    raw = [line for line in log_stream.getvalue().splitlines() if line.strip()]
    assert len(raw) >= 3
    for line in raw:
        json.loads(line)  # must not raise


def test_third_party_records_are_redacted(log_stream: io.StringIO) -> None:
    logging.getLogger("httpx").warning("calling with Authorization: Bearer %s", SECRET)
    dumped = log_stream.getvalue()
    assert SECRET not in dumped
    assert "[REDACTED]" in dumped


def test_third_party_query_strings_do_not_leak_run_ids(log_stream: io.StringIO) -> None:
    logging.getLogger("httpx").warning(
        "HTTP Request: GET http://127.0.0.1:8642/v1/runs?api_key=%s", SECRET
    )
    dumped = log_stream.getvalue()
    assert SECRET not in dumped


def test_useful_third_party_warnings_and_errors_survive(log_stream: io.StringIO) -> None:
    logging.getLogger("httpx").warning("upstream timeout")
    logging.getLogger("mcp").error("session closed unexpectedly")
    logging.getLogger("httpx").info("per-request noise that should be dropped")
    events = [p["event"] for p in _lines(log_stream)]
    assert "upstream timeout" in events
    assert "session closed unexpectedly" in events
    assert "per-request noise that should be dropped" not in events


def test_third_party_debug_level_is_not_lowered(monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator who deliberately raised verbosity keeps it."""

    httpx_logger = logging.getLogger("httpx")
    saved = httpx_logger.level
    try:
        httpx_logger.setLevel(logging.DEBUG)
        handler = logging.StreamHandler(stream=io.StringIO())
        handler.setFormatter(obs_logging.JsonFormatter())
        quiet.apply_quiet_policy(handler)
        assert httpx_logger.level == logging.DEBUG
    finally:
        httpx_logger.setLevel(saved)
        quiet.remove_root_handlers()


def test_apply_quiet_policy_is_idempotent() -> None:
    handler = logging.StreamHandler(stream=io.StringIO())
    handler.setFormatter(obs_logging.JsonFormatter())
    try:
        quiet.apply_quiet_policy(handler)
        first = quiet.quiet_status()["root_bridge_handlers"]
        quiet.apply_quiet_policy(handler)
        quiet.apply_quiet_policy(handler)
        second = quiet.quiet_status()["root_bridge_handlers"]
        assert first == second == 1
    finally:
        quiet.remove_root_handlers()


def test_configure_logging_twice_keeps_a_single_bridge_handler(
    log_stream: io.StringIO,
) -> None:
    obs_logging.configure_logging()
    obs_logging.configure_logging()
    bridge = logging.getLogger(quiet.BRIDGE_LOGGER_NAME)
    assert len(bridge.handlers) == 1
    assert quiet.quiet_status()["root_bridge_handlers"] == 1


def test_bridge_propagation_stays_enabled_for_embedders(log_stream: io.StringIO) -> None:
    """caplog / embedding apps must still see bridge records."""

    assert logging.getLogger(quiet.BRIDGE_LOGGER_NAME).propagate is True


def test_root_handler_filters_the_bridge_tree() -> None:
    filt = quiet.BridgeTreeFilter()
    make = logging.LogRecord
    assert filt.filter(make("hermes_mcp_bridge", 20, __file__, 1, "m", None, None)) is False
    assert filt.filter(make("hermes_mcp_bridge.server", 20, __file__, 1, "m", None, None)) is False
    assert filt.filter(make("httpx", 20, __file__, 1, "m", None, None)) is True
    assert filt.filter(make("hermes_mcp_bridgex", 20, __file__, 1, "m", None, None)) is True


def test_capture_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(quiet.ENV_CAPTURE_THIRD_PARTY, "0")
    handler = logging.StreamHandler(stream=io.StringIO())
    handler.setFormatter(obs_logging.JsonFormatter())
    try:
        summary = quiet.apply_quiet_policy(handler)
        assert summary["third_party_captured"] is False
        assert summary["root_handler_installed"] is False
    finally:
        quiet.remove_root_handlers()


def test_observability_status_exposes_hygiene_without_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRIDGE_METRICS_TOKEN", SECRET)
    status = obs_logging.observability_status()
    assert status["hygiene"]["duplicate_suppression"] == "root_handler_filters_bridge_tree"
    assert SECRET not in json.dumps(status)


def test_warnings_are_captured_into_logging(log_stream: io.StringIO) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        warnings.warn("deprecated thing", DeprecationWarning, stacklevel=1)
    # py.warnings routes through root; the line must be JSON, not raw text.
    for line in log_stream.getvalue().splitlines():
        if line.strip():
            json.loads(line)


# --------------------------------------------------------------------------
# 2. Exporter: bind scope classification and unchanged authorization
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_exporter_env(monkeypatch: pytest.MonkeyPatch):
    for var in (exp.ENV_ENABLED, exp.ENV_HOST, exp.ENV_PORT, exp.ENV_ALLOW_REMOTE, exp.ENV_TOKEN):
        monkeypatch.delenv(var, raising=False)
    yield


@pytest.mark.parametrize(
    ("host", "scope"),
    [
        ("127.0.0.1", "loopback"),
        ("::1", "loopback"),
        ("localhost", "loopback"),
        ("172.17.0.1", "docker-gateway"),
        ("host.docker.internal", "docker-gateway"),
        ("0.0.0.0", "remote"),
        ("10.0.0.5", "remote"),
        ("192.168.1.10", "remote"),
    ],
)
def test_bind_scope_classification(host: str, scope: str) -> None:
    assert exp.bind_scope(host) == scope


def test_docker_gateway_is_not_loopback() -> None:
    assert exp.is_loopback("172.17.0.1") is False
    assert exp.is_docker_gateway("172.17.0.1") is True


def test_docker_gateway_still_requires_allow_remote() -> None:
    with pytest.raises(exp.MetricsExporterError):
        exp.validate_binding("172.17.0.1")


def test_docker_gateway_still_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(exp.ENV_ALLOW_REMOTE, "1")
    with pytest.raises(exp.MetricsExporterError):
        exp.validate_binding("172.17.0.1")
    monkeypatch.setenv(exp.ENV_TOKEN, "a-dedicated-random-token")
    exp.validate_binding("172.17.0.1")  # both conditions met


def test_loopback_never_needs_allow_remote() -> None:
    exp.validate_binding("127.0.0.1")


def test_exporter_status_reports_docker_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(exp.ENV_HOST, "172.17.0.1")
    monkeypatch.setenv(exp.ENV_TOKEN, SECRET)
    status = exp.exporter_status()
    assert status["bind_scope"] == "docker-gateway"
    assert status["auth_required"] is True
    assert status["remote_exposure_allowed"] is False
    assert SECRET not in json.dumps(status)


def test_auth_is_not_relaxed_for_docker_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured token is enforced on every request regardless of scope."""

    monkeypatch.setenv(exp.ENV_TOKEN, "the-token")
    assert exp._authorized(None) is False
    assert exp._authorized("Bearer wrong") is False
    assert exp._authorized("the-token") is False  # missing scheme
    assert exp._authorized("Bearer the-token") is True


# --------------------------------------------------------------------------
# 3. Deploy assets: parseable, credential-free, cardinality-safe
# --------------------------------------------------------------------------


def test_deploy_assets_exist() -> None:
    for path in (SCRAPE_SNIPPET, RULES_FILE, ALERTMANAGER_FILE, DEPLOY_DIR / "README.md"):
        assert path.is_file(), path


def test_scrape_snippet_defines_one_job_with_token_file() -> None:
    doc = yaml.safe_load(SCRAPE_SNIPPET.read_text(encoding="utf-8"))
    jobs = doc["scrape_configs"]
    assert len(jobs) == 1, "must not duplicate the existing Prometheus server"
    job = jobs[0]
    assert job["job_name"] == "hermes-mcp-bridge"
    assert job["authorization"]["credentials_file"]
    assert "credentials" not in job["authorization"]
    targets = job["static_configs"][0]["targets"]
    assert targets == ["172.17.0.1:9464"]


def test_scrape_snippet_has_no_global_prometheus_sections() -> None:
    doc = yaml.safe_load(SCRAPE_SNIPPET.read_text(encoding="utf-8"))
    assert set(doc) == {"scrape_configs"}


def test_rules_file_is_valid_and_annotated() -> None:
    doc = yaml.safe_load(RULES_FILE.read_text(encoding="utf-8"))
    groups = doc["groups"]
    assert groups
    names = []
    for group in groups:
        assert group["name"].startswith("hermes-bridge.")
        for rule in group["rules"]:
            assert rule["alert"] not in names
            names.append(rule["alert"])
            assert rule["expr"].strip()
            assert rule["labels"]["severity"] in {"info", "warning", "critical"}
            assert rule["labels"]["service"] == "hermes-mcp-bridge"
            assert rule["annotations"]["summary"]
            assert rule["annotations"]["runbook"]
    assert len(names) >= 8


def test_rules_only_reference_exported_metrics() -> None:
    from hermes_mcp_bridge.observability.metrics import get_metrics, get_registry

    get_registry().reset()
    get_metrics()  # (re)declare the catalogue
    exported = set(get_registry().render().splitlines())
    declared = {
        line.split()[2]
        for line in exported
        if line.startswith("# TYPE ")
    }
    text = RULES_FILE.read_text(encoding="utf-8")
    referenced = set(re.findall(r"\bbridge_[a-z_]+", text))
    unknown = set()
    for name in referenced:
        base = name
        for suffix in ("_bucket", "_sum", "_count"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
        if base not in declared:
            unknown.add(name)
    assert not unknown, f"rules reference undeclared metrics: {sorted(unknown)}"


def test_rules_use_no_forbidden_high_cardinality_labels() -> None:
    text = RULES_FILE.read_text(encoding="utf-8")
    for forbidden in FORBIDDEN_LABELS:
        assert not re.search(rf"\b{re.escape(forbidden)}\s*=", text), forbidden


def test_rules_labels_are_allow_listed() -> None:
    text = RULES_FILE.read_text(encoding="utf-8")
    used = set(re.findall(r"\{([a-z_]+)=", text))
    target_labels = {"job", "instance", "service", "env", "le"}
    assert not (used - set(ALLOWED_LABELS) - target_labels)


def test_alertmanager_example_stays_local_and_credential_free() -> None:
    text = ALERTMANAGER_FILE.read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    for receiver in doc["receivers"]:
        for webhook in receiver.get("webhook_configs", []):
            assert webhook["url"].startswith("http://127.0.0.1")
    assert "smtp_auth_password:" not in text
    assert "api_url: https://hooks." not in text
    assert doc["route"]["receiver"]
    assert doc["inhibit_rules"]


def test_no_deploy_asset_contains_a_literal_credential() -> None:
    pattern = re.compile(r"(?i)\bbearer[ \t]+[A-Za-z0-9._\-]{8,}")
    for path in DEPLOY_DIR.iterdir():
        if path.suffix in {".yml", ".yaml", ".md"}:
            assert not pattern.search(path.read_text(encoding="utf-8")), path


def test_no_public_port_is_published_by_the_deploy_assets() -> None:
    for path in DEPLOY_DIR.glob("*.yml"):
        text = path.read_text(encoding="utf-8")
        assert "0.0.0.0:" not in text, path


# --------------------------------------------------------------------------
# 4. Compose: log rotation, metrics still off by default
# --------------------------------------------------------------------------


def test_compose_configures_json_file_rotation() -> None:
    doc = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    logging_cfg = doc["services"]["hermes-mcp-bridge"]["logging"]
    assert logging_cfg["driver"] == "json-file"
    assert str(logging_cfg["options"]["max-size"]) == "10m"
    assert str(logging_cfg["options"]["max-file"]) == "5"


def test_compose_keeps_metrics_off_by_default() -> None:
    doc = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    env = doc["services"]["hermes-mcp-bridge"]["environment"]
    assert env["BRIDGE_METRICS_ENABLED"] == "${BRIDGE_METRICS_ENABLED:-}"
    assert env["BRIDGE_METRICS_HOST"] == "${BRIDGE_METRICS_HOST:-127.0.0.1}"
    assert "BRIDGE_METRICS_TOKEN" not in env
    assert "BRIDGE_METRICS_ALLOW_REMOTE" not in env


def test_compose_does_not_publish_the_metrics_port() -> None:
    doc = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    service = doc["services"]["hermes-mcp-bridge"]
    assert "ports" not in service


# --------------------------------------------------------------------------
# 5. Deprecated root tracing shim
# --------------------------------------------------------------------------


def test_root_tracing_module_is_a_reexport() -> None:
    from hermes_mcp_bridge import tracing as shim
    from hermes_mcp_bridge.observability import tracing as canonical

    for name in (
        "build_trace_metadata",
        "parse_traceparent",
        "sanitize_trace_context",
        "tracing_readiness",
        "format_traceparent",
    ):
        assert getattr(shim, name) is getattr(canonical, name), name


def test_root_tracing_import_warns_deprecation() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-W",
            "error::DeprecationWarning",
            "-c",
            "import hermes_mcp_bridge.tracing",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env={"PYTHONPATH": str(REPO_ROOT / "src"), "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode != 0
    assert "DeprecationWarning" in result.stderr


def test_shim_keeps_the_public_api_compatible() -> None:
    from hermes_mcp_bridge.tracing import (
        build_trace_metadata,
        parse_traceparent,
        sanitize_trace_context,
        tracing_readiness,
    )

    valid = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    assert parse_traceparent(valid)["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    meta = build_trace_metadata({"traceparent": valid}, upstream_supported=False)
    assert meta["effective_support"] == "bridge_only"
    assert meta["advisory"] is True
    assert meta["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    cleaned = sanitize_trace_context(
        {"traceparent": valid, "prompt": "secret", "leaseToken": "abc", "correlation_id": "c1"}
    )
    assert set(cleaned) == {"traceparent", "correlation_id"}
    assert "traceparent" in tracing_readiness()["allowed_context_fields"]


def test_shim_never_leaks_secret_context_fields() -> None:
    from hermes_mcp_bridge.tracing import build_trace_metadata

    meta = build_trace_metadata(
        {"prompt": "top secret", "lease_token": "abc", "secret": "x"},
        upstream_supported=True,
    )
    assert meta["context"] == {}
    assert "top secret" not in json.dumps(meta)


# --------------------------------------------------------------------------
# 6. Smoke script contract
# --------------------------------------------------------------------------


def test_observability_smoke_script_passes_offline_checks() -> None:
    result = subprocess.run(
        [sys.executable, str(SMOKE_SCRIPT), "--check-config", "--check-logging"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "scrape job ok" in result.stdout
    assert "rules ok" in result.stdout
    assert "logging ok" in result.stdout


def test_observability_smoke_script_detects_an_inline_credential(tmp_path: Path) -> None:
    """The credential detector is real, not decorative."""

    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        module = importlib.import_module("observability_smoke")
        bad = tmp_path / "bad.yml"
        bad.write_text("authorization:\n  credentials: AbCdEf0123456789xyz\n", encoding="utf-8")
        with pytest.raises(module.CheckFailure):
            module._assert_no_inline_secret(bad)
        good = tmp_path / "good.yml"
        good.write_text(
            "authorization:\n  credentials_file: /etc/prometheus/secrets/tok\n", encoding="utf-8"
        )
        module._assert_no_inline_secret(good)  # must not raise
    finally:
        sys.path.remove(str(REPO_ROOT / "scripts"))
