"""Hermetic security/contract tests for V2 Phase 2 GitHub DIRECT reads."""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from hermes_mcp_bridge import contracts
from hermes_mcp_bridge.v2.credentials import (
    CredentialBroker,
    CredentialCapabilityStatus,
    StaticCredentialBroker,
)
from hermes_mcp_bridge.v2.enums import (
    CapabilityState,
    ExecutionMode,
    MutationClass,
    SecurityTier,
)
from hermes_mcp_bridge.v2.github_auth import (
    GitHubAuthorization,
    GitHubAuthorizationError,
    StaticGitHubAuthorizationProvider,
)
from hermes_mcp_bridge.v2.github_direct import (
    GITHUB_ACCEPT,
    GITHUB_API_VERSION,
    GitHubDirectDenied,
    GitHubDirectError,
    GitHubDirectReadExecutor,
    GitHubRepositoryScope,
)
from hermes_mcp_bridge.v2.github_registry import (
    GITHUB_DIRECT_READ_TOOL_IDS,
    GITHUB_READ_CREDENTIAL_CAPABILITY,
    build_github_direct_read_registry,
    github_direct_read_policy_rules,
)
from hermes_mcp_bridge.v2.policy import PolicyRuleSet

TOKEN = "github_phase2_test_token_DO_NOT_LEAK"
REPOSITORY = "pestoura/hermes-mcp-bridge"
ROOT = Path(__file__).resolve().parents[1]


class RecordingTransport:
    def __init__(self, responses: list[httpx.Response] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._responses = list(responses or [])

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._responses:
            response = self._responses.pop(0)
            response.request = request
            return response
        return httpx.Response(200, json={}, request=request)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


class CountingCredentialBroker:
    """Test broker proving resource scope is checked before readiness."""

    def __init__(self, state: CapabilityState = CapabilityState.READY) -> None:
        self.status_calls = 0
        self._delegate = _broker(state)

    def status(
        self,
        credential_capability_id: str,
    ) -> CredentialCapabilityStatus | None:
        self.status_calls += 1
        return self._delegate.status(credential_capability_id)

    def is_ready(self, credential_capability_id: str) -> bool:
        status = self.status(credential_capability_id)
        return bool(status and status.is_ready)


def _broker(
    state: CapabilityState = CapabilityState.READY,
) -> StaticCredentialBroker:
    return StaticCredentialBroker(
        [
            CredentialCapabilityStatus(
                capability_id=GITHUB_READ_CREDENTIAL_CAPABILITY,
                provider="github",
                state=state,
            )
        ]
    )


def _auth_provider(*, include: bool = True) -> StaticGitHubAuthorizationProvider:
    entries = (
        {
            (
                GITHUB_READ_CREDENTIAL_CAPABILITY,
                REPOSITORY,
            ): GitHubAuthorization(TOKEN)
        }
        if include
        else {}
    )
    return StaticGitHubAuthorizationProvider(entries)


def _executor(
    recording: RecordingTransport,
    *,
    scope: GitHubRepositoryScope | None = None,
    broker: CredentialBroker | None = None,
    provider: StaticGitHubAuthorizationProvider | None = None,
    rules: PolicyRuleSet | None = None,
    max_result_bytes: int = 64 * 1024,
) -> GitHubDirectReadExecutor:
    return GitHubDirectReadExecutor(
        registry=build_github_direct_read_registry(),
        rules=rules if rules is not None else github_direct_read_policy_rules(),
        credential_broker=broker if broker is not None else _broker(),
        authorization_provider=(provider if provider is not None else _auth_provider()),
        scope=(scope if scope is not None else GitHubRepositoryScope([REPOSITORY])),
        transport=recording.transport(),
        max_result_bytes=max_result_bytes,
    )


def _response(
    payload: dict[str, Any],
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    return httpx.Response(status, json=payload, headers=headers or {})


def _repo_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "full_name": REPOSITORY,
        "private": False,
        "visibility": "public",
        "default_branch": "main",
        "archived": False,
        "disabled": False,
        "fork": False,
        "open_issues_count": 3,
        "html_url": "https://github.com/pestoura/hermes-mcp-bridge",
        "updated_at": "2026-08-08T10:00:00Z",
        "pushed_at": "2026-08-08T09:59:00Z",
        "language": "Python",
        "license": {"spdx_id": "MIT"},
        "description": "Bridge",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Registry and architecture contract
# ---------------------------------------------------------------------------


def test_registry_contains_exact_five_phase2_tools() -> None:
    registry = build_github_direct_read_registry()
    tool_ids = tuple(tool.tool_id for tool in registry.ordered())
    assert tool_ids == GITHUB_DIRECT_READ_TOOL_IDS
    assert len(registry) == 5
    for tool in registry.ordered():
        assert tool.execution_mode is ExecutionMode.DIRECT
        assert tool.read_only is True
        assert tool.mutation_class is MutationClass.NONE
        assert tool.security_tier is SecurityTier.T1
        assert tool.credential_capability_id == GITHUB_READ_CREDENTIAL_CAPABILITY
        assert tool.result_shaping.value == "REQUIRED"


def test_policy_rules_are_explicit_for_each_tool() -> None:
    registry = build_github_direct_read_registry()
    rules = github_direct_read_policy_rules()
    actions = {tool.policy_action for tool in registry.ordered()}
    assert {rule.policy_action for rule in rules.ordered()} == actions
    assert all("*" not in rule.policy_action for rule in rules.ordered())


def test_phase2_core_does_not_import_hermes_llm_client() -> None:
    import hermes_mcp_bridge.v2.github_direct as module

    source = inspect.getsource(module)
    for forbidden in ("HermesClient", "hermes_prompt", "hermes_submit", "agentic"):
        assert forbidden not in source


def test_v1_contract_remains_exactly_27_tools() -> None:
    assert len(contracts.required_tools("1.0.0")) == 27


# ---------------------------------------------------------------------------
# Scope and secret material
# ---------------------------------------------------------------------------


def test_repository_scope_is_exact_case_insensitive_and_no_wildcards() -> None:
    scope = GitHubRepositoryScope(["Pestoura/Hermes-MCP-Bridge"])
    assert scope.allows("pestoura", "hermes-mcp-bridge") is True
    assert scope.allows("pestoura", "other") is False
    with pytest.raises(ValueError):
        GitHubRepositoryScope(["pestoura/*"])
    with pytest.raises(ValueError):
        GitHubRepositoryScope([])


@pytest.mark.parametrize("value", ["", "   ", "abc\rdef", "abc\ndef"])
def test_authorization_material_rejects_invalid_header_values(value: str) -> None:
    with pytest.raises(GitHubAuthorizationError):
        GitHubAuthorization(value)


def test_authorization_material_and_provider_are_redacted() -> None:
    material = GitHubAuthorization(TOKEN)
    provider = StaticGitHubAuthorizationProvider(
        {(GITHUB_READ_CREDENTIAL_CAPABILITY, REPOSITORY): material}
    )
    assert TOKEN not in repr(material)
    assert TOKEN not in str(material)
    assert TOKEN not in repr(provider)
    assert material.header_value() == f"Bearer {TOKEN}"


@pytest.mark.asyncio
async def test_policy_denial_happens_before_auth_resolution_and_network() -> None:
    recording = RecordingTransport()
    provider = _auth_provider()
    executor = _executor(
        recording,
        provider=provider,
        rules=PolicyRuleSet([]),
    )

    with pytest.raises(GitHubDirectDenied, match="POLICY_MISSING_POLICY_RULE"):
        await executor.get_repo("pestoura", "hermes-mcp-bridge")

    assert provider.resolve_calls == 0
    assert recording.requests == []


@pytest.mark.asyncio
async def test_scope_denial_precedes_readiness_auth_and_network() -> None:
    recording = RecordingTransport()
    provider = _auth_provider()
    broker = CountingCredentialBroker()
    executor = _executor(
        recording,
        provider=provider,
        broker=broker,
        scope=GitHubRepositoryScope(["pestoura/allowed-only"]),
    )

    with pytest.raises(GitHubDirectDenied, match="RESOURCE_SCOPE_DENIED"):
        await executor.get_repo("pestoura", "hermes-mcp-bridge")

    assert broker.status_calls == 0
    assert provider.resolve_calls == 0
    assert recording.requests == []


@pytest.mark.asyncio
async def test_non_ready_credential_denies_before_auth_resolution_and_network() -> None:
    recording = RecordingTransport()
    provider = _auth_provider()
    executor = _executor(
        recording,
        provider=provider,
        broker=_broker(CapabilityState.DEGRADED),
    )

    pattern = "POLICY_CREDENTIAL_CAPABILITY_NOT_READY"
    with pytest.raises(GitHubDirectDenied, match=pattern):
        await executor.get_repo("pestoura", "hermes-mcp-bridge")

    assert provider.resolve_calls == 0
    assert recording.requests == []


@pytest.mark.asyncio
async def test_missing_authorization_material_fails_closed_without_network() -> None:
    recording = RecordingTransport()
    provider = _auth_provider(include=False)
    executor = _executor(recording, provider=provider)

    with pytest.raises(GitHubDirectDenied, match="CREDENTIAL_MATERIAL_UNAVAILABLE"):
        await executor.get_repo("pestoura", "hermes-mcp-bridge")

    assert provider.resolve_calls == 1
    assert recording.requests == []


# ---------------------------------------------------------------------------
# HTTP contract and result shaping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_repo_uses_fixed_headers_get_only_and_shapes_result() -> None:
    recording = RecordingTransport(
        [
            _response(
                _repo_payload(),
                headers={
                    "X-RateLimit-Remaining": "4999",
                    "X-GitHub-Request-Id": "REQ-1",
                },
            )
        ]
    )
    result = await _executor(recording).get_repo(
        "pestoura",
        "hermes-mcp-bridge",
    )

    assert len(recording.requests) == 1
    request = recording.requests[0]
    assert request.method == "GET"
    assert request.url.scheme == "https"
    assert request.url.host == "api.github.com"
    assert request.url.path == "/repos/pestoura/hermes-mcp-bridge"
    assert request.headers["accept"] == GITHUB_ACCEPT
    assert request.headers["x-github-api-version"] == GITHUB_API_VERSION
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert result.tool_id == "github.get_repo"
    assert result.repository == REPOSITORY
    assert result.data == {
        "archived": False,
        "default_branch": "main",
        "full_name": REPOSITORY,
        "html_url": "https://github.com/pestoura/hermes-mcp-bridge",
        "private": False,
        "updated_at": "2026-08-08T10:00:00Z",
        "visibility": "public",
    }
    assert result.rate_limit_remaining == 4999
    assert result.request_id == "REQ-1"
    assert result.raw_bytes > result.returned_bytes
    serialized = json.dumps(result.canonical(), sort_keys=True)
    assert TOKEN not in serialized


@pytest.mark.asyncio
async def test_get_repo_select_is_explicit_and_unknown_fields_fail() -> None:
    recording = RecordingTransport([_response(_repo_payload())])
    result = await _executor(recording).get_repo(
        "pestoura",
        "hermes-mcp-bridge",
        select=["full_name", "language", "license"],
    )
    assert result.data == {
        "full_name": REPOSITORY,
        "language": "Python",
        "license": "MIT",
    }

    recording = RecordingTransport([_response(_repo_payload())])
    with pytest.raises(GitHubDirectDenied, match="INVALID_RESULT_SELECTION"):
        await _executor(recording).get_repo(
            "pestoura",
            "hermes-mcp-bridge",
            select=["clone_url"],
        )


@pytest.mark.asyncio
async def test_get_pr_uses_pull_endpoint_and_body_is_opt_in() -> None:
    payload = {
        "number": 48,
        "title": "Phase 1",
        "state": "closed",
        "draft": False,
        "merged": True,
        "body": "sensitive project prose",
        "user": {"login": "pestoura"},
        "head": {"ref": "feature", "sha": "a" * 40},
        "base": {"ref": "main", "sha": "b" * 40},
        "html_url": "https://github.com/pestoura/hermes-mcp-bridge/pull/48",
        "created_at": "2026-08-08T09:00:00Z",
        "updated_at": "2026-08-08T10:00:00Z",
    }
    recording = RecordingTransport([_response(payload)])
    result = await _executor(recording).get_pr(
        "pestoura",
        "hermes-mcp-bridge",
        48,
    )
    assert recording.requests[0].url.path.endswith("/pulls/48")
    assert "body" not in result.data
    assert result.data["merged"] is True

    recording = RecordingTransport([_response(payload)])
    selected = await _executor(recording).get_pr(
        "pestoura",
        "hermes-mcp-bridge",
        48,
        select=["number", "body"],
    )
    assert selected.data == {
        "number": 48,
        "body": "sensitive project prose",
    }


@pytest.mark.asyncio
async def test_get_issue_normalizes_labels_assignees_and_pr_marker() -> None:
    payload = {
        "number": 43,
        "title": "Evidence",
        "state": "closed",
        "state_reason": "completed",
        "body": "details",
        "user": {"login": "pestoura"},
        "labels": [{"name": "enhancement"}, {"name": "v2"}],
        "assignees": [{"login": "pestoura"}],
        "comments": 2,
        "pull_request": {"url": "https://api.github.com/example"},
        "html_url": "https://github.com/pestoura/hermes-mcp-bridge/issues/43",
        "created_at": "2026-08-08T08:00:00Z",
        "updated_at": "2026-08-08T09:00:00Z",
        "closed_at": "2026-08-08T09:00:00Z",
    }
    recording = RecordingTransport([_response(payload)])
    result = await _executor(recording).get_issue(
        "pestoura",
        "hermes-mcp-bridge",
        43,
    )
    assert recording.requests[0].url.path.endswith("/issues/43")
    assert result.data["labels"] == ["enhancement", "v2"]
    assert result.data["assignees"] == ["pestoura"]
    assert result.data["is_pull_request"] is True
    assert "body" not in result.data


@pytest.mark.asyncio
async def test_get_checks_uses_encoded_ref_and_bounded_page() -> None:
    payload = {
        "total_count": 1,
        "check_runs": [
            {
                "id": 1,
                "name": "CI",
                "status": "completed",
                "conclusion": "success",
                "head_sha": "a" * 40,
                "started_at": "2026-08-08T09:00:00Z",
                "completed_at": "2026-08-08T09:01:00Z",
                "html_url": "https://github.com/example/check/1",
                "app": {"slug": "github-actions"},
            }
        ],
    }
    recording = RecordingTransport([_response(payload)])
    result = await _executor(recording).get_checks(
        "pestoura",
        "hermes-mcp-bridge",
        "heads/feature",
        per_page=17,
    )
    request = recording.requests[0]
    assert request.method == "GET"
    assert request.url.path.endswith("/commits/heads/feature/check-runs")
    assert request.url.params["filter"] == "latest"
    assert request.url.params["page"] == "1"
    assert request.url.params["per_page"] == "17"
    assert result.data["check_runs"][0]["app"] == "github-actions"


@pytest.mark.asyncio
async def test_search_is_repository_scoped_and_structured() -> None:
    payload = {
        "total_count": 1,
        "incomplete_results": False,
        "items": [
            {
                "number": 43,
                "title": "Phase 0 evidence",
                "state": "closed",
                "comments": 0,
                "user": {"login": "pestoura"},
                "html_url": ("https://github.com/pestoura/hermes-mcp-bridge/issues/43"),
                "created_at": "2026-08-08T08:00:00Z",
                "updated_at": "2026-08-08T09:00:00Z",
                "body": "not returned by search",
            }
        ],
    }
    recording = RecordingTransport([_response(payload)])
    result = await _executor(recording).search(
        "pestoura",
        "hermes-mcp-bridge",
        "phase evidence",
        item_type="issue",
        state="closed",
        per_page=7,
    )
    request = recording.requests[0]
    assert request.url.path == "/search/issues"
    expected_query = "phase evidence repo:pestoura/hermes-mcp-bridge is:issue state:closed"
    assert request.url.params["q"] == expected_query
    assert request.url.params["per_page"] == "7"
    assert result.data["items"][0]["item_type"] == "issue"
    assert "body" not in result.data["items"][0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    ["repo:evil/repo", "foo OR bar", "(foo)", "state:open"],
)
async def test_search_rejects_injection_before_credentials(text: str) -> None:
    recording = RecordingTransport()
    provider = _auth_provider()
    executor = _executor(recording, provider=provider)
    with pytest.raises(GitHubDirectDenied, match="UNSAFE_SEARCH_SYNTAX"):
        await executor.search("pestoura", "hermes-mcp-bridge", text)
    assert provider.resolve_calls == 0
    assert recording.requests == []


@pytest.mark.asyncio
async def test_redirect_is_not_followed() -> None:
    recording = RecordingTransport(
        [httpx.Response(301, headers={"Location": "https://evil.example/steal"})]
    )
    with pytest.raises(GitHubDirectError, match="REDIRECT_BLOCKED") as exc_info:
        await _executor(recording).get_repo(
            "pestoura",
            "hermes-mcp-bridge",
        )
    assert exc_info.value.status_code == 301
    assert len(recording.requests) == 1
    assert recording.requests[0].url.host == "api.github.com"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "headers", "code"),
    [
        (401, {}, "AUTHENTICATION_FAILED"),
        (403, {"X-RateLimit-Remaining": "10"}, "FORBIDDEN"),
        (
            403,
            {"X-RateLimit-Remaining": "0", "Retry-After": "60"},
            "RATE_LIMITED",
        ),
        (404, {}, "NOT_FOUND"),
        (410, {}, "GONE"),
        (422, {}, "INVALID_REQUEST"),
        (429, {"Retry-After": "30"}, "RATE_LIMITED"),
        (500, {}, "UPSTREAM_ERROR"),
    ],
)
async def test_http_errors_are_categorized_and_never_include_token(
    status: int,
    headers: dict[str, str],
    code: str,
) -> None:
    recording = RecordingTransport(
        [httpx.Response(status, text=f"server echoed {TOKEN}", headers=headers)]
    )
    with pytest.raises(GitHubDirectError) as exc_info:
        await _executor(recording).get_repo(
            "pestoura",
            "hermes-mcp-bridge",
        )
    error = exc_info.value
    assert error.code == code
    assert TOKEN not in str(error)
    assert TOKEN not in repr(error)
    if code == "RATE_LIMITED" and "Retry-After" in headers:
        assert error.retry_after_seconds == int(headers["Retry-After"])


@pytest.mark.asyncio
async def test_invalid_json_and_non_object_json_fail_closed() -> None:
    recording = RecordingTransport([httpx.Response(200, content=b"not-json")])
    with pytest.raises(GitHubDirectError, match="INVALID_UPSTREAM_JSON"):
        await _executor(recording).get_repo(
            "pestoura",
            "hermes-mcp-bridge",
        )

    recording = RecordingTransport([httpx.Response(200, json=[1, 2, 3])])
    with pytest.raises(GitHubDirectError, match="INVALID_UPSTREAM_SHAPE"):
        await _executor(recording).get_repo(
            "pestoura",
            "hermes-mcp-bridge",
        )


@pytest.mark.asyncio
async def test_result_budget_is_enforced_after_shaping() -> None:
    recording = RecordingTransport([_response(_repo_payload(description="x" * 5000))])
    with pytest.raises(GitHubDirectError, match="RESULT_BUDGET_EXCEEDED"):
        await _executor(recording, max_result_bytes=1024).get_repo(
            "pestoura",
            "hermes-mcp-bridge",
            select=["description"],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "args", "kwargs"),
    [
        ("get_pr", ("pestoura", "hermes-mcp-bridge", 0), {}),
        ("get_issue", ("pestoura", "hermes-mcp-bridge", -1), {}),
        ("get_checks", ("pestoura", "hermes-mcp-bridge", ""), {}),
        (
            "get_checks",
            ("pestoura", "hermes-mcp-bridge", "main"),
            {"per_page": 101},
        ),
        (
            "search",
            ("pestoura", "hermes-mcp-bridge", "query"),
            {"per_page": 31},
        ),
    ],
)
async def test_invalid_arguments_fail_before_network(
    method: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> None:
    recording = RecordingTransport()
    executor = _executor(recording)
    with pytest.raises(GitHubDirectDenied):
        await getattr(executor, method)(*args, **kwargs)
    assert recording.requests == []


def test_phase2_source_has_no_v1_wiring_change() -> None:
    server_path = ROOT / "src" / "hermes_mcp_bridge" / "server.py"
    server_source = server_path.read_text(encoding="utf-8")
    assert "github_direct" not in server_source
    assert "GITHUB_DIRECT_READ_TOOL_IDS" not in server_source


def test_result_object_has_no_token_or_llm_accounting_fields() -> None:
    from hermes_mcp_bridge.v2.github_direct import GitHubDirectResult

    fields = set(GitHubDirectResult.__dataclass_fields__)
    forbidden = {
        "token",
        "authorization",
        "input_tokens",
        "output_tokens",
        "total_tokens",
    }
    assert not forbidden & fields
