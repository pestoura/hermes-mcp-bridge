#!/usr/bin/env python3
"""Five-tool, fixed-repository GitHub read-only MCP for the Phase 2 V1 shadow.

This server is intentionally incapable of mutation or generic execution. The
repository is fixed by configuration, authorization is file-backed, and every
provider request is delegated to the existing GET-only DIRECT executor.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from hermes_mcp_bridge.v2.github_direct import (  # noqa: E402
    GitHubDirectReadExecutor,
    GitHubRepositoryScope,
)
from hermes_mcp_bridge.v2.github_readiness import GitHubReadReadinessBroker  # noqa: E402
from hermes_mcp_bridge.v2.github_registry import (  # noqa: E402
    build_github_direct_read_registry,
    github_direct_read_policy_rules,
)
from hermes_mcp_bridge.v2.github_secret_provider import (  # noqa: E402
    DEFAULT_SECRET_NAME,
    FileGitHubAuthorizationProvider,
    GitHubProviderType,
)
from hermes_mcp_bridge.v2.shadow_isolation import SHADOW_MCP_TOOL_NAMES  # noqa: E402

REPOSITORY_ENV = "HERMES_V2_SHADOW_REPOSITORY"


def _repository() -> tuple[str, str, str]:
    value = str(os.environ.get(REPOSITORY_ENV, "")).strip()
    if value.count("/") != 1 or any(token in value for token in ("*", "?", "[", "]", "\\")):
        raise RuntimeError("SHADOW_REPOSITORY_INVALID")
    owner, repo = value.split("/", 1)
    if not owner or not repo:
        raise RuntimeError("SHADOW_REPOSITORY_INVALID")
    return value, owner, repo


def _executor() -> tuple[GitHubDirectReadExecutor, str, str]:
    repository, owner, repo = _repository()
    scope = GitHubRepositoryScope([repository])
    provider = FileGitHubAuthorizationProvider(
        scope=scope,
        provider_type=GitHubProviderType.GITHUB_APP,
        secret_name=DEFAULT_SECRET_NAME,
    )
    readiness = GitHubReadReadinessBroker(provider)
    if not readiness.is_ready("github.read"):
        raise RuntimeError("SHADOW_GITHUB_CREDENTIAL_NOT_READY")
    return (
        GitHubDirectReadExecutor(
            registry=build_github_direct_read_registry(),
            rules=github_direct_read_policy_rules(),
            credential_broker=readiness,
            authorization_provider=provider,
            scope=scope,
        ),
        owner,
        repo,
    )


mcp = FastMCP(
    "phase2-read",
    instructions=(
        "Phase 2 acceptance shadow. Read-only GitHub access to one fixed repository. "
        "No mutation, shell, filesystem, browser, messaging, or generic execution capability."
    ),
)


@mcp.tool(name="github_get_repo")
async def github_get_repo() -> dict[str, Any]:
    """Read normalized metadata for the single authorized repository."""
    executor, owner, repo = _executor()
    return (await executor.get_repo(owner, repo)).data


@mcp.tool(name="github_get_pr")
async def github_get_pr(number: int) -> dict[str, Any]:
    """Read one pull request by number from the authorized repository."""
    executor, owner, repo = _executor()
    return (await executor.get_pr(owner, repo, number)).data


@mcp.tool(name="github_get_issue")
async def github_get_issue(number: int) -> dict[str, Any]:
    """Read one issue by number from the authorized repository."""
    executor, owner, repo = _executor()
    return (await executor.get_issue(owner, repo, number)).data


@mcp.tool(name="github_get_checks")
async def github_get_checks(ref: str) -> dict[str, Any]:
    """Read check runs for one ref in the authorized repository."""
    executor, owner, repo = _executor()
    return (await executor.get_checks(owner, repo, ref)).data


@mcp.tool(name="github_search")
async def github_search(text: str) -> dict[str, Any]:
    """Search issues and pull requests within the authorized repository."""
    executor, owner, repo = _executor()
    return (await executor.search(owner, repo, text)).data


def main() -> None:
    # Keep the exported surface tied to the contract at import time. A future
    # tool addition must change the isolation schema/tests deliberately.
    if tuple(sorted(SHADOW_MCP_TOOL_NAMES)) != tuple(
        sorted(
            (
                "github_get_checks",
                "github_get_issue",
                "github_get_pr",
                "github_get_repo",
                "github_search",
            )
        )
    ):
        raise RuntimeError("SHADOW_TOOL_CONTRACT_DRIFT")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
