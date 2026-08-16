"""EPIC-03 Vault credential backend contracts.

Tests are deliberately synthetic: no Vault endpoint, SecretID, Vault token, GitHub
credential, secret path or real secret is used.  The sentinel exists only to prove
that credential material cannot escape the final adapter boundary.
"""

from __future__ import annotations

import copy
import json
import pickle

import pytest

from hermes_mcp_bridge.v2.enums import CapabilityState
from hermes_mcp_bridge.v2.provider_audit import (
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

SYNTHETIC_MATERIAL = "SYNTHETIC_EPIC03_MATERIAL"
READ_OPERATION = "github.repo_read"
READ_CREDENTIAL = "github.read"
TARGET = "pestoura/hermes-mcp-bridge"


class _SyntheticCapabilityProvider:
    """Capability-oriented fake. It has no concept of Vault paths."""

    def __init__(self, *, ready: bool = True, fail_apply: bool = False) -> None:
        self.ready = ready
        self.fail_apply = fail_apply
        self.status_calls = 0
        self.request_calls = 0
        self.revoke_calls = 0
        self.cleanup_calls: list[int] = []
        self.grant_ids: list[int] = []

    def status(self, provider_id: str, credential_capability_id: str) -> bool:
        self.status_calls += 1
        return self.ready and (provider_id, credential_capability_id) == (
            "github",
            READ_CREDENTIAL,
        )

    def request(self, provider_id: str, credential_capability_id: str) -> CredentialRecord:
        self.request_calls += 1
        if not self.status(provider_id, credential_capability_id):
            raise CredentialError(ProviderReason.E_CRED_UNAVAILABLE, credential_capability_id)

        grant_id = self.request_calls
        self.grant_ids.append(grant_id)

        def _apply(headers: dict[str, str]) -> dict[str, str]:
            if self.fail_apply:
                raise CredentialError(ProviderReason.E_CRED_UNAVAILABLE, READ_CREDENTIAL)
            return {**headers, "Authorization": SYNTHETIC_MATERIAL}

        def _revoke() -> None:
            self.cleanup_calls.append(grant_id)

        return CredentialRecord(
            provider_id=provider_id,
            credential_capability_id=credential_capability_id,
            ready=True,
            apply=_apply,
            revoke=_revoke,
        )

    def revoke(self, provider_id: str, credential_capability_id: str) -> None:
        assert (provider_id, credential_capability_id) == ("github", READ_CREDENTIAL)
        self.revoke_calls += 1


class _SyntheticCancel(BaseException):
    """Represents a cancellation-style interruption that is not Exception."""


def _broker(provider: _SyntheticCapabilityProvider) -> ProviderCredentialBroker:
    manifest = github_manifest(include_write=False)
    broker = ProviderCredentialBroker({manifest.provider_id: manifest.credential_domain})
    broker.bind_provider(
        provider_id="github",
        credential_capability_id=READ_CREDENTIAL,
        provider=provider,
    )
    return broker


def _gateway(provider: _SyntheticCapabilityProvider, *, adapter=None):
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
    scopes = ScopeResolver({READ_OPERATION: (TARGET,)})
    policy = PolicyPort({READ_OPERATION: "ALLOW"})
    sink = MemoryAuditSink()

    def _default_adapter(request, headers, deadline_ms):
        assert request.capability_id == READ_OPERATION
        assert headers["Authorization"] == SYNTHETIC_MATERIAL
        return ProviderCallResult(payload={"full_name": TARGET}, byte_count=64)

    gateway = ProviderGateway(
        registry=registry,
        policy=policy,
        scopes=scopes,
        broker=broker,
        audit=IntegrationAuditLedger(sink),
        adapters={"github": adapter or _default_adapter},
    )
    request = ProviderRequest(
        request_id="epic03-req-1",
        principal_ref="principal-opaque",
        provider_id="github",
        capability_id=READ_OPERATION,
        target_scope_ref=TARGET,
    )
    return gateway, sink, request


def test_epic03_bound_provider_mints_separate_request_scoped_handles() -> None:
    provider = _SyntheticCapabilityProvider()
    broker = _broker(provider)

    first = broker.resolve(provider_id="github", credential_capability_id=READ_CREDENTIAL)
    second = broker.resolve(provider_id="github", credential_capability_id=READ_CREDENTIAL)

    assert first is not second
    assert provider.request_calls == 2
    assert provider.grant_ids == [1, 2]
    assert first.apply({})["Authorization"] == SYNTHETIC_MATERIAL
    assert second.apply({})["Authorization"] == SYNTHETIC_MATERIAL
    first.revoke()
    second.revoke()
    assert provider.cleanup_calls == [1, 2]


def test_epic03_handle_cleanup_is_idempotent_and_non_serializable() -> None:
    provider = _SyntheticCapabilityProvider()
    handle = _broker(provider).resolve(
        provider_id="github", credential_capability_id=READ_CREDENTIAL
    )

    assert SYNTHETIC_MATERIAL not in repr(handle)
    assert SYNTHETIC_MATERIAL not in str(handle)
    with pytest.raises(TypeError):
        copy.copy(handle)
    with pytest.raises(TypeError):
        copy.deepcopy(handle)
    with pytest.raises(TypeError):
        pickle.dumps(handle)
    with pytest.raises(TypeError):
        json.dumps(handle)

    handle.revoke()
    handle.revoke()
    assert provider.cleanup_calls == [1]


def test_epic03_cross_domain_denied_before_provider_request() -> None:
    provider = _SyntheticCapabilityProvider()
    broker = _broker(provider)

    with pytest.raises(CredentialError) as excinfo:
        broker.resolve(provider_id="github", credential_capability_id="jira.read")

    assert excinfo.value.reason is ProviderReason.E_CRED_CROSS_DOMAIN
    assert provider.request_calls == 0


def test_epic03_unavailable_provider_fails_closed_without_request() -> None:
    provider = _SyntheticCapabilityProvider(ready=False)
    broker = _broker(provider)

    assert broker.status("github", READ_CREDENTIAL) is False
    with pytest.raises(CredentialError) as excinfo:
        broker.resolve(provider_id="github", credential_capability_id=READ_CREDENTIAL)

    assert excinfo.value.reason is ProviderReason.E_CRED_UNAVAILABLE
    assert provider.request_calls == 0


def test_epic03_gateway_walking_skeleton_sanitizes_result_and_audit() -> None:
    provider = _SyntheticCapabilityProvider()
    gateway, sink, request = _gateway(provider)

    outcome = gateway.invoke(request)

    assert outcome.outcome is OutcomeClass.SUCCESS
    assert outcome.reason_code is ProviderReason.OK
    assert outcome.payload == {"full_name": TARGET}
    assert gateway.provider_calls == 1
    assert gateway.credential_resolutions == 1
    assert provider.cleanup_calls == [1]
    assert SYNTHETIC_MATERIAL not in json.dumps(outcome.payload, sort_keys=True)
    assert SYNTHETIC_MATERIAL not in json.dumps(sink.records, sort_keys=True)


def test_epic03_apply_failure_still_cleans_up_and_never_calls_adapter() -> None:
    provider = _SyntheticCapabilityProvider(fail_apply=True)
    adapter_calls = 0

    def _adapter(request, headers, deadline_ms):
        nonlocal adapter_calls
        adapter_calls += 1
        return ProviderCallResult(payload={"unexpected": True}, byte_count=16)

    gateway, sink, request = _gateway(provider, adapter=_adapter)
    outcome = gateway.invoke(request)

    assert outcome.outcome is OutcomeClass.REFUSED
    assert outcome.reason_code is ProviderReason.E_CRED_UNAVAILABLE
    assert adapter_calls == 0
    assert provider.cleanup_calls == [1]
    assert SYNTHETIC_MATERIAL not in json.dumps(sink.records, sort_keys=True)


def test_epic03_cancellation_style_interruption_still_cleans_up() -> None:
    provider = _SyntheticCapabilityProvider()

    def _adapter(request, headers, deadline_ms):
        assert headers["Authorization"] == SYNTHETIC_MATERIAL
        raise _SyntheticCancel()

    gateway, _, request = _gateway(provider, adapter=_adapter)

    with pytest.raises(_SyntheticCancel):
        gateway.invoke(request)

    assert provider.cleanup_calls == [1]


def test_epic03_broker_revoke_disables_future_requests_and_notifies_provider() -> None:
    provider = _SyntheticCapabilityProvider()
    broker = _broker(provider)

    broker.revoke("github", READ_CREDENTIAL)

    assert provider.revoke_calls == 1
    assert broker.status("github", READ_CREDENTIAL) is False
    with pytest.raises(CredentialError) as excinfo:
        broker.resolve(provider_id="github", credential_capability_id=READ_CREDENTIAL)
    assert excinfo.value.reason is ProviderReason.E_CRED_REVOKED
    assert provider.request_calls == 0
