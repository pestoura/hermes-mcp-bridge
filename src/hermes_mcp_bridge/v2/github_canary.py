"""Explicit, default-OFF canary router for the five ``github.*`` DIRECT reads.

Design constraints (all load-bearing for the Phase 2 gate):

* **Default disabled.** :class:`GitHubCanaryConfig` defaults ``enabled`` to
  ``False``. Nothing DIRECT executes unless a caller explicitly enables it.
* **Exact repository allow-list.** No wildcards; enforced by
  :class:`~hermes_mcp_bridge.v2.github_direct.GitHubRepositoryScope` and
  re-checked here before the executor is consulted.
* **Not on the V1 surface.** This module is never imported by ``server.py`` and
  adds no MCP tool. The V1 contract stays at 27 tools. The canary is reachable
  only through this in-process router and through the internal collector
  harness (``scripts/v2_phase2_direct_read_acceptance.py``).
* **No silent fallback inside a DIRECT sample.** :meth:`GitHubCanaryRouter.route`
  decides ONE path and labels the outcome ``DIRECT`` or ``V1_FALLBACK``. A
  sample tagged DIRECT that fails is reported as a DIRECT failure — it is never
  quietly re-run through V1 under the same label. Callers that want the V1
  behaviour must observe ``RouteDecision.path`` and act on it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum, unique
from typing import Any

from .github_direct import (
    GitHubDirectError,
    GitHubDirectReadExecutor,
    GitHubDirectResult,
    GitHubRepositoryScope,
)
from .github_registry import GITHUB_DIRECT_READ_TOOL_IDS


@unique
class ExecutionPath(StrEnum):
    """Which path a routed operation actually took."""

    DIRECT = "DIRECT"
    V1_FALLBACK = "V1_FALLBACK"


@unique
class FallbackReason(StrEnum):
    """Why DIRECT was not eligible. Never contains a value or a path."""

    NONE = "NONE"
    FEATURE_DISABLED = "FEATURE_DISABLED"
    TOOL_NOT_CANARIED = "TOOL_NOT_CANARIED"
    REPOSITORY_NOT_ALLOWED = "REPOSITORY_NOT_ALLOWED"
    CREDENTIAL_NOT_READY = "CREDENTIAL_NOT_READY"


@dataclass(frozen=True, slots=True)
class GitHubCanaryConfig:
    """Explicit canary wiring. ``enabled`` is ``False`` by default."""

    scope: GitHubRepositoryScope
    enabled: bool = False
    tool_ids: frozenset[str] = field(default_factory=lambda: frozenset(GITHUB_DIRECT_READ_TOOL_IDS))

    def __post_init__(self) -> None:
        unknown = sorted(self.tool_ids - set(GITHUB_DIRECT_READ_TOOL_IDS))
        if unknown:
            raise ValueError("canary tool_ids outside the Phase 2 DIRECT read set")
        if not self.tool_ids:
            raise ValueError("canary tool_ids must not be empty")

    @property
    def repositories(self) -> tuple[str, ...]:
        return self.scope.repositories

    def describe(self) -> dict[str, Any]:
        """Non-secret canary description suitable for evidence."""
        return {
            "direct_feature_enabled": bool(self.enabled),
            "canary_tool_ids": sorted(self.tool_ids),
            "canary_repositories": list(self.repositories),
            "wildcard_scopes": False,
        }


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """Outcome of one routed operation, with an unambiguous path label."""

    tool_id: str
    repository: str
    path: ExecutionPath
    eligible_for_direct: bool
    fallback_reason: FallbackReason = FallbackReason.NONE
    result: GitHubDirectResult | None = None
    error_code: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.result is not None and self.error_code is None

    def describe(self) -> dict[str, Any]:
        return {
            "eligible_for_direct": self.eligible_for_direct,
            "error_code": self.error_code,
            "fallback_reason": self.fallback_reason.value,
            "path": self.path.value,
            "repository": self.repository,
            "tool_id": self.tool_id,
        }


class GitHubCanaryRouter:
    """Route the five DIRECT reads, or explicitly hand back to V1."""

    __slots__ = ("_config", "_executor", "_readiness")

    def __init__(
        self,
        *,
        config: GitHubCanaryConfig,
        executor: GitHubDirectReadExecutor,
        readiness: Any | None = None,
    ) -> None:
        self._config = config
        self._executor = executor
        self._readiness = readiness

    @property
    def config(self) -> GitHubCanaryConfig:
        return self._config

    def eligibility(self, tool_id: str, repository: str) -> FallbackReason:
        """Return ``NONE`` when DIRECT is eligible, else the blocking reason."""
        if not self._config.enabled:
            return FallbackReason.FEATURE_DISABLED
        if tool_id not in self._config.tool_ids:
            return FallbackReason.TOOL_NOT_CANARIED
        if not isinstance(repository, str) or repository.count("/") != 1:
            return FallbackReason.REPOSITORY_NOT_ALLOWED
        owner, repo = repository.split("/", 1)
        try:
            allowed = self._config.scope.allows(owner, repo)
        except Exception:
            allowed = False
        if not allowed:
            return FallbackReason.REPOSITORY_NOT_ALLOWED
        if self._readiness is not None and not self._readiness.is_ready("github.read"):
            return FallbackReason.CREDENTIAL_NOT_READY
        return FallbackReason.NONE

    async def route(
        self,
        tool_id: str,
        repository: str,
        operation: Callable[[GitHubDirectReadExecutor], Awaitable[GitHubDirectResult]],
    ) -> RouteDecision:
        """Execute ``operation`` DIRECT, or return an explicit V1 fallback decision.

        Never silently retries a failed DIRECT attempt through V1: a DIRECT
        failure is returned with ``path=DIRECT`` and an ``error_code``.
        """
        reason = self.eligibility(tool_id, repository)
        if reason is not FallbackReason.NONE:
            return RouteDecision(
                tool_id=tool_id,
                repository=repository,
                path=ExecutionPath.V1_FALLBACK,
                eligible_for_direct=False,
                fallback_reason=reason,
            )

        try:
            result = await operation(self._executor)
        except GitHubDirectError as exc:
            return RouteDecision(
                tool_id=tool_id,
                repository=repository,
                path=ExecutionPath.DIRECT,
                eligible_for_direct=True,
                error_code=exc.code,
            )
        except Exception as exc:  # pragma: no cover - defensive, no detail leaked
            return RouteDecision(
                tool_id=tool_id,
                repository=repository,
                path=ExecutionPath.DIRECT,
                eligible_for_direct=True,
                error_code=f"UNEXPECTED_{type(exc).__name__}",
            )

        return RouteDecision(
            tool_id=tool_id,
            repository=repository,
            path=ExecutionPath.DIRECT,
            eligible_for_direct=True,
            result=result,
        )

    def __repr__(self) -> str:
        return (
            "GitHubCanaryRouter("
            f"enabled={self._config.enabled}, "
            f"tools={len(self._config.tool_ids)}, "
            f"repositories={len(self._config.repositories)})"
        )

    __str__ = __repr__


__all__ = [
    "ExecutionPath",
    "FallbackReason",
    "GitHubCanaryConfig",
    "GitHubCanaryRouter",
    "RouteDecision",
]
