"""Governed GitHub REST DIRECT read executor for V2 Phase 2.

The execution path is deterministic and contains no Hermes/LLM client:

    typed operation -> exact repository scope -> registry/policy/readiness
      -> authorization material -> GitHub GET -> normalized result shaping
      -> bounded result

Scope is checked before credential readiness so requests outside the allowed
resource set cannot observe internal credential state. This is repo-side core
only: it is not wired into the V1 MCP server and does not claim that a real
Jarvas-side GitHub authorization provider exists.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal
from urllib.parse import quote

import httpx

from .canonical import canonical_json_bytes
from .credentials import CredentialBroker
from .enums import ExecutionMode, PolicyDecision
from .github_auth import GitHubAuthorization, GitHubAuthorizationProvider
from .github_registry import GITHUB_READ_CREDENTIAL_CAPABILITY
from .policy import PolicyEngine, PolicyRuleSet
from .registry import ToolRegistry
from .schema import ToolDefinition

GITHUB_API_BASE_URL = "https://api.github.com"
GITHUB_API_VERSION = "2026-03-10"
GITHUB_ACCEPT = "application/vnd.github+json"
GITHUB_USER_AGENT = "hermes-mcp-bridge-v2-direct-read"

_REPO_PART_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_SEARCH_BOOLEAN_RE = re.compile(
    r"(?:^|\s)(?:AND|OR|NOT)(?:\s|$)",
    re.IGNORECASE,
)
_DEFAULT_MAX_RESULT_BYTES = 64 * 1024
_MAX_RESULT_BYTES = 1024 * 1024


class GitHubDirectError(RuntimeError):
    """Safe structured DIRECT error; never embeds response bodies or headers."""

    __slots__ = ("code", "retry_after_seconds", "status_code")

    def __init__(
        self,
        code: str,
        *,
        status_code: int | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        self.code = code
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        super().__init__(code)

    def __str__(self) -> str:
        suffix = f" status={self.status_code}" if self.status_code is not None else ""
        return f"GitHub DIRECT error: {self.code}{suffix}"


class GitHubDirectDenied(GitHubDirectError):
    """Policy/scope/credential gate denied before backend execution."""


class GitHubRepositoryScope:
    """Exact, case-insensitive repository allow-list. Wildcards are invalid."""

    __slots__ = ("_repositories",)

    def __init__(self, repositories: Iterable[str]) -> None:
        normalized = frozenset(
            _normalize_repository_ref(value) for value in repositories
        )
        if not normalized:
            raise ValueError("at least one repository scope is required")
        self._repositories = normalized

    @property
    def repositories(self) -> tuple[str, ...]:
        return tuple(sorted(self._repositories))

    def allows(self, owner: str, repo: str) -> bool:
        return _repository_ref(owner, repo) in self._repositories

    def require(self, owner: str, repo: str) -> str:
        repository = _repository_ref(owner, repo)
        if repository not in self._repositories:
            raise GitHubDirectDenied("RESOURCE_SCOPE_DENIED")
        return repository

    def __repr__(self) -> str:
        return f"GitHubRepositoryScope(repositories={self.repositories!r})"


@dataclass(frozen=True, slots=True)
class GitHubDirectResult:
    """Normalized, bounded DIRECT result without credential material."""

    tool_id: str
    repository: str
    data: dict[str, Any]
    raw_bytes: int
    returned_bytes: int
    status_code: int
    api_version: str
    rate_limit_remaining: int | None = None
    request_id: str | None = None

    def canonical(self) -> dict[str, Any]:
        return {
            "api_version": self.api_version,
            "data": self.data,
            "rate_limit_remaining": self.rate_limit_remaining,
            "raw_bytes": self.raw_bytes,
            "repository": self.repository,
            "request_id": self.request_id,
            "returned_bytes": self.returned_bytes,
            "status_code": self.status_code,
            "tool_id": self.tool_id,
        }


@dataclass(frozen=True, slots=True)
class _BackendResponse:
    payload: dict[str, Any]
    raw_bytes: int
    status_code: int
    rate_limit_remaining: int | None
    request_id: str | None


def _normalize_repository_part(value: str) -> str:
    if not isinstance(value, str):
        raise GitHubDirectDenied("INVALID_REPOSITORY_SCOPE")
    cleaned = value.strip()
    if not _REPO_PART_RE.fullmatch(cleaned):
        raise GitHubDirectDenied("INVALID_REPOSITORY_SCOPE")
    if any(token in cleaned for token in ("*", "?", "[", "]", "/", "\\")):
        raise GitHubDirectDenied("INVALID_REPOSITORY_SCOPE")
    if cleaned in {".", ".."}:
        raise GitHubDirectDenied("INVALID_REPOSITORY_SCOPE")
    return cleaned.lower()


def _repository_ref(owner: str, repo: str) -> str:
    return f"{_normalize_repository_part(owner)}/{_normalize_repository_part(repo)}"


def _normalize_repository_ref(value: str) -> str:
    if not isinstance(value, str) or value.count("/") != 1:
        raise ValueError("repository scope must be owner/repo")
    owner, repo = value.split("/", 1)
    try:
        return _repository_ref(owner, repo)
    except GitHubDirectDenied as exc:
        raise ValueError("invalid repository scope") from exc


def _positive_int(
    value: int,
    *,
    minimum: int,
    maximum: int,
    code: str,
) -> int:
    invalid_type = isinstance(value, bool) or not isinstance(value, int)
    if invalid_type or not minimum <= value <= maximum:
        raise GitHubDirectDenied(code)
    return value


def _safe_ref(value: str) -> str:
    if not isinstance(value, str):
        raise GitHubDirectDenied("INVALID_REF")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 200 or "\r" in cleaned or "\n" in cleaned:
        raise GitHubDirectDenied("INVALID_REF")
    return cleaned


def _safe_search_text(value: str) -> str:
    if not isinstance(value, str):
        raise GitHubDirectDenied("INVALID_SEARCH_TEXT")
    text = " ".join(value.strip().split())
    if not text or len(text) > 200:
        raise GitHubDirectDenied("INVALID_SEARCH_TEXT")
    unsafe = (
        ":" in text
        or "(" in text
        or ")" in text
        or _SEARCH_BOOLEAN_RE.search(text) is not None
    )
    if unsafe:
        raise GitHubDirectDenied("UNSAFE_SEARCH_SYNTAX")
    return text


def _header_int(headers: httpx.Headers, name: str) -> int | None:
    value = headers.get(name)
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _select_fields(
    payload: dict[str, Any],
    *,
    allowed: frozenset[str],
    defaults: tuple[str, ...],
    select: Iterable[str] | None,
) -> dict[str, Any]:
    if select is None:
        fields = defaults
    else:
        if isinstance(select, str):
            raise GitHubDirectDenied("INVALID_RESULT_SELECTION")
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in select:
            if not isinstance(raw, str):
                raise GitHubDirectDenied("INVALID_RESULT_SELECTION")
            field = raw.strip()
            if not field or field not in allowed or field in seen:
                raise GitHubDirectDenied("INVALID_RESULT_SELECTION")
            normalized.append(field)
            seen.add(field)
        if not normalized:
            raise GitHubDirectDenied("INVALID_RESULT_SELECTION")
        fields = tuple(normalized)
    return {field: payload.get(field) for field in fields}


def _user_login(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    login = value.get("login")
    return login if isinstance(login, str) else None


def _ref_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {"ref": value.get("ref"), "sha": value.get("sha")}


def _normalize_repo(payload: dict[str, Any]) -> dict[str, Any]:
    license_payload = payload.get("license")
    license_id = (
        license_payload.get("spdx_id")
        if isinstance(license_payload, dict)
        else None
    )
    return {
        "archived": bool(payload.get("archived", False)),
        "default_branch": payload.get("default_branch"),
        "description": payload.get("description"),
        "disabled": bool(payload.get("disabled", False)),
        "fork": bool(payload.get("fork", False)),
        "full_name": payload.get("full_name"),
        "html_url": payload.get("html_url"),
        "language": payload.get("language"),
        "license": license_id,
        "open_issues_count": payload.get("open_issues_count"),
        "private": bool(payload.get("private", False)),
        "pushed_at": payload.get("pushed_at"),
        "updated_at": payload.get("updated_at"),
        "visibility": payload.get("visibility"),
    }


def _normalize_pr(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "base": _ref_summary(payload.get("base")),
        "body": payload.get("body"),
        "created_at": payload.get("created_at"),
        "draft": bool(payload.get("draft", False)),
        "head": _ref_summary(payload.get("head")),
        "html_url": payload.get("html_url"),
        "merged": bool(payload.get("merged", False)),
        "number": payload.get("number"),
        "state": payload.get("state"),
        "title": payload.get("title"),
        "updated_at": payload.get("updated_at"),
        "user": _user_login(payload.get("user")),
    }


def _normalize_issue(payload: dict[str, Any]) -> dict[str, Any]:
    labels = payload.get("labels")
    assignees = payload.get("assignees")
    label_names = (
        [
            item.get("name")
            for item in labels
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        ]
        if isinstance(labels, list)
        else []
    )
    assignee_names = (
        [login for item in assignees if (login := _user_login(item)) is not None]
        if isinstance(assignees, list)
        else []
    )
    return {
        "assignees": assignee_names,
        "body": payload.get("body"),
        "closed_at": payload.get("closed_at"),
        "comments": payload.get("comments"),
        "created_at": payload.get("created_at"),
        "html_url": payload.get("html_url"),
        "is_pull_request": isinstance(payload.get("pull_request"), dict),
        "labels": label_names,
        "number": payload.get("number"),
        "state": payload.get("state"),
        "state_reason": payload.get("state_reason"),
        "title": payload.get("title"),
        "updated_at": payload.get("updated_at"),
        "user": _user_login(payload.get("user")),
    }


def _normalize_checks(payload: dict[str, Any]) -> dict[str, Any]:
    runs = payload.get("check_runs")
    normalized_runs: list[dict[str, Any]] = []
    if isinstance(runs, list):
        for item in runs:
            if not isinstance(item, dict):
                continue
            app = item.get("app")
            normalized_runs.append(
                {
                    "app": app.get("slug") if isinstance(app, dict) else None,
                    "completed_at": item.get("completed_at"),
                    "conclusion": item.get("conclusion"),
                    "head_sha": item.get("head_sha"),
                    "html_url": item.get("html_url"),
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "started_at": item.get("started_at"),
                    "status": item.get("status"),
                }
            )
    total = payload.get("total_count", len(normalized_runs))
    return {"check_runs": normalized_runs, "total_count": total}


def _normalize_search(payload: dict[str, Any]) -> dict[str, Any]:
    items = payload.get("items")
    normalized_items: list[dict[str, Any]] = []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            normalized_items.append(
                {
                    "comments": item.get("comments"),
                    "created_at": item.get("created_at"),
                    "html_url": item.get("html_url"),
                    "item_type": (
                        "pull_request"
                        if isinstance(item.get("pull_request"), dict)
                        else "issue"
                    ),
                    "number": item.get("number"),
                    "state": item.get("state"),
                    "title": item.get("title"),
                    "updated_at": item.get("updated_at"),
                    "user": _user_login(item.get("user")),
                }
            )
    return {
        "incomplete_results": bool(payload.get("incomplete_results", False)),
        "items": normalized_items,
        "total_count": payload.get("total_count", len(normalized_items)),
    }


_REPO_ALLOWED = frozenset(_normalize_repo({}).keys())
_REPO_DEFAULT = (
    "full_name",
    "private",
    "visibility",
    "default_branch",
    "archived",
    "html_url",
    "updated_at",
)
_PR_ALLOWED = frozenset(_normalize_pr({}).keys())
_PR_DEFAULT = (
    "number",
    "title",
    "state",
    "draft",
    "merged",
    "user",
    "head",
    "base",
    "html_url",
    "updated_at",
)
_ISSUE_ALLOWED = frozenset(_normalize_issue({}).keys())
_ISSUE_DEFAULT = (
    "number",
    "title",
    "state",
    "state_reason",
    "user",
    "labels",
    "assignees",
    "comments",
    "is_pull_request",
    "html_url",
    "updated_at",
)
_CHECKS_ALLOWED = frozenset({"total_count", "check_runs"})
_CHECKS_DEFAULT = ("total_count", "check_runs")
_SEARCH_ALLOWED = frozenset({"total_count", "incomplete_results", "items"})
_SEARCH_DEFAULT = ("total_count", "incomplete_results", "items")

#: Public, read-only view of the **default result shaping** applied by
#: :class:`GitHubDirectReadExecutor` when a caller passes no ``select``.
#:
#: This is the single source of truth for "what a DIRECT result contains by
#: default". Consumers that must reason about the full default shape — notably
#: the connected acceptance collector, which compares a DIRECT result against
#: the V1 shadow result — read it from here instead of duplicating the tuples,
#: so the two can never drift apart. It is a ``MappingProxyType`` over immutable
#: tuples: exporting it changes no executor default and no executor semantics.
GITHUB_DIRECT_DEFAULT_RESULT_FIELDS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "github.get_repo": _REPO_DEFAULT,
        "github.get_pr": _PR_DEFAULT,
        "github.get_issue": _ISSUE_DEFAULT,
        "github.get_checks": _CHECKS_DEFAULT,
        "github.search": _SEARCH_DEFAULT,
    }
)


class GitHubDirectReadExecutor:
    """Async deterministic executor for the five Phase 2 GitHub read tools."""

    __slots__ = (
        "_authorization_provider",
        "_credential_broker",
        "_max_result_bytes",
        "_registry",
        "_rules",
        "_scope",
        "_transport",
    )

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        rules: PolicyRuleSet,
        credential_broker: CredentialBroker,
        authorization_provider: GitHubAuthorizationProvider,
        scope: GitHubRepositoryScope,
        transport: httpx.AsyncBaseTransport | None = None,
        max_result_bytes: int = _DEFAULT_MAX_RESULT_BYTES,
    ) -> None:
        if not registry.frozen:
            raise ValueError("DIRECT registry must be frozen")
        invalid_type = isinstance(max_result_bytes, bool) or not isinstance(
            max_result_bytes,
            int,
        )
        if invalid_type:
            raise ValueError("max_result_bytes must be an integer")
        if not 1024 <= max_result_bytes <= _MAX_RESULT_BYTES:
            raise ValueError("max_result_bytes outside allowed bounds")
        self._registry = registry
        self._rules = rules
        self._credential_broker = credential_broker
        self._authorization_provider = authorization_provider
        self._scope = scope
        self._transport = transport
        self._max_result_bytes = max_result_bytes

    def _authorize(
        self,
        tool_id: str,
        owner: str,
        repo: str,
    ) -> tuple[ToolDefinition, str, GitHubAuthorization]:
        tool = self._registry.get(tool_id)
        if tool.execution_mode is not ExecutionMode.DIRECT or not tool.read_only:
            raise GitHubDirectDenied("TOOL_NOT_DIRECT_READ")

        # Resource scope is deliberately checked before policy/readiness. This
        # prevents an out-of-scope caller from learning credential health.
        repository = self._scope.require(owner, repo)

        evaluation = PolicyEngine(
            self._registry,
            self._rules,
            self._credential_broker,
        ).evaluate(tool_id)
        if evaluation.decision is not PolicyDecision.ALLOW:
            raise GitHubDirectDenied(f"POLICY_{evaluation.reason_code.value}")

        capability_id = tool.credential_capability_id
        if capability_id != GITHUB_READ_CREDENTIAL_CAPABILITY:
            raise GitHubDirectDenied("INVALID_CREDENTIAL_CAPABILITY")

        authorization = self._authorization_provider.resolve(
            capability_id,
            repository,
        )
        if authorization is None:
            raise GitHubDirectDenied("CREDENTIAL_MATERIAL_UNAVAILABLE")
        return tool, repository, authorization

    async def _get(
        self,
        *,
        tool: ToolDefinition,
        authorization: GitHubAuthorization,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> _BackendResponse:
        headers = {
            "Accept": GITHUB_ACCEPT,
            "Authorization": authorization.header_value(),
            "User-Agent": GITHUB_USER_AGENT,
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }
        try:
            async with httpx.AsyncClient(
                base_url=GITHUB_API_BASE_URL,
                transport=self._transport,
                follow_redirects=False,
                timeout=httpx.Timeout(float(tool.timeout_seconds)),
                trust_env=False,
            ) as client:
                response = await client.get(path, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise GitHubDirectError("UPSTREAM_TRANSPORT_ERROR") from exc

        if response.status_code != 200:
            self._raise_for_status(response)

        try:
            payload = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise GitHubDirectError(
                "INVALID_UPSTREAM_JSON",
                status_code=response.status_code,
            ) from exc
        if not isinstance(payload, dict):
            raise GitHubDirectError(
                "INVALID_UPSTREAM_SHAPE",
                status_code=response.status_code,
            )
        return _BackendResponse(
            payload=payload,
            raw_bytes=len(response.content),
            status_code=response.status_code,
            rate_limit_remaining=_header_int(
                response.headers,
                "X-RateLimit-Remaining",
            ),
            request_id=response.headers.get("X-GitHub-Request-Id"),
        )

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        status = response.status_code
        retry_after = _header_int(response.headers, "Retry-After")
        remaining = _header_int(response.headers, "X-RateLimit-Remaining")
        if status == 429 or (status == 403 and remaining == 0):
            code = "RATE_LIMITED"
        elif status == 401:
            code = "AUTHENTICATION_FAILED"
        elif status == 403:
            code = "FORBIDDEN"
        elif status == 404:
            code = "NOT_FOUND"
        elif status == 410:
            code = "GONE"
        elif status == 422:
            code = "INVALID_REQUEST"
        elif 300 <= status < 400:
            code = "REDIRECT_BLOCKED"
        elif 500 <= status <= 599:
            code = "UPSTREAM_ERROR"
        else:
            code = "UNEXPECTED_STATUS"
        raise GitHubDirectError(
            code,
            status_code=status,
            retry_after_seconds=retry_after,
        )

    def _result(
        self,
        *,
        tool_id: str,
        repository: str,
        shaped: dict[str, Any],
        backend: _BackendResponse,
    ) -> GitHubDirectResult:
        try:
            returned = canonical_json_bytes(shaped)
        except (TypeError, ValueError) as exc:
            raise GitHubDirectError("NON_CANONICAL_RESULT") from exc
        if len(returned) > self._max_result_bytes:
            raise GitHubDirectError("RESULT_BUDGET_EXCEEDED")
        return GitHubDirectResult(
            tool_id=tool_id,
            repository=repository,
            data=shaped,
            raw_bytes=backend.raw_bytes,
            returned_bytes=len(returned),
            status_code=backend.status_code,
            api_version=GITHUB_API_VERSION,
            rate_limit_remaining=backend.rate_limit_remaining,
            request_id=backend.request_id,
        )

    async def get_repo(
        self,
        owner: str,
        repo: str,
        *,
        select: Iterable[str] | None = None,
    ) -> GitHubDirectResult:
        tool, repository, authorization = self._authorize(
            "github.get_repo",
            owner,
            repo,
        )
        owner_path = quote(owner.strip(), safe="")
        repo_path = quote(repo.strip(), safe="")
        backend = await self._get(
            tool=tool,
            authorization=authorization,
            path=f"/repos/{owner_path}/{repo_path}",
        )
        shaped = _select_fields(
            _normalize_repo(backend.payload),
            allowed=_REPO_ALLOWED,
            defaults=_REPO_DEFAULT,
            select=select,
        )
        return self._result(
            tool_id=tool.tool_id,
            repository=repository,
            shaped=shaped,
            backend=backend,
        )

    async def get_pr(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        select: Iterable[str] | None = None,
    ) -> GitHubDirectResult:
        number = _positive_int(
            number,
            minimum=1,
            maximum=2_147_483_647,
            code="INVALID_NUMBER",
        )
        tool, repository, authorization = self._authorize(
            "github.get_pr",
            owner,
            repo,
        )
        owner_path = quote(owner.strip(), safe="")
        repo_path = quote(repo.strip(), safe="")
        backend = await self._get(
            tool=tool,
            authorization=authorization,
            path=f"/repos/{owner_path}/{repo_path}/pulls/{number}",
        )
        shaped = _select_fields(
            _normalize_pr(backend.payload),
            allowed=_PR_ALLOWED,
            defaults=_PR_DEFAULT,
            select=select,
        )
        return self._result(
            tool_id=tool.tool_id,
            repository=repository,
            shaped=shaped,
            backend=backend,
        )

    async def get_issue(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        select: Iterable[str] | None = None,
    ) -> GitHubDirectResult:
        number = _positive_int(
            number,
            minimum=1,
            maximum=2_147_483_647,
            code="INVALID_NUMBER",
        )
        tool, repository, authorization = self._authorize(
            "github.get_issue",
            owner,
            repo,
        )
        owner_path = quote(owner.strip(), safe="")
        repo_path = quote(repo.strip(), safe="")
        backend = await self._get(
            tool=tool,
            authorization=authorization,
            path=f"/repos/{owner_path}/{repo_path}/issues/{number}",
        )
        shaped = _select_fields(
            _normalize_issue(backend.payload),
            allowed=_ISSUE_ALLOWED,
            defaults=_ISSUE_DEFAULT,
            select=select,
        )
        return self._result(
            tool_id=tool.tool_id,
            repository=repository,
            shaped=shaped,
            backend=backend,
        )

    async def get_checks(
        self,
        owner: str,
        repo: str,
        ref: str,
        *,
        per_page: int = 30,
        select: Iterable[str] | None = None,
    ) -> GitHubDirectResult:
        ref = _safe_ref(ref)
        per_page = _positive_int(
            per_page,
            minimum=1,
            maximum=100,
            code="INVALID_PER_PAGE",
        )
        tool, repository, authorization = self._authorize(
            "github.get_checks",
            owner,
            repo,
        )
        owner_path = quote(owner.strip(), safe="")
        repo_path = quote(repo.strip(), safe="")
        ref_path = quote(ref, safe="")
        backend = await self._get(
            tool=tool,
            authorization=authorization,
            path=f"/repos/{owner_path}/{repo_path}/commits/{ref_path}/check-runs",
            params={"filter": "latest", "page": 1, "per_page": per_page},
        )
        shaped = _select_fields(
            _normalize_checks(backend.payload),
            allowed=_CHECKS_ALLOWED,
            defaults=_CHECKS_DEFAULT,
            select=select,
        )
        return self._result(
            tool_id=tool.tool_id,
            repository=repository,
            shaped=shaped,
            backend=backend,
        )

    async def search(
        self,
        owner: str,
        repo: str,
        text: str,
        *,
        item_type: Literal["issue", "pr", "any"] = "any",
        state: Literal["open", "closed", "any"] = "any",
        per_page: int = 20,
        select: Iterable[str] | None = None,
    ) -> GitHubDirectResult:
        text = _safe_search_text(text)
        if item_type not in {"issue", "pr", "any"}:
            raise GitHubDirectDenied("INVALID_SEARCH_TYPE")
        if state not in {"open", "closed", "any"}:
            raise GitHubDirectDenied("INVALID_SEARCH_STATE")
        per_page = _positive_int(
            per_page,
            minimum=1,
            maximum=30,
            code="INVALID_PER_PAGE",
        )

        tool, repository, authorization = self._authorize(
            "github.search",
            owner,
            repo,
        )
        qualifiers = [f"repo:{repository}"]
        if item_type != "any":
            qualifiers.append(f"is:{item_type}")
        if state != "any":
            qualifiers.append(f"state:{state}")
        query = " ".join([text, *qualifiers])

        backend = await self._get(
            tool=tool,
            authorization=authorization,
            path="/search/issues",
            params={"q": query, "page": 1, "per_page": per_page},
        )
        shaped = _select_fields(
            _normalize_search(backend.payload),
            allowed=_SEARCH_ALLOWED,
            defaults=_SEARCH_DEFAULT,
            select=select,
        )
        return self._result(
            tool_id=tool.tool_id,
            repository=repository,
            shaped=shaped,
            backend=backend,
        )


__all__ = [
    "GITHUB_ACCEPT",
    "GITHUB_API_BASE_URL",
    "GITHUB_API_VERSION",
    "GITHUB_DIRECT_DEFAULT_RESULT_FIELDS",
    "GitHubDirectDenied",
    "GitHubDirectError",
    "GitHubDirectReadExecutor",
    "GitHubDirectResult",
    "GitHubRepositoryScope",
]
