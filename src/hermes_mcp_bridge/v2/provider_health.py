"""Phase 7 health probes: bounded, read-only, demote-only.

> **V2 · PHASE 7 · runtime, disabled by default behind ``PROVIDER_FEATURE_ENABLED``**

A probe proves *scope*, never effect: a ``DIRECT_WRITE`` capability is probed by
a read that demonstrates the credential carries the scope, never by a trial
mutation. A probe is bounded by a deadline and a byte cap, follows no redirect,
inherits no environment proxy, and returns only an enumerated state plus a
closed reason code — no body, no header, no host detail beyond the allow-list
membership already declared in the manifest.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .enums import CapabilityState
from .provider_contract import CapabilityDeclaration, ProviderManifest, ProviderReason
from .provider_registry import HealthReport

PROBE_MAX_BYTES = 65_536
PROBE_TIMEOUT_S = 10.0


@dataclass(frozen=True, slots=True)
class ProbeRequest:
    """One bounded read used to classify a capability."""

    capability_id: str
    host: str
    path: str
    headers: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    status_code: int
    byte_count: int
    reason: ProviderReason = ProviderReason.OK


def _classify(outcome: ProbeOutcome, declaration: CapabilityDeclaration) -> HealthReport:
    if outcome.reason is not ProviderReason.OK:
        state = CapabilityState.UNAVAILABLE
        if outcome.reason in (ProviderReason.E_PROVIDER_AUTH, ProviderReason.E_CRED_REVOKED):
            state = CapabilityState.DENIED
        return HealthReport(
            capability_id=declaration.capability_id, state=state, reason=outcome.reason
        )
    if outcome.status_code == 200:
        return HealthReport(
            capability_id=declaration.capability_id,
            state=CapabilityState.READY,
            reason=ProviderReason.OK,
        )
    if outcome.status_code in (401, 403):
        return HealthReport(
            capability_id=declaration.capability_id,
            state=CapabilityState.DENIED,
            reason=ProviderReason.E_PROVIDER_AUTH,
        )
    if outcome.status_code == 429:
        return HealthReport(
            capability_id=declaration.capability_id,
            state=CapabilityState.DEGRADED,
            reason=ProviderReason.E_PROVIDER_RATE_LIMIT,
        )
    # Inconclusive: reads may serve DEGRADED, writes fail closed (the registry
    # enforces the write demotion; the probe only reports what it saw).
    state = (
        CapabilityState.UNAVAILABLE if declaration.is_write else CapabilityState.DEGRADED
    )
    return HealthReport(
        capability_id=declaration.capability_id,
        state=state,
        reason=ProviderReason.E_CAP_PROBE_INCONCLUSIVE,
    )


def http_probe(request: ProbeRequest, *, timeout_s: float = PROBE_TIMEOUT_S) -> ProbeOutcome:
    """Execute one bounded HTTPS GET. No redirects, no proxy inheritance."""

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args: object, **kwargs: object) -> None:
            return None

    url = f"https://{request.host}{request.path}"
    opener = urllib.request.build_opener(
        _NoRedirect(),
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )
    req = urllib.request.Request(url, headers=dict(request.headers), method="GET")
    try:
        with opener.open(req, timeout=timeout_s) as response:
            body = response.read(PROBE_MAX_BYTES + 1)
            if len(body) > PROBE_MAX_BYTES:
                return ProbeOutcome(
                    status_code=response.status,
                    byte_count=len(body),
                    reason=ProviderReason.E_PROVIDER_RESULT_TOO_LARGE,
                )
            try:
                json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return ProbeOutcome(
                    status_code=response.status,
                    byte_count=len(body),
                    reason=ProviderReason.E_PROVIDER_SHAPE,
                )
            return ProbeOutcome(status_code=response.status, byte_count=len(body))
    except urllib.error.HTTPError as exc:
        return ProbeOutcome(status_code=int(exc.code), byte_count=0)
    except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError):
        # Fail closed before any conclusion is drawn; no exception detail leaks.
        return ProbeOutcome(
            status_code=0, byte_count=0, reason=ProviderReason.E_PROVIDER_FAULT
        )


def probe_manifest(
    manifest: ProviderManifest,
    *,
    execute: Callable[[ProbeRequest], ProbeOutcome],
    paths: Mapping[str, str],
    headers_for: Callable[[str], Mapping[str, str]],
) -> tuple[HealthReport, ...]:
    """Probe every capability of ``manifest`` and classify readiness."""
    reports: list[HealthReport] = []
    for declaration in manifest.capabilities:
        path = paths.get(declaration.capability_id)
        if path is None:
            reports.append(
                HealthReport(
                    capability_id=declaration.capability_id,
                    state=CapabilityState.UNAVAILABLE,
                    reason=ProviderReason.E_CAP_PROBE_INCONCLUSIVE,
                )
            )
            continue
        host = declaration.egress_hosts[0]
        outcome = execute(
            ProbeRequest(
                capability_id=declaration.capability_id,
                host=host,
                path=path,
                headers=headers_for(declaration.credential_capability_id),
            )
        )
        reports.append(_classify(outcome, declaration))
    return tuple(reports)


__all__ = [
    "PROBE_MAX_BYTES",
    "PROBE_TIMEOUT_S",
    "ProbeOutcome",
    "ProbeRequest",
    "http_probe",
    "probe_manifest",
]
