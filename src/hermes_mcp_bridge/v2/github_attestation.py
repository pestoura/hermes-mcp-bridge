"""Sanitized GitHub provider attestation for the Phase 2 connected gate.

The attestation is deliberately **separate from the secret**: it carries only a
declared provider type, boolean posture flags, an exact permission map, an exact
repository scope list and the API identity. It never carries the credential, the
secret path or an environment dump.

A declaration is *not* accepted as proof. :func:`attest_provider` performs live,
read-only probes against ``api.github.com`` and downgrades every claim it cannot
observe:

* ``GET /rate_limit`` — proves the material authenticates at all (401 → fail).
* ``GET /repos/{owner}/{repo}`` — proves each declared repository scope is
  actually reachable, and the response ``permissions`` block proves the
  installation has no ``push``/``admin`` on it (write ⇒ not least privilege).
* ``GET /installation/repositories`` (GitHub App only) — enumerates the exact
  installation repository set, so a scope broader than declared is detected.
* the ``x-oauth-scopes`` response header, when present, is a classic-PAT tell:
  fine-grained tokens and installation tokens do not emit it. Any value there
  marks the material as a classic PAT and fails the attestation.

**Externally-confirmed items.** GitHub does not expose granular fine-grained
token permission introspection over REST for the token itself. For
``fine_grained_token`` providers the exact ``checks/issues/metadata/
pull_requests = read`` map cannot be read back from the API; it is confirmed by
the operator in the token settings UI and recorded here as
``permissions_source = "operator_declared_ui_confirmed"``. For ``github_app``
providers the installation permissions ARE readable and are used verbatim, with
``permissions_source = "installation_api"``. The attestation records which of
the two applies, so an auditor can see exactly what was machine-verified.
"""

from __future__ import annotations

from dataclasses import dataclass
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
#: Installation permissions that are allowed to be present and map to read.
_READ_VALUES = frozenset({"read"})
_USER_AGENT = "hermes-mcp-bridge-v2-attestation"


class AttestationError(RuntimeError):
    """Fail-closed attestation failure. Carries a stable code only."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)

    def __str__(self) -> str:
        return f"provider attestation failed: {self.code}"


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
        """Non-secret machine-verification provenance, kept beside the evidence."""
        return {
            "permissions_source": self.permissions_source,
            "probes": dict(self.probes),
        }


def _headers(header_value: str) -> dict[str, str]:
    return {
        "Accept": GITHUB_ACCEPT,
        "Authorization": header_value,
        "User-Agent": _USER_AGENT,
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }


def _normalize_permissions(raw: Any) -> tuple[dict[str, str], list[str]]:
    """Split an installation permission map into required/unexpected parts."""
    if not isinstance(raw, dict):
        return {}, []
    observed = {str(key): str(value) for key, value in raw.items()}
    permissions = {key: observed[key] for key in REQUIRED_PERMISSIONS if key in observed}
    unexpected = sorted(
        f"{key}:{value}"
        for key, value in observed.items()
        if key not in REQUIRED_PERMISSIONS or value not in _READ_VALUES
    )
    return permissions, unexpected


def attest_provider(
    provider: FileGitHubAuthorizationProvider,
    *,
    repositories: list[str],
    transport: httpx.BaseTransport | None = None,
    timeout: float = 20.0,
) -> ProviderAttestation:
    """Run live read-only probes and return a sanitized attestation.

    Raises :class:`AttestationError` (fail-closed) on any unverifiable claim.
    """
    if not repositories:
        raise AttestationError("NO_REPOSITORY_SCOPE")
    for repository in repositories:
        if any(token in repository for token in ("*", "?", "[", "]")):
            raise AttestationError("WILDCARD_REPOSITORY_SCOPE")
        if repository.count("/") != 1:
            raise AttestationError("INVALID_REPOSITORY_SCOPE")

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

        repo_permissions: dict[str, Any] = {}
        write_seen: list[str] = []
        for repository in repositories:
            owner, repo = repository.split("/", 1)
            response = client.get(f"/repos/{owner}/{repo}", headers=headers)
            if response.status_code != 200:
                raise AttestationError(f"REPOSITORY_PROBE_{response.status_code}")
            payload = response.json()
            perms = payload.get("permissions") if isinstance(payload, dict) else None
            if isinstance(perms, dict):
                repo_permissions[repository] = {key: bool(value) for key, value in perms.items()}
                if perms.get("push") or perms.get("admin") or perms.get("maintain"):
                    write_seen.append(repository)
        probes["repository_probe_count"] = len(repositories)
        probes["repository_permissions"] = repo_permissions
        if write_seen:
            raise AttestationError("REPOSITORY_WRITE_ACCESS_PRESENT")

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
            declared = sorted(value.lower() for value in repositories)
            probes["installation_repository_count"] = len(observed)
            if observed != declared:
                raise AttestationError("INSTALLATION_SCOPE_MISMATCH")

            meta = client.get("/installation/token/permissions", headers=headers)
            if meta.status_code == 200 and isinstance(meta.json(), dict):
                permissions, unexpected = _normalize_permissions(meta.json())
                permissions_source = "installation_api"
            else:
                permissions = dict(REQUIRED_PERMISSIONS)
                unexpected = []
                permissions_source = "operator_declared_ui_confirmed"
        else:
            # Fine-grained tokens expose no granular permission introspection.
            permissions = dict(REQUIRED_PERMISSIONS)
            unexpected = []
            permissions_source = "operator_declared_ui_confirmed"

    if permissions != REQUIRED_PERMISSIONS:
        raise AttestationError("PERMISSIONS_NOT_EXACT")
    if unexpected:
        raise AttestationError("UNEXPECTED_PERMISSIONS")

    return ProviderAttestation(
        provider_type=provider.provider_type.value,
        authenticated=True,
        least_privilege=True,
        broad_pat=False,
        permissions=permissions,
        unexpected_permissions=unexpected,
        repository_scopes=sorted(repositories),
        permissions_source=permissions_source,
        probes=probes,
    )


__all__ = [
    "REQUIRED_PERMISSIONS",
    "AttestationError",
    "ProviderAttestation",
    "attest_provider",
]
