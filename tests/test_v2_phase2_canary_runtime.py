"""Tests for the Phase 2 canary runtime: secret provider, readiness broker,
canary router, provider attestation and the connected collector harness.

Everything here is hermetic. No real GitHub credential, no network, no Hermes.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import stat
from pathlib import Path
from typing import Any

import httpx
import pytest

from hermes_mcp_bridge import contracts
from hermes_mcp_bridge.v2.enums import CapabilityState
from hermes_mcp_bridge.v2.github_attestation import (
    REQUIRED_PERMISSIONS,
    AttestationError,
    attest_provider,
)
from hermes_mcp_bridge.v2.github_canary import (
    ExecutionPath,
    FallbackReason,
    GitHubCanaryConfig,
    GitHubCanaryRouter,
)
from hermes_mcp_bridge.v2.github_direct import (
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


def _load_collector() -> Any:
    spec = importlib.util.spec_from_file_location("phase2_collector", COLLECTOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
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


def _attestation_handler(
    *,
    oauth_scopes: str | None = None,
    repo_permissions: dict[str, bool] | None = None,
) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rate_limit":
            headers = {"x-oauth-scopes": oauth_scopes} if oauth_scopes else {}
            return httpx.Response(200, json={"rate": {}}, headers=headers)
        if request.url.path == f"/repos/{REPOSITORY}":
            perms = repo_permissions or {
                "admin": False,
                "maintain": False,
                "push": False,
                "pull": True,
            }
            return httpx.Response(
                200,
                json={"full_name": REPOSITORY, "permissions": perms},
            )
        return httpx.Response(404, json={})

    return handler


def test_attestation_accepts_least_privilege_fine_grained(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    attestation = attest_provider(
        provider,
        repositories=[REPOSITORY],
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
    assert attestation.attestation_notes()["permissions_source"] == "operator_declared_ui_confirmed"


def test_attestation_rejects_classic_pat_material(tmp_path: Path) -> None:
    provider = _provider(tmp_path, value=CLASSIC)
    with pytest.raises(AttestationError) as exc:
        attest_provider(
            provider,
            repositories=[REPOSITORY],
            transport=httpx.MockTransport(_attestation_handler()),
        )
    assert exc.value.code == "CLASSIC_PAT_REJECTED"


def test_attestation_rejects_oauth_scopes_header(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    with pytest.raises(AttestationError) as exc:
        attest_provider(
            provider,
            repositories=[REPOSITORY],
            transport=httpx.MockTransport(_attestation_handler(oauth_scopes="repo, read:org")),
        )
    assert exc.value.code == "CLASSIC_PAT_DETECTED"


def test_attestation_rejects_write_access(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    with pytest.raises(AttestationError) as exc:
        attest_provider(
            provider,
            repositories=[REPOSITORY],
            transport=httpx.MockTransport(
                _attestation_handler(repo_permissions={"push": True, "pull": True})
            ),
        )
    assert exc.value.code == "REPOSITORY_WRITE_ACCESS_PRESENT"


def test_attestation_rejects_wildcard_scope(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    with pytest.raises(AttestationError) as exc:
        attest_provider(
            provider,
            repositories=["pestoura/*"],
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
            transport=httpx.MockTransport(handler),
        )
    assert exc.value.code == "AUTHENTICATION_FAILED"


def test_attestation_output_never_contains_material(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    attestation = attest_provider(
        provider,
        repositories=[REPOSITORY],
        transport=httpx.MockTransport(_attestation_handler()),
    )
    text = json.dumps(
        [attestation.evidence(), attestation.attestation_notes()],
        sort_keys=True,
    )
    assert FINE_GRAINED not in text
    assert str(tmp_path) not in text


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


def test_collector_normalizes_both_sides_to_the_same_field_set() -> None:
    collector = _load_collector()
    direct = {
        "full_name": REPOSITORY,
        "private": True,
        "default_branch": "main",
        "archived": False,
        "html_url": "ignored-by-comparison",
    }
    shadow = {
        "full_name": REPOSITORY,
        "private": True,
        "default_branch": "main",
        "archived": False,
        "extra_agentic_commentary": "ignored",
    }
    assert collector.normalized_digest("github.get_repo", direct) == collector.normalized_digest(
        "github.get_repo", shadow
    )


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
