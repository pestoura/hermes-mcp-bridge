"""Phase 3 lane L3 — canonical operation digest and digest-bound approvals.

Hermetic: no network, no credentials, no filesystem writes, no subprocess.
Every temporal decision uses an injected ``now``.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest

from hermes_mcp_bridge.v2.canonical import canonical_hash
from hermes_mcp_bridge.v2.enums import (
    ApprovalState,
    MutationReasonCode,
    MutationStage,
    WriteCapabilityId,
)
from hermes_mcp_bridge.v2.errors import (
    ApprovalError,
    DigestMismatchError,
    MutationDeniedError,
)
from hermes_mcp_bridge.v2.mutation_digest import (
    OPERATION_DIGEST_SCHEMA,
    ApprovalRecord,
    ApprovalStore,
    DigestBinding,
    OperationDescriptor,
    OperationPreconditions,
    compute_operation_digest,
    digest_evidence,
    require_digest,
    require_repository,
    require_sha,
)

BASE = "a" * 40
HEAD = "b" * 40
SNAPSHOT = "c" * 64
NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


def descriptor(**overrides: object) -> OperationDescriptor:
    kwargs: dict[str, object] = {
        "operation": "github.create_pr",
        "capability": WriteCapabilityId.PR,
        "repository": "pestoura/hermes-mcp-bridge",
        "arguments": {"title": "T", "body": "B", "head": "feat/x", "base": "main"},
        "preconditions": OperationPreconditions(expected_head_sha=HEAD),
        "policy_version": "policy-2026.08.1",
        "registry_snapshot_hash": SNAPSHOT,
    }
    kwargs.update(overrides)
    return OperationDescriptor(**kwargs)  # type: ignore[arg-type]


def fetch(store: ApprovalStore, approval_id: str) -> ApprovalRecord:
    record = store.get(approval_id)
    assert record is not None
    return record


def approval(
    store: ApprovalStore,
    desc: OperationDescriptor,
    *,
    approval_id: str = "apr-1",
    principal: str = "svc-jarvas",
    approver: str = "pedro",
    digest: str | None = None,
    expires_in: int = 3600,
) -> ApprovalRecord:
    record = ApprovalRecord(
        approval_id=approval_id,
        principal=principal,
        approver=approver,
        operation_digest=digest or compute_operation_digest(desc),
        repository=desc.repository,
        operation=desc.operation,
        nonce="n-0123456789abcdef",
        expires_at=NOW + timedelta(seconds=expires_in),
        trust_context="channel:cli",
    )
    return store.issue(record)


# --------------------------------------------------------------------------
# canonicalization / determinism
# --------------------------------------------------------------------------


def test_digest_is_lowercase_64_hex() -> None:
    value = compute_operation_digest(descriptor())
    assert len(value) == 64
    assert value == value.lower()
    assert require_digest(value) == value


def test_digest_stable_for_equivalent_arguments() -> None:
    """Argument insertion order is not semantic: the digest must not see it."""
    a = descriptor(arguments={"title": "T", "body": "B", "head": "feat/x", "base": "main"})
    b = descriptor(arguments={"base": "main", "head": "feat/x", "body": "B", "title": "T"})
    assert compute_operation_digest(a) == compute_operation_digest(b)


def test_digest_matches_documented_payload_shape() -> None:
    desc = descriptor()
    expected = canonical_hash(
        {
            "schema": OPERATION_DIGEST_SCHEMA,
            "operation": "github.create_pr",
            "capability": "github.write.pr",
            "repository": "pestoura/hermes-mcp-bridge",
            "arguments": {"title": "T", "body": "B", "head": "feat/x", "base": "main"},
            "preconditions": {"expected_head_sha": HEAD},
            "policy_version": "policy-2026.08.1",
            "registry_snapshot_hash": SNAPSHOT,
        }
    )
    assert compute_operation_digest(desc) == expected


@pytest.mark.parametrize(
    "overrides",
    [
        {"operation": "github.create_branch"},
        {"capability": WriteCapabilityId.BRANCH},
        {"repository": "pestoura/other-repo"},
        {"arguments": {"title": "T2", "body": "B", "head": "feat/x", "base": "main"}},
        {"arguments": {"title": "T", "body": "B2", "head": "feat/x", "base": "main"}},
        {"preconditions": OperationPreconditions(expected_head_sha="d" * 40)},
        {"policy_version": "policy-2026.08.2"},
        {"registry_snapshot_hash": "e" * 64},
    ],
)
def test_digest_changes_on_any_semantic_change(overrides: dict[str, object]) -> None:
    assert compute_operation_digest(descriptor()) != compute_operation_digest(
        descriptor(**overrides)
    )


def test_editorial_body_is_inside_the_digest() -> None:
    """An edited PR body means a new approval (ADR-0021)."""
    original = descriptor()
    edited = descriptor(arguments={**dict(original.arguments), "body": "edited"})
    assert compute_operation_digest(original) != compute_operation_digest(edited)


def test_float_argument_is_rejected_not_coerced() -> None:
    with pytest.raises(MutationDeniedError) as exc:
        descriptor(arguments={"weight": 1.5})
    assert exc.value.reason is MutationReasonCode.INVALID_ARGUMENTS


def test_non_serializable_argument_is_rejected() -> None:
    with pytest.raises(MutationDeniedError):
        descriptor(arguments={"when": NOW})


def test_preconditions_require_at_least_one_sha() -> None:
    with pytest.raises(MutationDeniedError) as exc:
        OperationPreconditions(required_checks_policy="strict")
    assert exc.value.stage is MutationStage.PRECONDITION_REVALIDATION


@pytest.mark.parametrize("bad", ["A" * 40, "b" * 39, "z" * 40, "", "0x" + "b" * 38])
def test_sha_grammar_fails_closed(bad: str) -> None:
    with pytest.raises(MutationDeniedError):
        require_sha(bad)


@pytest.mark.parametrize(
    "bad",
    [
        "../etc/passwd",
        "owner/repo/extra",
        "https://github.com/owner/repo",
        "owner",
        "",
        "owner/re po",
    ],
)
def test_repository_grammar_rejects_traversal_and_urls(bad: str) -> None:
    with pytest.raises(MutationDeniedError) as exc:
        require_repository(bad)
    assert exc.value.stage is MutationStage.SCOPE


def test_registry_snapshot_hash_must_be_a_digest() -> None:
    with pytest.raises(MutationDeniedError):
        descriptor(registry_snapshot_hash="not-a-digest")


def test_capability_must_be_a_write_capability() -> None:
    with pytest.raises(MutationDeniedError) as exc:
        descriptor(capability="github.read")
    assert exc.value.reason is MutationReasonCode.WRITE_CAPABILITY_MISMATCH


# --------------------------------------------------------------------------
# evidence hygiene
# --------------------------------------------------------------------------


def test_digest_evidence_contains_no_arguments_or_payload() -> None:
    evidence = digest_evidence(descriptor())
    serialized = canonical_hash(evidence)  # proves it is canonically serializable
    assert serialized
    assert "arguments" not in evidence
    joined = " ".join(evidence.values())
    for secret_ish in ("feat/x", "T", "B"):
        assert f'"{secret_ish}"' not in joined
    assert evidence["operation_digest"] == compute_operation_digest(descriptor())


def test_approval_evidence_never_exposes_nonce() -> None:
    store = ApprovalStore()
    record = approval(store, descriptor())
    evidence = record.evidence()
    assert "nonce" not in evidence
    assert record.nonce not in " ".join(evidence.values())


def test_error_string_is_stage_reason_only() -> None:
    store = ApprovalStore()
    desc = descriptor()
    approval(store, desc)
    with pytest.raises(ApprovalError) as exc:
        store.verify_and_consume("unknown", desc, principal="svc-jarvas", now=NOW)
    assert str(exc.value) == "APPROVAL:APPROVAL_UNKNOWN"


# --------------------------------------------------------------------------
# approval binding / consumption
# --------------------------------------------------------------------------


def test_approval_happy_path_consumes_once() -> None:
    store = ApprovalStore()
    desc = descriptor()
    approval(store, desc)
    consumed = store.verify_and_consume("apr-1", desc, principal="svc-jarvas", now=NOW)
    assert consumed.state is ApprovalState.CONSUMED
    assert consumed.consumed_at == NOW


def test_approval_single_use() -> None:
    store = ApprovalStore()
    desc = descriptor()
    approval(store, desc)
    store.verify_and_consume("apr-1", desc, principal="svc-jarvas", now=NOW)
    with pytest.raises(ApprovalError) as exc:
        store.verify_and_consume("apr-1", desc, principal="svc-jarvas", now=NOW)
    assert exc.value.reason is MutationReasonCode.APPROVAL_ALREADY_CONSUMED


def test_approval_digest_mismatch_denies() -> None:
    """An approval for A cannot execute B."""
    store = ApprovalStore()
    approved = descriptor()
    approval(store, approved)
    other = descriptor(arguments={**dict(approved.arguments), "title": "hijack"})
    with pytest.raises(DigestMismatchError) as exc:
        store.verify_and_consume("apr-1", other, principal="svc-jarvas", now=NOW)
    assert exc.value.reason is MutationReasonCode.APPROVAL_DIGEST_MISMATCH


def test_approval_expiry_denies_and_marks_expired() -> None:
    store = ApprovalStore()
    desc = descriptor()
    approval(store, desc, expires_in=60)
    later = NOW + timedelta(seconds=61)
    with pytest.raises(ApprovalError) as exc:
        store.verify_and_consume("apr-1", desc, principal="svc-jarvas", now=later)
    assert exc.value.reason is MutationReasonCode.APPROVAL_EXPIRED
    assert fetch(store, "apr-1").state is ApprovalState.EXPIRED


def test_approval_wrong_principal_denies() -> None:
    store = ApprovalStore()
    desc = descriptor()
    approval(store, desc)
    with pytest.raises(ApprovalError) as exc:
        store.verify_and_consume("apr-1", desc, principal="someone-else", now=NOW)
    assert exc.value.reason is MutationReasonCode.APPROVAL_SCOPE_MISMATCH


def test_revoked_approval_denies() -> None:
    store = ApprovalStore()
    desc = descriptor()
    approval(store, desc)
    store.revoke("apr-1")
    with pytest.raises(ApprovalError):
        store.verify_and_consume("apr-1", desc, principal="svc-jarvas", now=NOW)


def test_merge_requires_distinct_approver() -> None:
    store = ApprovalStore()
    desc = descriptor(
        operation="github.merge_pr",
        capability=WriteCapabilityId.MERGE,
        arguments={"pr_number": 7},
    )
    assert desc.requires_distinct_approver
    approval(store, desc, principal="pedro", approver="pedro")
    with pytest.raises(ApprovalError) as exc:
        store.verify_and_consume("apr-1", desc, principal="pedro", now=NOW)
    assert exc.value.reason is MutationReasonCode.APPROVER_NOT_DISTINCT


def test_create_operations_do_not_require_distinct_approver() -> None:
    assert descriptor().requires_distinct_approver is False


def test_concurrent_approval_consumption_one_winner() -> None:
    store = ApprovalStore()
    desc = descriptor()
    approval(store, desc)
    winners: list[str] = []
    losers: list[MutationReasonCode] = []
    barrier = threading.Barrier(8)

    def attempt() -> None:
        barrier.wait()
        try:
            record = store.verify_and_consume("apr-1", desc, principal="svc-jarvas", now=NOW)
            winners.append(record.approval_id)
        except ApprovalError as exc:
            losers.append(exc.reason)

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(winners) == 1
    assert len(losers) == 7
    assert set(losers) == {MutationReasonCode.APPROVAL_ALREADY_CONSUMED}


def test_duplicate_approval_id_is_rejected() -> None:
    store = ApprovalStore()
    desc = descriptor()
    approval(store, desc)
    with pytest.raises(MutationDeniedError):
        approval(store, desc)


def test_purge_expired_transitions_pending_only() -> None:
    store = ApprovalStore()
    desc = descriptor()
    approval(store, desc, approval_id="apr-1", expires_in=10)
    approval(store, desc, approval_id="apr-2", expires_in=10_000)
    assert store.purge_expired(NOW + timedelta(seconds=11)) == 1
    assert fetch(store, "apr-1").state is ApprovalState.EXPIRED
    assert fetch(store, "apr-2").state is ApprovalState.PENDING


def test_digest_binding_evidence_is_consistent() -> None:
    store = ApprovalStore()
    desc = descriptor()
    approval(store, desc)
    consumed = store.verify_and_consume("apr-1", desc, principal="svc-jarvas", now=NOW)
    binding = DigestBinding(descriptor=desc, approval=consumed)
    assert binding.operation_digest == compute_operation_digest(desc)
    evidence = binding.evidence()
    assert evidence["state"] == ApprovalState.CONSUMED.value
    assert evidence["operation_digest"] == binding.operation_digest


FORBIDDEN_MODULES = frozenset(
    {"subprocess", "socket", "httpx", "requests", "urllib", "sqlite3", "shutil", "os"}
)


def imported_modules(module_file: str) -> set[str]:
    """Top-level module names actually imported by a source file (AST, not text)."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path(module_file).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def test_module_is_offline_and_shell_free() -> None:
    """L3 must not acquire I/O, process or network surface (AST-checked)."""
    import hermes_mcp_bridge.v2.mutation_digest as module

    assert imported_modules(module.__file__) & FORBIDDEN_MODULES == set()
