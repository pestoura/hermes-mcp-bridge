"""Phase 3 lane L1 — least-privilege GitHub write credential/capability split.

Hermetic: zero network, zero real credential. Every test runs to completion
whether or not a live ``github.write.*`` credential exists on the host.

Covers ``docs/v2/phase3/credential-split.md`` + ADR-0020, criteria A3-03,
A3-04 (permission half) and A3-14.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path

import pytest

from hermes_mcp_bridge.v2.enums import (
    FORBIDDEN_PERMISSION,
    READ_CAPABILITY_ID,
    CapabilityState,
    MutationReasonCode,
    MutationStage,
    WriteCapabilityId,
)
from hermes_mcp_bridge.v2.errors import MutationDeniedError, WriteCapabilityError
from hermes_mcp_bridge.v2.github_direct import GitHubRepositoryScope
from hermes_mcp_bridge.v2.github_readiness import (
    GitHubReadReadinessBroker,
    exact_permission_failure,
    has_forbidden_permission,
    normalize_permissions,
    read_capability_satisfies,
)
from hermes_mcp_bridge.v2.github_registry import GITHUB_READ_CREDENTIAL_CAPABILITY
from hermes_mcp_bridge.v2.github_secret_provider import (
    AuthorizationStatus,
    GitHubProviderType,
)
from hermes_mcp_bridge.v2.github_write_credentials import (
    INTENDED_WRITE_PERMISSIONS,
    WRITE_SECRET_NAMES,
    FileWriteMaterialProvider,
    WriteCapabilityBroker,
    WriteCapabilityReadiness,
    WriteMaterialProvider,
    assert_read_write_disjoint,
    intended_permissions,
    is_write_capability,
    parse_write_capability,
    permission_failure,
    write_capability_ids,
)

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "hermes_mcp_bridge"
    / "v2"
    / "github_write_credentials.py"
)

SCOPE = GitHubRepositoryScope(["pestoura/hermes-mcp-bridge"])
IN_SCOPE = "pestoura/hermes-mcp-bridge"
OUT_OF_SCOPE = "someone-else/other-repo"

#: Synthetic, non-real installation-token-shaped material. Never a real token.
SYNTHETIC_APP_MATERIAL = "ghs_" + ("s3cr3t" * 12)
SYNTHETIC_FINE_GRAINED = "github_pat_" + ("A1b2C3d4" * 8)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _secret_file(tmp_path: Path, name: str, value: str, mode: int = 0o600) -> Path:
    target = tmp_path / name
    target.write_text(value, encoding="utf-8")
    os.chmod(target, mode)
    return target


def _env_for(
    capability: WriteCapabilityId,
    path: Path | None,
    *,
    bare_value: str | None = None,
) -> dict[str, str]:
    env: dict[str, str] = {}
    name = WRITE_SECRET_NAMES[capability]
    if path is not None:
        env[f"{name}_FILE"] = str(path)
    if bare_value is not None:
        env[name] = bare_value
    return env


def _provider(
    capability: WriteCapabilityId,
    env: Mapping[str, str],
    *,
    provider_type: GitHubProviderType = GitHubProviderType.GITHUB_APP,
    require_secure_mode: bool = True,
) -> FileWriteMaterialProvider:
    return FileWriteMaterialProvider(
        capability=capability,
        scope=SCOPE,
        provider_type=provider_type,
        require_secure_mode=require_secure_mode,
        env=env,
    )


def _ready_broker(
    tmp_path: Path,
    capability: WriteCapabilityId = WriteCapabilityId.BRANCH,
    *,
    granted: Mapping[str, str] | None = None,
    policy_allows: bool = True,
) -> WriteCapabilityBroker:
    path = _secret_file(tmp_path, f"{capability.name.lower()}.token", SYNTHETIC_APP_MATERIAL)
    provider = _provider(capability, _env_for(capability, path))
    permissions = dict(intended_permissions(capability)) if granted is None else dict(granted)
    return WriteCapabilityBroker(
        [provider],
        attested_permissions={capability: permissions},
        policy_allows={capability: policy_allows},
    )


class _StubProvider:
    """Minimal in-memory ``WriteMaterialProvider`` for hermetic broker tests."""

    def __init__(
        self,
        capability: WriteCapabilityId,
        status: AuthorizationStatus = AuthorizationStatus.READY,
    ) -> None:
        self._capability = capability
        self.status = status
        self.probe_calls = 0
        self.resolve_calls = 0

    @property
    def capability(self) -> WriteCapabilityId:
        return self._capability

    def probe(self) -> AuthorizationStatus:
        self.probe_calls += 1
        return self.status

    def resolve(self, capability_id: str, repository: str):
        self.resolve_calls += 1
        from hermes_mcp_bridge.v2.github_auth import GitHubAuthorization

        if capability_id != self._capability.value:
            return None
        return GitHubAuthorization(SYNTHETIC_APP_MATERIAL)


# --------------------------------------------------------------------------- #
# A3-03 — read/write disjointness
# --------------------------------------------------------------------------- #


def test_write_capability_ids_are_exactly_the_three_declared_ids() -> None:
    assert write_capability_ids() == (
        "github.write.branch",
        "github.write.merge",
        "github.write.pr",
    )


def test_read_capability_is_never_a_write_capability() -> None:
    assert READ_CAPABILITY_ID not in write_capability_ids()
    assert not is_write_capability(READ_CAPABILITY_ID)
    assert parse_write_capability(READ_CAPABILITY_ID) is None
    assert_read_write_disjoint()


def test_read_capability_id_matches_the_phase2_registry_constant() -> None:
    assert READ_CAPABILITY_ID == GITHUB_READ_CREDENTIAL_CAPABILITY


@pytest.mark.parametrize("capability", list(WriteCapabilityId))
def test_write_capability_never_satisfies_a_read_tool(capability: WriteCapabilityId) -> None:
    assert read_capability_satisfies(capability.value) is False


def test_read_capability_satisfies_only_the_read_id() -> None:
    assert read_capability_satisfies(READ_CAPABILITY_ID) is True
    assert read_capability_satisfies("  GitHub.Read  ") is True
    assert read_capability_satisfies("github.admin") is False


def test_read_broker_refuses_every_write_capability(tmp_path: Path) -> None:
    class _Probe:
        def probe(self) -> AuthorizationStatus:
            return AuthorizationStatus.READY

    broker = GitHubReadReadinessBroker(_Probe())
    assert broker.is_ready(READ_CAPABILITY_ID) is True
    for capability in WriteCapabilityId:
        assert broker.status(capability.value) is None
        assert broker.is_ready(capability.value) is False


def test_write_broker_refuses_the_read_capability(tmp_path: Path) -> None:
    broker = _ready_broker(tmp_path)
    assert broker.readiness(READ_CAPABILITY_ID) is None
    assert broker.status(READ_CAPABILITY_ID) is None
    assert broker.is_ready(READ_CAPABILITY_ID) is False


def test_read_capability_cannot_be_authorized_for_a_mutation(tmp_path: Path) -> None:
    broker = _ready_broker(tmp_path)
    with pytest.raises(WriteCapabilityError) as excinfo:
        broker.authorize(READ_CAPABILITY_ID, IN_SCOPE)
    assert excinfo.value.reason is MutationReasonCode.READ_CAPABILITY_CANNOT_MUTATE
    assert excinfo.value.stage is MutationStage.CREDENTIAL


def test_one_write_provider_never_serves_another_write_capability(tmp_path: Path) -> None:
    path = _secret_file(tmp_path, "branch.token", SYNTHETIC_APP_MATERIAL)
    provider = _provider(WriteCapabilityId.BRANCH, _env_for(WriteCapabilityId.BRANCH, path))
    assert provider.resolve(WriteCapabilityId.MERGE.value, IN_SCOPE) is None
    assert provider.last_status is AuthorizationStatus.CAPABILITY_MISMATCH
    assert provider.resolve(READ_CAPABILITY_ID, IN_SCOPE) is None
    assert provider.last_status is AuthorizationStatus.CAPABILITY_MISMATCH


def test_each_write_capability_has_a_distinct_secret_name() -> None:
    names = [WRITE_SECRET_NAMES[member] for member in WriteCapabilityId]
    assert len(set(names)) == len(names)
    assert all(name.startswith("BRIDGE_V2_GITHUB_WRITE_") for name in names)


# --------------------------------------------------------------------------- #
# A3-03 / A3-04 — exact permissions, superset failure, Administration
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("capability", list(WriteCapabilityId))
def test_intended_permissions_match_credential_split_doc(capability: WriteCapabilityId) -> None:
    expected = {
        WriteCapabilityId.BRANCH: {"contents": "write", "metadata": "read"},
        WriteCapabilityId.PR: {
            "contents": "read",
            "metadata": "read",
            "pull_requests": "write",
        },
        WriteCapabilityId.MERGE: {
            "checks": "read",
            "contents": "write",
            "metadata": "read",
            "pull_requests": "write",
        },
    }[capability]
    assert dict(intended_permissions(capability)) == expected
    assert dict(INTENDED_WRITE_PERMISSIONS[capability]) == expected


def test_no_intended_permission_map_requests_administration() -> None:
    for capability in WriteCapabilityId:
        assert not has_forbidden_permission(intended_permissions(capability))


def test_exact_match_is_the_only_success() -> None:
    for capability in WriteCapabilityId:
        assert permission_failure(capability, dict(intended_permissions(capability))) is None


def test_permission_superset_is_a_failure_not_a_convenience() -> None:
    granted = dict(intended_permissions(WriteCapabilityId.BRANCH))
    granted["issues"] = "write"
    assert (
        permission_failure(WriteCapabilityId.BRANCH, granted)
        is MutationReasonCode.PERMISSION_SUPERSET
    )


def test_missing_permission_is_a_mismatch() -> None:
    granted = dict(intended_permissions(WriteCapabilityId.MERGE))
    granted.pop("checks")
    assert (
        permission_failure(WriteCapabilityId.MERGE, granted)
        is MutationReasonCode.WRITE_CAPABILITY_MISMATCH
    )


def test_stronger_level_than_intended_is_a_mismatch() -> None:
    granted = dict(intended_permissions(WriteCapabilityId.PR))
    granted["contents"] = "write"
    assert (
        permission_failure(WriteCapabilityId.PR, granted)
        is MutationReasonCode.WRITE_CAPABILITY_MISMATCH
    )


def test_unprobed_permissions_are_not_compliant() -> None:
    assert (
        permission_failure(WriteCapabilityId.BRANCH, None)
        is MutationReasonCode.WRITE_CAPABILITY_NOT_READY
    )


@pytest.mark.parametrize("level", ["read", "write", "admin"])
def test_administration_permission_wins_over_every_other_verdict(level: str) -> None:
    granted = dict(intended_permissions(WriteCapabilityId.BRANCH))
    granted[FORBIDDEN_PERMISSION.lower()] = level
    granted["extra_permission"] = "write"
    assert (
        permission_failure(WriteCapabilityId.BRANCH, granted)
        is MutationReasonCode.ADMINISTRATION_PERMISSION_PRESENT
    )


def test_administration_detection_is_case_insensitive() -> None:
    assert has_forbidden_permission({FORBIDDEN_PERMISSION: "read"}) is True
    assert has_forbidden_permission({"ADMINISTRATION": "read"}) is True
    assert has_forbidden_permission({"administration": "read"}) is True
    assert has_forbidden_permission({"administrator": "read"}) is False


def test_permission_comparison_is_case_insensitive_and_whitespace_tolerant() -> None:
    granted = {" Contents ": " WRITE ", "Metadata": "Read"}
    assert permission_failure(WriteCapabilityId.BRANCH, granted) is None


def test_normalize_permissions_preserves_none_and_drops_non_strings() -> None:
    assert normalize_permissions(None) is None
    assert normalize_permissions({}) == {}
    assert normalize_permissions({"a": "READ", 1: "read", "b": 2}) == {"a": "read"}  # type: ignore[dict-item]


def test_exact_permission_failure_is_reusable_for_the_read_map() -> None:
    intended = {"metadata": "read", "pull_requests": "read"}
    assert exact_permission_failure(intended, dict(intended)) is None
    assert (
        exact_permission_failure(intended, {**intended, "contents": "write"})
        is MutationReasonCode.PERMISSION_SUPERSET
    )


# --------------------------------------------------------------------------- #
# fail-closed readiness (configured / authenticated / healthy / policy-allowed)
# --------------------------------------------------------------------------- #


def test_no_configured_write_credential_reports_unavailable_not_ready() -> None:
    """Hermetic completion with zero write credentials present on the host."""
    broker = WriteCapabilityBroker()
    for capability in WriteCapabilityId:
        readiness = broker.readiness(capability.value)
        assert readiness is not None
        assert readiness.state is CapabilityState.UNAVAILABLE
        assert readiness.reason is MutationReasonCode.WRITE_CAPABILITY_NOT_READY
        assert readiness.is_ready is False
        assert readiness.permissions_attested is False


def test_unconfigured_provider_is_unavailable() -> None:
    provider = _provider(WriteCapabilityId.PR, {})
    broker = WriteCapabilityBroker([provider])
    readiness = broker.readiness(WriteCapabilityId.PR.value)
    assert readiness is not None
    assert readiness.state is CapabilityState.UNAVAILABLE
    assert provider.probe() is AuthorizationStatus.NOT_CONFIGURED


def test_bare_environment_material_is_rejected_as_denied() -> None:
    env = _env_for(WriteCapabilityId.BRANCH, None, bare_value=SYNTHETIC_APP_MATERIAL)
    provider = _provider(WriteCapabilityId.BRANCH, env)
    assert provider.probe() is AuthorizationStatus.ENV_MATERIAL_REJECTED
    broker = WriteCapabilityBroker([provider])
    readiness = broker.readiness(WriteCapabilityId.BRANCH.value)
    assert readiness is not None
    assert readiness.state is CapabilityState.DENIED
    assert readiness.is_ready is False


def test_healthy_but_policy_denied_is_not_ready(tmp_path: Path) -> None:
    broker = _ready_broker(tmp_path, policy_allows=False)
    readiness = broker.readiness(WriteCapabilityId.BRANCH.value)
    assert readiness is not None
    assert readiness.state is CapabilityState.HEALTHY
    assert readiness.state.is_healthy is True
    assert readiness.state.is_ready is False
    assert readiness.reason is MutationReasonCode.WRITE_CAPABILITY_NOT_READY


def test_all_four_conditions_together_yield_ready(tmp_path: Path) -> None:
    broker = _ready_broker(tmp_path)
    readiness = broker.readiness(WriteCapabilityId.BRANCH.value)
    assert readiness is not None
    assert readiness.state is CapabilityState.READY
    assert readiness.reason is None
    assert readiness.is_ready is True
    assert broker.is_ready(WriteCapabilityId.BRANCH.value) is True


def test_ready_capability_projects_onto_the_shared_status_model(tmp_path: Path) -> None:
    broker = _ready_broker(tmp_path)
    status = broker.status(WriteCapabilityId.BRANCH.value)
    assert status is not None
    assert status.capability_id == WriteCapabilityId.BRANCH.value
    assert status.state is CapabilityState.READY
    assert status.is_ready is True


def test_superset_permissions_deny_even_with_valid_material(tmp_path: Path) -> None:
    granted = dict(intended_permissions(WriteCapabilityId.BRANCH))
    granted["administration"] = "write"
    broker = _ready_broker(tmp_path, granted=granted)
    readiness = broker.readiness(WriteCapabilityId.BRANCH.value)
    assert readiness is not None
    assert readiness.state is CapabilityState.DENIED
    assert readiness.reason is MutationReasonCode.ADMINISTRATION_PERMISSION_PRESENT
    assert readiness.is_ready is False


def test_unattested_permissions_block_readiness(tmp_path: Path) -> None:
    path = _secret_file(tmp_path, "branch.token", SYNTHETIC_APP_MATERIAL)
    provider = _provider(WriteCapabilityId.BRANCH, _env_for(WriteCapabilityId.BRANCH, path))
    broker = WriteCapabilityBroker([provider], policy_allows={WriteCapabilityId.BRANCH: True})
    readiness = broker.readiness(WriteCapabilityId.BRANCH.value)
    assert readiness is not None
    assert readiness.is_ready is False
    assert readiness.permissions_attested is False


def test_a_healthy_read_path_never_implies_a_ready_write_path(tmp_path: Path) -> None:
    class _Probe:
        def probe(self) -> AuthorizationStatus:
            return AuthorizationStatus.READY

    read_broker = GitHubReadReadinessBroker(_Probe())
    write_broker = WriteCapabilityBroker()
    assert read_broker.is_ready(READ_CAPABILITY_ID) is True
    assert all(not write_broker.is_ready(c.value) for c in WriteCapabilityId)


def test_probe_exception_fails_closed() -> None:
    class _Exploding(_StubProvider):
        def probe(self) -> AuthorizationStatus:
            raise RuntimeError("backend down")

    broker = WriteCapabilityBroker([_Exploding(WriteCapabilityId.MERGE)])
    readiness = broker.readiness(WriteCapabilityId.MERGE.value)
    assert readiness is not None
    assert readiness.state is CapabilityState.UNAVAILABLE
    assert readiness.is_ready is False


def test_unknown_capability_id_yields_none(tmp_path: Path) -> None:
    broker = _ready_broker(tmp_path)
    for value in ("", "   ", "github.write", "github.write.delete", "github.admin", None):
        assert broker.readiness(value) is None  # type: ignore[arg-type]


def test_report_lists_every_write_capability_sorted(tmp_path: Path) -> None:
    broker = _ready_broker(tmp_path)
    rows = broker.report()
    assert [row["capability_id"] for row in rows] == list(write_capability_ids())
    assert READ_CAPABILITY_ID not in [row["capability_id"] for row in rows]


def test_broker_rejects_two_providers_for_the_same_capability() -> None:
    with pytest.raises(ValueError):
        WriteCapabilityBroker(
            [_StubProvider(WriteCapabilityId.PR), _StubProvider(WriteCapabilityId.PR)]
        )


# --------------------------------------------------------------------------- #
# authorization gate
# --------------------------------------------------------------------------- #


def test_authorize_returns_material_only_when_ready(tmp_path: Path) -> None:
    broker = _ready_broker(tmp_path)
    material = broker.authorize(WriteCapabilityId.BRANCH.value, IN_SCOPE)
    assert material.header_value().startswith("Bearer ")
    assert repr(material) == "GitHubAuthorization(<redacted>)"


def test_authorize_denies_when_not_ready() -> None:
    broker = WriteCapabilityBroker()
    with pytest.raises(WriteCapabilityError) as excinfo:
        broker.authorize(WriteCapabilityId.PR.value, IN_SCOPE)
    assert excinfo.value.reason is MutationReasonCode.WRITE_CAPABILITY_NOT_READY
    assert isinstance(excinfo.value, MutationDeniedError)


def test_authorize_denies_an_out_of_scope_repository(tmp_path: Path) -> None:
    broker = _ready_broker(tmp_path)
    with pytest.raises(WriteCapabilityError) as excinfo:
        broker.authorize(WriteCapabilityId.BRANCH.value, OUT_OF_SCOPE)
    assert excinfo.value.reason is MutationReasonCode.WRITE_CAPABILITY_NOT_READY


def test_authorize_denies_an_unknown_capability(tmp_path: Path) -> None:
    broker = _ready_broker(tmp_path)
    with pytest.raises(WriteCapabilityError) as excinfo:
        broker.authorize("github.write.delete", IN_SCOPE)
    assert excinfo.value.reason is MutationReasonCode.WRITE_CAPABILITY_MISMATCH


def test_denial_message_is_exactly_stage_and_reason() -> None:
    broker = WriteCapabilityBroker()
    with pytest.raises(WriteCapabilityError) as excinfo:
        broker.authorize(WriteCapabilityId.MERGE.value, IN_SCOPE)
    assert str(excinfo.value) == "CREDENTIAL:WRITE_CAPABILITY_NOT_READY"


# --------------------------------------------------------------------------- #
# material handling — opaque, classic-PAT rejection, file hardening
# --------------------------------------------------------------------------- #


def test_classic_pat_prefixed_is_rejected(tmp_path: Path) -> None:
    path = _secret_file(tmp_path, "classic.token", "ghp_" + "x" * 36)
    provider = _provider(WriteCapabilityId.BRANCH, _env_for(WriteCapabilityId.BRANCH, path))
    assert provider.probe() is AuthorizationStatus.CLASSIC_PAT_REJECTED


def test_classic_pat_legacy_40_hex_is_rejected(tmp_path: Path) -> None:
    path = _secret_file(tmp_path, "legacy.token", "a1b2c3d4" * 5)
    provider = _provider(WriteCapabilityId.BRANCH, _env_for(WriteCapabilityId.BRANCH, path))
    assert provider.probe() is AuthorizationStatus.CLASSIC_PAT_REJECTED


def test_provider_type_mismatch_is_rejected(tmp_path: Path) -> None:
    path = _secret_file(tmp_path, "fg.token", SYNTHETIC_FINE_GRAINED)
    provider = _provider(
        WriteCapabilityId.PR,
        _env_for(WriteCapabilityId.PR, path),
        provider_type=GitHubProviderType.GITHUB_APP,
    )
    assert provider.probe() is AuthorizationStatus.PROVIDER_TYPE_MISMATCH


def test_fine_grained_material_accepted_for_a_fine_grained_provider(tmp_path: Path) -> None:
    path = _secret_file(tmp_path, "fg.token", SYNTHETIC_FINE_GRAINED)
    provider = _provider(
        WriteCapabilityId.PR,
        _env_for(WriteCapabilityId.PR, path),
        provider_type=GitHubProviderType.FINE_GRAINED_TOKEN,
    )
    assert provider.probe() is AuthorizationStatus.READY


def test_long_stateless_installation_token_is_accepted(tmp_path: Path) -> None:
    """No length/charset expectation: 2026 ``ghs_<app-id>_<jwt>`` is variable."""
    synthetic = "ghs_1234567890_" + ("aA1-_." * 120)
    assert len(synthetic) > 520
    path = _secret_file(tmp_path, "long.token", synthetic)
    provider = _provider(WriteCapabilityId.MERGE, _env_for(WriteCapabilityId.MERGE, path))
    assert provider.probe() is AuthorizationStatus.READY


def test_source_declares_no_material_length_expectation() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "_MAX_MATERIAL_LENGTH" not in source


def test_group_or_other_readable_secret_file_is_rejected(tmp_path: Path) -> None:
    path = _secret_file(tmp_path, "open.token", SYNTHETIC_APP_MATERIAL, mode=0o644)
    provider = _provider(WriteCapabilityId.BRANCH, _env_for(WriteCapabilityId.BRANCH, path))
    assert provider.probe() is AuthorizationStatus.FILE_PERMISSIONS_TOO_OPEN
    assert stat.S_IMODE(path.stat().st_mode) & 0o077


def test_symlinked_secret_file_is_rejected(tmp_path: Path) -> None:
    real = _secret_file(tmp_path, "real.token", SYNTHETIC_APP_MATERIAL)
    link = tmp_path / "link.token"
    link.symlink_to(real)
    provider = _provider(WriteCapabilityId.BRANCH, _env_for(WriteCapabilityId.BRANCH, link))
    assert provider.probe() is AuthorizationStatus.FILE_NOT_REGULAR


def test_missing_file_is_unreadable_not_ready(tmp_path: Path) -> None:
    provider = _provider(
        WriteCapabilityId.BRANCH,
        _env_for(WriteCapabilityId.BRANCH, tmp_path / "absent.token"),
    )
    assert provider.probe() is AuthorizationStatus.FILE_UNREADABLE


def test_empty_and_truncated_files_are_rejected(tmp_path: Path) -> None:
    empty = _secret_file(tmp_path, "empty.token", "   ")
    short = _secret_file(tmp_path, "short.token", "ghs_abc")
    assert (
        _provider(WriteCapabilityId.BRANCH, _env_for(WriteCapabilityId.BRANCH, empty)).probe()
        is AuthorizationStatus.FILE_EMPTY
    )
    assert (
        _provider(WriteCapabilityId.BRANCH, _env_for(WriteCapabilityId.BRANCH, short)).probe()
        is AuthorizationStatus.MATERIAL_MALFORMED
    )


def test_oversized_material_is_rejected(tmp_path: Path) -> None:
    path = _secret_file(tmp_path, "big.token", "ghs_" + "y" * 9000)
    provider = _provider(WriteCapabilityId.BRANCH, _env_for(WriteCapabilityId.BRANCH, path))
    assert provider.probe() is AuthorizationStatus.MATERIAL_MALFORMED


def test_material_is_not_cached_between_resolutions(tmp_path: Path) -> None:
    path = _secret_file(tmp_path, "rotate.token", SYNTHETIC_APP_MATERIAL)
    provider = _provider(WriteCapabilityId.BRANCH, _env_for(WriteCapabilityId.BRANCH, path))
    assert provider.resolve(WriteCapabilityId.BRANCH.value, IN_SCOPE) is not None
    path.write_text("ghp_" + "z" * 36, encoding="utf-8")
    os.chmod(path, 0o600)
    assert provider.resolve(WriteCapabilityId.BRANCH.value, IN_SCOPE) is None
    assert provider.last_status is AuthorizationStatus.CLASSIC_PAT_REJECTED


def test_out_of_scope_repository_resolves_to_nothing(tmp_path: Path) -> None:
    path = _secret_file(tmp_path, "scope.token", SYNTHETIC_APP_MATERIAL)
    provider = _provider(WriteCapabilityId.BRANCH, _env_for(WriteCapabilityId.BRANCH, path))
    assert provider.resolve(WriteCapabilityId.BRANCH.value, OUT_OF_SCOPE) is None
    assert provider.last_status is AuthorizationStatus.REPOSITORY_OUT_OF_SCOPE


def test_malformed_repository_reference_is_out_of_scope(tmp_path: Path) -> None:
    path = _secret_file(tmp_path, "scope.token", SYNTHETIC_APP_MATERIAL)
    provider = _provider(WriteCapabilityId.BRANCH, _env_for(WriteCapabilityId.BRANCH, path))
    for repository in ("", "no-slash", "a/b/c", 42):
        assert provider.resolve(WriteCapabilityId.BRANCH.value, repository) is None  # type: ignore[arg-type]
        assert provider.last_status is AuthorizationStatus.REPOSITORY_OUT_OF_SCOPE


# --------------------------------------------------------------------------- #
# A3-14 — no credential exposure
# --------------------------------------------------------------------------- #


def test_provider_repr_and_str_are_redacted(tmp_path: Path) -> None:
    path = _secret_file(tmp_path, "redact.token", SYNTHETIC_APP_MATERIAL)
    provider = _provider(WriteCapabilityId.BRANCH, _env_for(WriteCapabilityId.BRANCH, path))
    for text in (repr(provider), str(provider)):
        assert SYNTHETIC_APP_MATERIAL not in text
        assert str(path) not in text
        assert "<redacted>" in text


def test_describe_leaks_no_value_path_or_env_name(tmp_path: Path) -> None:
    path = _secret_file(tmp_path, "describe.token", SYNTHETIC_APP_MATERIAL)
    provider = _provider(WriteCapabilityId.BRANCH, _env_for(WriteCapabilityId.BRANCH, path))
    described = provider.describe()
    blob = repr(described)
    assert SYNTHETIC_APP_MATERIAL not in blob
    assert str(path) not in blob
    assert WRITE_SECRET_NAMES[WriteCapabilityId.BRANCH] not in blob
    assert described["capability_id"] == WriteCapabilityId.BRANCH.value


def test_readiness_and_report_carry_no_secret_derived_field(tmp_path: Path) -> None:
    broker = _ready_broker(tmp_path)
    readiness = broker.readiness(WriteCapabilityId.BRANCH.value)
    assert readiness is not None
    blob = repr(readiness) + repr(readiness.sanitized()) + repr(broker.report())
    assert SYNTHETIC_APP_MATERIAL not in blob
    assert str(tmp_path) not in blob
    for token in ("token", "secret", "material", "authorization", "password"):
        assert token not in blob.lower()


def test_broker_repr_is_bounded_and_secret_free(tmp_path: Path) -> None:
    broker = _ready_broker(tmp_path)
    assert repr(broker) == "WriteCapabilityBroker(capabilities=1)"
    assert str(broker) == repr(broker)


def test_denial_never_carries_a_provider_status_or_path(tmp_path: Path) -> None:
    path = _secret_file(tmp_path, "denied.token", "ghp_" + "q" * 36)
    provider = _provider(WriteCapabilityId.BRANCH, _env_for(WriteCapabilityId.BRANCH, path))
    broker = WriteCapabilityBroker(
        [provider],
        attested_permissions={
            WriteCapabilityId.BRANCH: dict(intended_permissions(WriteCapabilityId.BRANCH))
        },
        policy_allows={WriteCapabilityId.BRANCH: True},
    )
    with pytest.raises(WriteCapabilityError) as excinfo:
        broker.authorize(WriteCapabilityId.BRANCH.value, IN_SCOPE)
    message = str(excinfo.value)
    assert message == "CREDENTIAL:WRITE_CAPABILITY_NOT_READY"
    assert str(path) not in message
    assert "ghp_" not in message


def test_module_source_contains_no_credential_value_literal() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")
    for marker in ("ghp_", "gho_", "ghs_ey", "github_pat_1"):
        assert marker not in source


def test_readiness_dataclass_is_frozen(tmp_path: Path) -> None:
    broker = _ready_broker(tmp_path)
    readiness = broker.readiness(WriteCapabilityId.BRANCH.value)
    assert isinstance(readiness, WriteCapabilityReadiness)
    with pytest.raises(AttributeError):
        readiness.state = CapabilityState.READY  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# broker/Vault abstraction readiness
# --------------------------------------------------------------------------- #


def test_file_provider_satisfies_the_write_material_protocol(tmp_path: Path) -> None:
    path = _secret_file(tmp_path, "proto.token", SYNTHETIC_APP_MATERIAL)
    provider = _provider(WriteCapabilityId.BRANCH, _env_for(WriteCapabilityId.BRANCH, path))
    assert isinstance(provider, WriteMaterialProvider)


def test_an_alternative_backend_needs_no_broker_change() -> None:
    """A future Vault/secret-manager backend only implements the protocol."""
    stub = _StubProvider(WriteCapabilityId.MERGE)
    assert isinstance(stub, WriteMaterialProvider)
    broker = WriteCapabilityBroker(
        [stub],
        attested_permissions={
            WriteCapabilityId.MERGE: dict(intended_permissions(WriteCapabilityId.MERGE))
        },
        policy_allows={WriteCapabilityId.MERGE: True},
    )
    assert broker.is_ready(WriteCapabilityId.MERGE.value) is True
    assert broker.authorize(WriteCapabilityId.MERGE.value, IN_SCOPE) is not None
    assert stub.resolve_calls == 1


def test_module_imports_no_vault_dependency() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            assert "vault" not in stripped.lower()
            assert "hvac" not in stripped.lower()


def test_provider_constructor_rejects_invalid_arguments() -> None:
    with pytest.raises(ValueError):
        FileWriteMaterialProvider(capability="github.write.branch", scope=SCOPE)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        FileWriteMaterialProvider(
            capability=WriteCapabilityId.BRANCH,
            scope=SCOPE,
            provider_type="github_app",  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError):
        FileWriteMaterialProvider(
            capability=WriteCapabilityId.BRANCH, scope=SCOPE, secret_name="  "
        )


# --------------------------------------------------------------------------- #
# destructive exclusion (L1 half: no admin credential surface)
# --------------------------------------------------------------------------- #


def test_no_admin_capability_is_representable() -> None:
    values = {member.value for member in WriteCapabilityId}
    assert "github.admin" not in values
    assert not any("admin" in value for value in values)


def test_module_declares_no_repository_deletion_surface() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8").lower()
    for marker in ("delete_repository", '"delete"', "'delete'"):
        assert marker not in source
