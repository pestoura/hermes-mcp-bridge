"""EPIC-03 hardening: concrete Vault provider cancellation and batch isolation.

Only synthetic capability grants are used.  No live Vault transport, path,
bootstrap material or real credential is involved.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from hermes_mcp_bridge.v2.batch_contract import (
    BatchFailurePolicy,
    BatchRequest,
    BatchStatus,
    BatchStep,
    StepStatus,
)
from hermes_mcp_bridge.v2.batch_scheduler import BatchScheduler
from hermes_mcp_bridge.v2.enums import CapabilityState
from hermes_mcp_bridge.v2.provider_audit import IntegrationAuditLedger, MemoryAuditSink, OutcomeClass
from hermes_mcp_bridge.v2.provider_contract import ProviderReason
from hermes_mcp_bridge.v2.provider_credentials import ProviderCredentialBroker
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

SYNTHETIC_MATERIAL = "SYNTHETIC_EPIC03_HARDENING_MATERIAL"
READ_OPERATION = "github.repo_read"
READ_CREDENTIAL = "github.read"
TARGET = "pestoura/hermes-mcp-bridge"


class _Grant:
    def __init__(self, grant_id: int, cleanup: list[int]) -> None:
        self.grant_id = grant_id
        self._cleanup = cleanup

    def apply(self, headers: dict[str, str]) -> dict[str, str]:
        return {**headers, "Authorization": SYNTHETIC_MATERIAL}

    def revoke(self) -> None:
        self._cleanup.append(self.grant_id)


class _Client:
    def __init__(self) -> None:
        self.request_calls = 0
        self.revoke_calls = 0
        self.grant_ids: list[int] = []
        self.cleanup_calls: list[int] = []

    def status(self, provider_id: str, credential_capability_id: str) -> bool:
        return (provider_id, credential_capability_id) == ("github", READ_CREDENTIAL)

    def request(self, provider_id: str, credential_capability_id: str) -> _Grant:
        self.request_calls += 1
        grant_id = self.request_calls
        self.grant_ids.append(grant_id)
        return _Grant(grant_id, self.cleanup_calls)

    def revoke(self, provider_id: str, credential_capability_id: str) -> None:
        self.revoke_calls += 1


class _SyntheticCancel(BaseException):
    pass


def _gateway(client: _Client, *, adapter=None):
    provider = VaultCredentialProvider(client=client)
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

    def _default_adapter(request, headers, deadline_ms):
        assert headers["Authorization"] == SYNTHETIC_MATERIAL
        return ProviderCallResult(payload={"full_name": TARGET}, byte_count=64)

    gateway = ProviderGateway(
        registry=registry,
        policy=PolicyPort({READ_OPERATION: "ALLOW"}),
        scopes=ScopeResolver({READ_OPERATION: (TARGET,)}),
        broker=broker,
        audit=IntegrationAuditLedger(sink),
        adapters={"github": adapter or _default_adapter},
    )
    return gateway, sink


def _request(request_id: str) -> ProviderRequest:
    return ProviderRequest(
        request_id=request_id,
        principal_ref="principal-opaque",
        provider_id="github",
        capability_id=READ_OPERATION,
        target_scope_ref=TARGET,
    )


def test_epic03_concrete_vault_provider_cleanup_survives_cancellation() -> None:
    client = _Client()

    def _adapter(request, headers, deadline_ms):
        assert headers["Authorization"] == SYNTHETIC_MATERIAL
        raise _SyntheticCancel()

    gateway, sink = _gateway(client, adapter=_adapter)

    with pytest.raises(_SyntheticCancel):
        gateway.invoke(_request("cancel-1"))

    assert client.request_calls == 1
    assert client.cleanup_calls == [1]
    assert SYNTHETIC_MATERIAL not in json.dumps(sink.records, sort_keys=True)


def test_epic03_batch_uses_separate_grants_and_independent_cleanup() -> None:
    client = _Client()
    gateway, sink = _gateway(client)

    async def _execute(step: BatchStep):
        # Yield while the scheduler meter is active so true multi-step admission
        # is observable without threads or external I/O.
        await asyncio.sleep(0.01)
        outcome = gateway.invoke(_request(f"batch-{step.step_id}"))
        assert outcome.outcome is OutcomeClass.SUCCESS
        assert outcome.reason_code is ProviderReason.OK
        return dict(outcome.payload)

    scheduler = BatchScheduler(_execute, enabled=True)
    request = BatchRequest(
        batch_id="epic03-batch-1",
        steps=(
            BatchStep(step_id="read-a", tool="github.get_repo", step_timeout_s=5),
            BatchStep(step_id="read-b", tool="github.get_repo", step_timeout_s=5),
        ),
        failure_policy=BatchFailurePolicy.CONTINUE_ON_ERROR,
        max_parallelism=2,
        batch_timeout_s=10,
    )

    result = asyncio.run(scheduler.run(request))

    assert result.aggregate_status is BatchStatus.SUCCESS
    assert [entry.status for entry in result.steps] == [StepStatus.SUCCESS, StepStatus.SUCCESS]
    assert result.max_observed_inflight >= 2
    assert client.request_calls == 2
    assert len(client.grant_ids) == 2
    assert len(set(client.grant_ids)) == 2
    assert sorted(client.cleanup_calls) == sorted(client.grant_ids)
    assert gateway.provider_calls == 2
    assert gateway.credential_resolutions == 2
    assert SYNTHETIC_MATERIAL not in json.dumps(
        [dict(entry.result or {}) for entry in result.steps], sort_keys=True
    )
    assert SYNTHETIC_MATERIAL not in json.dumps(sink.records, sort_keys=True)
