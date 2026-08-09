# Phase 6 Runbook Registry Test Plan (Planned)

> **V2 · PHASE 6 · DESIGN ONLY · NOT IMPLEMENTED · NO V1 IMPACT**
>
> Test names below are **planned**. None of these tests exists yet, and a test
> name in this plan is not evidence of acceptance.

Proposed homes:

- `tests/test_v2_phase6_runbook_registry.py` — hermetic; **zero real network
  access**, zero credential resolution.
- `tests/test_v2_phase6_runbook_acceptance.py` — evidence/gate shape.
- Connected collector script (INNER) plus an independent OUTER verifier,
  mirroring the Phase 2/3 pattern.

## Identity, versioning, pinning

| Test | Asserts | Criterion |
|---|---|---|
| `test_runbook_id_grammar_rejects_invalid` | Bad `runbook_id` → `RB_ID_INVALID` | A6-05 |
| `test_runbook_namespace_disjoint_from_tools` | Tool-name collision → `RB_NAMESPACE_COLLISION` | A6-05 |
| `test_runbook_version_digest_compile` | Compile is deterministic; digest stable | A6-03 |
| `test_recompile_is_byte_identical` | Two compiles → identical IR bytes | A6-03 |
| `test_editorial_change_does_not_change_digest` | Description edit → same digest | A6-03 |
| `test_semantic_change_changes_digest` | Any IR-relevant change → new digest | A6-03 |
| `test_readmission_same_version_different_digest_conflicts` | → `RB_DIGEST_CONFLICT`, nothing overwritten | A6-05 |
| `test_registry_events_append_only` | No in-place mutation of admitted records | A6-05 |
| `test_weakening_change_requires_major` | Looser policy/approval/capability as MINOR → `RB_VERSION_BUMP_INVALID` | A6-05 |
| `test_runbook_immutable_pin_rejects_floating_tool` | "latest" → `RB_UNPINNED_REFERENCE` | A6-06 |
| `test_composition_pins_digest_and_bounds_depth` | Unpinned or too-deep composition rejected | A6-06 |
| `test_composition_cycle_rejected` | → `RB_COMPOSITION_CYCLE` | A6-06 |
| `test_invocation_requires_expected_digest` | Missing → `RB_DIGEST_REQUIRED` | A6-07 |
| `test_digest_mismatch_denies_zero_side_effects` | No broker call, no HTTP | A6-07 |
| `test_yanked_runbook_not_invocable` | → `RB_YANKED`, no grace period | A6-08 |
| `test_yank_during_execution_cancels_at_node_boundary` | Committed nodes compensated or dead-lettered | A6-08 |

## Admission validation

| Test | Asserts | Criterion |
|---|---|---|
| `test_runbook_admission_zero_network` | No socket, no provider call during admission | A6-04 |
| `test_runbook_admission_zero_credential_resolution` | Broker never invoked | A6-04 |
| `test_runbook_admission_stage_ordering` | A stage failure prevents later stages observably | A6-03 |
| `test_runbook_admission_unevaluated_is_failure` | Skipped stage → failure, not N/A | A6-03 |
| `test_nondeterministic_compile_rejected` | → `RB_COMPILE_NONDETERMINISTIC` | A6-03 |
| `test_admission_does_not_imply_promotion` | `ADMITTED` is not invocable in production | A6-01 |

## Parameter and output schema

| Test | Asserts | Criterion |
|---|---|---|
| `test_unknown_property_rejected` | Closed schema | A6-09 |
| `test_missing_required_parameter_rejected` | → `RB_SCHEMA_INVALID` | A6-09 |
| `test_oversize_payload_rejected` | `max_param_bytes` enforced | A6-09 |
| `test_unbounded_string_rejected_at_admission` | Missing `max_length` → reject | A6-09 |
| `test_secret_shaped_parameter_rejected` | → `RB_SECRET_PARAMETER` | A6-09 |
| `test_resource_ref_requires_kind_and_pattern` | Malformed ref rejected | A6-09 |
| `test_unsafe_binding_rejected` | Templating/expression → `RB_UNSAFE_BINDING` | A6-10 |
| `test_binding_type_mismatch_rejected` | Static type check | A6-10 |
| `test_transform_node_rejected_until_od024` | → `RB_TRANSFORM_UNDEFINED` | A6-10 |
| `test_output_shaping_drops_undeclared_fields` | Closed output schema | A6-22 |
| `test_redaction_withholds_unprovable_field` | Fail closed, not masked | A6-22 |

## Capability and credentials

| Test | Asserts | Criterion |
|---|---|---|
| `test_runbook_capability_exact_match_required` | Superset and subset both fail | A6-11 |
| `test_admin_capability_unreferenceable` | → `RB_ADMIN_CAPABILITY_FORBIDDEN` | A6-11 |
| `test_read_capability_cannot_satisfy_write_node` | Separation enforced | A6-11 |
| `test_per_node_credential_projection` | Node sees only its own credential | A6-11 |
| `test_capability_not_ready_denies_before_resolution` | Ordering respected | A6-11 |
| `test_capability_drift_invalidates_approval` | → `RB_CAPABILITY_DRIFT` | A6-18 |
| `test_credential_material_never_serialized` | Absent from result, log, trace, metric, evidence | A6-22 |
| `test_metadata_does_not_expand_authority` | Declaring a capability grants nothing | A6-11 |

## Policy, approval, destructive marking

| Test | Asserts | Criterion |
|---|---|---|
| `test_missing_policy_entry_denies` | → `RB_POLICY_MISSING` | A6-12 |
| `test_declared_class_weaker_than_aggregate_rejected` | → `RB_POLICY_CLASS_TOO_WEAK` | A6-12 |
| `test_runbook_class_cannot_preauthorize_node` | Node policy still evaluated | A6-12 |
| `test_runbook_destructive_computed_vs_declared` | Under-declaration rejected | A6-13 |
| `test_destructive_forces_dual_approval` | → `RB_APPROVAL_CLASS_TOO_WEAK` | A6-13 |
| `test_destructive_forces_no_retry` | Retry class enforced | A6-13 |
| `test_irreversible_requires_acceptance_record` | → `RB_IRREVERSIBLE_UNACCEPTED` | A6-14 |
| `test_runbook_plan_digest_stable_for_equivalent_inputs` | Defaults materialized identically | A6-18 |
| `test_runbook_plan_digest_changes_on_any_semantic_change` | Argument/scope/version/snapshot change | A6-18 |
| `test_runbook_approval_digest_mismatch_denies` | Approval for A cannot run B | A6-18 |
| `test_runbook_approval_expiry_denies` | → `RB_APPROVAL_EXPIRED` | A6-18 |
| `test_runbook_approval_single_use` | Second use → already consumed | A6-18 |
| `test_runbook_approval_concurrent_one_winner` | Atomic consumption | A6-18 |
| `test_runbook_approval_self_approval_denied` | Requester ≠ approver for DUAL | A6-18 |
| `test_sensitive_argument_commitment_not_plaintext` | Digest stable, value never stored | A6-22 |
| `test_out_of_scope_resource_zero_resolution_zero_http` | Scope intersection enforced | A6-07 |
| `test_unauthorized_caller_sees_runbook_as_unknown` | No existence leak | A6-23 |

## Rollback, timeouts, budgets, cancellation

| Test | Asserts | Criterion |
|---|---|---|
| `test_runbook_rollback_declaration_required` | Mutating node without declaration rejected | A6-14 |
| `test_automatic_rollback_requires_registered_compensation` | → `RB_COMPENSATION_UNREGISTERED` | A6-14 |
| `test_compensation_is_independently_governed` | Own policy, audit, idempotency | A6-14 |
| `test_compensation_reverse_order_from_audit_records` | Not from the planned graph | A6-15 |
| `test_unsafe_compensation_dead_letters_without_write` | Zero write attempts | A6-15 |
| `test_partial_compensation_reports_residuals` | `COMPENSATED_PARTIAL` + exact list | A6-15 |
| `test_missing_timeout_rejected` | → `RB_TIMEOUT_MISSING` | A6-16 |
| `test_node_timeouts_must_fit_runbook_timeout` | → `RB_TIMEOUT_INCONSISTENT` | A6-16 |
| `test_deadline_propagates_to_nodes` | No node outlives the runbook deadline | A6-16 |
| `test_timeout_on_mutating_runbook_enters_compensation` | No silent retry | A6-16 |
| `test_caller_cannot_widen_budgets` | Only tightening accepted | A6-17 |
| `test_agentic_budget_zero_by_default` | → `RB_AGENTIC_NOT_PERMITTED` when non-zero | A6-17 |
| `test_deterministic_runbook_zero_llm_tokens` | Real runtime accounting = 0 | A6-17 |
| `test_runbook_cancellation_propagates` | Scheduler + nodes | A6-16 |
| `test_rate_limit_produces_no_duplicate_write` | Retry-After handling | A6-19 |

## Idempotency, audit, evidence

| Test | Asserts | Criterion |
|---|---|---|
| `test_execution_idempotency_single_provider_mutation` | Repeat → zero extra mutations | A6-19 |
| `test_idempotency_key_digest_conflict` | Same key, different digest → conflict | A6-19 |
| `test_write_ahead_record_precedes_every_mutation` | Zero mutations without one | A6-20 |
| `test_audit_store_append_only` | No in-place edit | A6-20 |
| `test_audit_write_failure_denies_mutation` | → `RB_AUDIT_WRITE_FAILED` | A6-20 |
| `test_owner_unresolvable_rejected` | → `RB_OWNER_UNRESOLVABLE` | A6-21 |
| `test_review_overdue_denies_high_blast_radius` | → `RB_REVIEW_OVERDUE` | A6-21 |
| `test_runbook_evidence_complete_and_digested` | All mandatory fields + `evidence_digest` | A6-22 |
| `test_runbook_evidence_redaction_scan_clean` | No secret material anywhere | A6-22 |
| `test_snapshot_hashes_recorded` | Runbook + capability snapshots | A6-22 |
| `test_runbook_v1_isolation` | V1 still exactly 27 tools, contract unchanged | A6-02 |

## Migration equivalence

| Test | Asserts | Criterion |
|---|---|---|
| `test_dag_and_runbook_plan_digest_equivalence` | Same canonical bytes for equivalent plans | A6-25 |
| `test_promoted_runbook_matches_reference_dag_behaviour` | Same node set, same order, same outcomes | A6-25 |
| `test_promotion_cannot_weaken_controls` | Promoted runbook ≥ DAG plan's controls | A6-25 |

## Connected exemplar (INNER/OUTER)

| Test / step | Asserts | Criterion |
|---|---|---|
| `collector_admit_promote_exemplar` | `RB-GITHUB-PR-LIFECYCLE-001` admitted and staged-promoted | A6-24 |
| `collector_execute_exemplar_disposable_repo` | End-to-end run against a disposable repository | A6-24 |
| `collector_cleanup_residual_zero` | Residual object count 0 | A6-24 |
| `outer_verify_repository_state_matches_evidence` | Independent verification | A6-24 |
| `outer_verify_no_unrecorded_mutation` | Provider state ⊆ audited mutations | A6-20 |

## Discipline

- The hermetic suite must pass with zero real network access; a test that needs
  the network belongs in the connected collector.
- Negative and adversarial tests are mandatory, not optional coverage.
- A criterion without a passing named test counts as failed (A6-26).
