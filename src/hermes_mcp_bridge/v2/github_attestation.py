"""Sanitized GitHub provider attestation for the Phase 2 connected gate.

The attestation has **two** halves and neither one is allowed to invent the
other:

1. **Externally confirmed declaration** (:class:`ProviderAttestationInput`) — a
   sanitized, secret-free JSON document supplied by the operator via
   ``--provider-attestation``. It carries the exact permission map, the exact
   repository scopes, an explicit ``confirmation`` boolean and a restricted
   ``confirmation_source``. This is the *only* accepted source for facts the
   GitHub REST API cannot introspect. The collector never fabricates it, and the
   file path is never retained anywhere.
2. **Live read-only probes** (:func:`attest_provider`) — positive connectivity
   proofs executed with the real token against ``api.github.com``. They prove
   the token authenticates and can actually perform each required *read*. They
   never mutate anything and never probe for the *absence* of write.

Why the declaration is required
-------------------------------

GitHub's REST API (version ``2026-03-10``) offers no self-introspection endpoint
for an already-issued credential's permission set:

* a **fine-grained PAT** cannot enumerate its own selected repositories or its
  own permission map; GitHub additionally grants read access to public
  repositories regardless of selection, so a successful public-repo read proves
  nothing about the selection;
* an **installation token** carries its permissions/repositories in the
  *mint response* of ``POST /app/installations/{id}/access_tokens``; once
  issued, there is no equivalent self-introspection endpoint.
  ``GET /installation/repositories`` *is* valid for installation tokens and is
  used here to enumerate the installation repository set.

Consequently the exact permission map is **externally confirmed** (settings UI
or the mint response) and recorded as such, while authentication and read
capability are **machine-verified**. The evidence keeps the two apart.

Why the repository ``permissions`` block is *not* used
------------------------------------------------------

``GET /repos/{owner}/{repo}`` returns a ``permissions`` block describing the
*principal's computed role on the repository* — for an owner it reports
``admin: true`` even when the fine-grained PAT in use is restricted to read.
Treating it as token capability is unsound, so it is neither used to accept nor
to reject a provider here.

Input hardening
---------------

The declaration document is **schema-closed**: only the exact top-level keys in
:data:`ALLOWED_ATTESTATION_KEYS` are accepted and any other field is rejected
with ``ATTESTATION_UNEXPECTED_FIELD`` *before* any content is read, so the
sanitized input cannot carry arbitrary or secret-like data under creative names.
``confirmed_at`` must additionally be a timezone-aware ISO-8601 timestamp
(explicit offset or ``Z``) for the record to be auditable and reproducible.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from .github_direct import GITHUB_ACCEPT, GITHUB_API_BASE_URL, GITHUB_API_VERSION
from .github_registry import GITHUB_READ_CREDENTIAL_CAPABILITY
from .github_secret_provider import (
    AuthorizationStatus,
    FileGitHubAuthorizationProvider,
    GitHubProviderType,
)

#: Exact permission map required by the Phase 2 gate.
REQUIRED_PERMISSIONS: dict[str, str] = {
    "checks": "read",
    "issues": "read",
    "metadata": "read",
    "pull_requests": "read",
}

#: Versioned schema identifier for the sanitized external attestation input.
ATTESTATION_INPUT_SCHEMA = "hermes-v2-phase2-provider-attestation/1"

#: Confirmation sources accepted per provider type. Nothing else is allowed.
ALLOWED_CONFIRMATION_SOURCES: dict[str, frozenset[str]] = {
    GitHubProviderType.FINE_GRAINED_TOKEN.value: frozenset({"github_settings_ui"}),
    GitHubProviderType.GITHUB_APP.value: frozenset(
        {"github_app_settings_ui", "installation_token_mint_response"}
    ),
}

_WILDCARD_CHARS = ("*", "?", "[", "]")
_USER_AGENT = "hermes-mcp-bridge-v2-attestation"
#: Exact, closed set of top-level keys the sanitized declaration may carry.
#: The document is schema-closed: any other key — however innocuously named —
#: is rejected before any content is processed, so the file cannot be used to
#: smuggle arbitrary or secret-like payloads under creative field names.
ALLOWED_ATTESTATION_KEYS = frozenset(
    {
        "schema",
        "provider_type",
        "permissions",
        "unexpected_permissions",
        "repository_scopes",
        "confirmation",
        "confirmation_source",
        "confirmed_at",
    }
)
#: Retained for defence in depth and for a more precise code when an obviously
#: secret-like key is used; the closed key set above already rejects them.
_SECRET_LIKE_KEYS = frozenset(
    {
        "token",
        "secret",
        "credential",
        "material",
        "authorization",
        "password",
        "private_key",
        "path",
        "secret_path",
        "file",
    }
)


class AttestationError(RuntimeError):
    """Fail-closed attestation failure. Carries a stable code only."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)

    def __str__(self) -> str:
        return f"provider attestation failed: {self.code}"


@dataclass(frozen=True, slots=True)
class ProviderAttestationInput:
    """Sanitized, secret-free external attestation supplied by the operator."""

    provider_type: str
    permissions: dict[str, str]
    unexpected_permissions: list[str]
    repository_scopes: list[str]
    confirmation: bool
    confirmation_source: str
    confirmed_at: str

    def notes(self) -> dict[str, Any]:
        """Non-secret provenance for the evidence. Never contains a path."""
        return {
            "schema": ATTESTATION_INPUT_SCHEMA,
            "confirmation": self.confirmation,
            "confirmation_source": self.confirmation_source,
            "confirmed_at": self.confirmed_at,
        }


def _require_str(payload: dict[str, Any], key: str, code: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AttestationError(code)
    return value.strip()


def load_attestation_input(path: str | Path) -> ProviderAttestationInput:
    """Parse and validate the external attestation document.

    The path is used to read the file and is then discarded; it never appears in
    the returned object, in any error, or in the evidence.
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise AttestationError("ATTESTATION_INPUT_UNREADABLE") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AttestationError("ATTESTATION_INPUT_MALFORMED") from exc
    if not isinstance(payload, dict):
        raise AttestationError("ATTESTATION_INPUT_MALFORMED")

    # ---- schema-closed key validation, before any content is processed -----
    # The declaration may only carry the exact top-level keys of the schema.
    # Obviously secret-like names keep their more specific code; anything else
    # outside the closed set fails with ATTESTATION_UNEXPECTED_FIELD. Nothing
    # arbitrary can therefore ride along in the sanitized input.
    for key in payload:
        name = str(key).strip().lower()
        if name in _SECRET_LIKE_KEYS:
            raise AttestationError("ATTESTATION_INPUT_SECRET_LIKE_FIELD")
        if str(key) not in ALLOWED_ATTESTATION_KEYS:
            raise AttestationError("ATTESTATION_UNEXPECTED_FIELD")

    schema = _require_str(payload, "schema", "ATTESTATION_SCHEMA_MISSING")
    if schema != ATTESTATION_INPUT_SCHEMA:
        raise AttestationError("ATTESTATION_SCHEMA_UNSUPPORTED")

    provider_type = _require_str(payload, "provider_type", "ATTESTATION_PROVIDER_TYPE_MISSING")
    if provider_type not in ALLOWED_CONFIRMATION_SOURCES:
        raise AttestationError("ATTESTATION_PROVIDER_TYPE_INVALID")

    permissions_raw = payload.get("permissions")
    if not isinstance(permissions_raw, dict) or not permissions_raw:
        raise AttestationError("ATTESTATION_PERMISSIONS_MISSING")
    permissions = {str(key): str(value) for key, value in permissions_raw.items()}

    unexpected_raw = payload.get("unexpected_permissions")
    if not isinstance(unexpected_raw, list):
        raise AttestationError("ATTESTATION_UNEXPECTED_PERMISSIONS_MISSING")
    unexpected = [str(item) for item in unexpected_raw]

    scopes_raw = payload.get("repository_scopes")
    if not isinstance(scopes_raw, list) or not scopes_raw:
        raise AttestationError("ATTESTATION_REPOSITORY_SCOPES_MISSING")
    scopes: list[str] = []
    for item in scopes_raw:
        if not isinstance(item, str) or item.count("/") != 1 or not item.strip():
            raise AttestationError("ATTESTATION_REPOSITORY_SCOPE_INVALID")
        if any(char in item for char in _WILDCARD_CHARS):
            raise AttestationError("ATTESTATION_WILDCARD_REPOSITORY_SCOPE")
        scopes.append(item.strip().lower())

    if payload.get("confirmation") is not True:
        raise AttestationError("ATTESTATION_NOT_CONFIRMED")

    source = _require_str(payload, "confirmation_source", "ATTESTATION_SOURCE_MISSING")
    if source not in ALLOWED_CONFIRMATION_SOURCES[provider_type]:
        raise AttestationError("ATTESTATION_SOURCE_NOT_ALLOWED")

    confirmed_at = _require_str(payload, "confirmed_at", "ATTESTATION_CONFIRMED_AT_MISSING")
    # ISO-8601 must be timezone-aware (explicit offset or ``Z``). A naive
    # timestamp is not auditable/reproducible across hosts, so it fails closed.
    # No maximum age window is enforced at this phase.
    try:
        parsed = datetime.fromisoformat(confirmed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AttestationError("ATTESTATION_CONFIRMED_AT_INVALID") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AttestationError("ATTESTATION_CONFIRMED_AT_NOT_TIMEZONE_AWARE")

    return ProviderAttestationInput(
        provider_type=provider_type,
        permissions=permissions,
        unexpected_permissions=unexpected,
        repository_scopes=sorted(scopes),
        confirmation=True,
        confirmation_source=source,
        confirmed_at=confirmed_at,
    )


@dataclass(frozen=True, slots=True)
class ProviderAttestation:
    """Sanitized provider attestation; safe to embed verbatim in evidence."""

    provider_type: str
    authenticated: bool
    least_privilege: bool
    broad_pat: bool
    permissions: dict[str, str]
    unexpected_permissions: list[str]
    repository_scopes: list[str]
    permissions_source: str
    probes: dict[str, Any]

    def evidence(self) -> dict[str, Any]:
        """Return exactly the ``github_provider`` block the validator expects."""
        return {
            "authenticated": self.authenticated,
            "base_url": GITHUB_API_BASE_URL,
            "broad_pat": self.broad_pat,
            "credential_capability": GITHUB_READ_CREDENTIAL_CAPABILITY,
            "github_api_version": GITHUB_API_VERSION,
            "least_privilege": self.least_privilege,
            "permissions": dict(self.permissions),
            "provider_type": self.provider_type,
            "repository_scopes": list(self.repository_scopes),
            "unexpected_permissions": list(self.unexpected_permissions),
        }

    def attestation_notes(self) -> dict[str, Any]:
        """Non-secret provenance kept beside the evidence.

        ``machine_verified`` lists what the live probes actually proved;
        ``externally_confirmed`` lists what only the operator declaration
        covers. No path and no secret appears here.
        """
        return {
            "permissions_source": self.permissions_source,
            "machine_verified": [
                "authentication",
                "repository_metadata_read",
                "pull_requests_read",
                "issues_read",
                "check_runs_read",
                *(
                    ["installation_repository_set"]
                    if "installation_repository_count" in self.probes
                    else []
                ),
            ],
            "externally_confirmed": [
                "exact_permission_map",
                "exact_repository_selection",
            ],
            "probes": dict(self.probes),
        }


def _headers(header_value: str) -> dict[str, str]:
    return {
        "Accept": GITHUB_ACCEPT,
        "Authorization": header_value,
        "User-Agent": _USER_AGENT,
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }


def _count(payload: Any) -> int:
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        value = payload.get("total_count")
        return int(value) if isinstance(value, int) else 0
    return 0


def attest_provider(
    provider: FileGitHubAuthorizationProvider,
    *,
    repositories: list[str],
    declaration: ProviderAttestationInput,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 20.0,
) -> ProviderAttestation:
    """Cross-check the declaration and run live read-only probes.

    Every mismatch between the declaration and the CLI/provider configuration,
    and every failed probe, raises :class:`AttestationError` (fail-closed) before
    any evidence can be produced.
    """
    if not repositories:
        raise AttestationError("NO_REPOSITORY_SCOPE")
    normalized: list[str] = []
    for repository in repositories:
        if any(token in repository for token in _WILDCARD_CHARS):
            raise AttestationError("WILDCARD_REPOSITORY_SCOPE")
        if repository.count("/") != 1:
            raise AttestationError("INVALID_REPOSITORY_SCOPE")
        normalized.append(repository.strip().lower())

    # ---- declaration cross-check (before any network call) -----------------
    if declaration.provider_type != provider.provider_type.value:
        raise AttestationError("ATTESTATION_PROVIDER_TYPE_MISMATCH")
    if declaration.repository_scopes != sorted(set(normalized)):
        raise AttestationError("ATTESTATION_REPOSITORY_SCOPE_MISMATCH")
    if declaration.permissions != REQUIRED_PERMISSIONS:
        raise AttestationError("ATTESTATION_PERMISSIONS_NOT_EXACT")
    if declaration.unexpected_permissions:
        raise AttestationError("ATTESTATION_UNEXPECTED_PERMISSIONS")
    if declaration.confirmation is not True:
        raise AttestationError("ATTESTATION_NOT_CONFIRMED")

    status = provider.probe()
    if status is AuthorizationStatus.CLASSIC_PAT_REJECTED:
        raise AttestationError("CLASSIC_PAT_REJECTED")
    if status is not AuthorizationStatus.READY:
        raise AttestationError(f"CREDENTIAL_{status.value}")

    material = provider.resolve(GITHUB_READ_CREDENTIAL_CAPABILITY, repositories[0])
    if material is None:
        raise AttestationError(f"CREDENTIAL_{provider.last_status.value}")
    headers = _headers(material.header_value())
    del material

    probes: dict[str, Any] = {}
    with httpx.Client(
        base_url=GITHUB_API_BASE_URL,
        transport=transport,
        follow_redirects=False,
        timeout=httpx.Timeout(timeout),
        trust_env=False,
    ) as client:
        rate = client.get("/rate_limit", headers=headers)
        if rate.status_code == 401:
            raise AttestationError("AUTHENTICATION_FAILED")
        if rate.status_code != 200:
            raise AttestationError(f"RATE_LIMIT_PROBE_{rate.status_code}")
        probes["auth_probe_status"] = rate.status_code

        oauth_scopes = rate.headers.get("x-oauth-scopes")
        broad_pat = bool(oauth_scopes is not None and oauth_scopes.strip())
        probes["oauth_scopes_header_present"] = oauth_scopes is not None
        if broad_pat:
            raise AttestationError("CLASSIC_PAT_DETECTED")

        # ---- live POSITIVE read probes, one set per declared repository ----
        read_probes: dict[str, Any] = {}
        for repository in normalized:
            owner, repo = repository.split("/", 1)

            meta = client.get(f"/repos/{owner}/{repo}", headers=headers)
            if meta.status_code != 200:
                raise AttestationError(f"REPOSITORY_PROBE_{meta.status_code}")
            payload = meta.json()
            if not isinstance(payload, dict):
                raise AttestationError("REPOSITORY_PROBE_INVALID_SHAPE")
            default_branch = payload.get("default_branch")
            if not isinstance(default_branch, str) or not default_branch.strip():
                raise AttestationError("REPOSITORY_DEFAULT_BRANCH_MISSING")

            pulls = client.get(
                f"/repos/{owner}/{repo}/pulls",
                params={"state": "all", "per_page": 1},
                headers=headers,
            )
            if pulls.status_code != 200:
                raise AttestationError(f"PULLS_READ_PROBE_{pulls.status_code}")

            issues = client.get(
                f"/repos/{owner}/{repo}/issues",
                params={"state": "all", "per_page": 1},
                headers=headers,
            )
            if issues.status_code != 200:
                raise AttestationError(f"ISSUES_READ_PROBE_{issues.status_code}")

            checks = client.get(
                f"/repos/{owner}/{repo}/commits/{default_branch}/check-runs",
                params={"per_page": 1},
                headers=headers,
            )
            if checks.status_code != 200:
                raise AttestationError(f"CHECK_RUNS_READ_PROBE_{checks.status_code}")

            read_probes[repository] = {
                "metadata_status": meta.status_code,
                "pulls_status": pulls.status_code,
                "pulls_sample_count": _count(pulls.json()),
                "issues_status": issues.status_code,
                "issues_sample_count": _count(issues.json()),
                "check_runs_status": checks.status_code,
                "check_runs_total_count": _count(checks.json()),
            }
        probes["repository_probe_count"] = len(normalized)
        probes["repository_read_probes"] = read_probes

        if provider.provider_type is GitHubProviderType.GITHUB_APP:
            installation = client.get("/installation/repositories", headers=headers)
            if installation.status_code != 200:
                raise AttestationError(f"INSTALLATION_PROBE_{installation.status_code}")
            body = installation.json()
            items = body.get("repositories") if isinstance(body, dict) else None
            observed = sorted(
                str(item.get("full_name", "")).lower()
                for item in (items or [])
                if isinstance(item, dict)
            )
            probes["installation_repository_count"] = len(observed)
            if observed != sorted(set(normalized)):
                raise AttestationError("INSTALLATION_SCOPE_MISMATCH")
            permissions_source = (
                "installation_token_mint_response"
                if declaration.confirmation_source == "installation_token_mint_response"
                else "operator_declared_ui_confirmed"
            )
        else:
            # Fine-grained tokens expose no self-introspection of their own
            # selected repositories or permission map, and GitHub grants
            # read-only access to public repositories independently of the
            # selection. The exact selected scope is therefore confirmed by the
            # external attestation and *enforced* at runtime by
            # ``GitHubRepositoryScope`` in the executor.
            permissions_source = "operator_declared_ui_confirmed"
            probes["fine_grained_self_enumeration_available"] = False

    return ProviderAttestation(
        provider_type=provider.provider_type.value,
        authenticated=True,
        least_privilege=True,
        broad_pat=False,
        permissions=dict(declaration.permissions),
        unexpected_permissions=list(declaration.unexpected_permissions),
        repository_scopes=sorted(set(normalized)),
        permissions_source=permissions_source,
        probes=probes,
    )


__all__ = [
    "ALLOWED_ATTESTATION_KEYS",
    "ALLOWED_CONFIRMATION_SOURCES",
    "ATTESTATION_INPUT_SCHEMA",
    "REQUIRED_PERMISSIONS",
    "AttestationError",
    "ProviderAttestation",
    "ProviderAttestationInput",
    "attest_provider",
    "load_attestation_input",
]
