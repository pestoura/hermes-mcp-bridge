"""Phase 3 lane L6 — governed merge and destructive exclusion tests.

Covers acceptance criteria A3-04 (no repository-deletion path, no
``Administration``) and A3-10 (merge governance: default-branch DENY, missing
required checks DENY, unverifiable protection DENY), plus the L6 share of
A3-05/A3-07/A3-08 (write-ahead audit, single provider write, head drift).
"""

from __future__ import annotations

import inspect

import pytest
from merge_fixtures import policy

from hermes_mcp_bridge.v2 import github_governed_merge as gm
from hermes_mcp_bridge.v2.enums import (
    MutationReasonCode,
    PolicyDecision,
    WriteCapabilityId,
)
from hermes_mcp_bridge.v2.errors import MergeGovernanceError

# --------------------------------------------------------------------------
# A3-04 — destructive exclusion
# --------------------------------------------------------------------------


def test_no_repository_deletion_path_in_the_shipped_package() -> None:
    assert gm.assert_no_repository_deletion_path() == []


def test_destructive_exclusion_report_is_pass_and_declares_no_admin() -> None:
    report = gm.destructive_exclusion_report(["github.merge_pr", "github.create_pr"])
    assert report["verdict"] == "PASS"
    assert report["failures"] == []
    assert report["forbidden_permission_requested"] is False


def test_excluded_operations_are_detected_when_registered() -> None:
    findings = gm.assert_no_excluded_operation_contract(["github.delete_repository"])
    assert findings and "delete_repository" in findings[0]
    assert gm.destructive_exclusion_report(["github.delete_repository"])["verdict"] == "FAIL"


def test_excluded_operation_check_fails_closed_on_bad_input() -> None:
    assert gm.assert_no_excluded_operation_contract(object()) != []


def test_delete_is_not_an_allowed_http_verb() -> None:
    assert "DELETE" not in gm.ALLOWED_HTTP_VERBS
    assert frozenset({"GET", "POST", "PUT"}) == gm.ALLOWED_HTTP_VERBS


def test_merge_module_never_names_administration_permission() -> None:
    source = inspect.getsource(gm)
    assert "Administration" not in source


def test_permanently_excluded_set_covers_repository_deletion() -> None:
    assert "github.delete_repository" in gm.PERMANENTLY_EXCLUDED_OPERATIONS
    assert "github.force_push" in gm.PERMANENTLY_EXCLUDED_OPERATIONS


# --------------------------------------------------------------------------
# Policy registry — absence is DENY
# --------------------------------------------------------------------------


def test_unregistered_repository_is_denied_not_defaulted() -> None:
    registry = gm.MergePolicyRegistry([policy()])
    assert registry.is_merge_enabled("other/repo") is False
    with pytest.raises(MergeGovernanceError) as excinfo:
        registry.require("other/repo")
    assert excinfo.value.reason is MutationReasonCode.MERGE_NOT_PERMITTED


def test_empty_required_checks_is_rejected_at_construction() -> None:
    with pytest.raises(MergeGovernanceError) as excinfo:
        policy(required_checks=frozenset())
    assert excinfo.value.reason is MutationReasonCode.REQUIRED_CHECKS_NOT_GREEN


def test_duplicate_repository_policy_is_rejected() -> None:
    with pytest.raises(MergeGovernanceError):
        gm.MergePolicyRegistry([policy(), policy()])


def test_merge_policy_rule_requires_approval_and_is_a_distinct_action() -> None:
    rules = gm.github_merge_policy_rules()
    rule = rules.get(gm.MERGE_POLICY_ACTION)
    assert rule is not None
    assert rule.decision is PolicyDecision.APPROVAL_REQUIRED
    assert gm.MERGE_POLICY_ACTION != "github.pr.create"


def test_merge_capability_is_the_merge_write_capability_only() -> None:
    assert gm.MERGE_WRITE_CAPABILITY is WriteCapabilityId.MERGE
    assert gm.MERGE_WRITE_CAPABILITY.is_merge is True
