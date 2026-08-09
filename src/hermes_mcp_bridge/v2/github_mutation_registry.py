"""Phase 3 lane L2 — typed mutation registry for ``create_branch``/``create_pr``.

Scope of this module (and nothing else):

* the two typed mutation contracts specified by
  ``docs/v2/phase3/mutation-semantics.md`` — ``github.create_branch`` and
  ``github.create_pr``;
* their strict (``additionalProperties: false``) input/output schemas;
* their mutation metadata: risk / mutation class, the required write
  capability, the approval requirement, the idempotency class, parallel
  safety, timeout and retry with fail-closed result classification, and the
  mandatory read-back/verification contract;
* explicit per-operation policy rules — a missing rule is DENY by
  construction through the existing ``MISSING_POLICY_RULE`` path;
* fail-closed argument validation (ref grammar, 40-hex preconditions, same
  repository head).

Deliberately **not** in this module:

* no HTTP, no provider client, no executor — lane L5 owns the write path;
* no generic shell / arbitrary-command surface;
* no ``delete_repository`` entry and no merge entry (lane L6);
* no duplicated enum or error taxonomy — every classification value comes
  from :mod:`hermes_mcp_bridge.v2.enums` and every failure is raised from
  :mod:`hermes_mcp_bridge.v2.errors` (Controller lane C-1).

V1 is untouched: this module is inside the isolated ``v2`` package, is not
imported by the V1 server/tool path, and registers no MCP tool.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum, unique
from typing import Any

from .capabilities import CapabilityDescriptor, CapabilityRegistry
from .enums import (
    MUTATION_STAGE_ORDER as MUTATION_STAGE_REFERENCE,
)
from .enums import (
    READ_CAPABILITY_ID,
    ApprovalRequirement,
    CapabilityState,
    ExecutionMode,
    IdempotencySemantics,
    MutationClass,
    MutationOutcome,
    MutationReasonCode,
    MutationStage,
    PolicyDecision,
    ResultShaping,
    RetryClass,
    SecurityTier,
    Stability,
    WriteCapabilityId,
)
from .errors import (
    MutationDeniedError,
    MutationIndeterminateError,
    MutationScopeError,
)
from .github_registry import GITHUB_API_CAPABILITY
from .policy import PolicyRule, PolicyRuleSet
from .registry import ToolRegistry
from .schema import ResourceKey, RetryPolicy, ToolDefinition

#: Capability id prefix every Phase 3 write capability must carry. The read
#: capability can never satisfy a mutation: see :func:`_require_write_capability`.
REQUIRED_WRITE_CAPABILITY_PREFIX = "github.write"

CREATE_BRANCH_TOOL_ID = "github.create_branch"
CREATE_PR_TOOL_ID = "github.create_pr"

#: The complete, closed set of mutations this lane registers. Anything else is
#: an unknown mutation and is denied at lookup time.
MUTATION_TOOL_IDS: tuple[str, ...] = (CREATE_BRANCH_TOOL_ID, CREATE_PR_TOOL_ID)

#: Operations that must never gain a registry entry in V2. Present only so the
#: prohibition is expressible and testable; no contract is built for them.
FORBIDDEN_MUTATION_TOOL_IDS: tuple[str, ...] = (
    "github.delete_repository",
    "github.delete_ref",
    "github.update_ref",
    "github.merge_pr",
)

#: Operations that are destructive by nature. Looking one up raises
#: ``DESTRUCTIVE_OPERATION_FORBIDDEN`` rather than the generic unknown code.
DESTRUCTIVE_TOOL_IDS: frozenset[str] = frozenset({"github.delete_repository", "github.delete_ref"})

_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
_REPO_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_BRANCH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")

MAX_TITLE_LENGTH = 200
MAX_BODY_LENGTH = 8000
MUTATION_TIMEOUT_SECONDS = 30


@unique
class ParallelSafety(StrEnum):
    """Whether two concurrent invocations may run against the same resource.

    Not a duplicate of a C-1 enum: C-1 covers outcome/idempotency/approval
    lifecycle, not scheduling. Both Phase 3 mutations are
    ``SERIALIZE_PER_RESOURCE``: concurrency is bounded by the tool's
    ``resource_key``, never globally and never unbounded.
    """

    SERIALIZE_PER_RESOURCE = "SERIALIZE_PER_RESOURCE"
    UNRESTRICTED = "UNRESTRICTED"

    @property
    def requires_lease(self) -> bool:
        return self is ParallelSafety.SERIALIZE_PER_RESOURCE


def _deny(
    reason: MutationReasonCode,
    stage: MutationStage = MutationStage.REGISTRY,
    *,
    detail: str = "",
) -> MutationDeniedError:
    """Build a redacted denial. Arguments are never placed in the message."""
    return MutationDeniedError(reason, stage, detail=detail)


# ---------------------------------------------------------------------------
# Fail-closed argument validation
# ---------------------------------------------------------------------------


def validate_repository(owner: str, repo: str) -> str:
    """Return the canonical ``owner/repo`` reference or deny.

    Denies with ``INVALID_ARGUMENTS`` at the ``SCOPE`` stage; the caller's raw
    value is never echoed back.
    """
    if not isinstance(owner, str) or not isinstance(repo, str):
        raise MutationScopeError(MutationReasonCode.INVALID_ARGUMENTS, MutationStage.SCOPE)
    if not _REPO_SEGMENT_RE.fullmatch(owner) or not _REPO_SEGMENT_RE.fullmatch(repo):
        raise MutationScopeError(MutationReasonCode.INVALID_ARGUMENTS, MutationStage.SCOPE)
    return f"{owner}/{repo}"


def validate_branch_name(name: str) -> str:
    """Validate a branch name against the Phase 3 strict grammar.

    Rejected: empty, leading ``-``, ``..``, ``//``, backslash, whitespace,
    control characters, non-ASCII, trailing ``/`` or ``.``, ``.lock`` suffix,
    ``@{``, ``~``, ``^``, ``:``, ``?``, ``*``, ``[``, and anything that would
    resolve outside ``refs/heads/`` (a caller-supplied ``refs/`` prefix is a
    rejection, not a normalization).
    """
    if not isinstance(name, str) or not name:
        raise _deny(MutationReasonCode.INVALID_REF_NAME)
    if not name.isascii():
        raise _deny(MutationReasonCode.INVALID_REF_NAME)
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in name):
        raise _deny(MutationReasonCode.INVALID_REF_NAME)
    if any(char.isspace() for char in name):
        raise _deny(MutationReasonCode.INVALID_REF_NAME)
    if name.startswith("-") or name.startswith("/") or name.startswith("."):
        raise _deny(MutationReasonCode.INVALID_REF_NAME)
    if name.endswith("/") or name.endswith(".") or name.endswith(".lock"):
        raise _deny(MutationReasonCode.INVALID_REF_NAME)
    for token in ("..", "//", "\\", "@{", "~", "^", ":", "?", "*", "[", "]"):
        if token in name:
            raise _deny(MutationReasonCode.INVALID_REF_NAME)
    if name.lower().startswith("refs/"):
        raise _deny(MutationReasonCode.INVALID_REF_NAME)
    if not _BRANCH_SEGMENT_RE.fullmatch(name):
        raise _deny(MutationReasonCode.INVALID_REF_NAME)
    return name


def qualified_ref(branch: str) -> str:
    """Return ``refs/heads/<branch>`` for an already-validated branch name.

    The ``refs/heads/`` prefix is added here and is never caller-controlled.
    """
    return f"refs/heads/{validate_branch_name(branch)}"


def validate_sha(value: str, *, stage: MutationStage = MutationStage.REGISTRY) -> str:
    """Require a lowercase 40-hex commit SHA. Abbreviated SHAs are rejected."""
    if not isinstance(value, str) or not _SHA40_RE.fullmatch(value):
        raise _deny(MutationReasonCode.INVALID_ARGUMENTS, stage)
    return value


def _validate_text(value: Any, *, maximum: int, required: bool) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise _deny(MutationReasonCode.INVALID_ARGUMENTS)
    if required and not value.strip():
        raise _deny(MutationReasonCode.INVALID_ARGUMENTS)
    if len(value) > maximum:
        raise _deny(MutationReasonCode.INVALID_ARGUMENTS)
    if any(ord(char) < 0x20 and char not in "\n\t" for char in value):
        raise _deny(MutationReasonCode.INVALID_ARGUMENTS)
    if "\x7f" in value:
        raise _deny(MutationReasonCode.INVALID_ARGUMENTS)
    return value


def _require_write_capability(capability: WriteCapabilityId) -> str:
    """Return the capability id, proving read/write disjointness at build time."""
    value = capability.value
    if value == READ_CAPABILITY_ID or not value.startswith(REQUIRED_WRITE_CAPABILITY_PREFIX):
        raise _deny(MutationReasonCode.WRITE_CAPABILITY_MISMATCH, MutationStage.CREDENTIAL)
    return value


# ---------------------------------------------------------------------------
# Typed mutation contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReadBackContract:
    """Mandatory post-write verification for a mutation.

    A mutation is only ``COMMITTED`` when the read-back observes the created
    object and every field in :attr:`verified_fields` matches the request. An
    unverifiable read-back is :class:`MutationIndeterminateError`, never a
    success and never a clean failure.
    """

    required: bool
    endpoint_template: str
    verified_fields: tuple[str, ...]
    unverifiable_outcome: MutationOutcome = MutationOutcome.AMBIGUOUS

    def __post_init__(self) -> None:
        if not self.required:
            raise _deny(MutationReasonCode.INVALID_ARGUMENTS)
        if not self.verified_fields:
            raise _deny(MutationReasonCode.INVALID_ARGUMENTS)
        if self.unverifiable_outcome is not MutationOutcome.AMBIGUOUS:
            raise _deny(MutationReasonCode.INVALID_ARGUMENTS)

    def indeterminate(self) -> MutationIndeterminateError:
        """The error a verifier must raise when read-back cannot be proven."""
        return MutationIndeterminateError(
            MutationReasonCode.RECONCILIATION_REQUIRED, MutationStage.READ_BACK
        )

    def canonical(self) -> dict[str, Any]:
        return {
            "endpoint_template": self.endpoint_template,
            "required": self.required,
            "unverifiable_outcome": self.unverifiable_outcome.value,
            "verified_fields": list(self.verified_fields),
        }


@dataclass(frozen=True, slots=True)
class MutationContract:
    """Complete, typed metadata for one Phase 3 mutation.

    Every classification field is a C-1 enum. The contract carries no
    credential material, no endpoint host and no executor: it describes what a
    mutation *is*, not how it is performed.
    """

    tool_id: str
    definition: ToolDefinition
    write_capability: WriteCapabilityId
    parallel_safety: ParallelSafety
    read_back: ReadBackContract
    method: str
    endpoint_template: str
    success_status: tuple[int, ...]
    clean_failure_status: tuple[int, ...]
    ambiguous_status: tuple[int, ...]
    precondition_fields: tuple[str, ...]
    result_fields: tuple[str, ...]
    compensation: str

    def __post_init__(self) -> None:
        if self.tool_id not in MUTATION_TOOL_IDS:
            raise _deny(MutationReasonCode.MUTATION_NOT_REGISTERED)
        if self.definition.tool_id != self.tool_id:
            raise _deny(MutationReasonCode.INVALID_ARGUMENTS)
        if self.definition.read_only or not self.mutation_class.mutates:
            raise _deny(MutationReasonCode.INVALID_ARGUMENTS)
        if self.mutation_class.is_destructive or self.definition.is_destructive:
            raise _deny(MutationReasonCode.DESTRUCTIVE_OPERATION_FORBIDDEN)
        if self.method != "POST":
            raise _deny(MutationReasonCode.INVALID_ARGUMENTS)
        if "{" not in self.endpoint_template or self.endpoint_template.startswith("http"):
            raise _deny(MutationReasonCode.INVALID_ARGUMENTS)
        if self.definition.credential_capability_id != self.write_capability.value:
            raise _deny(MutationReasonCode.WRITE_CAPABILITY_MISMATCH, MutationStage.CREDENTIAL)
        _require_write_capability(self.write_capability)
        if self.idempotency is not IdempotencySemantics.IDEMPOTENT_BY_PRECONDITION:
            raise _deny(MutationReasonCode.INVALID_ARGUMENTS)
        if not self.idempotency.requires_precondition or not self.precondition_fields:
            raise _deny(MutationReasonCode.INVALID_ARGUMENTS)
        if self.retry_policy.retry_class is not RetryClass.NO_RETRY:
            raise _deny(MutationReasonCode.INVALID_ARGUMENTS)
        if self.retry_policy.max_attempts != 1:
            raise _deny(MutationReasonCode.INVALID_ARGUMENTS)
        if self.approval_requirement is not ApprovalRequirement.REQUIRED:
            raise _deny(MutationReasonCode.APPROVAL_MISSING, MutationStage.APPROVAL)
        overlap = set(self.success_status) & (
            set(self.clean_failure_status) | set(self.ambiguous_status)
        )
        if overlap or set(self.clean_failure_status) & set(self.ambiguous_status):
            raise _deny(MutationReasonCode.INVALID_ARGUMENTS)

    # --- projected metadata (single source: the ToolDefinition) -----------

    @property
    def mutation_class(self) -> MutationClass:
        return self.definition.mutation_class

    @property
    def security_tier(self) -> SecurityTier:
        return self.definition.security_tier

    @property
    def idempotency(self) -> IdempotencySemantics:
        return self.definition.idempotency

    @property
    def approval_requirement(self) -> ApprovalRequirement:
        return self.definition.approval_requirement

    @property
    def policy_action(self) -> str:
        return self.definition.policy_action

    @property
    def timeout_seconds(self) -> int:
        return self.definition.timeout_seconds

    @property
    def retry_policy(self) -> RetryPolicy:
        return self.definition.retry_policy

    @property
    def required_capability(self) -> str:
        """The write capability id required to execute this mutation."""
        return self.write_capability.value

    def classify_status(self, status_code: int) -> MutationOutcome:
        """Fail-closed classification of a provider status code.

        Known-success ⇒ ``COMMITTED`` (still subject to read-back), known
        clean rejection ⇒ ``FAILED_CLEAN``, everything else — including any
        status the contract does not enumerate — ⇒ ``AMBIGUOUS``.
        """
        if status_code in self.success_status:
            return MutationOutcome.COMMITTED
        if status_code in self.clean_failure_status:
            return MutationOutcome.FAILED_CLEAN
        return MutationOutcome.AMBIGUOUS

    def classify_transport_failure(self) -> MutationOutcome:
        """A timeout/reset is never a clean failure."""
        return MutationOutcome.AMBIGUOUS

    def canonical(self) -> dict[str, Any]:
        """Deterministic, non-secret projection of the contract."""
        return {
            "ambiguous_status": list(self.ambiguous_status),
            "approval_requirement": self.approval_requirement.value,
            "clean_failure_status": list(self.clean_failure_status),
            "compensation": self.compensation,
            "definition": self.definition.canonical(),
            "endpoint_template": self.endpoint_template,
            "idempotency": self.idempotency.value,
            "method": self.method,
            "mutation_class": self.mutation_class.value,
            "parallel_safety": self.parallel_safety.value,
            "precondition_fields": list(self.precondition_fields),
            "read_back": self.read_back.canonical(),
            "required_capability": self.required_capability,
            "result_fields": list(self.result_fields),
            "retry_policy": self.retry_policy.canonical(),
            "security_tier": self.security_tier.value,
            "stage_order_reference": [stage.value for stage in MUTATION_STAGE_REFERENCE],
            "success_status": list(self.success_status),
            "timeout_seconds": self.timeout_seconds,
            "tool_id": self.tool_id,
        }


# ---------------------------------------------------------------------------
# Strict schemas
# ---------------------------------------------------------------------------


def _object_schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required,
    }


def _sha_property(description_hint: str) -> dict[str, Any]:
    return {
        "type": "string",
        "pattern": "^[0-9a-f]{40}$",
        "minLength": 40,
        "maxLength": 40,
        "title": description_hint,
    }


def _repository_properties() -> dict[str, Any]:
    return {
        "owner": {"type": "string", "minLength": 1, "maxLength": 100},
        "repo": {"type": "string", "minLength": 1, "maxLength": 100},
    }


def create_branch_input_schema() -> dict[str, Any]:
    properties = _repository_properties()
    properties.update(
        {
            "branch": {"type": "string", "minLength": 1, "maxLength": 255},
            "base_sha": _sha_property("pinned base commit"),
        }
    )
    return _object_schema(properties, ["owner", "repo", "branch", "base_sha"])


def create_pr_input_schema() -> dict[str, Any]:
    properties = _repository_properties()
    properties.update(
        {
            "head": {"type": "string", "minLength": 1, "maxLength": 255},
            "base": {"type": "string", "minLength": 1, "maxLength": 255},
            "title": {"type": "string", "minLength": 1, "maxLength": MAX_TITLE_LENGTH},
            "body": {"type": "string", "maxLength": MAX_BODY_LENGTH},
            "draft": {"type": "boolean"},
            "expected_head_sha": _sha_property("expected head commit"),
        }
    )
    return _object_schema(
        properties, ["owner", "repo", "head", "base", "title", "expected_head_sha"]
    )


def _result_schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    common = {
        "repository": {"type": "string"},
        "operation_digest": {"type": "string"},
        "idempotency_status": {
            "type": "string",
            "enum": ["NEW", "REPLAYED", "IN_PROGRESS"],
        },
        "outcome": {
            "type": "string",
            "enum": ["PENDING", "COMMITTED", "FAILED_CLEAN", "AMBIGUOUS", "DENIED"],
        },
    }
    common.update(properties)
    return _object_schema(common, ["repository", "outcome", "idempotency_status", *required])


def create_branch_output_schema() -> dict[str, Any]:
    return _result_schema(
        {
            "ref": {"type": "string"},
            "sha": _sha_property("created ref target"),
            "url": {"type": "string"},
        },
        ["ref"],
    )


def create_pr_output_schema() -> dict[str, Any]:
    return _result_schema(
        {
            "number": {"type": "integer", "minimum": 1},
            "head_sha": _sha_property("head commit at creation"),
            "state": {"type": "string", "enum": ["open", "closed"]},
            "draft": {"type": "boolean"},
            "url": {"type": "string"},
        },
        ["number"],
    )


# ---------------------------------------------------------------------------
# Typed argument normalization (fail-closed, no defaults after the digest)
# ---------------------------------------------------------------------------


def normalize_create_branch_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize ``create_branch`` arguments.

    Returns the fully-resolved argument map used for the operation digest.
    Unknown keys are rejected (schema-closed), never ignored.
    """
    _reject_unknown_keys(arguments, create_branch_input_schema())
    repository = validate_repository(arguments.get("owner"), arguments.get("repo"))
    branch = validate_branch_name(arguments.get("branch"))
    base_sha = validate_sha(arguments.get("base_sha"))
    return {
        "base_sha": base_sha,
        "branch": branch,
        "ref": f"refs/heads/{branch}",
        "repository": repository,
    }


def normalize_create_pr_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize ``create_pr`` arguments.

    ``head`` and ``base`` are plain branch names in the *same* repository: a
    cross-fork ``owner:branch`` head is rejected by the branch grammar, and
    ``head == base`` is rejected explicitly. ``draft`` defaults to ``True``.
    """
    _reject_unknown_keys(arguments, create_pr_input_schema())
    repository = validate_repository(arguments.get("owner"), arguments.get("repo"))
    head = validate_branch_name(arguments.get("head"))
    base = validate_branch_name(arguments.get("base"))
    if head == base:
        raise _deny(MutationReasonCode.INVALID_ARGUMENTS)
    title = _validate_text(arguments.get("title"), maximum=MAX_TITLE_LENGTH, required=True)
    body = _validate_text(arguments.get("body"), maximum=MAX_BODY_LENGTH, required=False)
    draft = arguments.get("draft", True)
    if not isinstance(draft, bool):
        raise _deny(MutationReasonCode.INVALID_ARGUMENTS)
    expected_head_sha = validate_sha(arguments.get("expected_head_sha"))
    return {
        "base": base,
        "body": body,
        "draft": draft,
        "expected_head_sha": expected_head_sha,
        "head": head,
        "repository": repository,
        "title": title,
    }


def _reject_unknown_keys(arguments: Any, schema: dict[str, Any]) -> None:
    if not isinstance(arguments, dict):
        raise _deny(MutationReasonCode.INVALID_ARGUMENTS)
    allowed = set(schema["properties"])
    if set(arguments) - allowed:
        raise _deny(MutationReasonCode.INVALID_ARGUMENTS)
    missing = set(schema["required"]) - set(arguments)
    if missing:
        raise _deny(MutationReasonCode.INVALID_ARGUMENTS)


#: Argument normalizers keyed by tool id. L5 must use these rather than
#: re-implementing validation.
ARGUMENT_NORMALIZERS = {
    CREATE_BRANCH_TOOL_ID: normalize_create_branch_arguments,
    CREATE_PR_TOOL_ID: normalize_create_pr_arguments,
}


def normalize_arguments(tool_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Normalize arguments for a registered mutation; unknown ⇒ DENY."""
    normalizer = ARGUMENT_NORMALIZERS.get(tool_id)
    if normalizer is None:
        raise _unknown_mutation(tool_id)
    return normalizer(arguments)


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------


def _unknown_mutation(tool_id: Any) -> MutationDeniedError:
    """Denial for a lookup of something this lane does not register."""
    identifier = tool_id.strip().lower() if isinstance(tool_id, str) else ""
    if identifier in DESTRUCTIVE_TOOL_IDS:
        return _deny(MutationReasonCode.DESTRUCTIVE_OPERATION_FORBIDDEN)
    if identifier in FORBIDDEN_MUTATION_TOOL_IDS:
        return _deny(MutationReasonCode.MUTATION_NOT_REGISTERED)
    return _deny(MutationReasonCode.UNKNOWN_MUTATION)


def _mutation_definition(
    *,
    tool_id: str,
    policy_action: str,
    input_schema: dict[str, Any],
    output_schema: dict[str, Any],
    capability: WriteCapabilityId,
) -> ToolDefinition:
    return ToolDefinition(
        tool_id=tool_id,
        provider="github",
        operation=tool_id.split(".", 1)[1],
        execution_mode=ExecutionMode.DIRECT,
        input_schema=input_schema,
        output_schema=output_schema,
        security_tier=SecurityTier.T3,
        read_only=False,
        mutation_class=MutationClass.STANDARD,
        idempotency=IdempotencySemantics.IDEMPOTENT_BY_PRECONDITION,
        policy_action=policy_action,
        approval_requirement=ApprovalRequirement.REQUIRED,
        capability_id=GITHUB_API_CAPABILITY,
        credential_capability_id=_require_write_capability(capability),
        timeout_seconds=MUTATION_TIMEOUT_SECONDS,
        retry_policy=RetryPolicy(retry_class=RetryClass.NO_RETRY, max_attempts=1),
        resource_key=ResourceKey(scope="repository", selector="default"),
        result_shaping=ResultShaping.REQUIRED,
        stability=Stability.EXPERIMENTAL,
        backend="github-rest",
    )


def create_branch_contract() -> MutationContract:
    """Typed contract for ``github.create_branch`` (create a new ref only)."""
    return MutationContract(
        tool_id=CREATE_BRANCH_TOOL_ID,
        definition=_mutation_definition(
            tool_id=CREATE_BRANCH_TOOL_ID,
            policy_action="github.branch.create",
            input_schema=create_branch_input_schema(),
            output_schema=create_branch_output_schema(),
            capability=WriteCapabilityId.BRANCH,
        ),
        write_capability=WriteCapabilityId.BRANCH,
        parallel_safety=ParallelSafety.SERIALIZE_PER_RESOURCE,
        read_back=ReadBackContract(
            required=True,
            endpoint_template="/repos/{owner}/{repo}/git/ref/heads/{branch}",
            verified_fields=("ref", "sha"),
        ),
        method="POST",
        endpoint_template="/repos/{owner}/{repo}/git/refs",
        success_status=(201,),
        clean_failure_status=(401, 403, 404, 422),
        ambiguous_status=(408, 429, 500, 502, 503, 504),
        precondition_fields=("base_sha",),
        result_fields=("ref", "sha", "url"),
        compensation="delete_created_ref_if_unreferenced_and_at_base_sha",
    )


def create_pr_contract() -> MutationContract:
    """Typed contract for ``github.create_pr`` (open one draft PR)."""
    return MutationContract(
        tool_id=CREATE_PR_TOOL_ID,
        definition=_mutation_definition(
            tool_id=CREATE_PR_TOOL_ID,
            policy_action="github.pr.create",
            input_schema=create_pr_input_schema(),
            output_schema=create_pr_output_schema(),
            capability=WriteCapabilityId.PR,
        ),
        write_capability=WriteCapabilityId.PR,
        parallel_safety=ParallelSafety.SERIALIZE_PER_RESOURCE,
        read_back=ReadBackContract(
            required=True,
            endpoint_template="/repos/{owner}/{repo}/pulls/{number}",
            verified_fields=("number", "head_sha", "state"),
        ),
        method="POST",
        endpoint_template="/repos/{owner}/{repo}/pulls",
        success_status=(201,),
        clean_failure_status=(401, 403, 404, 422),
        ambiguous_status=(408, 429, 500, 502, 503, 504),
        precondition_fields=("expected_head_sha",),
        result_fields=("number", "head_sha", "state", "draft", "url"),
        compensation="close_pull_request_never_delete",
    )


def mutation_contracts() -> dict[str, MutationContract]:
    """Both contracts keyed by tool id, in stable order."""
    contracts = [create_branch_contract(), create_pr_contract()]
    return {contract.tool_id: contract for contract in sorted(contracts, key=lambda c: c.tool_id)}


def mutation_definitions() -> list[ToolDefinition]:
    """The two typed mutation definitions in stable tool-id order."""
    return [contract.definition for contract in mutation_contracts().values()]


# ---------------------------------------------------------------------------
# Registry and policy
# ---------------------------------------------------------------------------


def build_github_mutation_registry(
    *,
    api_state: CapabilityState = CapabilityState.READY,
    branch_capability_state: CapabilityState = CapabilityState.READY,
    pr_capability_state: CapabilityState = CapabilityState.READY,
) -> ToolRegistry:
    """Build a frozen registry containing exactly the two Phase 3 mutations.

    The write capabilities are registered as their own descriptors; the read
    capability is deliberately absent, so no mutation can resolve
    ``github.read``.
    """
    capabilities = CapabilityRegistry(
        [
            CapabilityDescriptor(
                capability_id=GITHUB_API_CAPABILITY,
                provider="github",
                state=api_state,
                description="GitHub REST API connectivity for typed V2 operations.",
            ),
            CapabilityDescriptor(
                capability_id=WriteCapabilityId.BRANCH.value,
                provider="github",
                state=branch_capability_state,
                description="Least-privilege authorization to create a branch ref.",
            ),
            CapabilityDescriptor(
                capability_id=WriteCapabilityId.PR.value,
                provider="github",
                state=pr_capability_state,
                description="Least-privilege authorization to open a pull request.",
            ),
        ]
    )
    return ToolRegistry(capabilities, mutation_definitions()).freeze()


def github_mutation_policy_rules() -> PolicyRuleSet:
    """Explicit per-operation rules. No wildcard, no rule for anything else.

    Both operations are ``APPROVAL_REQUIRED`` by rule *and* ``REQUIRED`` by
    tool declaration, so neither can be downgraded to a bare ALLOW. Any other
    action has no rule and is denied through ``MISSING_POLICY_RULE``.
    """
    return PolicyRuleSet(
        [
            PolicyRule(
                policy_action="github.branch.create",
                decision=PolicyDecision.APPROVAL_REQUIRED,
            ),
            PolicyRule(
                policy_action="github.pr.create",
                decision=PolicyDecision.APPROVAL_REQUIRED,
            ),
        ]
    )


def get_mutation_contract(tool_id: str) -> MutationContract:
    """Fail-closed contract lookup. Unknown/forbidden operations DENY."""
    if not isinstance(tool_id, str):
        raise _unknown_mutation(tool_id)
    contract = mutation_contracts().get(tool_id.strip().lower())
    if contract is None:
        raise _unknown_mutation(tool_id)
    return contract


def exported_mutation_contracts() -> dict[str, dict[str, Any]]:
    """Canonical, non-secret export of both contracts for lane L5."""
    return {tool_id: contract.canonical() for tool_id, contract in mutation_contracts().items()}


__all__ = [
    "ARGUMENT_NORMALIZERS",
    "CREATE_BRANCH_TOOL_ID",
    "CREATE_PR_TOOL_ID",
    "DESTRUCTIVE_TOOL_IDS",
    "FORBIDDEN_MUTATION_TOOL_IDS",
    "MAX_BODY_LENGTH",
    "MAX_TITLE_LENGTH",
    "MUTATION_STAGE_REFERENCE",
    "MUTATION_TIMEOUT_SECONDS",
    "MUTATION_TOOL_IDS",
    "REQUIRED_WRITE_CAPABILITY_PREFIX",
    "MutationContract",
    "ParallelSafety",
    "ReadBackContract",
    "build_github_mutation_registry",
    "create_branch_contract",
    "create_branch_input_schema",
    "create_branch_output_schema",
    "create_pr_contract",
    "create_pr_input_schema",
    "create_pr_output_schema",
    "exported_mutation_contracts",
    "get_mutation_contract",
    "github_mutation_policy_rules",
    "mutation_contracts",
    "mutation_definitions",
    "normalize_arguments",
    "normalize_create_branch_arguments",
    "normalize_create_pr_arguments",
    "qualified_ref",
    "validate_branch_name",
    "validate_repository",
    "validate_sha",
]
