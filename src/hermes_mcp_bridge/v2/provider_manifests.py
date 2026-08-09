"""Phase 7 in-repo provider allow-list and manifests.

> **V2 · PHASE 7 · runtime, disabled by default behind ``PROVIDER_FEATURE_ENABLED``**

The allow-list is *code*, not configuration discovery: there is no entry-point
scan, no plugin directory and no dynamic import. Removing a provider id from
:data:`PROVIDER_ALLOW_LIST` is the layer-1 rollback path - the provider then
resolves to ``E-PROVIDER-UNKNOWN`` with zero side effects.

Only two providers are declared ``ACCEPTED``: **github**, the reference
implementation already carried by Phases 2-3, and **jira**, whose credential and
contract are genuinely available on this host. Every other provider is either a
``CANDIDATE`` (shape defined, credential/contract not evidenced) or explicitly
``BLOCKED_UNCONFIRMED`` and is refused at registration.
"""

from __future__ import annotations

from .enums import IdempotencySemantics, MutationClass, SecurityTier
from .provider_contract import (
    PROVIDER_CONTRACT_VERSION,
    CapabilityClass,
    CapabilityDeclaration,
    CredentialDomain,
    ProviderManifest,
    ProviderStatus,
)

#: Providers the gateway may resolve at all. Order is irrelevant; membership is
#: the entire authorization to exist.
PROVIDER_ALLOW_LIST: tuple[str, ...] = ("github", "jira")

#: Providers whose lane is explicitly not accepted. Declared here so the state
#: is auditable rather than merely absent.
BLOCKED_UNCONFIRMED_PROVIDERS: tuple[str, ...] = (
    "ritmo",  # existence, API surface and data classification unconfirmed
)

#: Providers with a designed shape but no evidenced credential/contract on this
#: host. They are not in the allow-list and cannot be registered.
CANDIDATE_PROVIDERS: tuple[str, ...] = (
    "calendar",
    "cloudflare",
    "docker",
    "email",
    "grafana",
    "homeassistant",
    "n8n",
    "systemd",
)

GITHUB_READ_CREDENTIAL = "github.read"
GITHUB_WRITE_CREDENTIAL = "github.write"
JIRA_READ_CREDENTIAL = "jira.read"

GITHUB_API_HOST = "api.github.com"
JIRA_API_HOST_DEFAULT = "pedroestoura.atlassian.net"


def github_manifest(*, include_write: bool = False) -> ProviderManifest:
    """GitHub reference manifest: five read capabilities, optional write lane."""
    capabilities = [
        CapabilityDeclaration(
            capability_id="github.repo_read",
            capability_class=CapabilityClass.DIRECT_READ,
            tool_id="github.get_repo",
            credential_capability_id=GITHUB_READ_CREDENTIAL,
            security_tier=SecurityTier.T1,
            mutation_class=MutationClass.NONE,
            idempotency=IdempotencySemantics.READ,
            scopes=("repo:read",),
            egress_hosts=(GITHUB_API_HOST,),
        ),
        CapabilityDeclaration(
            capability_id="github.pr_read",
            capability_class=CapabilityClass.DIRECT_READ,
            tool_id="github.get_pull_request",
            credential_capability_id=GITHUB_READ_CREDENTIAL,
            security_tier=SecurityTier.T1,
            mutation_class=MutationClass.NONE,
            idempotency=IdempotencySemantics.READ,
            scopes=("repo:read", "pull_requests:read"),
            egress_hosts=(GITHUB_API_HOST,),
        ),
        CapabilityDeclaration(
            capability_id="github.checks_read",
            capability_class=CapabilityClass.DIRECT_READ,
            tool_id="github.list_checks",
            credential_capability_id=GITHUB_READ_CREDENTIAL,
            security_tier=SecurityTier.T1,
            mutation_class=MutationClass.NONE,
            idempotency=IdempotencySemantics.READ,
            scopes=("checks:read", "repo:read"),
            egress_hosts=(GITHUB_API_HOST,),
        ),
    ]
    granted: dict[str, tuple[str, ...]] = {
        GITHUB_READ_CREDENTIAL: ("checks:read", "pull_requests:read", "repo:read"),
    }
    write_capability_id: str | None = None
    if include_write:
        write_capability_id = GITHUB_WRITE_CREDENTIAL
        granted[GITHUB_WRITE_CREDENTIAL] = ("contents:write", "pull_requests:write")
        capabilities.append(
            CapabilityDeclaration(
                capability_id="github.pr_create",
                capability_class=CapabilityClass.DIRECT_WRITE,
                tool_id="github.create_pull_request",
                credential_capability_id=GITHUB_WRITE_CREDENTIAL,
                security_tier=SecurityTier.T3,
                mutation_class=MutationClass.STANDARD,
                idempotency=IdempotencySemantics.KEYED_IDEMPOTENT,
                scopes=("pull_requests:write",),
                egress_hosts=(GITHUB_API_HOST,),
                approval_required=True,
            )
        )
    return ProviderManifest(
        provider_id="github",
        provider_version="1",
        contract_version=PROVIDER_CONTRACT_VERSION,
        capabilities=tuple(capabilities),
        credential_domain=CredentialDomain(
            provider_id="github",
            read_capability_id=GITHUB_READ_CREDENTIAL,
            write_capability_id=write_capability_id,
            granted_scopes=granted,
        ),
        status=ProviderStatus.ACCEPTED,
    )


def jira_manifest(*, host: str = JIRA_API_HOST_DEFAULT) -> ProviderManifest:
    """Jira Cloud manifest — read-only. The write lane is deliberately absent.

    The available credential is a shared, long-lived API token used by other
    host automations; a write capability on that credential would violate the
    least-privilege rule, so Phase 7 accepts Jira for reads only.
    """
    capabilities = (
        CapabilityDeclaration(
            capability_id="jira.issue_read",
            capability_class=CapabilityClass.DIRECT_READ,
            tool_id="jira.get_issue",
            credential_capability_id=JIRA_READ_CREDENTIAL,
            security_tier=SecurityTier.T1,
            mutation_class=MutationClass.NONE,
            idempotency=IdempotencySemantics.READ,
            scopes=("read:jira-work",),
            egress_hosts=(host,),
        ),
        CapabilityDeclaration(
            capability_id="jira.project_read",
            capability_class=CapabilityClass.DIRECT_READ,
            tool_id="jira.get_project",
            credential_capability_id=JIRA_READ_CREDENTIAL,
            security_tier=SecurityTier.T1,
            mutation_class=MutationClass.NONE,
            idempotency=IdempotencySemantics.READ,
            scopes=("read:jira-work",),
            egress_hosts=(host,),
        ),
    )
    return ProviderManifest(
        provider_id="jira",
        provider_version="1",
        contract_version=PROVIDER_CONTRACT_VERSION,
        capabilities=capabilities,
        credential_domain=CredentialDomain(
            provider_id="jira",
            read_capability_id=JIRA_READ_CREDENTIAL,
            granted_scopes={JIRA_READ_CREDENTIAL: ("read:jira-work",)},
        ),
        status=ProviderStatus.ACCEPTED,
    )


def accepted_manifests(*, jira_host: str = JIRA_API_HOST_DEFAULT) -> tuple[ProviderManifest, ...]:
    """The manifests for the two genuinely supported providers."""
    return (github_manifest(), jira_manifest(host=jira_host))


def accepted_tool_ids(*, jira_host: str = JIRA_API_HOST_DEFAULT) -> tuple[str, ...]:
    ids: list[str] = []
    for manifest in accepted_manifests(jira_host=jira_host):
        ids.extend(capability.tool_id for capability in manifest.capabilities)
    return tuple(sorted(ids))


__all__ = [
    "BLOCKED_UNCONFIRMED_PROVIDERS",
    "CANDIDATE_PROVIDERS",
    "GITHUB_API_HOST",
    "JIRA_API_HOST_DEFAULT",
    "PROVIDER_ALLOW_LIST",
    "accepted_manifests",
    "accepted_tool_ids",
    "github_manifest",
    "jira_manifest",
]
