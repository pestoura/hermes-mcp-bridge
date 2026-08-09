"""Phase 3 lane L6 — merge gate chain tests (A3-10, A3-08).

Each gate gets a positive case and at least one fail-closed case, and the
report proves which gates were actually walked.
"""

from __future__ import annotations

import pytest
from merge_fixtures import (
    HEAD,
    OTHER,
    REPO,
    observation,
    policy,
    pull_request,
    request,
)

from hermes_mcp_bridge.v2 import github_governed_merge as gm
from hermes_mcp_bridge.v2.enums import MutationReasonCode, MutationStage
from hermes_mcp_bridge.v2.errors import MergeGovernanceError, MutationDeniedError


def denies(reason: MutationReasonCode, **obs: object) -> MergeGovernanceError:
    with pytest.raises(MutationDeniedError) as excinfo:
        gm.evaluate_merge_gates(request(), observation(**obs), policy())
    assert excinfo.value.reason is reason
    return excinfo.value  # type: ignore[return-value]


# --------------------------------------------------------------------------
# Happy path and gate ordering
# --------------------------------------------------------------------------


def test_all_gates_pass_on_a_clean_governed_merge() -> None:
    report = gm.evaluate_merge_gates(request(), observation(), policy())
    assert report.all_gates_cleared
    assert report.cleared_gates == gm.MERGE_GATE_ORDER
    assert report.merge_method is gm.MergeMethod.SQUASH
    assert report.required_checks == ("ci",)


def test_gate_order_is_complete_unique_and_scope_first() -> None:
    assert len(gm.MERGE_GATE_ORDER) == len(set(gm.MERGE_GATE_ORDER)) == len(gm.MergeGate)
    assert gm.MERGE_GATE_ORDER[0] is gm.MergeGate.REPOSITORY_MERGE_ENABLED
    assert gm.MERGE_GATE_ORDER[-1] is gm.MergeGate.HEAD_SHA_PINNED


def test_report_canonical_is_non_secret_and_sorted() -> None:
    payload = gm.evaluate_merge_gates(request(), observation(), policy()).canonical()
    assert payload["repository"] == REPO
    assert "sha" not in payload
    assert list(payload) == sorted(payload)


# --------------------------------------------------------------------------
# Gate 1/2 — repository enablement and default-branch protection
# --------------------------------------------------------------------------


def test_policy_for_a_different_repository_is_denied() -> None:
    with pytest.raises(MergeGovernanceError) as excinfo:
        gm.evaluate_merge_gates(request(), observation(), policy(repository="other/repo"))
    assert excinfo.value.reason is MutationReasonCode.MERGE_NOT_PERMITTED


def test_merging_into_the_default_branch_is_denied_by_default() -> None:
    with pytest.raises(MergeGovernanceError) as excinfo:
        gm.evaluate_merge_gates(
            request(base="main"),
            observation(pull_request=pull_request(base_ref="main")),
            policy(),
        )
    assert excinfo.value.reason is MutationReasonCode.MERGE_TARGET_DEFAULT_BRANCH


def test_default_branch_merge_is_allowed_only_with_explicit_opt_in() -> None:
    report = gm.evaluate_merge_gates(
        request(base="main"),
        observation(pull_request=pull_request(base_ref="main")),
        policy(default_branch_merge_allowed=True),
    )
    assert report.all_gates_cleared


def test_unknown_default_branch_is_unverifiable_not_permissive() -> None:
    denies(MutationReasonCode.PROTECTION_STATE_UNVERIFIABLE, default_branch="")


# --------------------------------------------------------------------------
# Gate 3 — pull request state
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pr_kwargs",
    (
        {"state": "closed"},
        {"draft": True},
        {"mergeable": None},
        {"mergeable": False},
        {"mergeable_state": "dirty"},
        {"mergeable_state": None},
        {"mergeable_state": "blocked"},
        {"base_ref": "other"},
    ),
)
def test_unmergeable_pull_request_states_are_denied(pr_kwargs: dict[str, object]) -> None:
    denies(
        MutationReasonCode.PULL_REQUEST_NOT_MERGEABLE,
        pull_request=pull_request(**pr_kwargs),
    )


# --------------------------------------------------------------------------
# Gate 4 — required checks
# --------------------------------------------------------------------------


def test_missing_required_check_is_denied() -> None:
    denies(MutationReasonCode.REQUIRED_CHECKS_NOT_GREEN, checks=())


@pytest.mark.parametrize(
    "status,conclusion",
    (
        ("completed", "failure"),
        ("completed", "neutral"),
        ("completed", "skipped"),
        ("completed", None),
        ("in_progress", "success"),
        ("queued", None),
    ),
)
def test_non_green_checks_are_denied(status: str, conclusion: str | None) -> None:
    denies(
        MutationReasonCode.REQUIRED_CHECKS_NOT_GREEN,
        checks=(gm.CheckState(name="ci", status=status, conclusion=conclusion),),
    )


def test_protection_required_checks_are_unioned_with_policy_checks() -> None:
    denies(
        MutationReasonCode.REQUIRED_CHECKS_NOT_GREEN,
        protection=gm.BranchProtectionState(
            readable=True,
            required_checks=frozenset({"ci", "security"}),
            required_approving_review_count=1,
            approving_reviews=1,
        ),
    )


def test_extra_green_checks_do_not_widen_the_required_set() -> None:
    report = gm.evaluate_merge_gates(
        request(),
        observation(
            checks=(
                gm.CheckState(name="ci", status="completed", conclusion="success"),
                gm.CheckState(name="optional", status="completed", conclusion="failure"),
            )
        ),
        policy(),
    )
    assert report.required_checks == ("ci",)


# --------------------------------------------------------------------------
# Gate 5 — protection, reviews, admin bypass
# --------------------------------------------------------------------------


def test_unreadable_protection_state_is_denied() -> None:
    denies(
        MutationReasonCode.PROTECTION_STATE_UNVERIFIABLE,
        protection=gm.BranchProtectionState(readable=False),
    )


def test_admin_bypass_enabled_protection_is_denied() -> None:
    denies(
        MutationReasonCode.PROTECTION_STATE_UNVERIFIABLE,
        protection=gm.BranchProtectionState(
            readable=True,
            required_checks=frozenset({"ci"}),
            required_approving_review_count=1,
            approving_reviews=1,
            enforce_admins=False,
        ),
    )


def test_insufficient_approvals_are_denied() -> None:
    denies(
        MutationReasonCode.REQUIRED_REVIEWS_NOT_SATISFIED,
        protection=gm.BranchProtectionState(
            readable=True,
            required_checks=frozenset({"ci"}),
            required_approving_review_count=2,
            approving_reviews=1,
        ),
    )


def test_changes_requested_blocks_even_with_enough_approvals() -> None:
    denies(
        MutationReasonCode.REQUIRED_REVIEWS_NOT_SATISFIED,
        protection=gm.BranchProtectionState(
            readable=True,
            required_checks=frozenset({"ci"}),
            required_approving_review_count=1,
            approving_reviews=3,
            changes_requested=1,
        ),
    )


def test_self_approval_is_denied_when_a_distinct_approver_is_required() -> None:
    with pytest.raises(MutationDeniedError) as excinfo:
        gm.evaluate_merge_gates(
            request(principal="agent", approver="agent"), observation(), policy()
        )
    assert excinfo.value.reason is MutationReasonCode.APPROVER_NOT_DISTINCT
    assert excinfo.value.stage is MutationStage.APPROVAL


# --------------------------------------------------------------------------
# Gate 6 — head SHA pinning (A3-08)
# --------------------------------------------------------------------------


def test_head_drift_between_approval_and_execution_is_denied() -> None:
    with pytest.raises(MutationDeniedError) as excinfo:
        gm.evaluate_merge_gates(
            request(),
            observation(pull_request=pull_request(head_sha=OTHER)),
            policy(),
        )
    assert excinfo.value.reason is MutationReasonCode.PRECONDITION_DRIFT
    assert excinfo.value.stage is MutationStage.PRECONDITION_REVALIDATION


def test_body_pins_the_sha_and_the_policy_fixed_method() -> None:
    body = gm.merge_request_body(request(), policy())
    assert body == {"merge_method": "squash", "sha": HEAD}


def test_caller_cannot_choose_the_merge_method() -> None:
    assert "merge_method" not in {f for f in gm.MergeRequest.__slots__}


def test_endpoint_is_the_single_allow_listed_merge_path() -> None:
    assert gm.merge_endpoint(request()) == "/repos/octo/lab/pulls/12/merge"


# --------------------------------------------------------------------------
# Response classification
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,expected",
    (
        (200, None),
        (409, MutationReasonCode.PRECONDITION_DRIFT),
        (405, MutationReasonCode.PULL_REQUEST_NOT_MERGEABLE),
        (403, MutationReasonCode.WRITE_CAPABILITY_NOT_READY),
        (404, MutationReasonCode.MERGE_NOT_PERMITTED),
        (500, MutationReasonCode.RECONCILIATION_REQUIRED),
        (502, MutationReasonCode.RECONCILIATION_REQUIRED),
        (201, MutationReasonCode.RECONCILIATION_REQUIRED),
    ),
)
def test_merge_status_classification_is_fail_closed(
    status: int, expected: MutationReasonCode | None
) -> None:
    assert gm.classify_merge_status(status) is expected


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------


def test_denials_never_echo_arguments_or_paths() -> None:
    error = denies(MutationReasonCode.REQUIRED_CHECKS_NOT_GREEN, checks=())
    text = str(error)
    assert REPO not in text
    assert HEAD not in text
    assert "/repos/" not in text
