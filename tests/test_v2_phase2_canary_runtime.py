"""Tests for the Phase 2 canary runtime: secret provider, readiness broker,
canary router, provider attestation and the connected collector harness.

Everything here is hermetic. No real GitHub credential, no network, no Hermes.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sqlite3
import stat
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from hermes_mcp_bridge import contracts
from hermes_mcp_bridge.v2.enums import CapabilityState
from hermes_mcp_bridge.v2.github_attestation import (
    ATTESTATION_INPUT_SCHEMA,
    REQUIRED_PERMISSIONS,
    AttestationError,
    ProviderAttestationInput,
    attest_provider,
    load_attestation_input,
)
from hermes_mcp_bridge.v2.github_canary import (
    ExecutionPath,
    FallbackReason,
    GitHubCanaryConfig,
    GitHubCanaryRouter,
)
from hermes_mcp_bridge.v2.github_direct import (
    GITHUB_DIRECT_DEFAULT_RESULT_FIELDS,
    GitHubDirectDenied,
    GitHubDirectReadExecutor,
    GitHubRepositoryScope,
)
from hermes_mcp_bridge.v2.github_readiness import GitHubReadReadinessBroker
from hermes_mcp_bridge.v2.github_registry import (
    GITHUB_DIRECT_READ_TOOL_IDS,
    build_github_direct_read_registry,
    github_direct_read_policy_rules,
)
from hermes_mcp_bridge.v2.github_secret_provider import (
    AuthorizationStatus,
    FileGitHubAuthorizationProvider,
    GitHubProviderType,
    MaterialClass,
    classify_material,
)

ROOT = Path(__file__).resolve().parents[1]
COLLECTOR = ROOT / "scripts" / "v2_phase2_direct_read_acceptance.py"
REPOSITORY = "pestoura/hermes-mcp-bridge"
FINE_GRAINED = "github_pat_" + "A" * 40
APP_TOKEN = "ghs_" + "B" * 36
CLASSIC = "ghp_" + "C" * 36
CLASSIC_LEGACY = "0123456789abcdef" * 2 + "01234567"
# Stateless GitHub App installation token shape rolled out during 2026:
# ``ghs_<app-id>_<jwt>``. Synthetic, non-functional, > 520 characters, and it
# uses the dots/dashes/underscores the new format allows. No real token is used.
APP_TOKEN_STATELESS = (
    "ghs_123456_"
    + "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9."
    + ("aB3-dE5_fG7hI9jK" * 30)
    + ".sIgNaTuRe-PaRt_0123456789"
)


def _load_collector() -> Any:
    spec = importlib.util.spec_from_file_location("phase2_collector", COLLECTOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # dataclass(slots=True) rebuilds the class and looks the defining module up
    # in sys.modules, so the module must be registered before execution.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _secret_file(tmp_path: Path, value: str, mode: int = 0o600) -> Path:
    path = tmp_path / "gh-token"
    path.write_text(value, encoding="utf-8")
    os.chmod(path, mode)
    return path


def _provider(
    tmp_path: Path,
    *,
    value: str = FINE_GRAINED,
    mode: int = 0o600,
    provider_type: GitHubProviderType = GitHubProviderType.FINE_GRAINED_TOKEN,
    use_env_value: bool = False,
    repositories: tuple[str, ...] = (REPOSITORY,),
) -> FileGitHubAuthorizationProvider:
    env: dict[str, str] = {}
    if use_env_value:
        env["BRIDGE_V2_GITHUB_DIRECT_READ_TOKEN"] = value
    else:
        env["BRIDGE_V2_GITHUB_DIRECT_READ_TOKEN_FILE"] = str(_secret_file(tmp_path, value, mode))
    return FileGitHubAuthorizationProvider(
        scope=GitHubRepositoryScope(repositories),
        provider_type=provider_type,
        env=env,
    )


# ---------------------------------------------------------------------------
# 1. secret provider
# ---------------------------------------------------------------------------


def test_fine_grained_material_resolves_at_the_boundary(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    assert provider.probe() is AuthorizationStatus.READY
    material = provider.resolve("github.read", REPOSITORY)
    assert material is not None
    assert material.header_value() == f"Bearer {FINE_GRAINED}"


def test_classic_pat_is_rejected(tmp_path: Path) -> None:
    provider = _provider(tmp_path, value=CLASSIC)
    assert provider.probe() is AuthorizationStatus.CLASSIC_PAT_REJECTED
    assert provider.resolve("github.read", REPOSITORY) is None


def test_legacy_unprefixed_classic_pat_is_rejected(tmp_path: Path) -> None:
    provider = _provider(tmp_path, value=CLASSIC_LEGACY)
    assert provider.probe() is AuthorizationStatus.CLASSIC_PAT_REJECTED


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (FINE_GRAINED, MaterialClass.FINE_GRAINED_TOKEN),
        (APP_TOKEN, MaterialClass.GITHUB_APP),
        (CLASSIC, MaterialClass.CLASSIC_PAT),
        (CLASSIC_LEGACY, MaterialClass.CLASSIC_PAT),
        ("x" * 30, MaterialClass.UNKNOWN),
    ],
)
def test_material_classification(value: str, expected: MaterialClass) -> None:
    assert classify_material(value) is expected


def test_provider_type_mismatch_is_rejected(tmp_path: Path) -> None:
    provider = _provider(
        tmp_path,
        value=FINE_GRAINED,
        provider_type=GitHubProviderType.GITHUB_APP,
    )
    assert provider.probe() is AuthorizationStatus.PROVIDER_TYPE_MISMATCH
    assert provider.resolve("github.read", REPOSITORY) is None


def test_stateless_ghs_token_over_520_chars_is_accepted(tmp_path: Path) -> None:
    """2026 stateless ``ghs_<app-id>_<jwt>`` material is opaque and length-free."""
    assert len(APP_TOKEN_STATELESS) > 520
    assert classify_material(APP_TOKEN_STATELESS) is MaterialClass.GITHUB_APP
    provider = _provider(
        tmp_path,
        value=APP_TOKEN_STATELESS,
        provider_type=GitHubProviderType.GITHUB_APP,
    )
    assert provider.probe() is AuthorizationStatus.READY
    material = provider.resolve("github.read", REPOSITORY)
    assert material is not None
    assert material.header_value() == f"Bearer {APP_TOKEN_STATELESS}"


def test_provider_has_no_exact_length_expectation() -> None:
    source = (ROOT / "src" / "hermes_mcp_bridge" / "v2" / "github_secret_provider.py").read_text(
        encoding="utf-8"
    )
    assert "_MAX_MATERIAL_LENGTH" not in source
    assert "_MAX_MATERIAL_BYTES = 8192" in source


def test_classic_pat_rejection_survives_the_relaxed_bound(tmp_path: Path) -> None:
    for value in (CLASSIC, CLASSIC_LEGACY, "gho_" + "D" * 600):
        provider = _provider(tmp_path, value=value)
        assert provider.probe() is AuthorizationStatus.CLASSIC_PAT_REJECTED


def test_material_beyond_the_resource_bound_is_rejected(tmp_path: Path) -> None:
    provider = _provider(
        tmp_path,
        value="ghs_" + "E" * 9000,
        provider_type=GitHubProviderType.GITHUB_APP,
    )
    assert provider.probe() is AuthorizationStatus.MATERIAL_MALFORMED


def test_env_supplied_value_is_rejected(tmp_path: Path) -> None:
    provider = _provider(tmp_path, use_env_value=True)
    assert provider.probe() is AuthorizationStatus.ENV_MATERIAL_REJECTED


def test_missing_configuration_is_not_configured() -> None:
    provider = FileGitHubAuthorizationProvider(
        scope=GitHubRepositoryScope([REPOSITORY]),
        provider_type=GitHubProviderType.FINE_GRAINED_TOKEN,
        env={},
    )
    assert provider.probe() is AuthorizationStatus.NOT_CONFIGURED
    assert provider.resolve("github.read", REPOSITORY) is None


def test_missing_token_file_is_unreadable(tmp_path: Path) -> None:
    provider = FileGitHubAuthorizationProvider(
        scope=GitHubRepositoryScope([REPOSITORY]),
        provider_type=GitHubProviderType.FINE_GRAINED_TOKEN,
        env={
            "BRIDGE_V2_GITHUB_DIRECT_READ_TOKEN_FILE": str(tmp_path / "absent"),
        },
    )
    assert provider.probe() is AuthorizationStatus.FILE_UNREADABLE


def test_empty_token_file_is_rejected(tmp_path: Path) -> None:
    provider = _provider(tmp_path, value="   ")
    assert provider.probe() is AuthorizationStatus.FILE_EMPTY


def test_world_readable_token_file_is_rejected(tmp_path: Path) -> None:
    provider = _provider(tmp_path, mode=0o644)
    assert provider.probe() is AuthorizationStatus.FILE_PERMISSIONS_TOO_OPEN
    assert provider.resolve("github.read", REPOSITORY) is None


def test_symlinked_token_file_is_rejected(tmp_path: Path) -> None:
    real = _secret_file(tmp_path, FINE_GRAINED)
    link = tmp_path / "link"
    link.symlink_to(real)
    provider = FileGitHubAuthorizationProvider(
        scope=GitHubRepositoryScope([REPOSITORY]),
        provider_type=GitHubProviderType.FINE_GRAINED_TOKEN,
        env={"BRIDGE_V2_GITHUB_DIRECT_READ_TOKEN_FILE": str(link)},
    )
    assert provider.probe() is AuthorizationStatus.FILE_NOT_REGULAR


def test_secret_file_read_is_not_lstat_then_open(tmp_path: Path) -> None:
    """Point 1: validation must run on the same fd that is read.

    An adversarial ``os.lstat``/``os.stat`` that reports a perfectly fine
    regular 0600 file must not be able to influence the outcome, because the
    provider is not allowed to consult it at all.
    """
    provider = _provider(tmp_path, mode=0o644)
    calls: list[str] = []

    class _FakeStat:
        st_mode = stat.S_IFREG | 0o600
        st_size = 64

    real_lstat, real_stat = os.lstat, os.stat

    def fake_lstat(*args: Any, **kwargs: Any) -> Any:
        calls.append("lstat")
        return _FakeStat()

    def fake_stat(*args: Any, **kwargs: Any) -> Any:
        calls.append("stat")
        return _FakeStat()

    os.lstat, os.stat = fake_lstat, fake_stat  # type: ignore[assignment]
    try:
        status = provider.probe()
    finally:
        os.lstat, os.stat = real_lstat, real_stat  # type: ignore[assignment]

    assert calls == []
    assert status is AuthorizationStatus.FILE_PERMISSIONS_TOO_OPEN


def test_secret_file_validation_uses_fstat_on_the_opened_fd(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    observed: list[int] = []
    real_fstat = os.fstat
    real_open = os.open
    opened: list[int] = []

    def spy_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        fd = real_open(path, flags, *args, **kwargs)
        opened.append(fd)
        # O_NOFOLLOW must be requested whenever the platform provides it.
        assert not hasattr(os, "O_NOFOLLOW") or flags & os.O_NOFOLLOW
        return fd

    def spy_fstat(fd: int) -> Any:
        observed.append(fd)
        return real_fstat(fd)

    os.open, os.fstat = spy_open, spy_fstat  # type: ignore[assignment]
    try:
        assert provider.probe() is AuthorizationStatus.READY
    finally:
        os.open, os.fstat = real_open, real_fstat  # type: ignore[assignment]

    assert opened, "the provider must open the secret through os.open"
    assert observed == opened, "validation must use fstat on the same descriptor"


def test_symlink_substitution_is_not_followed(tmp_path: Path) -> None:
    """Point 1: swapping the path for a symlink must never yield material."""
    secret = tmp_path / "real-secret"
    secret.write_text(FINE_GRAINED, encoding="utf-8")
    os.chmod(secret, 0o600)
    target = tmp_path / "gh-token"
    target.symlink_to(secret)

    provider = FileGitHubAuthorizationProvider(
        scope=GitHubRepositoryScope([REPOSITORY]),
        provider_type=GitHubProviderType.FINE_GRAINED_TOKEN,
        env={"BRIDGE_V2_GITHUB_DIRECT_READ_TOKEN_FILE": str(target)},
    )
    assert provider.probe() is AuthorizationStatus.FILE_NOT_REGULAR
    assert provider.resolve("github.read", REPOSITORY) is None


def test_substituted_inode_is_revalidated_on_every_read(tmp_path: Path) -> None:
    """A file swapped to world-readable between two reads is rejected on the second."""
    provider = _provider(tmp_path)
    assert provider.probe() is AuthorizationStatus.READY

    target = tmp_path / "gh-token"
    target.unlink()
    replacement = tmp_path / "replacement"
    replacement.write_text(FINE_GRAINED, encoding="utf-8")
    os.chmod(replacement, 0o644)
    replacement.rename(target)

    assert provider.probe() is AuthorizationStatus.FILE_PERMISSIONS_TOO_OPEN


def test_secret_provider_has_no_separate_lstat_open_pair() -> None:
    source = (
        ROOT / "src" / "hermes_mcp_bridge" / "v2" / "github_secret_provider.py"
    ).read_text(encoding="utf-8")
    assert "os.lstat(" not in source
    assert "os.stat(" not in source
    # the only open is the fd-based one, and it requests O_NOFOLLOW
    assert "with open(" not in source
    assert "os.open(target, flags)" in source
    assert "O_NOFOLLOW" in source
    assert "os.fstat(fd)" in source


def test_directory_as_secret_file_is_rejected(tmp_path: Path) -> None:
    directory = tmp_path / "as-dir"
    directory.mkdir(mode=0o700)
    provider = FileGitHubAuthorizationProvider(
        scope=GitHubRepositoryScope([REPOSITORY]),
        provider_type=GitHubProviderType.FINE_GRAINED_TOKEN,
        env={"BRIDGE_V2_GITHUB_DIRECT_READ_TOKEN_FILE": str(directory)},
    )
    assert provider.probe() in {
        AuthorizationStatus.FILE_NOT_REGULAR,
        AuthorizationStatus.FILE_UNREADABLE,
    }


def test_secret_provider_failures_never_leak_the_path(tmp_path: Path) -> None:
    provider = _provider(tmp_path, mode=0o644)
    status = provider.probe()
    rendered = json.dumps(provider.describe(), sort_keys=True)
    assert str(tmp_path) not in rendered
    assert str(tmp_path) not in status.value
    assert FINE_GRAINED not in rendered


def test_out_of_scope_repository_never_reads_material(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    assert provider.resolve("github.read", "other/repo") is None
    assert provider.last_status is AuthorizationStatus.REPOSITORY_OUT_OF_SCOPE


def test_wrong_capability_is_rejected(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    assert provider.resolve("github.write", REPOSITORY) is None
    assert provider.last_status is AuthorizationStatus.CAPABILITY_MISMATCH


def test_provider_never_exposes_value_or_path(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    provider.probe()
    rendered = json.dumps(provider.describe(), sort_keys=True)
    for text in (repr(provider), str(provider), rendered):
        assert FINE_GRAINED not in text
        assert str(tmp_path) not in text
        assert "_FILE" not in text
    assert "secret_path" not in provider.describe()


def test_rotation_takes_effect_without_reconstruction(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    assert provider.probe() is AuthorizationStatus.READY
    path = tmp_path / "gh-token"
    path.write_text(CLASSIC, encoding="utf-8")
    os.chmod(path, 0o600)
    assert provider.probe() is AuthorizationStatus.CLASSIC_PAT_REJECTED


def test_token_file_mode_is_actually_restricted(tmp_path: Path) -> None:
    path = _secret_file(tmp_path, FINE_GRAINED)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


# ---------------------------------------------------------------------------
# 2. readiness broker
# ---------------------------------------------------------------------------


def test_readiness_broker_exposes_only_status(tmp_path: Path) -> None:
    broker = GitHubReadReadinessBroker(_provider(tmp_path))
    status = broker.status("github.read")
    assert status is not None
    assert status.state is CapabilityState.READY
    assert broker.is_ready("github.read") is True
    payload = json.dumps(status.canonical(), sort_keys=True)
    assert FINE_GRAINED not in payload
    assert set(status.canonical()) == {
        "capability_id",
        "provider",
        "state",
        "version",
    }


def test_readiness_broker_has_no_secret_material_api(tmp_path: Path) -> None:
    broker = GitHubReadReadinessBroker(_provider(tmp_path))
    for name in ("resolve", "material", "token", "secret", "header_value"):
        assert not hasattr(broker, name)


@pytest.mark.parametrize(
    ("value", "mode", "expected"),
    [
        (CLASSIC, 0o600, CapabilityState.DENIED),
        (FINE_GRAINED, 0o644, CapabilityState.DENIED),
        ("   ", 0o600, CapabilityState.UNAVAILABLE),
    ],
)
def test_readiness_broker_fails_closed(
    tmp_path: Path,
    value: str,
    mode: int,
    expected: CapabilityState,
) -> None:
    broker = GitHubReadReadinessBroker(_provider(tmp_path, value=value, mode=mode))
    status = broker.status("github.read")
    assert status is not None and status.state is expected
    assert broker.is_ready("github.read") is False


def test_readiness_broker_rejects_unknown_capability(tmp_path: Path) -> None:
    broker = GitHubReadReadinessBroker(_provider(tmp_path))
    assert broker.status("github.write") is None
    assert broker.is_ready("github.write") is False


# ---------------------------------------------------------------------------
# 3. canary router
# ---------------------------------------------------------------------------


def _executor(
    tmp_path: Path,
    handler: Any,
    *,
    repositories: tuple[str, ...] = (REPOSITORY,),
) -> tuple[GitHubDirectReadExecutor, FileGitHubAuthorizationProvider]:
    provider = _provider(tmp_path, repositories=repositories)
    scope = GitHubRepositoryScope(repositories)
    executor = GitHubDirectReadExecutor(
        registry=build_github_direct_read_registry(),
        rules=github_direct_read_policy_rules(),
        credential_broker=GitHubReadReadinessBroker(provider),
        authorization_provider=provider,
        scope=scope,
        transport=httpx.MockTransport(handler),
    )
    return executor, provider


def _repo_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "full_name": REPOSITORY,
            "private": True,
            "default_branch": "main",
            "archived": False,
            "visibility": "private",
            "html_url": f"https://github.com/{REPOSITORY}",
            "updated_at": "2026-08-08T00:00:00Z",
        },
    )


def _router(
    tmp_path: Path,
    *,
    enabled: bool,
    handler: Any = _repo_handler,
) -> GitHubCanaryRouter:
    executor, provider = _executor(tmp_path, handler)
    return GitHubCanaryRouter(
        config=GitHubCanaryConfig(
            scope=GitHubRepositoryScope([REPOSITORY]),
            enabled=enabled,
        ),
        executor=executor,
        readiness=GitHubReadReadinessBroker(provider),
    )


def test_canary_is_disabled_by_default() -> None:
    config = GitHubCanaryConfig(scope=GitHubRepositoryScope([REPOSITORY]))
    assert config.enabled is False
    assert config.describe()["direct_feature_enabled"] is False


def test_disabled_canary_falls_back_to_v1_without_executing(tmp_path: Path) -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        nonlocal called
        called = True
        return _repo_handler(request)

    router = _router(tmp_path, enabled=False, handler=handler)

    async def op(executor: GitHubDirectReadExecutor):  # pragma: no cover
        raise AssertionError("DIRECT must not run when the canary is disabled")

    decision = asyncio.run(router.route("github.get_repo", REPOSITORY, op))
    assert decision.path is ExecutionPath.V1_FALLBACK
    assert decision.fallback_reason is FallbackReason.FEATURE_DISABLED
    assert decision.eligible_for_direct is False
    assert called is False


def test_enabled_canary_executes_direct(tmp_path: Path) -> None:
    router = _router(tmp_path, enabled=True)

    async def op(executor: GitHubDirectReadExecutor):
        owner, repo = REPOSITORY.split("/")
        return await executor.get_repo(owner, repo)

    decision = asyncio.run(router.route("github.get_repo", REPOSITORY, op))
    assert decision.path is ExecutionPath.DIRECT
    assert decision.succeeded
    assert decision.result is not None
    assert decision.result.data["full_name"] == REPOSITORY


def test_out_of_allowlist_repository_falls_back(tmp_path: Path) -> None:
    router = _router(tmp_path, enabled=True)

    async def op(executor: GitHubDirectReadExecutor):  # pragma: no cover
        raise AssertionError("must not execute out of allow-list")

    decision = asyncio.run(router.route("github.get_repo", "other/repo", op))
    assert decision.path is ExecutionPath.V1_FALLBACK
    assert decision.fallback_reason is FallbackReason.REPOSITORY_NOT_ALLOWED


def test_not_ready_credential_falls_back(tmp_path: Path) -> None:
    executor, _ = _executor(tmp_path, _repo_handler)
    broken_dir = tmp_path / "broken"
    broken_dir.mkdir(exist_ok=True)
    broken = _provider(broken_dir, value=CLASSIC)
    router = GitHubCanaryRouter(
        config=GitHubCanaryConfig(
            scope=GitHubRepositoryScope([REPOSITORY]),
            enabled=True,
        ),
        executor=executor,
        readiness=GitHubReadReadinessBroker(broken),
    )

    async def op(executor_: GitHubDirectReadExecutor):  # pragma: no cover
        raise AssertionError("must not execute with a not-ready credential")

    decision = asyncio.run(router.route("github.get_repo", REPOSITORY, op))
    assert decision.fallback_reason is FallbackReason.CREDENTIAL_NOT_READY


def test_direct_failure_is_never_silently_relabelled(tmp_path: Path) -> None:
    router = _router(tmp_path, enabled=True)

    async def op(executor: GitHubDirectReadExecutor):
        raise GitHubDirectDenied("RESOURCE_SCOPE_DENIED")

    decision = asyncio.run(router.route("github.get_repo", REPOSITORY, op))
    assert decision.path is ExecutionPath.DIRECT
    assert decision.eligible_for_direct is True
    assert decision.succeeded is False
    assert decision.error_code == "RESOURCE_SCOPE_DENIED"
    assert decision.fallback_reason is FallbackReason.NONE


def test_canary_rejects_tools_outside_the_direct_read_set() -> None:
    with pytest.raises(ValueError):
        GitHubCanaryConfig(
            scope=GitHubRepositoryScope([REPOSITORY]),
            tool_ids=frozenset({"github.create_issue"}),
        )


def test_canary_describe_has_no_wildcards_and_no_secret(tmp_path: Path) -> None:
    config = GitHubCanaryConfig(
        scope=GitHubRepositoryScope([REPOSITORY]),
        enabled=True,
    )
    described = json.dumps(config.describe(), sort_keys=True)
    assert "*" not in described
    assert FINE_GRAINED not in described
    assert config.describe()["canary_tool_ids"] == sorted(GITHUB_DIRECT_READ_TOOL_IDS)


# ---------------------------------------------------------------------------
# 4. V1 isolation
# ---------------------------------------------------------------------------


def test_v1_tool_contract_is_unchanged_by_the_canary() -> None:
    version = contracts.CURRENT_CONTRACT_VERSION
    expected = len(contracts.required_tools(version))
    import hermes_mcp_bridge.v2.github_canary
    import hermes_mcp_bridge.v2.github_readiness
    import hermes_mcp_bridge.v2.github_secret_provider  # noqa: F401

    assert len(contracts.required_tools(version)) == expected == 27


def test_no_v1_module_imports_the_canary_runtime() -> None:
    src = ROOT / "src" / "hermes_mcp_bridge"
    tokens = ("github_canary", "github_secret_provider", "github_readiness")
    for path in src.rglob("*.py"):
        if "v2" in path.relative_to(src).parts:
            continue
        text = path.read_text(encoding="utf-8")
        for token in tokens:
            assert token not in text, f"{path.name} imports {token}"


# ---------------------------------------------------------------------------
# 5. provider attestation
# ---------------------------------------------------------------------------


DEFAULT_BRANCH = "main"


def _attestation_handler(
    *,
    oauth_scopes: str | None = None,
    repo_permissions: dict[str, bool] | None = None,
    installation_repositories: list[str] | None = None,
    default_branch: str = DEFAULT_BRANCH,
    fail_path: str | None = None,
    fail_status: int = 403,
) -> Any:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen.append(path)
        if fail_path is not None and path == fail_path:
            return httpx.Response(fail_status, json={})
        if path == "/rate_limit":
            headers = {"x-oauth-scopes": oauth_scopes} if oauth_scopes else {}
            return httpx.Response(200, json={"rate": {}}, headers=headers)
        if path == f"/repos/{REPOSITORY}":
            body: dict[str, Any] = {
                "full_name": REPOSITORY,
                "default_branch": default_branch,
            }
            # A repo `permissions` block reporting admin must NOT be treated as
            # token capability; it is the principal's computed role.
            body["permissions"] = repo_permissions or {
                "admin": True,
                "maintain": True,
                "push": True,
                "pull": True,
            }
            return httpx.Response(200, json=body)
        if path == f"/repos/{REPOSITORY}/pulls":
            return httpx.Response(200, json=[{"number": 1}])
        if path == f"/repos/{REPOSITORY}/issues":
            return httpx.Response(200, json=[{"number": 2}])
        if path == f"/repos/{REPOSITORY}/commits/{default_branch}/check-runs":
            return httpx.Response(200, json={"total_count": 3, "check_runs": []})
        if path == "/installation/repositories":
            names = installation_repositories or [REPOSITORY]
            return httpx.Response(
                200,
                json={
                    "total_count": len(names),
                    "repositories": [{"full_name": name} for name in names],
                },
            )
        return httpx.Response(404, json={})

    handler.seen = seen  # type: ignore[attr-defined]
    return handler


def _declaration(
    *,
    provider_type: str = "fine_grained_token",
    permissions: dict[str, str] | None = None,
    unexpected: list[str] | None = None,
    repositories: list[str] | None = None,
    source: str = "github_settings_ui",
) -> ProviderAttestationInput:
    return ProviderAttestationInput(
        provider_type=provider_type,
        permissions=dict(permissions or REQUIRED_PERMISSIONS),
        unexpected_permissions=list(unexpected or []),
        repository_scopes=sorted(repositories or [REPOSITORY]),
        confirmation=True,
        confirmation_source=source,
        confirmed_at="2026-08-08T10:00:00+00:00",
    )


def _declaration_document(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema": ATTESTATION_INPUT_SCHEMA,
        "provider_type": "fine_grained_token",
        "permissions": dict(REQUIRED_PERMISSIONS),
        "unexpected_permissions": [],
        "repository_scopes": [REPOSITORY],
        "confirmation": True,
        "confirmation_source": "github_settings_ui",
        "confirmed_at": "2026-08-08T10:00:00+00:00",
    }
    document.update(overrides)
    return document


def _write_declaration(tmp_path: Path, **overrides: Any) -> Path:
    path = tmp_path / "attestation.json"
    path.write_text(json.dumps(_declaration_document(**overrides)), encoding="utf-8")
    return path


def test_attestation_accepts_least_privilege_fine_grained(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    attestation = attest_provider(
        provider,
        repositories=[REPOSITORY],
        declaration=_declaration(),
        transport=httpx.MockTransport(_attestation_handler()),
    )
    evidence = attestation.evidence()
    assert evidence["provider_type"] == "fine_grained_token"
    assert evidence["authenticated"] is True
    assert evidence["least_privilege"] is True
    assert evidence["broad_pat"] is False
    assert evidence["permissions"] == REQUIRED_PERMISSIONS
    assert evidence["unexpected_permissions"] == []
    assert evidence["repository_scopes"] == [REPOSITORY]
    assert evidence["base_url"] == "https://api.github.com"
    assert evidence["github_api_version"] == "2026-03-10"
    notes = attestation.attestation_notes()
    assert notes["permissions_source"] == "operator_declared_ui_confirmed"
    assert "exact_permission_map" in notes["externally_confirmed"]
    assert "authentication" in notes["machine_verified"]


def test_attestation_runs_live_positive_read_probes(tmp_path: Path) -> None:
    """Point 5: read connectivity is proven for every required capability."""
    handler = _attestation_handler()
    provider = _provider(tmp_path)
    attestation = attest_provider(
        provider,
        repositories=[REPOSITORY],
        declaration=_declaration(),
        transport=httpx.MockTransport(handler),
    )
    seen = handler.seen  # type: ignore[attr-defined]
    assert "/rate_limit" in seen
    assert f"/repos/{REPOSITORY}" in seen
    assert f"/repos/{REPOSITORY}/pulls" in seen
    assert f"/repos/{REPOSITORY}/issues" in seen
    assert f"/repos/{REPOSITORY}/commits/{DEFAULT_BRANCH}/check-runs" in seen
    probe = attestation.probes["repository_read_probes"][REPOSITORY]
    assert probe["pulls_status"] == 200
    assert probe["issues_status"] == 200
    assert probe["check_runs_total_count"] == 3
    # No mutation-shaped request may ever be issued.
    assert all(not path.endswith("/merge") for path in seen)


@pytest.mark.parametrize(
    ("fail_path", "code"),
    [
        (f"/repos/{REPOSITORY}/pulls", "PULLS_READ_PROBE_403"),
        (f"/repos/{REPOSITORY}/issues", "ISSUES_READ_PROBE_403"),
        (
            f"/repos/{REPOSITORY}/commits/{DEFAULT_BRANCH}/check-runs",
            "CHECK_RUNS_READ_PROBE_403",
        ),
    ],
)
def test_attestation_fails_closed_when_a_read_probe_fails(
    tmp_path: Path,
    fail_path: str,
    code: str,
) -> None:
    provider = _provider(tmp_path)
    with pytest.raises(AttestationError) as exc:
        attest_provider(
            provider,
            repositories=[REPOSITORY],
            declaration=_declaration(),
            transport=httpx.MockTransport(_attestation_handler(fail_path=fail_path)),
        )
    assert exc.value.code == code


def test_attestation_never_calls_the_nonexistent_permissions_endpoint(
    tmp_path: Path,
) -> None:
    """Point 2: `/installation/token/permissions` is not a real REST endpoint."""
    handler = _attestation_handler()
    provider = _provider(
        tmp_path,
        value=APP_TOKEN,
        provider_type=GitHubProviderType.GITHUB_APP,
    )
    attestation = attest_provider(
        provider,
        repositories=[REPOSITORY],
        declaration=_declaration(
            provider_type="github_app",
            source="installation_token_mint_response",
        ),
        transport=httpx.MockTransport(handler),
    )
    seen = handler.seen  # type: ignore[attr-defined]
    assert "/installation/token/permissions" not in seen
    assert "/installation/repositories" in seen
    assert attestation.permissions_source == "installation_token_mint_response"
    assert "installation_api" not in json.dumps(attestation.attestation_notes())


def test_attestation_source_module_has_no_permissions_endpoint_reference() -> None:
    source = (
        ROOT / "src" / "hermes_mcp_bridge" / "v2" / "github_attestation.py"
    ).read_text(encoding="utf-8")
    assert "/installation/token/permissions" not in source
    assert '"installation_api"' not in source


def test_attestation_ignores_repo_permissions_block(tmp_path: Path) -> None:
    """Point 3: admin/push in the repo `permissions` block is not token capability."""
    provider = _provider(tmp_path)
    attestation = attest_provider(
        provider,
        repositories=[REPOSITORY],
        declaration=_declaration(),
        transport=httpx.MockTransport(
            _attestation_handler(
                repo_permissions={"admin": True, "maintain": True, "push": True, "pull": True}
            )
        ),
    )
    assert attestation.least_privilege is True
    notes = json.dumps(attestation.attestation_notes())
    assert "REPOSITORY_WRITE_ACCESS_PRESENT" not in notes
    assert "repository_permissions" not in notes


def test_repository_write_access_rejection_is_gone() -> None:
    source = (
        ROOT / "src" / "hermes_mcp_bridge" / "v2" / "github_attestation.py"
    ).read_text(encoding="utf-8")
    assert "REPOSITORY_WRITE_ACCESS_PRESENT" not in source


def test_attestation_rejects_classic_pat_material(tmp_path: Path) -> None:
    provider = _provider(tmp_path, value=CLASSIC)
    with pytest.raises(AttestationError) as exc:
        attest_provider(
            provider,
            repositories=[REPOSITORY],
            declaration=_declaration(),
            transport=httpx.MockTransport(_attestation_handler()),
        )
    assert exc.value.code == "CLASSIC_PAT_REJECTED"


def test_attestation_rejects_oauth_scopes_header(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    with pytest.raises(AttestationError) as exc:
        attest_provider(
            provider,
            repositories=[REPOSITORY],
            declaration=_declaration(),
            transport=httpx.MockTransport(_attestation_handler(oauth_scopes="repo, read:org")),
        )
    assert exc.value.code == "CLASSIC_PAT_DETECTED"


def test_attestation_rejects_wildcard_scope(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    with pytest.raises(AttestationError) as exc:
        attest_provider(
            provider,
            repositories=["pestoura/*"],
            declaration=_declaration(),
            transport=httpx.MockTransport(_attestation_handler()),
        )
    assert exc.value.code == "WILDCARD_REPOSITORY_SCOPE"


def test_attestation_fails_closed_on_401(tmp_path: Path) -> None:
    provider = _provider(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={})

    with pytest.raises(AttestationError) as exc:
        attest_provider(
            provider,
            repositories=[REPOSITORY],
            declaration=_declaration(),
            transport=httpx.MockTransport(handler),
        )
    assert exc.value.code == "AUTHENTICATION_FAILED"


def test_attestation_output_never_contains_material(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    attestation = attest_provider(
        provider,
        repositories=[REPOSITORY],
        declaration=_declaration(),
        transport=httpx.MockTransport(_attestation_handler()),
    )
    text = json.dumps(
        [attestation.evidence(), attestation.attestation_notes()],
        sort_keys=True,
    )
    assert FINE_GRAINED not in text
    assert str(tmp_path) not in text


# ---------------------------------------------------------------------------
# 5b. external attestation input (point 4)
# ---------------------------------------------------------------------------


def test_declaration_document_round_trips(tmp_path: Path) -> None:
    declaration = load_attestation_input(_write_declaration(tmp_path))
    assert declaration.provider_type == "fine_grained_token"
    assert declaration.permissions == REQUIRED_PERMISSIONS
    assert declaration.repository_scopes == [REPOSITORY]
    assert declaration.confirmation is True
    notes = declaration.notes()
    assert notes["schema"] == ATTESTATION_INPUT_SCHEMA
    assert str(tmp_path) not in json.dumps(notes)


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"schema": "other/1"}, "ATTESTATION_SCHEMA_UNSUPPORTED"),
        ({"confirmation": False}, "ATTESTATION_NOT_CONFIRMED"),
        ({"provider_type": "classic_pat"}, "ATTESTATION_PROVIDER_TYPE_INVALID"),
        ({"confirmation_source": "trust_me"}, "ATTESTATION_SOURCE_NOT_ALLOWED"),
        (
            {"confirmation_source": "installation_token_mint_response"},
            "ATTESTATION_SOURCE_NOT_ALLOWED",
        ),
        ({"repository_scopes": ["pestoura/*"]}, "ATTESTATION_WILDCARD_REPOSITORY_SCOPE"),
        ({"confirmed_at": "not-a-date"}, "ATTESTATION_CONFIRMED_AT_INVALID"),
        ({"unexpected_permissions": None}, "ATTESTATION_UNEXPECTED_PERMISSIONS_MISSING"),
    ],
)
def test_declaration_rejects_invalid_documents(
    tmp_path: Path,
    overrides: dict[str, Any],
    code: str,
) -> None:
    with pytest.raises(AttestationError) as exc:
        load_attestation_input(_write_declaration(tmp_path, **overrides))
    assert exc.value.code == code


def test_declaration_rejects_secret_like_fields(tmp_path: Path) -> None:
    with pytest.raises(AttestationError) as exc:
        load_attestation_input(_write_declaration(tmp_path, token="github_pat_x"))
    assert exc.value.code == "ATTESTATION_INPUT_SECRET_LIKE_FIELD"


@pytest.mark.parametrize(
    "field",
    [
        "credential_value",
        "raw_token",
        "notes",
        "notes_with_secret",
        "extra",
        "metadata",
    ],
)
def test_declaration_is_schema_closed(tmp_path: Path, field: str) -> None:
    """Unknown top-level fields fail closed; the input carries nothing extra."""
    with pytest.raises(AttestationError) as exc:
        load_attestation_input(_write_declaration(tmp_path, **{field: "anything"}))
    assert exc.value.code == "ATTESTATION_UNEXPECTED_FIELD"


def test_declaration_rejects_nested_arbitrary_field(tmp_path: Path) -> None:
    payload = {"inner": {"deep": ["value", {"more": 1}]}}
    with pytest.raises(AttestationError) as exc:
        load_attestation_input(_write_declaration(tmp_path, attachment=payload))
    assert exc.value.code == "ATTESTATION_UNEXPECTED_FIELD"


def test_declaration_unexpected_field_is_rejected_before_content(tmp_path: Path) -> None:
    """The closed-key check runs before schema/content validation."""
    with pytest.raises(AttestationError) as exc:
        load_attestation_input(_write_declaration(tmp_path, schema="other/1", credential_value="x"))
    assert exc.value.code == "ATTESTATION_UNEXPECTED_FIELD"


@pytest.mark.parametrize(
    "value",
    ["2026-08-08T10:00:00", "2026-08-08 10:00:00", "2026-08-08"],
)
def test_declaration_rejects_naive_confirmed_at(tmp_path: Path, value: str) -> None:
    with pytest.raises(AttestationError) as exc:
        load_attestation_input(_write_declaration(tmp_path, confirmed_at=value))
    assert exc.value.code == "ATTESTATION_CONFIRMED_AT_NOT_TIMEZONE_AWARE"


@pytest.mark.parametrize(
    "value",
    ["2026-08-08T10:00:00+00:00", "2026-08-08T10:00:00Z", "2026-08-08T11:00:00+01:00"],
)
def test_declaration_accepts_timezone_aware_confirmed_at(tmp_path: Path, value: str) -> None:
    declaration = load_attestation_input(_write_declaration(tmp_path, confirmed_at=value))
    assert declaration.confirmed_at == value


def test_stateless_app_token_attests_when_all_controls_pass(tmp_path: Path) -> None:
    provider = _provider(
        tmp_path,
        value=APP_TOKEN_STATELESS,
        provider_type=GitHubProviderType.GITHUB_APP,
    )
    attestation = attest_provider(
        provider,
        repositories=[REPOSITORY],
        declaration=_declaration(
            provider_type="github_app",
            source="installation_token_mint_response",
        ),
        transport=httpx.MockTransport(_attestation_handler()),
    )
    assert attestation.evidence()["provider_type"] == "github_app"
    assert attestation.authenticated is True
    assert APP_TOKEN_STATELESS not in json.dumps(
        [attestation.evidence(), attestation.attestation_notes()], sort_keys=True
    )


def test_declaration_confirmation_missing_is_rejected(tmp_path: Path) -> None:
    document = _declaration_document()
    document.pop("confirmation")
    path = tmp_path / "attestation.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(AttestationError) as exc:
        load_attestation_input(path)
    assert exc.value.code == "ATTESTATION_NOT_CONFIRMED"


@pytest.mark.parametrize(
    ("declaration_kwargs", "code"),
    [
        ({"provider_type": "github_app", "source": "github_app_settings_ui"},
         "ATTESTATION_PROVIDER_TYPE_MISMATCH"),
        ({"repositories": ["pestoura/other"]}, "ATTESTATION_REPOSITORY_SCOPE_MISMATCH"),
        ({"permissions": {"checks": "read"}}, "ATTESTATION_PERMISSIONS_NOT_EXACT"),
        (
            {"permissions": {**REQUIRED_PERMISSIONS, "contents": "write"}},
            "ATTESTATION_PERMISSIONS_NOT_EXACT",
        ),
        ({"unexpected": ["contents:write"]}, "ATTESTATION_UNEXPECTED_PERMISSIONS"),
    ],
)
def test_attestation_rejects_declaration_mismatch(
    tmp_path: Path,
    declaration_kwargs: dict[str, Any],
    code: str,
) -> None:
    provider = _provider(tmp_path)
    with pytest.raises(AttestationError) as exc:
        attest_provider(
            provider,
            repositories=[REPOSITORY],
            declaration=_declaration(**declaration_kwargs),
            transport=httpx.MockTransport(_attestation_handler()),
        )
    assert exc.value.code == code


def test_attestation_never_fabricates_required_permissions(tmp_path: Path) -> None:
    """Point 4: permissions come from the declaration, never from a default."""
    provider = _provider(tmp_path)
    attestation = attest_provider(
        provider,
        repositories=[REPOSITORY],
        declaration=_declaration(),
        transport=httpx.MockTransport(_attestation_handler()),
    )
    # The declaration object is the only source of the emitted map.
    assert attestation.permissions == REQUIRED_PERMISSIONS
    source = (
        ROOT / "src" / "hermes_mcp_bridge" / "v2" / "github_attestation.py"
    ).read_text(encoding="utf-8")
    assert "permissions = dict(REQUIRED_PERMISSIONS)" not in source


def test_app_installation_scope_mismatch_fails_closed(tmp_path: Path) -> None:
    provider = _provider(
        tmp_path,
        value=APP_TOKEN,
        provider_type=GitHubProviderType.GITHUB_APP,
    )
    with pytest.raises(AttestationError) as exc:
        attest_provider(
            provider,
            repositories=[REPOSITORY],
            declaration=_declaration(
                provider_type="github_app",
                source="github_app_settings_ui",
            ),
            transport=httpx.MockTransport(
                _attestation_handler(installation_repositories=[REPOSITORY, "pestoura/other"])
            ),
        )
    assert exc.value.code == "INSTALLATION_SCOPE_MISMATCH"


# ---------------------------------------------------------------------------
# 6. collector harness
# ---------------------------------------------------------------------------


def _targets() -> dict[str, Any]:
    return {
        "targets": {
            "github.get_repo": {"repository": REPOSITORY, "arguments": {}},
            "github.get_pr": {"repository": REPOSITORY, "arguments": {"number": 1}},
            "github.get_issue": {
                "repository": REPOSITORY,
                "arguments": {"number": 1},
            },
            "github.get_checks": {
                "repository": REPOSITORY,
                "arguments": {"ref": "main"},
            },
            "github.search": {"repository": REPOSITORY, "arguments": {"text": "gate"}},
        }
    }


def test_collector_plan_is_exactly_five_by_three() -> None:
    collector = _load_collector()
    plan = collector.build_plan(_targets())
    assert len(plan) == 15
    counts: dict[str, int] = {}
    for item in plan:
        counts[item["tool_id"]] = counts.get(item["tool_id"], 0) + 1
    assert counts == {tool: 3 for tool in GITHUB_DIRECT_READ_TOOL_IDS}
    assert sorted({item["repetition"] for item in plan}) == [1, 2, 3]


def test_collector_plan_rejects_incomplete_targets() -> None:
    collector = _load_collector()
    spec = _targets()
    del spec["targets"]["github.search"]
    with pytest.raises(collector.CollectorError) as exc:
        collector.build_plan(spec)
    assert exc.value.code == "TARGETS_INCOMPLETE"


def test_collector_plan_rejects_unexpected_tool() -> None:
    collector = _load_collector()
    spec = _targets()
    spec["targets"]["github.create_issue"] = {"repository": REPOSITORY}
    with pytest.raises(collector.CollectorError) as exc:
        collector.build_plan(spec)
    assert exc.value.code == "TARGETS_UNEXPECTED_TOOL"


def _repo_shape(**overrides: Any) -> dict[str, Any]:
    base = {
        "full_name": REPOSITORY,
        "private": True,
        "visibility": "private",
        "default_branch": "main",
        "archived": False,
        "html_url": f"https://github.com/{REPOSITORY}",
        "updated_at": "2026-08-01T00:00:00Z",
    }
    base.update(overrides)
    return base


def test_collector_normalizes_both_sides_to_the_same_field_set() -> None:
    collector = _load_collector()
    direct = _repo_shape()
    shadow = _repo_shape(extra_commentary="ignored")
    assert collector.normalized_digest("github.get_repo", direct) == collector.normalized_digest(
        "github.get_repo", shadow
    )


def test_comparison_fields_are_the_executor_default_result_fields() -> None:
    """The comparison must be the FULL default shape, never a narrower subset."""
    collector = _load_collector()
    assert set(collector.COMPARISON_FIELDS) == set(GITHUB_DIRECT_DEFAULT_RESULT_FIELDS)
    for tool_id, defaults in GITHUB_DIRECT_DEFAULT_RESULT_FIELDS.items():
        compared = collector.COMPARISON_FIELDS[tool_id]
        # every default result field of every tool enters the mapping
        assert set(defaults) <= set(compared), tool_id
        assert set(compared) == set(defaults), tool_id
    assert set(collector.COMPARISON_FIELDS["github.get_checks"]) == {
        "total_count",
        "check_runs",
    }
    assert set(collector.COMPARISON_FIELDS["github.search"]) == {
        "total_count",
        "incomplete_results",
        "items",
    }


def _checks(*runs: dict[str, Any]) -> dict[str, Any]:
    return {"total_count": 7, "check_runs": list(runs)}


def _run(name: str, conclusion: str = "success") -> dict[str, Any]:
    return {
        "app": "github-actions",
        "completed_at": "2026-08-01T00:10:00Z",
        "conclusion": conclusion,
        "head_sha": "d" * 40,
        "html_url": f"https://github.com/{REPOSITORY}/runs/{name}",
        "id": 1,
        "name": name,
        "started_at": "2026-08-01T00:00:00Z",
        "status": "completed",
    }


def test_changed_check_run_breaks_the_digest_with_equal_total_count() -> None:
    """A different check run must NOT pass as a semantic match."""
    collector = _load_collector()
    a = _checks(_run("build"), _run("lint"))
    b = _checks(_run("build"), _run("lint", conclusion="failure"))
    assert a["total_count"] == b["total_count"]
    assert collector.normalized_digest("github.get_checks", a) != collector.normalized_digest(
        "github.get_checks", b
    )


def test_check_run_order_is_canonical_and_not_semantic() -> None:
    collector = _load_collector()
    a = _checks(_run("build"), _run("lint"))
    b = _checks(_run("lint"), _run("build"))
    assert collector.normalized_digest("github.get_checks", a) == collector.normalized_digest(
        "github.get_checks", b
    )


def _search(*items: dict[str, Any]) -> dict[str, Any]:
    return {"total_count": 4, "incomplete_results": False, "items": list(items)}


def _item(number: int, title: str) -> dict[str, Any]:
    return {
        "comments": 0,
        "created_at": "2026-08-01T00:00:00Z",
        "html_url": f"https://github.com/{REPOSITORY}/issues/{number}",
        "item_type": "issue",
        "number": number,
        "state": "open",
        "title": title,
        "updated_at": "2026-08-01T00:00:00Z",
        "user": "pestoura",
    }


def test_changed_search_item_breaks_the_digest_with_equal_total_count() -> None:
    collector = _load_collector()
    a = _search(_item(1, "alpha"), _item(2, "beta"))
    b = _search(_item(1, "alpha"), _item(2, "beta-renamed"))
    assert a["total_count"] == b["total_count"]
    assert collector.normalized_digest("github.search", a) != collector.normalized_digest(
        "github.search", b
    )


def test_nested_structure_is_preserved_not_stringified() -> None:
    """A list of dicts must stay structured through normalization."""
    collector = _load_collector()
    normalized = collector.normalize_for_comparison("github.get_checks", _checks(_run("build")))
    assert isinstance(normalized["check_runs"], list)
    assert isinstance(normalized["check_runs"][0], dict)
    assert normalized["check_runs"][0]["name"] == "build"


def test_missing_nested_field_is_not_normalized_away() -> None:
    collector = _load_collector()
    full = _checks(_run("build"))
    missing = {"total_count": 7}
    assert collector.normalized_digest(
        "github.get_checks", full
    ) != collector.normalized_digest("github.get_checks", missing)


def test_shadow_prompt_requests_the_full_shape_and_nested_structure() -> None:
    collector = _load_collector()
    prompt = collector._shadow_prompt("github.get_checks", REPOSITORY, {"ref": "main"})
    for field in GITHUB_DIRECT_DEFAULT_RESULT_FIELDS["github.get_checks"]:
        assert field in prompt
    assert "check_runs" in prompt
    assert "no extra keys" in prompt
    assert "semantically equivalent" in prompt


def test_collector_detects_semantic_mismatch() -> None:
    collector = _load_collector()
    a = {"full_name": REPOSITORY, "private": True}
    b = {"full_name": REPOSITORY, "private": False}
    assert collector.normalized_digest("github.get_repo", a) != collector.normalized_digest(
        "github.get_repo", b
    )


def test_collector_token_accounting_fails_closed(tmp_path: Path) -> None:
    collector = _load_collector()
    db = tmp_path / "state.db"
    import sqlite3

    connection = sqlite3.connect(db)
    connection.execute(
        "CREATE TABLE session_model_usage ("
        "session_id TEXT, input_tokens INTEGER, output_tokens INTEGER)"
    )
    connection.commit()
    connection.close()

    calls: list[float] = []
    assert (
        collector.state_db_tokens(
            str(db),
            "missing-session",
            attempts=3,
            interval=0.0,
            sleep=calls.append,
        )
        is None
    )
    assert len(calls) == 2


def test_collector_reads_real_token_accounting(tmp_path: Path) -> None:
    collector = _load_collector()
    db = tmp_path / "state.db"
    import sqlite3

    connection = sqlite3.connect(db)
    connection.execute(
        "CREATE TABLE session_model_usage ("
        "session_id TEXT, input_tokens INTEGER, output_tokens INTEGER, "
        "reasoning_tokens INTEGER)"
    )
    connection.execute("INSERT INTO session_model_usage VALUES ('s1', 1000, 75, 25)")
    connection.commit()
    connection.close()

    tokens = collector.state_db_tokens(str(db), "s1", attempts=1, sleep=lambda _: None)
    assert tokens == {"input": 1000, "output": 75, "total": 1100}


def test_collector_state_db_is_opened_read_only(tmp_path: Path) -> None:
    collector = _load_collector()
    db = tmp_path / "state.db"
    import sqlite3

    connection = sqlite3.connect(db)
    connection.execute(
        "CREATE TABLE session_model_usage ("
        "session_id TEXT, input_tokens INTEGER, output_tokens INTEGER)"
    )
    connection.execute("INSERT INTO session_model_usage VALUES ('s1', 10, 5)")
    connection.commit()
    connection.close()

    before = db.read_bytes()
    collector.state_db_tokens(str(db), "s1", attempts=1, sleep=lambda _: None)
    assert db.read_bytes() == before

    ro = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("DELETE FROM session_model_usage")
    ro.close()


def test_collector_never_persists_prompts_or_outputs() -> None:
    text = COLLECTOR.read_text(encoding="utf-8")
    assert '"prompts_stored": False' in text
    assert '"outputs_stored": False' in text
    # The evidence document itself must carry no prompt/output payload keys.
    sample_block = text.split("samples.append(")[1].split("# ---- V1")[0]
    forbidden_keys = (
        '"prompt"',
        '"prompt_text"',
        '"output_text"',
        '"raw_output"',
        '"response_text"',
        '"content"',
    )
    for forbidden in forbidden_keys:
        assert forbidden not in sample_block


def test_collector_builds_no_hermes_client_on_the_direct_path() -> None:
    """DIRECT must not touch the Hermes/MCP session; only the shadow may."""
    text = COLLECTOR.read_text(encoding="utf-8")
    direct_block = text.split("# ---- DIRECT")[1].split("# ---- V1 agentic shadow")[0]
    for forbidden in ("session.call_tool", "hermes_prompt", "ClientSession"):
        assert forbidden not in direct_block


def test_collector_declares_the_exact_gate_and_schema() -> None:
    collector = _load_collector()
    assert collector.EVIDENCE_SCHEMA == "hermes-v2-phase2-direct-read-acceptance/1"
    assert collector.COLLECTION_GATE == "DIRECT_READ_EVIDENCE_COLLECTED"
    assert collector.EXPECTED_SAMPLE_COUNT == 15


def test_collector_cli_requires_connected_prerequisites() -> None:
    collector = _load_collector()
    parser = collector.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])
    args = parser.parse_args(
        [
            "--targets",
            "t.json",
            "--json-out",
            "o.json",
            "--source-commit",
            "1" * 40,
            "--direct-core-commit",
            "2" * 40,
            "--provider-type",
            "fine_grained_token",
            "--hermes-state-db",
            "state.db",
            "--provider-attestation",
            "attestation.json",
        ]
    )
    assert args.provider_type == "fine_grained_token"
    assert args.secret_name == "BRIDGE_V2_GITHUB_DIRECT_READ_TOKEN"


def test_collector_rejects_classic_pat_provider_type() -> None:
    collector = _load_collector()
    parser = collector.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--targets",
                "t.json",
                "--json-out",
                "o.json",
                "--source-commit",
                "1" * 40,
                "--direct-core-commit",
                "2" * 40,
                "--provider-type",
                "classic_pat",
                "--hermes-state-db",
                "state.db",
            ]
        )


def test_no_connected_evidence_is_committed() -> None:
    evidence_dir = ROOT / "docs" / "v2" / "evidence"
    names = {path.name for path in evidence_dir.glob("*.json")}
    assert "phase2-connected-direct-read-acceptance.json" not in names
    assert "phase2-direct-read-gate.json" not in names


# ---------------------------------------------------------------------------
# 7. shadow token accounting envelope (point 6)
# ---------------------------------------------------------------------------


class _Text:
    def __init__(self, text: str) -> None:
        self.text = text


class _Result:
    def __init__(self, structured: Any = None, text: str | None = None) -> None:
        self.structuredContent = structured
        self.content = [_Text(text)] if text is not None else []


def _realistic_envelope() -> dict[str, Any]:
    """A realistic hermes_prompt envelope: metadata at the top, answer nested."""
    return {
        "session_id": "sess-20260808-abcdef",
        "status": "completed",
        "model": "tencent/hy3",
        "result": {
            "output": json.dumps(
                {
                    "full_name": REPOSITORY,
                    "private": False,
                    "default_branch": "main",
                    "archived": False,
                }
            )
        },
    }


def test_payload_preserves_top_level_session_id() -> None:
    collector = _load_collector()
    payload = collector._payload(_Result(structured=_realistic_envelope()))
    assert isinstance(payload, dict)
    assert payload["session_id"] == "sess-20260808-abcdef"
    assert "result" in payload


def test_shadow_data_extracts_nested_answer_without_destroying_metadata() -> None:
    collector = _load_collector()
    envelope = _realistic_envelope()
    payload = collector._payload(_Result(structured=envelope))
    data = collector._shadow_data(payload)
    assert data["full_name"] == REPOSITORY
    assert data["default_branch"] == "main"
    # The envelope is untouched, so accounting can still resolve the session.
    assert collector._top_level_session_id(payload) == "sess-20260808-abcdef"


def test_token_accounting_resolves_session_from_realistic_envelope(
    tmp_path: Path,
) -> None:
    collector = _load_collector()
    db = tmp_path / "state.db"
    connection = sqlite3.connect(db)
    connection.execute(
        "CREATE TABLE session_model_usage ("
        "session_id TEXT, input_tokens INT, output_tokens INT, reasoning_tokens INT)"
    )
    connection.execute(
        "INSERT INTO session_model_usage VALUES (?, ?, ?, ?)",
        ("sess-20260808-abcdef", 120, 45, 7),
    )
    connection.commit()
    connection.close()

    payload = collector._payload(_Result(structured=_realistic_envelope()))
    session_id = collector._top_level_session_id(payload)
    assert session_id == "sess-20260808-abcdef"
    tokens = collector.state_db_tokens(str(db), session_id, attempts=1)
    assert tokens == {"input": 120, "output": 45, "total": 172}


def test_top_level_session_id_ignores_nested_session_id() -> None:
    collector = _load_collector()
    payload = {"result": {"session_id": "nested-only"}}
    assert collector._top_level_session_id(payload) is None


def test_payload_never_collapses_the_envelope_into_result() -> None:
    source = COLLECTOR.read_text(encoding="utf-8")
    assert 'structured.get("result", structured)' not in source


def test_shadow_data_parses_plain_text_answer() -> None:
    collector = _load_collector()
    payload = collector._payload(
        _Result(text=json.dumps({"session_id": "s1", "output": '{"total_count": 4}'}))
    )
    assert collector._top_level_session_id(payload) == "s1"
    assert collector._shadow_data(payload) == {"total_count": 4}


# ---------------------------------------------------------------------------
# 8. mcp import symbol (point 7)
# ---------------------------------------------------------------------------


def test_collector_uses_the_canonical_streamable_http_symbol() -> None:
    source = COLLECTOR.read_text(encoding="utf-8")
    assert "from mcp.client.streamable_http import streamable_http_client" in source
    assert "streamablehttp_client" not in source


def test_collector_streamable_symbol_matches_phase0() -> None:
    phase0 = (ROOT / "scripts" / "v2_phase0_benchmark.py").read_text(encoding="utf-8")
    collector_source = COLLECTOR.read_text(encoding="utf-8")
    line = "from mcp.client.streamable_http import streamable_http_client"
    assert line in phase0
    assert line in collector_source


# ---------------------------------------------------------------------------
# 9. window integrity and mutation basis (point 8)
# ---------------------------------------------------------------------------


def test_window_integrity_is_derived_not_hardcoded() -> None:
    collector = _load_collector()
    good = collector.WindowIntegrity(
        direct_transport_dedicated=True,
        direct_call_delta_exact=True,
        shadow_session_scoped_accounting=True,
    )
    assert good.contaminated is False
    described = good.describe()
    assert described["attribution_ambiguity"] is False
    assert described["direct_call_delta_exact"] is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {"direct_transport_dedicated": False},
        {"direct_call_delta_exact": False},
        {"shadow_session_scoped_accounting": False},
    ],
)
def test_window_integrity_flags_contamination(kwargs: dict[str, bool]) -> None:
    collector = _load_collector()
    base = {
        "direct_transport_dedicated": True,
        "direct_call_delta_exact": True,
        "shadow_session_scoped_accounting": True,
    }
    base.update(kwargs)
    window = collector.WindowIntegrity(**base)
    assert window.contaminated is True
    assert window.describe()["attribution_ambiguity"] is True


def test_direct_mutation_is_derived_from_executor_capability(tmp_path: Path) -> None:
    collector = _load_collector()
    executor, _ = _executor(tmp_path, _repo_handler)
    assert collector.direct_mutation_observed(executor) is False

    class _Writable:
        async def post(self) -> None: ...
        async def _get(self) -> None: ...

    with pytest.raises(collector.CollectorError) as exc:
        collector.direct_mutation_observed(_Writable())
    assert exc.value.code == "DIRECT_MUTATION_BASIS_INVALID"


def test_shadow_mutation_claim_fails_closed_without_a_basis() -> None:
    collector = _load_collector()
    with pytest.raises(collector.CollectorError) as exc:
        collector.shadow_mutation_observed("none")
    assert exc.value.code == "SHADOW_MUTATION_BASIS_UNPROVEN"
    with pytest.raises(collector.CollectorError):
        collector.shadow_mutation_observed("")


def test_shadow_mutation_claim_accepts_documented_bases() -> None:
    collector = _load_collector()
    for basis in ("github_audit_log_reviewed", "read_only_credential_enforced"):
        observed, recorded = collector.shadow_mutation_observed(basis)
        assert observed is False
        assert recorded == basis
        assert collector.SHADOW_MUTATION_BASES[recorded]


def test_collector_has_no_hardcoded_window_or_mutation_literals() -> None:
    source = COLLECTOR.read_text(encoding="utf-8")
    assert '"contaminated_window": False' not in source
    assert '"mutation_observed": False' not in source


# ---------------------------------------------------------------------------
# 10. collector CLI contract for the external attestation (point 4)
# ---------------------------------------------------------------------------


def _cli_args(**overrides: str) -> list[str]:
    values = {
        "--targets": "t.json",
        "--json-out": "o.json",
        "--source-commit": "1" * 40,
        "--direct-core-commit": "2" * 40,
        "--provider-type": "fine_grained_token",
        "--hermes-state-db": "state.db",
        "--provider-attestation": "attestation.json",
    }
    values.update(overrides)
    argv: list[str] = []
    for key, value in values.items():
        argv.extend([key, value])
    return argv


def test_collector_requires_a_provider_attestation() -> None:
    collector = _load_collector()
    parser = collector.build_parser()
    argv = _cli_args()
    index = argv.index("--provider-attestation")
    del argv[index : index + 2]
    with pytest.raises(SystemExit):
        parser.parse_args(argv)


def test_collector_shadow_mutation_basis_defaults_to_none() -> None:
    collector = _load_collector()
    args = collector.build_parser().parse_args(_cli_args())
    assert args.shadow_mutation_basis == "none"
    assert args.provider_attestation == "attestation.json"


def test_collector_rejects_unknown_shadow_mutation_basis() -> None:
    collector = _load_collector()
    parser = collector.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([*_cli_args(), "--shadow-mutation-basis", "vibes"])


def test_collector_never_retains_the_attestation_path() -> None:
    source = COLLECTOR.read_text(encoding="utf-8")
    assert '"attestation_path_recorded": False' in source
    # the path is consumed once, at load time, and never reaches the document
    assert source.count("args.provider_attestation") == 2
    document = source.split('"schema": EVIDENCE_SCHEMA', 1)[-1]
    assert "provider_attestation" not in document.split("def build_parser", 1)[0]
