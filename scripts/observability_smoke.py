#!/usr/bin/env python3
"""Observability smoke check for hermes-mcp-bridge 0.9.0.

Read-only by design: it never writes to the state database, never mutates the
environment of a running bridge and never prints secret material.

Checks
------

``--check-config`` (default, offline)
    Validates the deploy assets: the Prometheus scrape snippet, the alerting
    rules and the Alertmanager example are parseable YAML, reference only
    allow-listed low-cardinality labels, carry no inline credential, and point
    at a bearer *token file* rather than a literal token.

``--check-logging`` (offline)
    Emits a handful of events through the real logging pipeline into a buffer
    and asserts every line is valid JSON, appears exactly once, and carries no
    secret.

``--probe URL`` (online, optional)
    Performs a single authenticated GET against a running exporter and reports
    the status code and the number of ``bridge_*`` series. The token is read
    from ``BRIDGE_METRICS_TOKEN`` or ``--token-file``; it is never echoed.

Exit code is 0 when every selected check passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_DIR = REPO_ROOT / "deploy" / "observability"
SCRAPE_SNIPPET = DEPLOY_DIR / "prometheus-scrape.snippet.yml"
RULES_FILE = DEPLOY_DIR / "hermes-bridge.rules.yml"
ALERTMANAGER_FILE = DEPLOY_DIR / "alertmanager.example.yml"

sys.path.insert(0, str(REPO_ROOT / "src"))

#: Substrings that must never appear in a committed deploy asset.
_SECRET_MARKERS = (
    re.compile(r"(?i)\bbearer[ \t]+[A-Za-z0-9._\-]{8,}"),
    # Literal credential bound to a secret-ish key. ``*_file`` keys and values
    # that are filesystem paths are the *correct* pattern and must not trip it.
    re.compile(
        r"(?i)(?<![\w-])(credentials|password|api_key|apikey|token|secret)"
        r"(?!_file)\s*:\s*[\"']?(?![/.])[A-Za-z0-9._\-]{12,}"
    ),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


class CheckFailure(Exception):
    """A smoke check found a real problem."""


def _load_yaml(path: Path) -> Any:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - dev extra provides PyYAML
        raise CheckFailure(f"PyYAML is required for --check-config ({exc})") from exc
    if not path.is_file():
        raise CheckFailure(f"missing deploy asset: {path.relative_to(REPO_ROOT)}")
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise CheckFailure(f"{path.name}: not parseable YAML: {type(exc).__name__}") from exc


def _assert_no_inline_secret(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for pattern in _SECRET_MARKERS:
        match = pattern.search(text)
        if match:
            raise CheckFailure(
                f"{path.name}: looks like an inline credential at offset {match.start()}"
            )


def check_scrape_snippet() -> list[str]:
    notes: list[str] = []
    _assert_no_inline_secret(SCRAPE_SNIPPET)
    doc = _load_yaml(SCRAPE_SNIPPET)
    jobs = (doc or {}).get("scrape_configs")
    if not isinstance(jobs, list) or not jobs:
        raise CheckFailure("scrape snippet has no scrape_configs entries")
    if len(jobs) != 1:
        raise CheckFailure("scrape snippet must define exactly one job (no Prometheus dup)")
    job = jobs[0]
    if job.get("job_name") != "hermes-mcp-bridge":
        raise CheckFailure("unexpected job_name in scrape snippet")
    auth = job.get("authorization") or {}
    if not auth.get("credentials_file"):
        raise CheckFailure("scrape job must use authorization.credentials_file")
    if auth.get("credentials"):
        raise CheckFailure("scrape job must not inline authorization.credentials")
    targets = [t for sc in job.get("static_configs", []) for t in sc.get("targets", [])]
    if "172.17.0.1:9464" not in targets:
        raise CheckFailure("scrape job does not target the docker gateway 172.17.0.1:9464")
    notes.append(f"scrape job ok: targets={targets}, bearer via file")
    return notes


def check_rules() -> list[str]:
    from hermes_mcp_bridge.observability.metrics import (
        ALLOWED_LABELS,
        FORBIDDEN_LABELS,
    )

    _assert_no_inline_secret(RULES_FILE)
    doc = _load_yaml(RULES_FILE)
    groups = (doc or {}).get("groups")
    if not isinstance(groups, list) or not groups:
        raise CheckFailure("rules file has no groups")
    alerts = 0
    text = RULES_FILE.read_text(encoding="utf-8")
    for group in groups:
        for rule in group.get("rules", []):
            if "alert" not in rule:
                raise CheckFailure(f"rule without an alert name in group {group.get('name')}")
            if not rule.get("expr"):
                raise CheckFailure(f"alert {rule['alert']} has no expr")
            annotations = rule.get("annotations") or {}
            if not annotations.get("runbook"):
                raise CheckFailure(f"alert {rule['alert']} has no runbook annotation")
            alerts += 1
    for forbidden in FORBIDDEN_LABELS:
        if re.search(rf"\b{re.escape(forbidden)}\s*=", text):
            raise CheckFailure(f"rules reference forbidden high-cardinality label: {forbidden}")
    used = set(re.findall(r"\{([a-z_]+)=", text)) - {"job", "instance", "service", "env", "le"}
    unknown = used - set(ALLOWED_LABELS)
    if unknown:
        raise CheckFailure(f"rules use non-allow-listed labels: {sorted(unknown)}")
    return [f"rules ok: {alerts} alerts, labels within the allow-list"]


def check_alertmanager() -> list[str]:
    _assert_no_inline_secret(ALERTMANAGER_FILE)
    doc = _load_yaml(ALERTMANAGER_FILE)
    receivers = (doc or {}).get("receivers")
    if not isinstance(receivers, list) or not receivers:
        raise CheckFailure("alertmanager example has no receivers")
    for receiver in receivers:
        for webhook in receiver.get("webhook_configs", []):
            url = str(webhook.get("url", ""))
            if not (url.startswith("http://127.0.0.1") or url.startswith("http://localhost")):
                raise CheckFailure(
                    f"receiver {receiver.get('name')} posts off-host by default: {url}"
                )
    route = (doc or {}).get("route") or {}
    if not route.get("receiver"):
        raise CheckFailure("alertmanager example has no default route receiver")
    return ["alertmanager example ok: loopback receiver, no credentials"]


def check_logging() -> list[str]:
    from hermes_mcp_bridge.observability import logging as obs_logging
    from hermes_mcp_bridge.observability import quiet

    buffer = io.StringIO()
    root = logging.getLogger()
    bridge = logging.getLogger("hermes_mcp_bridge")
    saved_root = list(root.handlers)
    saved_bridge = list(bridge.handlers)
    saved_level = root.level
    try:
        obs_logging.configure_logging(force=True)
        for handler in bridge.handlers:
            if isinstance(handler, logging.StreamHandler):
                handler.setStream(buffer)  # type: ignore[attr-defined]
        for handler in root.handlers:
            if getattr(handler, quiet.HANDLER_MARKER, False) and isinstance(
                handler, logging.StreamHandler
            ):
                handler.setStream(buffer)  # type: ignore[attr-defined]

        obs_logging.log_event("smoke.event", outcome="success", tool="hermes_health")
        obs_logging.log_event(
            "smoke.secret", outcome="success", authorization="Bearer smoke-token-value"
        )
        logging.getLogger("httpx").warning("third party line with token=smoke-token-value")

        lines = [line for line in buffer.getvalue().splitlines() if line.strip()]
    finally:
        root.handlers[:] = saved_root
        bridge.handlers[:] = saved_bridge
        root.setLevel(saved_level)
        obs_logging._configured = False

    if not lines:
        raise CheckFailure("no log lines captured")
    events: list[str] = []
    for line in lines:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CheckFailure(f"non-JSON line on the log stream: {exc}") from exc
        events.append(str(payload.get("event")))
        if "smoke-token-value" in line:
            raise CheckFailure(f"secret leaked into log event {payload.get('event')}")
    for name in ("smoke.event", "smoke.secret"):
        count = events.count(name)
        if count != 1:
            raise CheckFailure(f"event {name} emitted {count} times (expected exactly 1)")
    return [f"logging ok: {len(lines)} JSON lines, no duplicates, no secrets"]


def _read_token(token_file: str | None) -> str | None:
    if token_file:
        try:
            return Path(token_file).read_text(encoding="utf-8").strip() or None
        except OSError as exc:
            raise CheckFailure(f"cannot read token file: {type(exc).__name__}") from exc
    return os.environ.get("BRIDGE_METRICS_TOKEN", "").strip() or None


def probe(url: str, token_file: str | None) -> list[str]:
    token = _read_token(token_file)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read().decode("utf-8", "replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        raise CheckFailure(
            f"exporter returned HTTP {exc.code} (token configured: {bool(token)})"
        ) from exc
    except OSError as exc:
        raise CheckFailure(f"exporter unreachable: {type(exc).__name__}") from exc
    series = {
        line.split("{", 1)[0].split(" ", 1)[0]
        for line in body.splitlines()
        if line and not line.startswith("#")
    }
    bridge_series = sorted(s for s in series if s.startswith("bridge_"))
    if not bridge_series:
        raise CheckFailure("exporter responded but exposed no bridge_* series")
    return [f"probe ok: HTTP {status}, {len(bridge_series)} bridge_* series"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-config", action="store_true", help="validate deploy assets")
    parser.add_argument("--check-logging", action="store_true", help="validate the log pipeline")
    parser.add_argument("--probe", metavar="URL", help="GET a running /metrics endpoint")
    parser.add_argument("--token-file", help="file holding the exporter bearer token")
    args = parser.parse_args(argv)

    selected = args.check_config or args.check_logging or bool(args.probe)
    if not selected:
        args.check_config = True
        args.check_logging = True

    failures: list[str] = []
    notes: list[str] = []
    if args.check_config:
        for check in (check_scrape_snippet, check_rules, check_alertmanager):
            try:
                notes.extend(check())
            except CheckFailure as exc:
                failures.append(str(exc))
    if args.check_logging:
        try:
            notes.extend(check_logging())
        except CheckFailure as exc:
            failures.append(str(exc))
    if args.probe:
        try:
            notes.extend(probe(args.probe, args.token_file))
        except CheckFailure as exc:
            failures.append(str(exc))

    for note in notes:
        print(f"ok   {note}")
    for failure in failures:
        print(f"FAIL {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
