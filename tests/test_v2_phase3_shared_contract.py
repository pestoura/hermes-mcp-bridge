"""Phase 3 Controller C-1 — shared enums/errors contract for lanes L1..L4.

These tests pin the surface that L1..L4 build against. They are hermetic:
no network, no credentials, no filesystem writes. They assert the contract
itself, not any mutation behaviour (no mutation code exists yet).
"""

from __future__ import annotations

import inspect

import pytest

from hermes_mcp_bridge import v2
from hermes_mcp_bridge.v2 import enums as v2_enums
from hermes_mcp_bridge.v2 import errors as v2_errors
from hermes_mcp_bridge.v2.enums import (
    FORBIDDEN_PERMISSION,
    MUTATION_STAGE_ORDER,
    READ_CAPABILITY_ID,
    ApprovalState,
    IdempotencySemantics,
    IdempotencyStatus,
    MutationOutcome,
    MutationReasonCode,
    MutationStage,
    WriteCapabilityId,
)
from hermes_mcp_bridge.v2.errors import (
    ApprovalError,
    AuditWriteError,
    ConcurrencyDriftError,
    DigestMismatchError,
    IdempotencyConflictError,
    MutationDeniedError,
    MutationError,
    MutationIndeterminateError,
    MutationScopeError,
    V2Error,
    WriteCapabilityError,
)

C1_ENUM_EXPORTS = (
    "ApprovalState",
    "IdempotencyStatus",
    "MutationOutcome",
    "MutationReasonCode",
    "MutationStage",
    "WriteCapabilityId",
)

C1_ERROR_EXPORTS = (
    "ApprovalError",
    "AuditWriteError",
    "ConcurrencyDriftError",
    "DigestMismatchError",
    "IdempotencyConflictError",
    "MutationDeniedError",
    "MutationError",
    "MutationIndeterminateError",
    "MutationScopeError",
    "WriteCapabilityError",
)


# --------------------------------------------------------------------------
# Export surface
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", C1_ENUM_EXPORTS)
def test_enum_is_exported_from_package_and_module(name: str) -> None:
    assert name in v2_enums.__all__
    assert name in v2.__all__
    assert getattr(v2, name) is getattr(v2_enums, name)


@pytest.mark.parametrize("name", C1_ERROR_EXPORTS)
def test_error_is_exported_from_package_and_module(name: str) -> None:
    assert name in v2_errors.__all__
    assert name in v2.__all__
    assert getattr(v2, name) is getattr(v2_errors, name)


@pytest.mark.parametrize(
    "name", ("FORBIDDEN_PERMISSION", "MUTATION_STAGE_ORDER", "READ_CAPABILITY_ID")
)
def test_constants_are_exported(name: str) -> None:
    assert name in v2_enums.__all__
    assert name in v2.__all__


@pytest.mark.parametrize("module", (v2, v2_enums, v2_errors))
def test_all_is_isort_ordered_and_unique(module: object) -> None:
    """``__all__`` follows the repo's RUF022 order: constants, then names."""
    names = list(module.__all__)  # type: ignore[attr-defined]
    assert len(names) == len(set(names))
    constants = [n for n in names if n.isupper()]
    rest = [n for n in names if not n.isupper()]
    assert names == constants + rest
    assert constants == sorted(constants)
    assert rest == sorted(rest)


@pytest.mark.parametrize("module", (v2, v2_enums, v2_errors))
def test_every_all_entry_resolves(module: object) -> None:
    for name in module.__all__:  # type: ignore[attr-defined]
        assert hasattr(module, name), name


def test_phase3_contract_marker_is_not_an_acceptance_gate() -> None:
    assert v2.PHASE3_CONTRACT == "PHASE_3_SHARED_CONTRACT_PUBLISHED_NOT_ACCEPTED"
    assert "ACCEPTED" not in v2.PHASE3_CONTRACT.removesuffix("NOT_ACCEPTED")


# --------------------------------------------------------------------------
# Capability disjointness (A3-03) and destructive exclusion (A3-04)
# --------------------------------------------------------------------------


def test_read_capability_is_never_a_write_capability() -> None:
    assert READ_CAPABILITY_ID == "github.read"
    values = {member.value for member in WriteCapabilityId}
    assert READ_CAPABILITY_ID not in values
    assert all(value.startswith("github.write.") for value in values)


def test_write_capability_ids_are_distinct() -> None:
    values = [member.value for member in WriteCapabilityId]
    assert len(values) == len(set(values)) == 3
    assert WriteCapabilityId.MERGE.is_merge is True
    assert WriteCapabilityId.BRANCH.is_merge is False
    assert WriteCapabilityId.PR.is_merge is False


def test_administration_permission_is_named_and_forbidden() -> None:
    assert FORBIDDEN_PERMISSION == "Administration"
    assert FORBIDDEN_PERMISSION not in {m.value for m in WriteCapabilityId}


def test_no_repository_deletion_token_in_the_contract() -> None:
    for module in (v2_enums, v2_errors):
        source = inspect.getsource(module)
        assert "DELETE /repos/" not in source
        assert "delete_repository" not in source
        assert "delete_repo" not in source


# --------------------------------------------------------------------------
# Fixed stage ordering (L5 contract)
# --------------------------------------------------------------------------


def test_stage_order_is_complete_and_unique() -> None:
    assert len(MUTATION_STAGE_ORDER) == len(MutationStage)
    assert len(set(MUTATION_STAGE_ORDER)) == len(MUTATION_STAGE_ORDER)
    assert set(MUTATION_STAGE_ORDER) == set(MutationStage)


def test_stage_order_is_fail_closed_scope_first_provider_call_late() -> None:
    order = list(MUTATION_STAGE_ORDER)
    assert order[0] is MutationStage.SCOPE
    # Every gate must precede the provider call.
    call_index = order.index(MutationStage.PROVIDER_CALL)
    for stage in (
        MutationStage.REGISTRY,
        MutationStage.POLICY,
        MutationStage.CREDENTIAL,
        MutationStage.APPROVAL,
        MutationStage.IDEMPOTENCY,
        MutationStage.PRECONDITION_REVALIDATION,
        MutationStage.WRITE_AHEAD_AUDIT,
    ):
        assert order.index(stage) < call_index
    # Read-back and shaping happen only after the call.
    assert order.index(MutationStage.READ_BACK) > call_index
    assert order.index(MutationStage.RESULT_SHAPING) > order.index(MutationStage.READ_BACK)


def test_write_ahead_audit_immediately_precedes_the_provider_call() -> None:
    order = list(MUTATION_STAGE_ORDER)
    assert order.index(MutationStage.PROVIDER_CALL) - 1 == order.index(
        MutationStage.WRITE_AHEAD_AUDIT
    )


def test_precondition_revalidation_happens_after_approval() -> None:
    order = list(MUTATION_STAGE_ORDER)
    assert order.index(MutationStage.PRECONDITION_REVALIDATION) > order.index(
        MutationStage.APPROVAL
    )


def test_stage_order_tuple_is_immutable() -> None:
    assert isinstance(MUTATION_STAGE_ORDER, tuple)


# --------------------------------------------------------------------------
# Outcome / idempotency / approval semantics
# --------------------------------------------------------------------------


def test_ambiguous_outcome_requires_reconciliation_and_forbids_retry() -> None:
    assert MutationOutcome.AMBIGUOUS.requires_reconciliation is True
    assert MutationOutcome.AMBIGUOUS.allows_new_attempt is False
    assert MutationOutcome.AMBIGUOUS.is_terminal is True


def test_only_failed_clean_allows_a_new_attempt() -> None:
    allowed = [m for m in MutationOutcome if m.allows_new_attempt]
    assert allowed == [MutationOutcome.FAILED_CLEAN]


def test_pending_is_the_only_non_terminal_outcome() -> None:
    non_terminal = [m for m in MutationOutcome if not m.is_terminal]
    assert non_terminal == [MutationOutcome.PENDING]


def test_committed_outcome_is_never_retriable() -> None:
    assert MutationOutcome.COMMITTED.allows_new_attempt is False
    assert MutationOutcome.COMMITTED.requires_reconciliation is False


def test_only_new_idempotency_status_executes_a_provider_call() -> None:
    executing = [m for m in IdempotencyStatus if m.executes_provider_call]
    assert executing == [IdempotencyStatus.NEW]


def test_only_pending_approval_is_usable() -> None:
    usable = [m for m in ApprovalState if m.is_usable]
    assert usable == [ApprovalState.PENDING]


def test_precondition_idempotency_is_distinct_from_keyed() -> None:
    member = IdempotencySemantics.IDEMPOTENT_BY_PRECONDITION
    assert member.requires_precondition is True
    assert member.requires_idempotency_key is False
    assert IdempotencySemantics.KEYED_IDEMPOTENT.requires_precondition is False
    assert IdempotencySemantics.KEYED_IDEMPOTENT.requires_idempotency_key is True


def test_reason_codes_are_unique_upper_snake_tokens() -> None:
    values = [m.value for m in MutationReasonCode]
    assert len(values) == len(set(values))
    for value in values:
        assert value == value.upper()
        assert value.replace("_", "").isalnum()


@pytest.mark.parametrize(
    "reason",
    (
        MutationReasonCode.REPOSITORY_OUT_OF_SCOPE,
        MutationReasonCode.READ_CAPABILITY_CANNOT_MUTATE,
        MutationReasonCode.ADMINISTRATION_PERMISSION_PRESENT,
        MutationReasonCode.APPROVAL_ALREADY_CONSUMED,
        MutationReasonCode.APPROVAL_DIGEST_MISMATCH,
        MutationReasonCode.PRECONDITION_DRIFT,
        MutationReasonCode.AUDIT_RECORD_UNWRITABLE,
        MutationReasonCode.DESTRUCTIVE_OPERATION_FORBIDDEN,
    ),
)
def test_required_reason_codes_exist(reason: MutationReasonCode) -> None:
    assert isinstance(reason.value, str) and reason.value


# --------------------------------------------------------------------------
# Error taxonomy
# --------------------------------------------------------------------------


def test_mutation_errors_derive_from_v2_error() -> None:
    for name in C1_ERROR_EXPORTS:
        cls = getattr(v2_errors, name)
        assert issubclass(cls, V2Error)
        assert issubclass(cls, MutationError)


@pytest.mark.parametrize(
    "cls",
    (
        MutationScopeError,
        WriteCapabilityError,
        ApprovalError,
        DigestMismatchError,
        IdempotencyConflictError,
        ConcurrencyDriftError,
        AuditWriteError,
    ),
)
def test_deny_errors_are_mutation_denied(cls: type[MutationError]) -> None:
    assert issubclass(cls, MutationDeniedError)


def test_indeterminate_is_not_a_deny() -> None:
    assert not issubclass(MutationIndeterminateError, MutationDeniedError)
    assert issubclass(MutationIndeterminateError, MutationError)


def test_digest_mismatch_is_an_approval_error() -> None:
    assert issubclass(DigestMismatchError, ApprovalError)


def test_error_str_is_a_stable_redacted_stage_reason_pair() -> None:
    err = MutationScopeError(
        MutationReasonCode.REPOSITORY_OUT_OF_SCOPE,
        MutationStage.SCOPE,
    )
    assert str(err) == "SCOPE:REPOSITORY_OUT_OF_SCOPE"
    assert err.reason is MutationReasonCode.REPOSITORY_OUT_OF_SCOPE
    assert err.stage is MutationStage.SCOPE
    assert err.detail == ""


def test_error_detail_is_never_placed_in_str() -> None:
    err = ApprovalError(
        MutationReasonCode.APPROVAL_EXPIRED,
        MutationStage.APPROVAL,
        detail="owner/repo@deadbeef",
    )
    assert str(err) == "APPROVAL:APPROVAL_EXPIRED"
    assert "owner/repo" not in str(err)
    assert err.detail == "owner/repo@deadbeef"


def test_error_is_raisable_and_catchable_as_base() -> None:
    with pytest.raises(MutationError) as excinfo:
        raise ConcurrencyDriftError(
            MutationReasonCode.PRECONDITION_DRIFT,
            MutationStage.PRECONDITION_REVALIDATION,
        )
    assert excinfo.value.stage is MutationStage.PRECONDITION_REVALIDATION


# --------------------------------------------------------------------------
# V1 isolation
# --------------------------------------------------------------------------


def test_v1_contract_untouched_by_the_shared_contract() -> None:
    from hermes_mcp_bridge import contracts

    assert contracts.CURRENT_CONTRACT_VERSION == "1.0.0"
    assert contracts.SCHEMA_VERSION == "0.6.1"
    assert contracts.expected_tool_count() == 27
