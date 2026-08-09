# Phase 3 Mutation Test Matrix (Planned)

> **V2 · PHASE 3 · PREPARATION ONLY · NOT IMPLEMENTED · NO V1 IMPACT**
>
> Test names below are **planned**. None of these tests exists yet, and a test
> name in this matrix is not evidence of acceptance.

Proposed home: `tests/test_v2_phase3_github_mutations.py` (hermetic) and
`tests/test_v2_phase3_mutation_acceptance.py` (evidence/gate shape). The
hermetic suite must pass with **zero real network access**.

## Ordering and fail-closed

| Test | Asserts | Criterion |
|---|---|---|
| `test_out_of_scope_repo_zero_resolution_zero_http` | Out-of-scope repository → no broker call, no HTTP | A3-09 |
| `test_unknown_mutation_denies` | Unregistered operation → DENY | A3-04 |
| `test_missing_policy_denies` | Absent policy entry → DENY | A3-16 |
| `test_read_capability_cannot_mutate` | `github.read` cannot satisfy a write | A3-03 |
| `test_write_capability_cannot_read_path` | Write capability not accepted on read tools | A3-03 |

## Credentials

| Test | Asserts | Criterion |
|---|---|---|
| `test_probed_permissions_exact_match` | Superset permissions fail the readiness check | A3-03 |
| `test_administration_permission_rejected` | Any `Administration` grant → capability NOT_READY | A3-04 |
| `test_no_delete_repo_request_possible` | No registry entry and no code path produces `DELETE /repos/` | A3-04 |
| `test_write_material_never_serialized` | Broker status-only; no material in result/log/trace | A3-14 |

## Approval and digest

| Test | Asserts | Criterion |
|---|---|---|
| `test_digest_stable_for_equivalent_arguments` | Canonical determinism | A3-06 |
| `test_digest_changes_on_any_semantic_change` | Title/body/SHA/policy/snapshot change → new digest | A3-06 |
| `test_approval_digest_mismatch_denies` | Approval for A cannot execute B | A3-06 |
| `test_approval_expiry_denies` | Expired approval → DENY | A3-06 |
| `test_approval_single_use` | Second use → `APPROVAL_ALREADY_CONSUMED` | A3-06 |
| `test_concurrent_approval_consumption_one_winner` | Atomic consumption under race | A3-06 |
| `test_merge_requires_distinct_approver` | Self-approval of a merge → DENY | A3-10 |

## Idempotency and concurrency

| Test | Asserts | Criterion |
|---|---|---|
| `test_repeated_request_single_provider_mutation` | Exactly one write | A3-07 |
| `test_in_progress_lease_blocks_second_write` | `IN_PROGRESS` returned, no second call | A3-07 |
| `test_ambiguous_outcome_requires_reconciliation_read` | No blind retry after timeout | A3-07 |
| `test_idempotency_key_scoped_by_principal_and_repo` | No cross-principal collision | A3-13 |
| `test_create_branch_existing_ref_not_treated_as_success` | `422` without a matching record → failure | A3-08 |
| `test_head_sha_drift_denies_create_pr` | Drift after approval → DENY | A3-08 |
| `test_merge_sends_expected_sha_and_handles_409` | Provider-side concurrency check used | A3-08 |

## TOCTOU

| Test | Asserts | Criterion |
|---|---|---|
| `test_preconditions_reread_after_approval` | Re-read happens after approval, before write | A3-08 |
| `test_protection_state_not_cached_across_approvals` | Fresh read each execution | A3-10 |
| `test_state_change_between_approval_and_execution_denies` | No auto-refresh of approval | A3-08 |

## Merge governance

| Test | Asserts | Criterion |
|---|---|---|
| `test_default_branch_merge_denied_by_default` | DENY unless explicitly enabled | A3-10 |
| `test_missing_required_checks_denies` | No required checks configured → DENY | A3-10 |
| `test_failed_or_pending_checks_deny` | Non-green checks → DENY | A3-10 |
| `test_draft_pr_merge_denied` | Draft → DENY | A3-10 |
| `test_merge_method_is_policy_fixed` | Caller cannot choose method | A3-10 |
| `test_merge_never_retried` | `NO_RETRY` honored | A3-15 |

## Input validation / injection

| Test | Asserts | Criterion |
|---|---|---|
| `test_branch_name_grammar_rejects_traversal_and_control_chars` | `..`, `//`, `\`, leading `-`, control, non-ASCII rejected | A3-09 |
| `test_ref_cannot_escape_refs_heads` | No `refs/tags`, no absolute ref smuggling | A3-09 |
| `test_no_caller_controlled_absolute_url` | Endpoint built from validated fields only | A3-09 |
| `test_pr_body_is_data_not_template` | No interpolation into executable context | A3-09 |
| `test_cross_fork_head_rejected` | Same-repository head only | A3-09 |

## Audit, evidence, observability

| Test | Asserts | Criterion |
|---|---|---|
| `test_write_ahead_record_precedes_provider_call` | Ordering enforced | A3-05 |
| `test_committed_mutation_without_audit_record_fails_gate` | Gate detects the gap | A3-05 |
| `test_audit_redaction_fail_closed` | Unprovable field omitted | A3-14 |
| `test_metric_labels_bounded_cardinality` | No repo/ref/PR as labels | A3-14 |
| `test_zero_hermes_tokens_on_mutation_path` | Real accounting, not assertion | A3-13 |

## Compensation

| Test | Asserts | Criterion |
|---|---|---|
| `test_branch_compensation_deletes_only_own_ref` | SHA and ownership checked | A3-11 |
| `test_pr_compensation_closes_never_deletes` | Close only | A3-11 |
| `test_merge_has_no_automatic_compensation` | Manual intervention | A3-12 |
| `test_unsafe_compensation_dead_letters` | No best-effort write | A3-12 |
| `test_residual_object_count_zero_after_cleanup` | Cleanup evidence | A3-11 |

## V1 isolation

| Test | Asserts | Criterion |
|---|---|---|
| `test_v1_tool_count_unchanged_27` | Contract preserved | A3-02 |
| `test_no_v1_module_imports_phase3` | Isolation preserved | A3-02 |
| `test_contract_and_schema_versions_unchanged` | `1.0.0` / `0.6.1` | A3-02 |
