"""Concrete EPIC-03 VaultCredentialProvider contracts.

The client and grants below are synthetic capability fakes.  No Vault endpoint,
secret path, bootstrap material or real credential is used by this suite.
"""

from __future__ import annotations

import inspect
import json

import pytest

from hermes_mcp_bridge.v2.enums import CapabilityState
from hermes_mcp_bridge.v2.provider_audit import IntegrationAuditLedger, MemoryAuditSink, OutcomeClass
from hermes_mcp_bridge.v2.provider_contract import ProviderReason
from hermes_mcp_bridge.v2.provider_credentials import CredentialError, ProviderCredentialBroker
from hermes_mcp_bridge.v2.provider_gateway import (
    PolicyPort,
    ProviderCallResult,
    ProviderGateway,
    ProviderRequest,
    ScopeResolver,
)
from hermes_mcp_bridge.v2.provider_manifests import PROVIDER_ALLOW_LIST, github_manifest
from hermes_mcp_bridge.v2.provider_registry import HealthReport, build_registry

SYNTHETIC_MATERIAL = "SYNTHETIC_EPIC03_VAULT_MATERIAL"
READ_OPERATION = "github.repo_read"
READ_CREDENTIAL = "github.read"
TARGET = "pestoura/hermes-mcp-bridge"


class _SyntheticGrant:
    def __init__(self, grant_id: int, cleanup: list[int]) -> None:
        self._grant_id = grant_id
        self._cleanup = cleanup

    def apply(self, headers: dict[str, str]) -> dict[str, str]:
        return {**headers, "Authorization": SYNTHETIC_MATERIAL}

    def revoke(self) -> None:
        self._cleanup.append(self._grant_id)

    def __repr__(self) -> str:
        return f"<_SyntheticGrant material={SYNTHETIC_MATERIAL}>"


class _SyntheticVaultClient:
    """Fake capability transport deliberately exposing no path-based method."""

    def __init__(self, *, ready: bool = True, fail_request: bool = False) -> None:
        self.ready = ready
        self.fail_request = fail_request
        self.status_calls = 0
        self.request_calls = 0
        self.revoke_calls = 0
        self.cleanup_calls: list[int] = []

    def status(self, provider_id: str, credential_capability_id: str) -> bool:
        self.status_calls += 1
        return self.ready and (provider_id, credential_capability_id) == (
            "github",
            READ_CREDENTIAL,
        )

    def request(self, provider_id: str, credential_capability_id: str) -> _SyntheticGrant:
        self.request_calls += 1
        if self.fail_request:
            raise RuntimeError("synthetic backend unavailable")
        return _SyntheticGrant(self.request_calls, self.cleanup_calls)

    def revoke(self, provider_id: str, credential_capability_id: str) -> None:
        self.revoke_calls += 1

    def __repr__(self) -> str:
        return f"<_SyntheticVaultClient material={SYNTHETIC_MATERIAL}>"


def _provider(client: _SyntheticVaultClient):
    from hermes_mcp_bridge.v2.vault_credentials import VaultCredentialProvider

    return VaultCredentialProvider(client=client)


def _gateway(provider):
    manifest = github_manifest(include_write=False)
    broker = ProviderCredentialBroker({manifest.provider_id: manifest.credential_domain})
    broker.bind_provider(
        provider_id="github",
        credential_capability_id=READ_CREDENTIAL,
        provider=provider,
    )
    registry = build_registry(
        allow_list=PROVIDER_ALLOW_LIST,
        tool_ids=[capability.tool_id for capability in manifest.capabilities],
        manifests=[manifest],
    )
    registry.promote_configured(
        HealthReport(capability_id=capability.capability_id, state=CapabilityState.READY)
        for capability in manifest.capabilities
    )
    sink = MemoryAuditSink()

    def _adapter(request, headers, deadline_ms):
        assert headers["Authorization"] == SYNTHETIC_MATERIAL
        return ProviderCallResult(payload={"full_name": TARGET}, byte_count=64)

    gateway = ProviderGateway(
        registry=registry,
        policy=PolicyPort({READ_OPERATION: "ALLOW"}),
        scopes=ScopeResolver({READ_OPERATION: (TARGET,)}),
        broker=broker,
        audit=IntegrationAuditLedger(sink),
        adapters={"github": _adapter},
    )
    request = ProviderRequest(
        request_id="epic03-vault-provider-1",
        principal_ref="principal-opaque",
        provider_id="github",
        capability_id=READ_OPERATION,
        target_scope_ref=TARGET,
    )
    return gateway, sink, request


def test_vault_provider_default_surface_is_capability_only_not_path_based() -> None:
    provider = _provider(_SyntheticVaultClient())

    request_parameters = tuple(inspect.signature(provider.request).parameters)
    status_parameters = tuple(inspect.signature(provider.status).parameters)
    revoke_parameters = tuple(inspect.signature(provider.revoke).parameters)

    assert request_parameters == ("provider_id", "credential_capability_id")
    assert status_parameters == request_parameters
    assert revoke_parameters == request_parameters
    assert all("path" not in name.lower() for name in request_parameters)


def test_vault_provider_request_returns_opaque_record_and_cleanup() -> None:
    client = _SyntheticVaultClient()
    provider = _provider(client)

    assert provider.status("github", READ_CREDENTIAL) is True
    record = provider.request("github", READ_CREDENTIAL)

    assert record.provider_id == "github"
    assert record.credential_capability_id == READ_CREDENTIAL
    assert record.ready is True
    assert SYNTHETIC_MATERIAL not in repr(record)
    assert record.apply({})["Authorization"] == SYNTHETIC_MATERIAL
    record.revoke()
    assert client.cleanup_calls == [1]


def test_vault_provider_refuses_undeclared_capability_before_client_request() -> None:
    client = _SyntheticVaultClient()
    provider = _provider(client)

    assert provider.status("github", "github.admin") is False
    with pytest.raises(CredentialError) as excinfo:
        provider.request("github", "github.admin")

    assert excinfo.value.reason is ProviderReason.E_CRED_CROSS_DOMAIN
    assert client.request_calls == 0


def test_vault_provider_unavailable_is_fail_closed_without_fallback() -> None:
    client = _SyntheticVaultClient(ready=False)
    provider = _provider(client)

    assert provider.status("github", READ_CREDENTIAL) is False
    with pytest.raises(CredentialError) as excinfo:
        provider.request("github", READ_CREDENTIAL)

    assert excinfo.value.reason is ProviderReason.E_CRED_UNAVAILABLE
    assert client.request_calls == 0


def test_vault_provider_backend_request_failure_is_sanitized() -> None:
    client = _SyntheticVaultClient(fail_request=True)
    provider = _provider(client)

    with pytest.raises(CredentialError) as excinfo:
        provider.request("github", READ_CREDENTIAL)

    assert excinfo.value.reason is ProviderReason.E_CRED_UNAVAILABLE
    assert SYNTHETIC_MATERIAL not in str(excinfo.value)


def test_vault_provider_repr_never_renders_client_or_material() -> None:
    provider = _provider(_SyntheticVaultClient())

    assert SYNTHETIC_MATERIAL not in repr(provider)
    assert SYNTHETIC_MATERIAL not in str(provider)
    assert "redacted" in repr(provider).lower()


def test_vault_provider_rejects_wildcard_capability_configuration() -> None:
    from hermes_mcp_bridge.v2.vault_credentials import VaultCredentialProvider

    with pytest.raises(ValueError):
        VaultCredentialProvider(
            client=_SyntheticVaultClient(),
            allowed_capabilities=frozenset({("github", "*")}),
        )


def test_vault_provider_real_walking_skeleton_is_sanitized_and_cleans_up() -> None:
    client = _SyntheticVaultClient()
    provider = _provider(client)
    gateway, sink, request = _gateway(provider)

    outcome = gateway.invoke(request)

    assert outcome.outcome is OutcomeClass.SUCCESS
    assert outcome.reason_code is ProviderReason.OK
    assert outcome.payload == {"full_name": TARGET}
    assert gateway.provider_calls == 1
    assert gateway.credential_resolutions == 1
    assert client.request_calls == 1
    assert client.cleanup_calls == [1]
    assert SYNTHETIC_MATERIAL not in json.dumps(outcome.payload, sort_keys=True)
    assert SYNTHETIC_MATERIAL not in json.dumps(sink.records, sort_keys=True)
