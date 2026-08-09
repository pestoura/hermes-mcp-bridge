"""Shared, non-secret builders for the Phase 3 lane L6 merge tests."""

from __future__ import annotations

from hermes_mcp_bridge.v2 import github_governed_merge as gm

REPO = "octo/lab"
HEAD = "a" * 40
OTHER = "b" * 40


def policy(**overrides: object) -> gm.RepositoryMergePolicy:
    kwargs: dict[str, object] = {
        "repository": REPO,
        "merge_method": gm.MergeMethod.SQUASH,
        "required_checks": frozenset({"ci"}),
    }
    kwargs.update(overrides)
    return gm.RepositoryMergePolicy(**kwargs)  # type: ignore[arg-type]


def request(**overrides: object) -> gm.MergeRequest:
    kwargs: dict[str, object] = {
        "principal": "agent",
        "repository": REPO,
        "number": 12,
        "base": "integration",
        "expected_head_sha": HEAD,
        "approval_id": "apr-1",
        "approver": "human",
    }
    kwargs.update(overrides)
    return gm.MergeRequest(**kwargs)  # type: ignore[arg-type]


def pull_request(**overrides: object) -> gm.PullRequestState:
    kwargs: dict[str, object] = {
        "number": 12,
        "state": "open",
        "draft": False,
        "mergeable": True,
        "mergeable_state": "clean",
        "head_sha": HEAD,
        "base_ref": "integration",
    }
    kwargs.update(overrides)
    return gm.PullRequestState(**kwargs)  # type: ignore[arg-type]


def protection(**overrides: object) -> gm.BranchProtectionState:
    kwargs: dict[str, object] = {
        "readable": True,
        "required_checks": frozenset({"ci"}),
        "required_approving_review_count": 1,
        "approving_reviews": 1,
    }
    kwargs.update(overrides)
    return gm.BranchProtectionState(**kwargs)  # type: ignore[arg-type]


def observation(**overrides: object) -> gm.MergeObservation:
    kwargs: dict[str, object] = {
        "pull_request": pull_request(),
        "protection": protection(),
        "checks": (gm.CheckState(name="ci", status="completed", conclusion="success"),),
        "default_branch": "main",
    }
    kwargs.update(overrides)
    return gm.MergeObservation(**kwargs)  # type: ignore[arg-type]
