"""Phase 9: executable rollback, credential rotation and audit restore drills.

Each drill runs against the real V2 primitives (provider registry, credential
broker, audit digest chain) with an injected clock, so the RTO/RPO assertions
are measured rather than declared. Hermetic: no network, no credentials, no
filesystem, no host mutation.
"""

from __future__ import annotations

import pytest

from hermes_mcp_bridge.v2.audit_chain import digest_chain
from hermes_mcp_bridge.v2.drills import (
    AUDIT_RPO_RECORDS,
    ROLLBACK_RTO_SECONDS,
    DrillError,
    drill_evidence,
    run_restore_drill,
    run_rollback_drill,
    run_rotation_drill,
)
from hermes_mcp_bridge.v2.provider_contract import CredentialDomain
from hermes_mcp_bridge.v2.provider_credentials import (
    CredentialError,
    CredentialRecord,
    ProviderCredentialBroker,
)


class Clock:
    """Deterministic monotonic clock; each read advances by ``step``."""

    def __init__(self, step: float = 0.5) -> None:
        self._now = 0.0
        self._step = step

    def __call__(self) -> float:
        value = self._now
        self._now += self._step
        return value


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------
def _allow_list_state() -> list[str]:
    return ["github", "jira"]


def test_p9_d01_rollback_by_allow_list_removal_succeeds() -> None:
    providers = _allow_list_state()

    def disable(provider_id: str) -> list[str]:
        providers.remove(provider_id)
        return list(providers)

    result = run_rollback_drill(
        registry_provider_ids=list(providers),
        withdraw="jira",
        disable=disable,
        capability_usable=lambda pid: pid in providers,
        live_after_drain=0,
        clock=Clock(),
    )
    assert result.passed, result.failures
    assert result.observations["providers_after"] == 1
    assert result.elapsed_seconds <= ROLLBACK_RTO_SECONDS


def test_p9_d02_rollback_fails_when_provider_stays_reachable() -> None:
    providers = _allow_list_state()
    result = run_rollback_drill(
        registry_provider_ids=list(providers),
        withdraw="jira",
        # A disable that does not actually remove anything.
        disable=lambda pid: list(providers),
        capability_usable=lambda pid: True,
        live_after_drain=0,
        clock=Clock(),
    )
    assert not result.passed
    assert any("still present" in failure for failure in result.failures)
    assert any("still reachable" in failure for failure in result.failures)


def test_p9_d03_rollback_fails_with_work_still_in_flight() -> None:
    providers = _allow_list_state()

    def disable(provider_id: str) -> list[str]:
        providers.remove(provider_id)
        return list(providers)

    result = run_rollback_drill(
        registry_provider_ids=list(providers),
        withdraw="jira",
        disable=disable,
        capability_usable=lambda pid: pid in providers,
        live_after_drain=2,
        clock=Clock(),
    )
    assert not result.passed
    assert any("in flight" in failure for failure in result.failures)


def test_p9_d04_rollback_breaching_rto_fails() -> None:
    providers = _allow_list_state()

    def disable(provider_id: str) -> list[str]:
        providers.remove(provider_id)
        return list(providers)

    result = run_rollback_drill(
        registry_provider_ids=list(providers),
        withdraw="jira",
        disable=disable,
        capability_usable=lambda pid: pid in providers,
        live_after_drain=0,
        clock=Clock(step=ROLLBACK_RTO_SECONDS + 1.0),
        target_seconds=ROLLBACK_RTO_SECONDS,
    )
    assert not result.passed
    assert any("RTO" in failure for failure in result.failures)


def test_p9_d05_unknown_withdraw_target_is_an_error() -> None:
    with pytest.raises(DrillError):
        run_rollback_drill(
            registry_provider_ids=["github"],
            withdraw="not-registered",
            disable=lambda pid: [],
            capability_usable=lambda pid: False,
            live_after_drain=0,
            clock=Clock(),
        )


# ---------------------------------------------------------------------------
# Credential rotation, against the real broker
# ---------------------------------------------------------------------------
def _broker() -> ProviderCredentialBroker:
    domain = CredentialDomain(
        provider_id="github",
        read_capability_id="github.read",
        write_capability_id="github.write",
        granted_scopes={"github.read": ("repo:read",), "github.write": ("repo:write",)},
    )
    return ProviderCredentialBroker({"github": domain})


def _record(broker: ProviderCredentialBroker, marker: str) -> CredentialRecord:
    return CredentialRecord(
        provider_id="github",
        credential_capability_id="github.read",
        ready=True,
        apply=lambda headers: {**headers, "authorization": f"Bearer <{marker}>"},
    )


def test_p9_d06_rotation_without_restart_keeps_capability_ready() -> None:
    broker = _broker()
    broker.register(_record(broker, "old"))

    result = run_rotation_drill(
        provider_id="github",
        capability_id="github.read",
        mint_handle=lambda: broker.resolve(
            provider_id="github",
            credential_capability_id="github.read",
            requested_scopes=("repo:read",),
        ),
        rotate=lambda: broker.rotate(_record(broker, "new")),
        status=lambda: broker.status("github", "github.read"),
        apply_headers=lambda handle: handle.apply({}),
        restart_observed=False,
        clock=Clock(),
    )
    assert result.passed, result.failures
    assert result.observations["ready_after_rotation"] is True
    assert result.observations["inflight_outcome"] == "completed_on_old_material"


def test_p9_d07_rotation_requiring_restart_fails() -> None:
    broker = _broker()
    broker.register(_record(broker, "old"))
    result = run_rotation_drill(
        provider_id="github",
        capability_id="github.read",
        mint_handle=lambda: broker.resolve(
            provider_id="github",
            credential_capability_id="github.read",
            requested_scopes=("repo:read",),
        ),
        rotate=lambda: broker.rotate(_record(broker, "new")),
        status=lambda: broker.status("github", "github.read"),
        apply_headers=lambda handle: handle.apply({}),
        restart_observed=True,
        clock=Clock(),
    )
    assert not result.passed
    assert any("restart" in failure for failure in result.failures)


def test_p9_d08_revoked_domain_fails_closed_not_open() -> None:
    broker = _broker()
    broker.register(_record(broker, "old"))
    broker.revoke("github", "github.read")
    assert broker.status("github", "github.read") is False
    with pytest.raises(CredentialError):
        broker.resolve(
            provider_id="github",
            credential_capability_id="github.read",
            requested_scopes=("repo:read",),
        )


def test_p9_d09_rotation_never_widens_scope() -> None:
    broker = _broker()
    broker.register(_record(broker, "old"))
    broker.rotate(_record(broker, "new"))
    with pytest.raises(CredentialError):
        broker.resolve(
            provider_id="github",
            credential_capability_id="github.read",
            requested_scopes=("repo:write",),
        )


def test_p9_d10_rotation_evidence_carries_no_material() -> None:
    broker = _broker()
    broker.register(_record(broker, "old"))
    result = run_rotation_drill(
        provider_id="github",
        capability_id="github.read",
        mint_handle=lambda: broker.resolve(
            provider_id="github",
            credential_capability_id="github.read",
            requested_scopes=("repo:read",),
        ),
        rotate=lambda: broker.rotate(_record(broker, "new")),
        status=lambda: broker.status("github", "github.read"),
        apply_headers=lambda handle: handle.apply({}),
        restart_observed=False,
        clock=Clock(),
    )
    serialized = repr(result.canonical())
    assert "Bearer" not in serialized
    assert "authorization" not in serialized.lower()


# ---------------------------------------------------------------------------
# Audit restore
# ---------------------------------------------------------------------------
def _records(count: int) -> list[dict[str, object]]:
    return [
        {"request_id": f"req-{index}", "outcome": "success", "sequence": index}
        for index in range(count)
    ]


def test_p9_d11_full_restore_reproduces_the_chain() -> None:
    original = _records(12)
    result = run_restore_drill(
        original_records=original, restored_records=list(original), clock=Clock()
    )
    assert result.passed, result.failures
    assert result.observations["records_lost"] == AUDIT_RPO_RECORDS
    assert result.observations["chain_matches"] is True


def test_p9_d12_lost_record_breaches_rpo_and_breaks_the_chain() -> None:
    original = _records(12)
    result = run_restore_drill(
        original_records=original, restored_records=original[:-1], clock=Clock()
    )
    assert not result.passed
    assert any("RPO" in failure for failure in result.failures)
    assert result.observations["chain_matches"] is False


def test_p9_d13_reordering_breaks_the_chain_even_with_no_loss() -> None:
    original = _records(6)
    reordered = [original[1], original[0], *original[2:]]
    result = run_restore_drill(
        original_records=original, restored_records=reordered, clock=Clock()
    )
    assert not result.passed
    assert result.observations["records_lost"] == 0
    assert any("chain" in failure for failure in result.failures)


def test_p9_d14_tampered_record_breaks_the_chain() -> None:
    original = _records(4)
    tampered = [dict(record) for record in original]
    tampered[2]["outcome"] = "refused"
    result = run_restore_drill(
        original_records=original, restored_records=tampered, clock=Clock()
    )
    assert not result.passed
    assert digest_chain(*tampered) != digest_chain(*original)


def test_p9_d15_aggregate_evidence_is_pass_only_when_every_drill_passes() -> None:
    providers = _allow_list_state()

    def disable(provider_id: str) -> list[str]:
        providers.remove(provider_id)
        return list(providers)

    broker = _broker()
    broker.register(_record(broker, "old"))
    original = _records(5)

    results = [
        run_rollback_drill(
            registry_provider_ids=list(providers),
            withdraw="jira",
            disable=disable,
            capability_usable=lambda pid: pid in providers,
            live_after_drain=0,
            clock=Clock(),
        ),
        run_rotation_drill(
            provider_id="github",
            capability_id="github.read",
            mint_handle=lambda: broker.resolve(
                provider_id="github",
                credential_capability_id="github.read",
                requested_scopes=("repo:read",),
            ),
            rotate=lambda: broker.rotate(_record(broker, "new")),
            status=lambda: broker.status("github", "github.read"),
            apply_headers=lambda handle: handle.apply({}),
            restart_observed=False,
            clock=Clock(),
        ),
        run_restore_drill(
            original_records=original, restored_records=list(original), clock=Clock()
        ),
    ]
    evidence = drill_evidence(results)
    assert evidence["passed"] is True
    assert evidence["failures"] == []
    assert {entry["drill"] for entry in evidence["drills"]} == {
        "rollback",
        "credential_rotation",
        "audit_restore",
    }
    # A single failing drill must sink the aggregate.
    failing = run_restore_drill(
        original_records=original, restored_records=original[:-1], clock=Clock()
    )
    assert drill_evidence([*results, failing])["passed"] is False
