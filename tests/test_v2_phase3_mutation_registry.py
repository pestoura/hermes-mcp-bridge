"""Lane L2 tests — typed GitHub mutation registry (`create_branch`, `create_pr`).

Hermetic: no network, no credential material, no executor. These tests own the
L2 contract surface only; the executor (L5), approval/digest (L3), audit (L4)
and merge governance (L6) are out of scope here.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from hermes_mcp_bridge.v2 import enums as c1_enums
from hermes_mcp_bridge.v2 import errors as c1_errors
from hermes_mcp_bridge.v2 import github_mutation_registry as reg
from hermes_mcp_bridge.v2.canonical import canonical_hash
from hermes_mcp_bridge.v2.enums import (
    ApprovalRequirement,
    CapabilityState,
    IdempotencySemantics,
    MutationClass,
    MutationOutcome,
    MutationReasonCode,
    MutationStage,
    PolicyDecision,
    RetryClass,
    SecurityTier,
    WriteCapabilityId,
)
from hermes_mcp_bridge.v2.errors import (
    MutationDeniedError,
    MutationIndeterminateError,
    MutationScopeError,
)
from hermes_mcp_bridge.v2.github_registry import (
    build_github_direct_read_registry,
    github_direct_read_definitions,
)
from hermes_mcp_bridge.v2.policy import PolicyEngine, ReasonCode

MODULE_PATH = Path(reg.__file__)
SOURCE = MODULE_PATH.read_text(encoding="utf-8")

VALID_BRANCH = {
    "owner": "pestoura",
    "repo": "hermes-mcp-bridge",
    "branch": "feat/lane-l2",
    "base_sha": "a" * 40,
}
VALID_PR = {
    "owner": "pestoura",
    "repo": "hermes-mcp-bridge",
    "head": "feat/lane-l2",
    "base": "main",
    "title": "Add lane L2",
    "expected_head_sha": "b" * 40,
}


# --------------------------------------------------------------------------
# Registered surface: exactly two mutations, nothing else
# --------------------------------------------------------------------------


def test_registry_exposes_exactly_create_branch_and_create_pr():
    assert reg.MUTATION_TOOL_IDS == ("github.create_branch", "github.create_pr")
    assert set(reg.mutation_contracts()) == set(reg.MUTATION_TOOL_IDS)
    assert [d.tool_id for d in reg.mutation_definitions()] == [
        "github.create_branch",
        "github.create_pr",
    ]


def test_no_delete_repository_entry_anywhere():
    assert "github.delete_repository" not in reg.mutation_contracts()
    assert "github.delete_repository" in reg.DESTRUCTIVE_TOOL_IDS
    for contract in reg.mutation_contracts().values():
        assert "delete" not in contract.endpoint_template
        assert contract.method == "POST"
    assert "DELETE" not in SOURCE
    assert '/repos/{owner}/{repo}"' not in SOURCE


def test_delete_repository_lookup_denies_as_destructive():
    with pytest.raises(MutationDeniedError) as excinfo:
        reg.get_mutation_contract("github.delete_repository")
    assert excinfo.value.reason is MutationReasonCode.DESTRUCTIVE_OPERATION_FORBIDDEN


@pytest.mark.parametrize("tool_id", ["github.merge_pr", "github.update_ref"])
def test_out_of_lane_operations_are_not_registered(tool_id):
    with pytest.raises(MutationDeniedError) as excinfo:
        reg.get_mutation_contract(tool_id)
    assert excinfo.value.reason is MutationReasonCode.MUTATION_NOT_REGISTERED


@pytest.mark.parametrize("tool_id", ["github.get_repo", "shell", "", None, 7])
def test_unknown_operation_denies(tool_id):
    with pytest.raises(MutationDeniedError) as excinfo:
        reg.get_mutation_contract(tool_id)
    assert excinfo.value.reason is MutationReasonCode.UNKNOWN_MUTATION


def test_no_generic_shell_or_command_surface():
    lowered = SOURCE.lower()
    for token in ("subprocess", "os.system", "shell=true", "exec_command", "popen"):
        assert token not in lowered


# --------------------------------------------------------------------------
# Mutation metadata
# --------------------------------------------------------------------------


@pytest.mark.parametrize("tool_id", reg.MUTATION_TOOL_IDS)
def test_risk_and_mutation_class(tool_id):
    contract = reg.get_mutation_contract(tool_id)
    assert contract.mutation_class is MutationClass.STANDARD
    assert contract.mutation_class.mutates
    assert not contract.mutation_class.is_destructive
    assert contract.security_tier is SecurityTier.T3
    assert not contract.security_tier.is_read_only_tier
    assert not contract.security_tier.is_destructive


@pytest.mark.parametrize(
    ("tool_id", "capability"),
    [
        ("github.create_branch", WriteCapabilityId.BRANCH),
        ("github.create_pr", WriteCapabilityId.PR),
    ],
)
def test_required_capability_is_github_write(tool_id, capability):
    contract = reg.get_mutation_contract(tool_id)
    assert contract.write_capability is capability
    assert contract.required_capability == capability.value
    assert contract.required_capability.startswith(reg.REQUIRED_WRITE_CAPABILITY_PREFIX)
    assert contract.definition.credential_capability_id == capability.value


def test_read_capability_can_never_satisfy_a_mutation():
    read_ids = {d.credential_capability_id for d in github_direct_read_definitions()}
    write_ids = {c.required_capability for c in reg.mutation_contracts().values()}
    assert read_ids == {c1_enums.READ_CAPABILITY_ID}
    assert not (read_ids & write_ids)
    registry = reg.build_github_mutation_registry()
    assert not registry.capabilities.contains(c1_enums.READ_CAPABILITY_ID)


def test_write_capability_absent_from_the_read_registry():
    read_registry = build_github_direct_read_registry()
    for capability in WriteCapabilityId:
        assert not read_registry.capabilities.contains(capability.value)


@pytest.mark.parametrize("tool_id", reg.MUTATION_TOOL_IDS)
def test_approval_is_required_and_not_downgradable(tool_id):
    contract = reg.get_mutation_contract(tool_id)
    assert contract.approval_requirement is ApprovalRequirement.REQUIRED
    assert contract.approval_requirement.may_require_approval


@pytest.mark.parametrize("tool_id", reg.MUTATION_TOOL_IDS)
def test_idempotency_class_is_by_precondition(tool_id):
    contract = reg.get_mutation_contract(tool_id)
    assert contract.idempotency is IdempotencySemantics.IDEMPOTENT_BY_PRECONDITION
    assert contract.idempotency.requires_precondition
    assert not contract.idempotency.requires_idempotency_key
    assert contract.precondition_fields


def test_precondition_fields_match_the_design():
    assert reg.get_mutation_contract("github.create_branch").precondition_fields == ("base_sha",)
    assert reg.get_mutation_contract("github.create_pr").precondition_fields == (
        "expected_head_sha",
    )


@pytest.mark.parametrize("tool_id", reg.MUTATION_TOOL_IDS)
def test_parallel_safety_serializes_per_resource(tool_id):
    contract = reg.get_mutation_contract(tool_id)
    assert contract.parallel_safety is reg.ParallelSafety.SERIALIZE_PER_RESOURCE
    assert contract.parallel_safety.requires_lease
    assert contract.definition.resource_key.scope == "repository"


@pytest.mark.parametrize("tool_id", reg.MUTATION_TOOL_IDS)
def test_timeout_and_retry_are_fail_closed(tool_id):
    contract = reg.get_mutation_contract(tool_id)
    assert contract.timeout_seconds == reg.MUTATION_TIMEOUT_SECONDS == 30
    assert contract.retry_policy.retry_class is RetryClass.NO_RETRY
    assert contract.retry_policy.max_attempts == 1


# --------------------------------------------------------------------------
# Result classification (fail-closed)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("tool_id", reg.MUTATION_TOOL_IDS)
def test_status_classification_success_and_clean_failure(tool_id):
    contract = reg.get_mutation_contract(tool_id)
    assert contract.classify_status(201) is MutationOutcome.COMMITTED
    for status in (401, 403, 404, 422):
        outcome = contract.classify_status(status)
        assert outcome is MutationOutcome.FAILED_CLEAN
        assert outcome.allows_new_attempt


@pytest.mark.parametrize("tool_id", reg.MUTATION_TOOL_IDS)
@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_ambiguous_status_requires_reconciliation(tool_id, status):
    contract = reg.get_mutation_contract(tool_id)
    outcome = contract.classify_status(status)
    assert outcome is MutationOutcome.AMBIGUOUS
    assert outcome.requires_reconciliation
    assert not outcome.allows_new_attempt


@pytest.mark.parametrize("tool_id", reg.MUTATION_TOOL_IDS)
@pytest.mark.parametrize("status", [0, 199, 200, 202, 301, 418, 599])
def test_unenumerated_status_defaults_to_ambiguous(tool_id, status):
    """Fail closed: a status the contract does not know is never a success."""
    contract = reg.get_mutation_contract(tool_id)
    assert contract.classify_status(status) is MutationOutcome.AMBIGUOUS


@pytest.mark.parametrize("tool_id", reg.MUTATION_TOOL_IDS)
def test_transport_failure_is_never_clean(tool_id):
    contract = reg.get_mutation_contract(tool_id)
    assert contract.classify_transport_failure() is MutationOutcome.AMBIGUOUS


@pytest.mark.parametrize("tool_id", reg.MUTATION_TOOL_IDS)
def test_status_classes_are_disjoint(tool_id):
    contract = reg.get_mutation_contract(tool_id)
    success = set(contract.success_status)
    clean = set(contract.clean_failure_status)
    ambiguous = set(contract.ambiguous_status)
    assert not success & clean
    assert not success & ambiguous
    assert not clean & ambiguous


# --------------------------------------------------------------------------
# Read-back / verification
# --------------------------------------------------------------------------


@pytest.mark.parametrize("tool_id", reg.MUTATION_TOOL_IDS)
def test_read_back_is_mandatory(tool_id):
    contract = reg.get_mutation_contract(tool_id)
    assert contract.read_back.required is True
    assert contract.read_back.verified_fields
    assert contract.read_back.unverifiable_outcome is MutationOutcome.AMBIGUOUS


def test_read_back_verifies_the_designed_fields():
    branch = reg.get_mutation_contract("github.create_branch").read_back
    pr = reg.get_mutation_contract("github.create_pr").read_back
    assert branch.verified_fields == ("ref", "sha")
    assert pr.verified_fields == ("number", "head_sha", "state")


@pytest.mark.parametrize("tool_id", reg.MUTATION_TOOL_IDS)
def test_unverifiable_read_back_raises_indeterminate_not_denial(tool_id):
    contract = reg.get_mutation_contract(tool_id)
    error = contract.read_back.indeterminate()
    assert isinstance(error, MutationIndeterminateError)
    assert not isinstance(error, MutationDeniedError)
    assert error.reason is MutationReasonCode.RECONCILIATION_REQUIRED
    assert error.stage is MutationStage.READ_BACK


def test_read_back_cannot_be_declared_optional():
    with pytest.raises(MutationDeniedError):
        reg.ReadBackContract(required=False, endpoint_template="/x/{y}", verified_fields=("a",))
    with pytest.raises(MutationDeniedError):
        reg.ReadBackContract(required=True, endpoint_template="/x/{y}", verified_fields=())


# --------------------------------------------------------------------------
# Policy: explicit rules only, missing rule is DENY
# --------------------------------------------------------------------------


def test_policy_rules_are_explicit_and_approval_required():
    rules = reg.github_mutation_policy_rules()
    assert len(rules) == 2
    for rule in rules.ordered():
        assert rule.decision is PolicyDecision.APPROVAL_REQUIRED
        assert rule.policy_action in {"github.branch.create", "github.pr.create"}


@pytest.mark.parametrize("tool_id", reg.MUTATION_TOOL_IDS)
def test_policy_engine_yields_approval_required(tool_id):
    registry = reg.build_github_mutation_registry()
    broker = _ready_broker()
    engine = PolicyEngine(registry, reg.github_mutation_policy_rules(), broker)
    evaluation = engine.evaluate(tool_id)
    assert evaluation.decision is PolicyDecision.APPROVAL_REQUIRED
    assert evaluation.reason_code is ReasonCode.APPROVAL_REQUIRED_BY_RULE
    assert not evaluation.allowed


@pytest.mark.parametrize("tool_id", reg.MUTATION_TOOL_IDS)
def test_missing_policy_rule_denies_by_construction(tool_id):
    from hermes_mcp_bridge.v2.policy import PolicyRuleSet

    registry = reg.build_github_mutation_registry()
    engine = PolicyEngine(registry, PolicyRuleSet([]), _ready_broker())
    evaluation = engine.evaluate(tool_id)
    assert evaluation.decision is PolicyDecision.DENY
    assert evaluation.reason_code is ReasonCode.MISSING_POLICY_RULE


@pytest.mark.parametrize("tool_id", reg.MUTATION_TOOL_IDS)
def test_explicit_allow_rule_cannot_bypass_tool_level_approval(tool_id):
    from hermes_mcp_bridge.v2.policy import PolicyRule, PolicyRuleSet

    registry = reg.build_github_mutation_registry()
    action = reg.get_mutation_contract(tool_id).policy_action
    rules = PolicyRuleSet([PolicyRule(policy_action=action, decision=PolicyDecision.ALLOW)])
    evaluation = PolicyEngine(registry, rules, _ready_broker()).evaluate(tool_id)
    assert evaluation.decision is PolicyDecision.APPROVAL_REQUIRED
    assert evaluation.reason_code is ReasonCode.APPROVAL_REQUIRED_BY_TOOL


def test_no_wildcard_rule_can_be_written():
    from hermes_mcp_bridge.v2.errors import PolicyValidationError
    from hermes_mcp_bridge.v2.policy import PolicyRule

    for action in ("github.*", "github.pr.?", "github.[a-z].create"):
        with pytest.raises(PolicyValidationError):
            PolicyRule(policy_action=action, decision=PolicyDecision.ALLOW)


@pytest.mark.parametrize("tool_id", reg.MUTATION_TOOL_IDS)
def test_write_capability_not_ready_denies(tool_id):
    from hermes_mcp_bridge.v2.credentials import (
        CredentialCapabilityStatus,
        StaticCredentialBroker,
    )

    registry = reg.build_github_mutation_registry()
    broker = StaticCredentialBroker(
        [
            CredentialCapabilityStatus(
                capability_id=capability.value,
                provider="github",
                state=CapabilityState.DEGRADED,
            )
            for capability in (WriteCapabilityId.BRANCH, WriteCapabilityId.PR)
        ]
    )
    engine = PolicyEngine(registry, reg.github_mutation_policy_rules(), broker)
    evaluation = engine.evaluate(tool_id)
    assert evaluation.decision is PolicyDecision.DENY
    assert evaluation.reason_code is ReasonCode.CREDENTIAL_CAPABILITY_NOT_READY


def _ready_broker():
    from hermes_mcp_bridge.v2.credentials import (
        CredentialCapabilityStatus,
        StaticCredentialBroker,
    )

    return StaticCredentialBroker(
        [
            CredentialCapabilityStatus(
                capability_id=capability.value,
                provider="github",
                state=CapabilityState.READY,
            )
            for capability in (WriteCapabilityId.BRANCH, WriteCapabilityId.PR)
        ]
    )


# --------------------------------------------------------------------------
# Schemas: strict, closed, no materialized credentials
# --------------------------------------------------------------------------


@pytest.mark.parametrize("tool_id", reg.MUTATION_TOOL_IDS)
def test_schemas_are_closed(tool_id):
    definition = reg.get_mutation_contract(tool_id).definition
    for schema in (definition.input_schema, definition.output_schema):
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert schema["required"]


def test_create_branch_schema_shape():
    schema = reg.create_branch_input_schema()
    assert set(schema["properties"]) == {"owner", "repo", "branch", "base_sha"}
    assert set(schema["required"]) == {"owner", "repo", "branch", "base_sha"}
    assert schema["properties"]["base_sha"]["pattern"] == "^[0-9a-f]{40}$"


def test_create_pr_schema_shape():
    schema = reg.create_pr_input_schema()
    assert set(schema["properties"]) == {
        "owner",
        "repo",
        "head",
        "base",
        "title",
        "body",
        "draft",
        "expected_head_sha",
    }
    assert "body" not in schema["required"]
    assert "draft" not in schema["required"]
    assert "expected_head_sha" in schema["required"]


def test_no_schema_property_is_a_credential_name():
    from hermes_mcp_bridge.v2.schema import SENSITIVE_CREDENTIAL_NAMES

    for contract in reg.mutation_contracts().values():
        for schema in (contract.definition.input_schema, contract.definition.output_schema):
            assert not set(schema["properties"]) & SENSITIVE_CREDENTIAL_NAMES


# --------------------------------------------------------------------------
# Argument validation / injection
# --------------------------------------------------------------------------


def test_valid_arguments_normalize():
    normalized = reg.normalize_arguments("github.create_branch", dict(VALID_BRANCH))
    assert normalized["repository"] == "pestoura/hermes-mcp-bridge"
    assert normalized["ref"] == "refs/heads/feat/lane-l2"
    pr = reg.normalize_arguments("github.create_pr", dict(VALID_PR))
    assert pr["draft"] is True
    assert pr["body"] == ""


@pytest.mark.parametrize(
    "branch",
    [
        "-evil",
        "a..b",
        "a//b",
        "a\\b",
        "with space",
        "tab\there",
        "control\x01",
        "não-ascii",
        "refs/heads/x",
        "refs/tags/v1",
        "/leading",
        "trailing/",
        "trailing.",
        "x.lock",
        "a@{b",
        "a~1",
        "a^1",
        "a:b",
        "a?b",
        "a*b",
        "a[b]",
        "",
        ".hidden",
    ],
)
def test_branch_name_grammar_rejects_traversal_and_control_chars(branch):
    with pytest.raises(MutationDeniedError) as excinfo:
        reg.validate_branch_name(branch)
    assert excinfo.value.reason is MutationReasonCode.INVALID_REF_NAME


def test_ref_cannot_escape_refs_heads():
    assert reg.qualified_ref("feat/x") == "refs/heads/feat/x"
    for smuggled in ("refs/tags/v1", "../tags/v1", "heads/../tags/v1"):
        with pytest.raises(MutationDeniedError):
            reg.qualified_ref(smuggled)


def test_cross_fork_head_rejected():
    args = dict(VALID_PR, head="otherowner:feat/x")
    with pytest.raises(MutationDeniedError):
        reg.normalize_create_pr_arguments(args)


def test_head_equal_to_base_rejected():
    with pytest.raises(MutationDeniedError) as excinfo:
        reg.normalize_create_pr_arguments(dict(VALID_PR, head="main", base="main"))
    assert excinfo.value.reason is MutationReasonCode.INVALID_ARGUMENTS


@pytest.mark.parametrize("sha", ["", "abc", "A" * 40, "g" * 40, "a" * 39, "a" * 41, None, 12345])
def test_abbreviated_or_malformed_sha_rejected(sha):
    with pytest.raises(MutationDeniedError):
        reg.validate_sha(sha)


def test_precondition_sha_is_required_for_both_operations():
    with pytest.raises(MutationDeniedError):
        reg.normalize_create_branch_arguments(
            {k: v for k, v in VALID_BRANCH.items() if k != "base_sha"}
        )
    with pytest.raises(MutationDeniedError):
        reg.normalize_create_pr_arguments(
            {k: v for k, v in VALID_PR.items() if k != "expected_head_sha"}
        )


def test_unknown_argument_key_rejected_schema_closed():
    with pytest.raises(MutationDeniedError) as excinfo:
        reg.normalize_create_branch_arguments(dict(VALID_BRANCH, force=True))
    assert excinfo.value.reason is MutationReasonCode.INVALID_ARGUMENTS
    with pytest.raises(MutationDeniedError):
        reg.normalize_create_pr_arguments(dict(VALID_PR, maintainer_can_modify=True))


@pytest.mark.parametrize(
    "owner,repo",
    [
        ("../etc", "repo"),
        ("owner", "re/po"),
        ("owner", ""),
        ("", "repo"),
        ("owner", "a" * 101),
        (None, "repo"),
    ],
)
def test_repository_reference_rejected(owner, repo):
    with pytest.raises(MutationScopeError) as excinfo:
        reg.validate_repository(owner, repo)
    assert excinfo.value.stage is MutationStage.SCOPE
    assert excinfo.value.reason is MutationReasonCode.INVALID_ARGUMENTS


def test_pr_body_is_data_not_template():
    payload = "{{ inject }} ${danger} `cmd` $(cmd)"
    normalized = reg.normalize_create_pr_arguments(dict(VALID_PR, body=payload))
    assert normalized["body"] == payload


def test_title_and_body_bounds_enforced():
    with pytest.raises(MutationDeniedError):
        reg.normalize_create_pr_arguments(dict(VALID_PR, title="x" * (reg.MAX_TITLE_LENGTH + 1)))
    with pytest.raises(MutationDeniedError):
        reg.normalize_create_pr_arguments(dict(VALID_PR, body="x" * (reg.MAX_BODY_LENGTH + 1)))
    with pytest.raises(MutationDeniedError):
        reg.normalize_create_pr_arguments(dict(VALID_PR, title="   "))
    with pytest.raises(MutationDeniedError):
        reg.normalize_create_pr_arguments(dict(VALID_PR, title="bad\x01title"))


def test_draft_must_be_boolean():
    with pytest.raises(MutationDeniedError):
        reg.normalize_create_pr_arguments(dict(VALID_PR, draft="true"))


def test_no_caller_controlled_absolute_url():
    for contract in reg.mutation_contracts().values():
        assert contract.endpoint_template.startswith("/repos/{owner}/{repo}")
        assert "://" not in contract.endpoint_template
        assert "://" not in contract.read_back.endpoint_template
    assert "http://" not in SOURCE
    assert "api.github.com" not in SOURCE


def test_normalize_arguments_denies_unknown_tool():
    with pytest.raises(MutationDeniedError) as excinfo:
        reg.normalize_arguments("github.delete_repository", {})
    assert excinfo.value.reason is MutationReasonCode.DESTRUCTIVE_OPERATION_FORBIDDEN


# --------------------------------------------------------------------------
# C-1 consumption: no duplicated taxonomy
# --------------------------------------------------------------------------


def test_registry_consumes_c1_enums_without_redefining_them():
    duplicated = {
        "MutationClass",
        "IdempotencySemantics",
        "ApprovalRequirement",
        "RetryClass",
        "SecurityTier",
        "MutationOutcome",
        "MutationReasonCode",
        "MutationStage",
        "IdempotencyStatus",
        "ApprovalState",
        "WriteCapabilityId",
        "PolicyDecision",
        "CapabilityState",
    }
    for name in duplicated:
        imported = getattr(reg, name, None)
        if imported is not None:
            assert imported is getattr(c1_enums, name), name
        assert f"class {name}(" not in SOURCE


def test_registry_reuses_the_c1_stage_order():
    assert reg.MUTATION_STAGE_REFERENCE is c1_enums.MUTATION_STAGE_ORDER
    for contract in reg.mutation_contracts().values():
        assert contract.canonical()["stage_order_reference"] == [
            stage.value for stage in c1_enums.MUTATION_STAGE_ORDER
        ]


def test_only_parallel_safety_is_locally_defined():
    local_enums = {
        name
        for name, obj in vars(reg).items()
        if inspect.isclass(obj)
        and issubclass(obj, __import__("enum").Enum)
        and obj.__module__ == reg.__name__
    }
    assert local_enums == {"ParallelSafety"}


def test_registry_raises_only_c1_error_types():
    for name in dir(reg):
        obj = getattr(reg, name)
        if inspect.isclass(obj) and issubclass(obj, Exception):
            assert obj.__module__ == c1_errors.__name__, name


def test_denials_are_redacted_stage_reason_pairs():
    with pytest.raises(MutationDeniedError) as excinfo:
        reg.normalize_create_branch_arguments(dict(VALID_BRANCH, branch="../../etc/passwd"))
    message = str(excinfo.value)
    assert message == "REGISTRY:INVALID_REF_NAME"
    assert "passwd" not in message
    assert "pestoura" not in message


# --------------------------------------------------------------------------
# Determinism of the exported contract
# --------------------------------------------------------------------------


def test_exported_contracts_are_deterministic():
    first = reg.exported_mutation_contracts()
    second = reg.exported_mutation_contracts()
    assert canonical_hash(first) == canonical_hash(second)
    assert list(first) == ["github.create_branch", "github.create_pr"]


def test_exported_contract_field_allow_list():
    expected = {
        "ambiguous_status",
        "approval_requirement",
        "clean_failure_status",
        "compensation",
        "definition",
        "endpoint_template",
        "idempotency",
        "method",
        "mutation_class",
        "parallel_safety",
        "precondition_fields",
        "read_back",
        "required_capability",
        "result_fields",
        "retry_policy",
        "security_tier",
        "stage_order_reference",
        "success_status",
        "timeout_seconds",
        "tool_id",
    }
    for payload in reg.exported_mutation_contracts().values():
        assert set(payload) == expected


def test_exported_contract_carries_no_secret_field():
    text = str(reg.exported_mutation_contracts()).lower()
    for token in ("token", "secret", "password", "authorization", "private_key", "cookie"):
        assert token not in text


def test_compensation_is_declared_and_non_destructive_for_pr():
    assert (
        reg.get_mutation_contract("github.create_pr").compensation
        == "close_pull_request_never_delete"
    )
    assert "delete" in reg.get_mutation_contract("github.create_branch").compensation


# --------------------------------------------------------------------------
# V1 isolation
# --------------------------------------------------------------------------


def test_v1_contract_unchanged_by_importing_this_module():
    from hermes_mcp_bridge import contracts

    assert contracts.CURRENT_CONTRACT_VERSION == "1.0.0"
    assert contracts.SCHEMA_VERSION == "0.6.1"
    assert contracts.expected_tool_count() == 27


def test_no_v1_module_imports_the_mutation_registry():
    src_root = MODULE_PATH.parents[1]
    for path in src_root.rglob("*.py"):
        if path.parent.name == "v2":
            continue
        text = path.read_text(encoding="utf-8")
        assert "github_mutation_registry" not in text, str(path)
