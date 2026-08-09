#!/usr/bin/env python3
"""Phase 7 connected acceptance run — real bounded reads, sanitized evidence.

Executes the accepted Phase 7 providers against their real authorized targets
through the *same* :class:`ProviderGateway` pipeline used by the hermetic suite,
then writes a sanitized evidence document consumed by
``validate_v2_phase7_integration_gate.py --connected-evidence``.

Guarantees:

* credential material is read from the host credential files, converted into an
  ``apply`` closure and never printed, stored, hashed or serialized;
* every request is a **read**; no mutation is attempted, so the mutation
  residual is structurally 0;
* token accounting is read from the real Hermes accounting store
  (``session_model_usage``), never estimated — a DIRECT provider read performs
  no Hermes LLM call, so the expected delta is exactly 0;
* an out-of-scope request is issued deliberately to prove zero provider calls
  and zero credential resolutions on the denial path, against the live registry.

Usage::

    python scripts/v2_phase7_connected_acceptance.py \
        --json-out docs/v2/evidence/phase7-connected-acceptance.json
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sqlite3
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from hermes_mcp_bridge.v2.provider_audit import (  # noqa: E402
    AuditKind,
    IntegrationAuditLedger,
    MemoryAuditSink,
    completeness,
)
from hermes_mcp_bridge.v2.provider_contract import ProviderReason, audit_safe  # noqa: E402
from hermes_mcp_bridge.v2.provider_credentials import (  # noqa: E402
    CredentialRecord,
    ProviderCredentialBroker,
)
from hermes_mcp_bridge.v2.provider_gateway import (  # noqa: E402
    PolicyPort,
    ProviderCallResult,
    ProviderDenied,
    ProviderGateway,
    ProviderRequest,
    ScopeResolver,
)
from hermes_mcp_bridge.v2.provider_health import http_probe, probe_manifest  # noqa: E402
from hermes_mcp_bridge.v2.provider_manifests import (  # noqa: E402
    PROVIDER_ALLOW_LIST,
    github_manifest,
    jira_manifest,
)
from hermes_mcp_bridge.v2.provider_registry import build_registry  # noqa: E402

GITHUB_TARGET = "pestoura/hermes-mcp-bridge"
HERMES_STATE_DB = Path.home() / ".hermes" / "state.db"

PROBE_PATHS = {
    "github.repo_read": f"/repos/{GITHUB_TARGET}",
    "github.pr_read": f"/repos/{GITHUB_TARGET}/pulls?per_page=1",
    "github.checks_read": "/rate_limit",
    "jira.issue_read": "/rest/api/3/myself",
    "jira.project_read": "/rest/api/3/project/search?maxResults=1",
}


def _github_token() -> str | None:
    """Read the gh CLI token without printing or persisting it."""
    hosts = Path.home() / ".config" / "gh" / "hosts.yml"
    if not hosts.is_file():
        return os.environ.get("GITHUB_TOKEN")
    for line in hosts.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("oauth_token:"):
            return stripped.split(":", 1)[1].strip()
    return os.environ.get("GITHUB_TOKEN")


def _jira_credential() -> tuple[str, str] | None:
    path = Path.home() / ".hermes" / "secrets" / "jira-api-token.json"
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    site = str(payload.get("site", "")).strip()
    host = site.split("//")[-1].strip("/")
    basic = base64.b64encode(
        f"{payload['email']}:{payload['token']}".encode()
    ).decode()
    return host, basic


def _usage_snapshot() -> tuple[dict[str, int], int]:
    """Per-session token totals from the real Hermes accounting store.

    Returned as ``(per_session_totals, grand_total)``. Attribution matters: the
    global total moves whenever *any* Hermes session is active, including the
    operator session that launched this script. A DIRECT provider read performs
    no Hermes LLM call, so the correct assertion is that the run created **no
    session of its own** and increased **no session it owns** — not that an
    unrelated concurrent session stood still.
    """
    if not HERMES_STATE_DB.is_file():
        return {}, 0
    try:
        connection = sqlite3.connect(f"file:{HERMES_STATE_DB}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}, 0
    try:
        rows = connection.execute(
            "SELECT session_id, COALESCE(SUM(input_tokens + output_tokens), 0) "
            "FROM session_model_usage GROUP BY session_id"
        ).fetchall()
    except sqlite3.Error:
        return {}, 0
    finally:
        connection.close()
    totals = {str(session): int(value) for session, value in rows}
    return totals, sum(totals.values())


def _http_get(host: str, path: str, headers: dict[str, str], timeout: float = 20.0):
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args: object, **kwargs: object) -> None:
            return None

    opener = urllib.request.build_opener(
        _NoRedirect(),
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )
    request = urllib.request.Request(f"https://{host}{path}", headers=headers, method="GET")
    with opener.open(request, timeout=timeout) as response:
        body = response.read(262_144)
        return response.status, body


def _adapter_for(host: str):
    def _call(request: ProviderRequest, headers: dict[str, str], deadline_ms: int):
        path = PROBE_PATHS[request.capability_id]
        try:
            status, body = _http_get(host, path, {**headers, "Accept": "application/json"},
                                     timeout=max(1.0, deadline_ms / 1000))
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise ProviderDenied(ProviderReason.E_PROVIDER_AUTH) from None
            if exc.code == 429:
                raise ProviderDenied(ProviderReason.E_PROVIDER_RATE_LIMIT) from None
            raise ProviderDenied(ProviderReason.E_PROVIDER_FAULT) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ProviderDenied(ProviderReason.E_PROVIDER_FAULT) from None
        if status != 200:
            raise ProviderDenied(ProviderReason.E_PROVIDER_SHAPE)
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ProviderDenied(ProviderReason.E_PROVIDER_SHAPE) from None
        # Field allow-list: the connected run records shape facts only, never
        # provider content, so the evidence cannot carry a body.
        payload = {
            "shape": "object" if isinstance(parsed, dict) else "array",
            "field_count": len(parsed) if isinstance(parsed, dict | list) else 0,
        }
        return ProviderCallResult(payload=payload, byte_count=len(body), provider_calls=1)

    return _call


def _run_provider(manifest, host: str, header_apply, targets: tuple[str, ...]) -> dict[str, Any]:
    registry = build_registry(
        allow_list=PROVIDER_ALLOW_LIST,
        tool_ids=[capability.tool_id for capability in manifest.capabilities],
        manifests=[manifest],
    )
    reports = probe_manifest(
        manifest,
        execute=http_probe,
        paths=PROBE_PATHS,
        headers_for=lambda capability_id: {
            **header_apply({}),
            "Accept": "application/json",
            "User-Agent": "hermes-mcp-bridge-v2-phase7",
        },
    )
    applied = registry.promote_configured(reports)

    broker = ProviderCredentialBroker({manifest.provider_id: manifest.credential_domain})
    for capability in manifest.credential_domain.capability_ids:
        broker.register(
            CredentialRecord(
                provider_id=manifest.provider_id,
                credential_capability_id=capability,
                ready=True,
                apply=header_apply,
            )
        )
    scopes = ScopeResolver()
    for capability in manifest.capabilities:
        scopes.allow(capability.capability_id, targets)
    sink = MemoryAuditSink()
    gateway = ProviderGateway(
        registry=registry,
        policy=PolicyPort(
            {capability.capability_id: "ALLOW" for capability in manifest.capabilities}
        ),
        scopes=scopes,
        broker=broker,
        audit=IntegrationAuditLedger(sink),
        adapters={manifest.provider_id: _adapter_for(host)},
    )

    outcomes: list[dict[str, Any]] = []
    latencies: list[int] = []
    index = 0
    for capability in manifest.capabilities:
        if not registry.is_usable(capability.capability_id):
            outcomes.append(
                {
                    "capability_id": capability.capability_id,
                    "outcome": "skipped_not_usable",
                    "reason_code": ProviderReason.E_CAP_NOT_READY.value,
                }
            )
            continue
        started = time.monotonic_ns()
        result = gateway.invoke(
            ProviderRequest(
                request_id=f"{manifest.provider_id}-connected-{index}",
                principal_ref="connected-acceptance",
                provider_id=manifest.provider_id,
                capability_id=capability.capability_id,
                target_scope_ref=targets[0],
            )
        )
        latencies.append((time.monotonic_ns() - started) // 1_000_000)
        index += 1
        outcomes.append(
            {
                "capability_id": capability.capability_id,
                "outcome": result.outcome.value,
                "reason_code": result.reason_code.value,
                "provider_calls": result.provider_calls,
                "byte_count": result.byte_count,
            }
        )

    # Deliberate denial path against the live registry.
    calls_before = gateway.provider_calls
    resolutions_before = gateway.credential_resolutions
    denied = gateway.invoke(
        ProviderRequest(
            request_id=f"{manifest.provider_id}-connected-denial",
            principal_ref="connected-acceptance",
            provider_id=manifest.provider_id,
            capability_id=manifest.capabilities[0].capability_id,
            target_scope_ref="OUT-OF-SCOPE-TARGET",
        )
    )
    terminal = [record for record in sink.records if record["kind"] == AuditKind.TERMINAL.value]
    executed = len([entry for entry in outcomes if entry["outcome"] != "skipped_not_usable"]) + 1

    return {
        "capability_snapshot_hash": registry.capability_snapshot_hash(),
        "write_capability_digest": registry.write_capability_digest(),
        "manifest_digest": manifest.manifest_digest(),
        "state": max(
            (report.state.value for report in applied),
            key=lambda value: ["DENIED", "UNAVAILABLE", "DEGRADED", "CONFIGURED",
                               "AVAILABLE", "HEALTHY", "READY"].index(value),
        ),
        "readiness": {report.capability_id: report.state.value for report in applied},
        "outcomes": outcomes,
        "denial": {
            "reason_code": denied.reason_code.value,
            "provider_calls_delta": gateway.provider_calls - calls_before,
            "credential_resolutions_delta": gateway.credential_resolutions - resolutions_before,
        },
        "provider_api_calls": gateway.provider_calls,
        "credential_resolutions": gateway.credential_resolutions,
        "latency_ms_max": max(latencies) if latencies else 0,
        "audit_terminal_records": len(terminal),
        "audit_completeness": completeness(
            terminal_records=len(terminal), terminal_outcomes=executed
        ),
        "audit_redaction_findings": len(audit_safe(list(sink.records))),
        "unknown_outcomes": len(gateway.unknown_outcomes),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-out", type=Path, required=True)
    args = parser.parse_args()

    sessions_before, total_before = _usage_snapshot()
    providers: dict[str, Any] = {}
    blockers: list[str] = []

    token = _github_token()
    if token:
        providers["github"] = _run_provider(
            github_manifest(),
            "api.github.com",
            lambda headers: {**headers, "Authorization": f"Bearer {token}"},
            (GITHUB_TARGET,),
        )
    else:
        blockers.append("github: no credential available")

    jira = _jira_credential()
    if jira:
        host, basic = jira
        providers["jira"] = _run_provider(
            jira_manifest(host=host),
            host,
            lambda headers: {**headers, "Authorization": f"Basic {basic}"},
            ("PPE",),
        )
    else:
        blockers.append("jira: no credential available")

    sessions_after, total_after = _usage_snapshot()
    # Sessions created *by this run*: a DIRECT provider read must create none.
    new_sessions = sorted(set(sessions_after) - set(sessions_before))
    attributed = sum(sessions_after[session] for session in new_sessions)
    concurrent_delta = (total_after - total_before) - attributed
    evidence = {
        "bridge_version": "1.0.0",
        "schema_version": "0.6.1",
        "blockers": blockers,
        "direct_tokens": attributed,
        "direct_sessions_created": len(new_sessions),
        "concurrent_operator_session_delta": concurrent_delta,
        "mutation_residual": 0,
        "providers": providers,
        "token_accounting_source": (
            "hermes state store session_model_usage, attributed per session_id"
        ),
    }
    findings = audit_safe(evidence)
    if findings:
        print(json.dumps({"error": "redaction", "findings": len(findings)}))
        return 2
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
