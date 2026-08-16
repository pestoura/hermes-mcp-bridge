"""Security review regressions for EPIC-03 Vault credential lifecycle.

Synthetic sentinels are intentionally used to prove cleanup and exception
redaction.  No live Vault transport, path, token or real credential is used.
"""

from __future__ import annotations

import pytest

from hermes_mcp_bridge.v2.provider_contract import ProviderReason
from hermes_mcp_bridge.v2.provider_credentials import (
    CredentialError,
    CredentialRecord,
    ProviderCredentialBroker,
)
from hermes_mcp_bridge.v2.provider_manifests import github_manifest
from hermes_mcp_bridge.v2.vault_credentials import VaultCredentialProvider

READ_CREDENTIAL = "github.read"
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


class _ExplodingVaultClient:
    def status(self, provider_id: str, credential_capability_id: str) -> bool:
        return True

    def request(self, provider_id: str, credential_capability_id: str):
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
    assert SYNTHETIC_ERROR_MATERIAL not in str(excinfo.value)


def test_vault_provider_suppresses_secret_bearing_request_exception_cause() -> None:
    provider = VaultCredentialProvider(client=_ExplodingVaultClient())

    with pytest.raises(CredentialError) as excinfo:
        provider.request("github", READ_CREDENTIAL)

    assert excinfo.value.reason is ProviderReason.E_CRED_UNAVAILABLE
    assert excinfo.value.__cause__ is None
    assert SYNTHETIC_ERROR_MATERIAL not in str(excinfo.value)


def test_vault_provider_suppresses_secret_bearing_revoke_exception_cause() -> None:
    provider = VaultCredentialProvider(client=_ExplodingVaultClient())

    with pytest.raises(CredentialError) as excinfo:
        provider.revoke("github", READ_CREDENTIAL)

    assert excinfo.value.reason is ProviderReason.E_CRED_UNAVAILABLE
    assert excinfo.value.__cause__ is None
    assert SYNTHETIC_ERROR_MATERIAL not in str(excinfo.value)
