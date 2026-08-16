"""Security review regressions for EPIC-03 Vault credential lifecycle.

Synthetic sentinels are intentionally used to prove cleanup and exception
redaction. No live Vault transport, path, token or real credential is used.
"""

from __future__ import annotations

import json

import pytest

from hermes_mcp_bridge.v2.enums import CapabilityState
from hermes_mcp_bridge.v2.provider_audit import (
    AuditKind,
    IntegrationAuditLedger,
    MemoryAuditSink,
    OutcomeClass,
)
from hermes_mcp_bridge.v2.provider_contract import ProviderReason
from hermes_mcp_bridge.v2.provider_credentials import (
    CredentialError,
    CredentialRecord,
    ProviderCredentialBroker,
)
from hermes_mcp_bridge.v2.provider_gateway import (
    PolicyPort,
    ProviderCallResult,
    ProviderGateway,
    ProviderRequest,
    ScopeResolver,
)
from hermes_mcp_bridge.v2.provider_manifests import PROVIDER_ALLOW_LIST, github_manifest
from hermes_mcp_bridge.v2.provider_registry import HealthReport, build_registry
from hermes_mcp_bridge.v2.vault_credentials import VaultCredentialProvider

READ_OPERATION = "github.repo_read"
READ_CREDENTIAL = "github.read"
TARGET = "pestoura/hermes-mcp-bridge"
SYNTHETIC_ERROR_MATERIAL = "SYNTHETIC_EPIC03_ERROR_MATERIAL"


def _broker(provider) -> ProviderCredentialBroker:
    manifest = github_manifest(include_write=False)
    broker = ProviderCredentialBroker({manifest.provider_id: manifest.credential_domain})
    broker.bind_provider(
        provider_id="github",
        credential_capability_id=READ_CREDENTIAL,
        provider=provider,
    )
    return broker


def _gateway(provider):
    manifest = github_manifest(include_write=False)
    broker = _broker(provider)
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
        request_id="epic03-review-cleanup-1",
        principal_ref="principal-opaque",
        provider_id="github",
        capability_id=READ_OPERATION,
        target_scope_ref=TARGET,
    )
    return gateway, sink, request


class _InvalidRecordProvider:
    def __init__(self, *, broad: bool = False, ready: bool = True) -> None:
        self.broad = broad
        self.ready = ready
        self.cleanup_calls = 0

    def status(self, provider_id: str, credential_capability_id: str) -> bool:
        return True

    def request(self, provider_id: str, credential_capability_id: str) -> CredentialRecord:
        def _cleanup() -> None:
            self.cleanup_calls += 1

        return CredentialRecord(
            provider_id=provider_id,
            credential_capability_id=credential_capability_id,
            ready=self.ready,
            apply=lambda headers: dict(headers),
            broad_credential=self.broad,
            revoke=_cleanup,
        )

    def revoke(self, provider_id: str, credential_capability_id: str) -> None:
        return None


class _ExplodingProvider:
    def status(self, provider_id: str, credential_capability_id: str) -> bool:
        return True

    def request(self, provider_id: str, credential_capability_id: str) -> CredentialRecord:
        raise RuntimeError(SYNTHETIC_ERROR_MATERIAL)

    def revoke(self, provider_id: str, credential_capability_id: str) -> None:
        raise RuntimeError(SYNTHETIC_ERROR_MATERIAL)


class _ExplodingGrant:
    def apply(self, headers: dict[str, str]) -> dict[str, str]:
        return dict(headers)

    def revoke(self) -> None:
        raise RuntimeError(SYNTHETIC_ERROR_MATERIAL)


class _ExplodingVaultClient:
    def __init__(self, *, issue_grant: bool = False) -> None:
        self.issue_grant = issue_grant

    def status(self, provider_id: str, credential_capability_id: str) -> bool:
        return True

    def request(self, provider_id: str, credential_capability_id: str):
        if self.issue_grant:
            return _ExplodingGrant()
        raise RuntimeError(SYNTHETIC_ERROR_MATERIAL)

    def revoke(self, provider_id: str, credential_capability_id: str) -> None:
        raise RuntimeError(SYNTHETIC_ERROR_MATERIAL)


def test_broker_cleans_up_provider_grant_when_record_is_broad_and_rejected() -> None:
    provider = _InvalidRecordProvider(broad=True)
    broker = _broker(provider)

    with pytest.raises(CredentialError) as excinfo:
        broker.resolve(provider_id="github", credential_capability_id=READ_CREDENTIAL)

    assert excinfo.value.reason is ProviderReason.E_CRED_CROSS_DOMAIN
    assert provider.cleanup_calls == 1


def test_broker_cleans_up_provider_grant_when_record_is_not_ready() -> None:
    provider = _InvalidRecordProvider(ready=False)
    broker = _broker(provider)

    with pytest.raises(CredentialError) as excinfo:
        broker.resolve(provider_id="github", credential_capability_id=READ_CREDENTIAL)

    assert excinfo.value.reason is ProviderReason.E_CRED_UNAVAILABLE
    assert provider.cleanup_calls == 1


def test_broker_suppresses_secret_bearing_backend_exception_cause() -> None:
    broker = _broker(_ExplodingProvider())

    with pytest.raises(CredentialError) as excinfo:
        broker.resolve(provider_id="github", credential_capability_id=READ_CREDENTIAL)

    assert excinfo.value.reason is ProviderReason.E_CRED_UNAVAILABLE
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert SYNTHETIC_ERROR_MATERIAL not in str(excinfo.value)


def test_vault_provider_suppresses_secret_bearing_request_exception_cause() -> None:
    provider = VaultCredentialProvider(client=_ExplodingVaultClient())

    with pytest.raises(CredentialError) as excinfo:
        provider.request("github", READ_CREDENTIAL)

    assert excinfo.value.reason is ProviderReason.E_CRED_UNAVAILABLE
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert SYNTHETIC_ERROR_MATERIAL not in str(excinfo.value)


def test_vault_provider_suppresses_secret_bearing_revoke_exception_cause() -> None:
    provider = VaultCredentialProvider(client=_ExplodingVaultClient())

    with pytest.raises(CredentialError) as excinfo:
        provider.revoke("github", READ_CREDENTIAL)

    assert excinfo.value.reason is ProviderReason.E_CRED_UNAVAILABLE
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert SYNTHETIC_ERROR_MATERIAL not in str(excinfo.value)


def test_vault_provider_sanitizes_per_grant_cleanup_exception() -> None:
    provider = VaultCredentialProvider(client=_ExplodingVaultClient(issue_grant=True))
    record = provider.request("github", READ_CREDENTIAL)

    with pytest.raises(CredentialError) as excinfo:
        record.revoke()

    assert excinfo.value.reason is ProviderReason.E_CRED_UNAVAILABLE
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert SYNTHETIC_ERROR_MATERIAL not in str(excinfo.value)


def test_gateway_records_terminal_sanitized_error_when_grant_cleanup_fails() -> None:
    provider = VaultCredentialProvider(client=_ExplodingVaultClient(issue_grant=True))
    gateway, sink, request = _gateway(provider)

    outcome = gateway.invoke(request)

    assert outcome.outcome is OutcomeClass.ERROR
    assert outcome.reason_code is ProviderReason.E_CRED_UNAVAILABLE
    assert outcome.payload == {}
    terminal = [record for record in sink.records if record["kind"] == AuditKind.TERMINAL.value]
    assert len(terminal) == 1
    assert terminal[0]["outcome"] == OutcomeClass.ERROR.value
    assert terminal[0]["reason_code"] == ProviderReason.E_CRED_UNAVAILABLE.value
    assert SYNTHETIC_ERROR_MATERIAL not in json.dumps(sink.records, sort_keys=True)
