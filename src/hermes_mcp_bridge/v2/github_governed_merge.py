"""Phase 3 lane L6 — governed merge and destructive exclusion.

Implements ``docs/v2/phase3/governed-merge.md`` and ADR-0023.

Two independent things live here and nothing else:

* ``github.merge_pr`` as a **conditional, governed** mutation. It is not part
  of the create-only increment: it carries its own contract, its own write
  capability (:attr:`~hermes_mcp_bridge.v2.enums.WriteCapabilityId.MERGE`), its
  own policy rule and a fixed gate chain that must pass *in full* before a
  single ``PUT`` is issued.
* the **permanent exclusion** of repository deletion and every equivalent
  destructive administration operation, expressed as executable assertions
  rather than prose.

Fail-closed rules (each has a test)
-----------------------------------

* Merge is denied unless the repository is both in the write allow-list and
  explicitly merge-enabled in policy. A repository absent from the merge policy
  is DENY, never a default-allow.
* Merging into the repository default branch is DENY unless that repository is
  explicitly marked ``default_branch_merge_allowed``.
* The PR must be open, non-draft, ``mergeable is True`` and its
  ``mergeable_state`` must be in an explicit allow-list. ``None``/unknown is
  DENY.
* Every required status check for the base branch must be ``success``. An empty
  or unreadable required-check set is DENY, not a pass.
* Branch protection and review state are read at execution time; an
  unevaluable protection payload is ``PROTECTION_STATE_UNVERIFIABLE`` DENY.
* ``expected_head_sha`` must still match the PR head, and it is sent as ``sha``
  so GitHub itself enforces optimistic concurrency (server ``409``).
* The merge method is policy-fixed per repository; the caller cannot choose it.
* Idempotency class ``NO_RETRY``: an ambiguous merge is resolved by *reading*
  the PR, never by re-issuing the write. Compensation is never automatic.
* No admin bypass, no auto-merge, no merge-queue manipulation: the executor has
  exactly one write verb and one allow-listed path shape.

Destructive exclusion
---------------------

``delete_repository`` and friends have no contract, no registry entry, no
capability and no code path. :func:`assert_no_repository_deletion_path` proves
that statically over the shipped package, and
:data:`PERMANENTLY_EXCLUDED_OPERATIONS` names the closed set so the prohibition
is testable rather than aspirational.

V1 is untouched: this module is inside the isolated ``v2`` package, registers no
MCP tool and is not imported by the V1 server path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum, unique
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

from .enums import (
    MutationReasonCode,
    MutationStage,
    PolicyDecision,
    WriteCapabilityId,
)
from .errors import MergeGovernanceError
from .policy import PolicyRule, PolicyRuleSet

#: The single governed merge operation id.
MERGE_TOOL_ID: Final[str] = "github.merge_pr"

#: Policy action for the governed merge. Distinct from the create actions so a
#: merge can never be authorized by a create rule.
MERGE_POLICY_ACTION: Final[str] = "github.pr.merge"

#: Operations that are permanently out of scope for V2. They have no contract,
#: no capability and no code path; the set exists so the exclusion is testable.
PERMANENTLY_EXCLUDED_OPERATIONS: frozenset[str] = frozenset(
    {
        "github.delete_repository",
        "github.delete_repo",
        "github.delete_ref",
        "github.force_push",
        "github.update_branch_protection",
        "github.manage_webhooks",
        "github.manage_deploy_keys",
        "github.manage_secrets",
        "github.org_admin",
    }
)

#: The only ``mergeable_state`` values a governed merge may proceed on.
ALLOWED_MERGEABLE_STATES: frozenset[str] = frozenset({"clean", "has_hooks"})

#: Check conclusions that count as green. Anything else — including ``None``,
#: ``neutral`` and ``skipped`` — fails the gate.
GREEN_CHECK_CONCLUSIONS: frozenset[str] = frozenset({"success"})

MERGE_TIMEOUT_SECONDS: Final[int] = 30

#: The only capability a governed merge may resolve. It is never the read
#: capability and never the branch/PR create capabilities.
MERGE_WRITE_CAPABILITY: Final[WriteCapabilityId] = WriteCapabilityId.MERGE

_SHA40_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{40}$")
_BRANCH_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")
_REPOSITORY_RE: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"
)


@unique
class MergeMethod(StrEnum):
    """Policy-fixed merge method. The caller never chooses this."""

    MERGE = "merge"
    SQUASH = "squash"
    REBASE = "rebase"


@unique
class MergeGate(StrEnum):
    """The fixed, non-reorderable governed-merge gate chain."""

    REPOSITORY_MERGE_ENABLED = "REPOSITORY_MERGE_ENABLED"
    BASE_NOT_DEFAULT_BRANCH = "BASE_NOT_DEFAULT_BRANCH"
    PULL_REQUEST_STATE = "PULL_REQUEST_STATE"
    REQUIRED_CHECKS_GREEN = "REQUIRED_CHECKS_GREEN"
    PROTECTION_AND_REVIEWS = "PROTECTION_AND_REVIEWS"
    HEAD_SHA_PINNED = "HEAD_SHA_PINNED"


#: The gate order asserted by the evaluator; reordering is a test failure.
MERGE_GATE_ORDER: tuple[MergeGate, ...] = (
    MergeGate.REPOSITORY_MERGE_ENABLED,
    MergeGate.BASE_NOT_DEFAULT_BRANCH,
    MergeGate.PULL_REQUEST_STATE,
    MergeGate.REQUIRED_CHECKS_GREEN,
    MergeGate.PROTECTION_AND_REVIEWS,
    MergeGate.HEAD_SHA_PINNED,
)


def _deny(
    reason: MutationReasonCode,
    stage: MutationStage = MutationStage.POLICY,
    *,
    detail: str = "",
) -> MergeGovernanceError:
    """Build a redacted merge denial. Arguments are never echoed."""
    return MergeGovernanceError(reason, stage, detail=detail)


def _require_repository(value: Any) -> str:
    if not isinstance(value, str) or not _REPOSITORY_RE.fullmatch(value):
        raise _deny(MutationReasonCode.INVALID_ARGUMENTS, MutationStage.SCOPE)
    return value


def _require_branch(value: Any) -> str:
    if not isinstance(value, str) or not _BRANCH_RE.fullmatch(value):
        raise _deny(MutationReasonCode.INVALID_ARGUMENTS, MutationStage.REGISTRY)
    return value


def _require_sha(value: Any) -> str:
    if not isinstance(value, str) or not _SHA40_RE.fullmatch(value):
        raise _deny(MutationReasonCode.INVALID_ARGUMENTS, MutationStage.REGISTRY)
    return value


def _require_pr_number(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _deny(MutationReasonCode.INVALID_ARGUMENTS, MutationStage.REGISTRY)
    return value


# ---------------------------------------------------------------------------
# Policy: which repositories may be merged, and how
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RepositoryMergePolicy:
    """Per-repository merge authorization. Absence of an entry is DENY.

    ``merge_method`` is fixed here, never supplied by the caller.
    ``default_branch_merge_allowed`` defaults to ``False``: merging into the
    repository default branch is refused unless an operator explicitly opts in.
    ``required_checks`` is the closed set of check names that must be green;
    an empty set is rejected at construction so "no required checks" can never
    silently become a pass.
    """

    repository: str
    merge_method: MergeMethod
    required_checks: frozenset[str]
    default_branch_merge_allowed: bool = False
    require_distinct_approver: bool = True

    def __post_init__(self) -> None:
        _require_repository(self.repository)
        if not isinstance(self.merge_method, MergeMethod):
            raise _deny(MutationReasonCode.INVALID_ARGUMENTS)
        if not isinstance(self.required_checks, frozenset) or not self.required_checks:
            raise _deny(MutationReasonCode.REQUIRED_CHECKS_NOT_GREEN)
        for name in self.required_checks:
            if not isinstance(name, str) or not name.strip():
                raise _deny(MutationReasonCode.REQUIRED_CHECKS_NOT_GREEN)
        if not isinstance(self.default_branch_merge_allowed, bool):
            raise _deny(MutationReasonCode.INVALID_ARGUMENTS)
        if not isinstance(self.require_distinct_approver, bool):
            raise _deny(MutationReasonCode.INVALID_ARGUMENTS)

    def canonical(self) -> dict[str, Any]:
        return {
            "default_branch_merge_allowed": self.default_branch_merge_allowed,
            "merge_method": self.merge_method.value,
            "repository": self.repository,
            "require_distinct_approver": self.require_distinct_approver,
            "required_checks": sorted(self.required_checks),
        }


class MergePolicyRegistry:
    """Closed mapping of merge-enabled repositories.

    A repository that is not registered is not merge-enabled, and the lookup
    raises ``MERGE_NOT_PERMITTED`` rather than returning ``None``, so a caller
    cannot accidentally treat absence as permission.
    """

    __slots__ = ("_policies",)

    def __init__(self, policies: list[RepositoryMergePolicy] | None = None) -> None:
        mapping: dict[str, RepositoryMergePolicy] = {}
        for policy in policies or []:
            if policy.repository in mapping:
                raise _deny(MutationReasonCode.INVALID_ARGUMENTS, detail="duplicate_repository")
            mapping[policy.repository] = policy
        self._policies = MappingProxyType(mapping)

    def __len__(self) -> int:
        return len(self._policies)

    def is_merge_enabled(self, repository: str) -> bool:
        return repository in self._policies

    def require(self, repository: str) -> RepositoryMergePolicy:
        policy = self._policies.get(_require_repository(repository))
        if policy is None:
            raise _deny(MutationReasonCode.MERGE_NOT_PERMITTED)
        return policy

    def canonical(self) -> list[dict[str, Any]]:
        return [self._policies[key].canonical() for key in sorted(self._policies)]


def github_merge_policy_rules() -> PolicyRuleSet:
    """The explicit merge rule. Approval is required and cannot be downgraded."""
    return PolicyRuleSet(
        [
            PolicyRule(
                policy_action=MERGE_POLICY_ACTION,
                decision=PolicyDecision.APPROVAL_REQUIRED,
            )
        ]
    )


# ---------------------------------------------------------------------------
# Observed live state (read at execution time, never cached)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PullRequestState:
    """The live PR facts the gate chain needs.

    ``mergeable`` is deliberately tri-state: GitHub returns ``null`` while it
    computes mergeability, and that is DENY, not "probably fine".
    """

    number: int
    state: str
    draft: bool
    mergeable: bool | None
    mergeable_state: str | None
    head_sha: str
    base_ref: str

    def __post_init__(self) -> None:
        _require_pr_number(self.number)
        _require_sha(self.head_sha)
        _require_branch(self.base_ref)
        if not isinstance(self.draft, bool):
            raise _deny(MutationReasonCode.INVALID_ARGUMENTS, MutationStage.REGISTRY)
        if not isinstance(self.state, str) or not self.state:
            raise _deny(MutationReasonCode.INVALID_ARGUMENTS, MutationStage.REGISTRY)
        if self.mergeable is not None and not isinstance(self.mergeable, bool):
            raise _deny(MutationReasonCode.INVALID_ARGUMENTS, MutationStage.REGISTRY)

    @property
    def is_open(self) -> bool:
        return self.state == "open"


@dataclass(frozen=True, slots=True)
class CheckState:
    """One observed check run for the head SHA."""

    name: str
    status: str
    conclusion: str | None

    @property
    def is_green(self) -> bool:
        """Green means completed *and* an explicitly green conclusion."""
        return self.status == "completed" and (self.conclusion or "") in GREEN_CHECK_CONCLUSIONS


@dataclass(frozen=True, slots=True)
class BranchProtectionState:
    """The live protection facts for the base branch.

    ``readable=False`` models an API error or a payload the executor could not
    interpret; it is an immediate ``PROTECTION_STATE_UNVERIFIABLE`` DENY.
    """

    readable: bool
    required_checks: frozenset[str] = frozenset()
    required_approving_review_count: int = 0
    approving_reviews: int = 0
    changes_requested: int = 0
    enforce_admins: bool = True

    @property
    def reviews_satisfied(self) -> bool:
        if self.changes_requested:
            return False
        return self.approving_reviews >= self.required_approving_review_count


@dataclass(frozen=True, slots=True)
class MergeObservation:
    """Everything read live, immediately before the merge decision."""

    pull_request: PullRequestState
    protection: BranchProtectionState
    checks: tuple[CheckState, ...]
    default_branch: str

    def check_by_name(self, name: str) -> CheckState | None:
        for check in self.checks:
            if check.name == name:
                return check
        return None


@dataclass(frozen=True, slots=True)
class MergeRequest:
    """A fully-specified governed merge attempt.

    ``expected_head_sha`` is the approver's pin; it is re-verified against the
    live PR and then sent to GitHub as ``sha`` so the provider enforces the
    same optimistic-concurrency check server-side.
    """

    principal: str
    repository: str
    number: int
    base: str
    expected_head_sha: str
    approval_id: str
    approver: str | None = None

    def __post_init__(self) -> None:
        _require_repository(self.repository)
        _require_pr_number(self.number)
        _require_branch(self.base)
        _require_sha(self.expected_head_sha)
        if not isinstance(self.principal, str) or not self.principal:
            raise _deny(MutationReasonCode.INVALID_ARGUMENTS, MutationStage.REGISTRY)
        if not isinstance(self.approval_id, str) or not self.approval_id:
            raise _deny(MutationReasonCode.APPROVAL_MISSING, MutationStage.APPROVAL)


# ---------------------------------------------------------------------------
# The gate chain
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MergeGateReport:
    """Which gates were evaluated and cleared. Non-secret, canonicalizable."""

    repository: str
    number: int
    cleared_gates: tuple[MergeGate, ...]
    merge_method: MergeMethod
    required_checks: tuple[str, ...]

    @property
    def all_gates_cleared(self) -> bool:
        return self.cleared_gates == MERGE_GATE_ORDER

    def canonical(self) -> dict[str, Any]:
        return {
            "cleared_gates": [gate.value for gate in self.cleared_gates],
            "merge_method": self.merge_method.value,
            "number": self.number,
            "repository": self.repository,
            "required_checks": list(self.required_checks),
        }


def evaluate_merge_gates(
    request: MergeRequest,
    observation: MergeObservation,
    policy: RepositoryMergePolicy,
) -> MergeGateReport:
    """Run the fixed gate chain. Any failure raises; there is no partial pass.

    The chain is evaluated in :data:`MERGE_GATE_ORDER` and the report proves
    which gates were actually walked, so a skipped gate is observable rather
    than implicit.
    """
    cleared: list[MergeGate] = []
    pr = observation.pull_request

    # 1. The repository must be explicitly merge-enabled and consistent.
    if policy.repository != request.repository:
        raise _deny(MutationReasonCode.MERGE_NOT_PERMITTED)
    cleared.append(MergeGate.REPOSITORY_MERGE_ENABLED)

    # 2. Default-branch merge is DENY unless explicitly opted in.
    default_branch = observation.default_branch
    if not isinstance(default_branch, str) or not default_branch:
        raise _deny(MutationReasonCode.PROTECTION_STATE_UNVERIFIABLE)
    if request.base == default_branch and not policy.default_branch_merge_allowed:
        raise _deny(MutationReasonCode.MERGE_TARGET_DEFAULT_BRANCH)
    cleared.append(MergeGate.BASE_NOT_DEFAULT_BRANCH)

    # 3. PR must be open, non-draft, mergeable and in an allow-listed state.
    if not pr.is_open or pr.draft:
        raise _deny(MutationReasonCode.PULL_REQUEST_NOT_MERGEABLE)
    if pr.mergeable is not True:
        raise _deny(MutationReasonCode.PULL_REQUEST_NOT_MERGEABLE)
    if (pr.mergeable_state or "") not in ALLOWED_MERGEABLE_STATES:
        raise _deny(MutationReasonCode.PULL_REQUEST_NOT_MERGEABLE)
    if pr.base_ref != request.base:
        raise _deny(MutationReasonCode.PULL_REQUEST_NOT_MERGEABLE)
    cleared.append(MergeGate.PULL_REQUEST_STATE)

    # 4. Every required check must be observed and green. Absence is DENY.
    required = set(policy.required_checks)
    if observation.protection.readable:
        required |= set(observation.protection.required_checks)
    if not required:
        raise _deny(MutationReasonCode.REQUIRED_CHECKS_NOT_GREEN)
    for name in sorted(required):
        check = observation.check_by_name(name)
        if check is None or not check.is_green:
            raise _deny(MutationReasonCode.REQUIRED_CHECKS_NOT_GREEN)
    cleared.append(MergeGate.REQUIRED_CHECKS_GREEN)

    # 5. Protection must be readable and reviews satisfied; admin bypass never.
    protection = observation.protection
    if not protection.readable:
        raise _deny(MutationReasonCode.PROTECTION_STATE_UNVERIFIABLE)
    if not protection.enforce_admins:
        raise _deny(MutationReasonCode.PROTECTION_STATE_UNVERIFIABLE)
    if not protection.reviews_satisfied:
        raise _deny(MutationReasonCode.REQUIRED_REVIEWS_NOT_SATISFIED)
    if policy.require_distinct_approver and request.approver == request.principal:
        raise _deny(MutationReasonCode.APPROVER_NOT_DISTINCT, MutationStage.APPROVAL)
    cleared.append(MergeGate.PROTECTION_AND_REVIEWS)

    # 6. The approver's pinned head SHA must still be the live head.
    if pr.head_sha != request.expected_head_sha:
        raise _deny(
            MutationReasonCode.PRECONDITION_DRIFT,
            MutationStage.PRECONDITION_REVALIDATION,
        )
    cleared.append(MergeGate.HEAD_SHA_PINNED)

    report = MergeGateReport(
        repository=request.repository,
        number=request.number,
        cleared_gates=tuple(cleared),
        merge_method=policy.merge_method,
        required_checks=tuple(sorted(required)),
    )
    if not report.all_gates_cleared:  # pragma: no cover - defensive
        raise _deny(MutationReasonCode.MERGE_NOT_PERMITTED, detail="gate_chain")
    return report


def merge_request_body(request: MergeRequest, policy: RepositoryMergePolicy) -> dict[str, Any]:
    """The exact provider body. ``sha`` pins optimistic concurrency server-side."""
    return {
        "merge_method": policy.merge_method.value,
        "sha": request.expected_head_sha,
    }


def merge_endpoint(request: MergeRequest) -> str:
    """The single allow-listed merge path. ``PUT`` is the only verb used."""
    owner, _, repo = request.repository.partition("/")
    return f"/repos/{owner}/{repo}/pulls/{request.number}/merge"


def classify_merge_status(status_code: int) -> MutationReasonCode | None:
    """Fail-closed classification of a merge response.

    ``200`` is the only success. ``409`` is head movement (clean drift, never a
    retry). ``405`` is a provider refusal. Anything unenumerated returns
    ``RECONCILIATION_REQUIRED`` so the caller must read the PR back rather than
    guess.
    """
    if status_code == 200:
        return None
    if status_code == 409:
        return MutationReasonCode.PRECONDITION_DRIFT
    if status_code == 405:
        return MutationReasonCode.PULL_REQUEST_NOT_MERGEABLE
    if status_code in (401, 403):
        return MutationReasonCode.WRITE_CAPABILITY_NOT_READY
    if status_code == 404:
        return MutationReasonCode.MERGE_NOT_PERMITTED
    return MutationReasonCode.RECONCILIATION_REQUIRED


# ---------------------------------------------------------------------------
# Destructive exclusion (A3-04)
# ---------------------------------------------------------------------------

#: Request shapes that would delete a repository. None may appear in the
#: shipped package; the scan is static and independent of the preflight script.
_REPO_DELETE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"""["']DELETE["']\s*,\s*["']/repos/"""),
    re.compile(r"""\.delete\(\s*f?["']/repos/"""),
    re.compile(r"""delete_repository\s*\("""),
    re.compile(r"""DELETE\s+/repos/\{[^/}]+\}/\{[^/}]+\}\s*$""", re.MULTILINE),
)

#: Verbs the V2 write path is permitted to use at all.
ALLOWED_HTTP_VERBS: frozenset[str] = frozenset({"GET", "POST", "PUT"})


def _package_sources() -> list[Path]:
    return sorted(p for p in Path(__file__).resolve().parent.rglob("*.py") if p.is_file())


def assert_no_repository_deletion_path() -> list[str]:
    """Static proof that no code path can emit a repository deletion.

    Returns the list of findings; an empty list is the only acceptable result.
    The scan covers the whole shipped ``v2`` package, not just this module, so
    a future lane cannot reintroduce the capability quietly.
    """
    findings: list[str] = []
    for path in _package_sources():
        text = path.read_text(encoding="utf-8")
        if path.name == Path(__file__).name:
            # This module names the prohibition; skip its own pattern literals
            # but still scan for a real call site.
            if re.search(r"""\.delete\(\s*f?["']/repos/""", text):
                findings.append(f"{path.name}: repository deletion call site")
            continue
        for pattern in _REPO_DELETE_PATTERNS:
            if pattern.search(text):
                findings.append(f"{path.name}: matches {pattern.pattern!r}")
    return findings


def assert_no_excluded_operation_contract(registered_tool_ids: Any) -> list[str]:
    """Prove that no permanently-excluded operation has a registry entry."""
    findings: list[str] = []
    try:
        ids = set(registered_tool_ids)
    except TypeError:  # fail closed on an unusable input
        return ["registered_tool_ids is not iterable"]
    for excluded in sorted(PERMANENTLY_EXCLUDED_OPERATIONS):
        if excluded in ids:
            findings.append(f"excluded operation registered: {excluded}")
    return findings


def destructive_exclusion_report(registered_tool_ids: Any = ()) -> dict[str, Any]:
    """Machine-readable A3-04 evidence. The verdict is clean only when empty."""
    findings = assert_no_repository_deletion_path()
    findings.extend(assert_no_excluded_operation_contract(registered_tool_ids))
    return {
        "allowed_http_verbs": sorted(ALLOWED_HTTP_VERBS),
        "excluded_operations": sorted(PERMANENTLY_EXCLUDED_OPERATIONS),
        "failures": findings,
        "forbidden_permission_requested": False,
        "verdict": "PASS" if not findings else "FAIL",
    }


__all__ = [
    "ALLOWED_HTTP_VERBS",
    "ALLOWED_MERGEABLE_STATES",
    "GREEN_CHECK_CONCLUSIONS",
    "MERGE_GATE_ORDER",
    "MERGE_POLICY_ACTION",
    "MERGE_TIMEOUT_SECONDS",
    "MERGE_TOOL_ID",
    "MERGE_WRITE_CAPABILITY",
    "PERMANENTLY_EXCLUDED_OPERATIONS",
    "BranchProtectionState",
    "CheckState",
    "MergeGate",
    "MergeGateReport",
    "MergeMethod",
    "MergeObservation",
    "MergePolicyRegistry",
    "MergeRequest",
    "PullRequestState",
    "RepositoryMergePolicy",
    "assert_no_excluded_operation_contract",
    "assert_no_repository_deletion_path",
    "classify_merge_status",
    "destructive_exclusion_report",
    "evaluate_merge_gates",
    "github_merge_policy_rules",
    "merge_endpoint",
    "merge_request_body",
]
