# Phase 5 Test Plan (Planned)

> **V2 · PHASE 5 · DESIGN ONLY · NOT IMPLEMENTED · NO V1 IMPACT**
>
> Test names below are **planned**. None exists. A test name is not evidence.

## Planned homes

| File | Nature |
|---|---|
| `tests/test_v2_phase5_dag_validation.py` | Hermetic; zero network |
| `tests/test_v2_phase5_plan_digest.py` | Hermetic; pure |
| `tests/test_v2_phase5_scheduler.py` | Hermetic; fake clock and fake pool |
| `tests/test_v2_phase5_checkpoint_resume.py` | Hermetic; in-memory store double |
| `tests/test_v2_phase5_compensation.py` | Hermetic |
| `tests/test_v2_phase5_dag_acceptance.py` | Evidence/gate shape |

None of these files may be created before `BATCH_ACCEPTED`.

## Non-runtime fixtures (safe to add now)

`tests/fixtures/v2_phase5/` — **data only**, no imports of Phase 3/4 modules, no
executable helpers, no conftest changes. Pure JSON plan documents used later by
the validation and digest suites, and usable immediately for design review.

| Fixture | Purpose |
|---|---|
| `plan_valid_linear.json` | Two-node read→transform plan; baseline valid shape |
| `plan_valid_diamond.json` | Fan-out/fan-in; parallelism and aggregation shape |
| `plan_cycle_simple.json` | `a→b→a`; expects `PLAN_CYCLE_DETECTED` |
| `plan_self_dependency.json` | `a→a`; expects `PLAN_SELF_DEPENDENCY` |
| `plan_unknown_dependency.json` | Edge to a missing node |
| `plan_binding_edge_undeclared.json` | Binding without matching `depends_on` |
| `plan_binding_type_mismatch.json` | Declared type ≠ source type |
| `plan_binding_control_field.json` | Binding targets `tool`; must be forbidden |
| `plan_unreachable_node.json` | Dead node with unconsumed output |
| `plan_digest_reorder_a.json` / `plan_digest_reorder_b.json` | Same semantics, different node/`depends_on` order and editorial metadata; digests must be equal |
| `plan_digest_semantic_change.json` | One argument changed; digest must differ |

These are inert `.json` files under a new directory that no Phase 3/4 code
reads. They cannot conflict with Phase 3/4 code paths.

## Matrix — validation and cycles

| Test | Asserts | Criterion |
|---|---|---|
| `test_cycle_detected_deterministically` | Same cycle reported identically across runs | A5-05 |
| `test_self_dependency_rejected` | `PLAN_SELF_DEPENDENCY` | A5-05 |
| `test_unknown_dependency_rejected` | `PLAN_UNKNOWN_DEPENDENCY` | A5-05 |
| `test_duplicate_edge_rejected` | Digest determinism preserved | A5-05 |
| `test_unreachable_node_rejected` | `PLAN_UNREACHABLE_NODE` | A5-05 |
| `test_depth_and_fanout_limits` | Bounded graph shape | A5-05, A5-18 |
| `test_invalid_plan_zero_resolution_zero_http` | No broker call, no HTTP on any rejection | A5-05, A5-09 |
| `test_unknown_field_rejected` | Fail-closed schema | A5-06 |

## Matrix — bindings and transforms

| Test | Asserts | Criterion |
|---|---|---|
| `test_binding_edge_must_be_declared` | `BINDING_EDGE_NOT_DECLARED` | A5-06 |
| `test_binding_unknown_field_rejected` | Static schema path resolution | A5-06 |
| `test_binding_type_mismatch_rejected` | Source/target type equality | A5-06 |
| `test_binding_cannot_target_control_field` | tool/policy/credential/scope untouchable | A5-06, A5-03 |
| `test_binding_runtime_revalidation_rejects_hostile_value` | Provider value re-checked, not retried | A5-06 |
| `test_binding_cannot_widen_scope` | Upstream-produced out-of-scope resource DENIED | A5-09 |
| `test_transform_ops_closed_set` | Unknown op rejected | A5-04 |
| `test_transform_no_eval_or_code` | No eval/template/subprocess path | A5-03, A5-04 |
| `test_transform_output_bounded` | Size ceilings enforced | A5-18 |
| `test_no_shell_or_http_node_kind` | Static scan over node kinds and registry | A5-03 |

## Matrix — digest and approval

| Test | Asserts | Criterion |
|---|---|---|
| `test_digest_stable_under_node_reorder` | Order is not semantics | A5-07 |
| `test_digest_stable_under_editorial_metadata` | Comments excluded | A5-07 |
| `test_digest_changes_on_arg_change` | Semantic sensitivity | A5-07 |
| `test_digest_changes_on_edge_change` | Graph is semantics | A5-07 |
| `test_digest_version_prefix_isolates` | Cross-version digests never equal | A5-07 |
| `test_approval_digest_mismatch_denies` | Exact match required | A5-08 |
| `test_approval_expiry_and_scope_denies` | Time and scope bound | A5-08 |
| `test_approval_single_use_atomic` | Exactly one concurrent consumer succeeds | A5-08 |
| `test_runtime_bound_mutation_requires_operation_digest` | Phase 3/5 composition rule | A5-08 |

## Matrix — scheduling

| Test | Asserts | Criterion |
|---|---|---|
| `test_parallelism_never_exceeds_min_bound` | Observed concurrency ceiling | A5-10 |
| `test_dispatch_order_deterministic` | `(rank, node_id)` tie-break | A5-10 |
| `test_same_resource_mutations_serialized` | No overlap on one resource | A5-10 |
| `test_slot_acquisition_order_no_deadlock` | global→provider→credential | A5-10 |
| `test_breaker_open_skips_provider_nodes` | Not counted as node failure | A5-19 |
| `test_deadline_exceeded_terminal` | No starvation, explicit status | A5-18 |
| `test_budget_exhaustion_explicit` | Never silent trim | A5-18 |
| `test_a5_19c_continue_independent_completes_unrelated_branches` | Independent branch survives a sibling failure | A5-19 |
| `test_a5_19d_fail_fast_skips_unstarted_with_upstream_abort` | Parallel branch still runs; only its dependents abort | A5-19 |
| `test_a5_20c_dry_run_makes_no_call_and_is_not_an_approval` | Zero calls, key shape only, `is_approval=False` | A5-20 |
| `test_a5_20d_dry_run_reports_denials_without_executing` | Denials surfaced, no execution | A5-09, A5-20 |

## Matrix — checkpoint, resume, lease

| Test | Asserts | Criterion |
|---|---|---|
| `test_write_ahead_before_mutating_dispatch` | Key persisted first | A5-15 |
| `test_resume_after_lease_recovery` | Recoverable, not cancelled | A5-16 |
| `test_resume_duplicates_zero_mutations` | Idempotency key reuse | A5-15 |
| `test_resume_reevaluates_policy_denied_node_skipped` | New authorization moment | A5-15, A5-09 |
| `test_resume_does_not_reconsume_approval` | `approval_ref` retained | A5-08, A5-15 |
| `test_stale_fence_token_rejected` | Compare-and-set | A5-16 |
| `test_tampered_checkpoint_dead_letters` | Integrity digest | A5-17 |
| `test_unsupported_state_schema_pauses` | Fail-closed on version drift | A5-17 |
| `test_checkpoint_contains_no_secret_material` | Redaction scan | A5-14 |
| `test_replay_zero_external_calls_and_flagged` | Replay distinctness | A5-21 |
| `test_replay_consumes_no_approval` | Zero approval consumption | A5-21 |

## Matrix — failure and `INDETERMINATE`

| Test | Asserts | Criterion |
|---|---|---|
| `test_fail_fast_skips_unstarted_nodes` | `UPSTREAM_ABORT` | A5-19 |
| `test_continue_independent_keeps_parallel_branch_alive` | Independent success not aborted | A5-19 |
| `test_continue_independent_completes_unrelated_branches` | `PARTIAL` with full map | A5-19 |
| `test_failed_requires_proof_of_no_commit` | Otherwise `INDETERMINATE` | A5-12 |
| `test_indeterminate_not_retried` | No auto-retry | A5-12 |
| `test_indeterminate_not_compensated` | No auto-compensation | A5-12 |
| `test_indeterminate_blocks_dependents` | `UPSTREAM_INDETERMINATE` | A5-12 |
| `test_plan_status_precedence` | `INDETERMINATE` dominates `PARTIAL` | A5-12 |
| `test_unknown_effects_always_reported` | No silent side effects | A5-12 |

## Matrix — compensation

| Test | Asserts | Criterion |
|---|---|---|
| `test_compensation_reverse_topological_order` | Ordering | A5-11 |
| `test_compensation_requires_read_back_verification` | 200 is not proof | A5-11 |
| `test_compensation_precondition_drift_is_unsafe` | Zero writes, dead-letter | A5-11 |
| `test_merged_pr_cannot_be_compensated` | Explicit unsafe case | A5-11 |
| `test_compensation_is_policy_evaluated` | Not a privileged path | A5-09, A5-11 |
| `test_compensation_idempotent_after_crash` | `comp_key` reconciliation | A5-11, A5-15 |
| `test_residual_objects_zero_after_clean_compensation` | Cleanup proof | A5-11 |
| `test_retained_effects_enumerated` | Honest reporting | A5-11 |

## Connected/OUTER evidence (planned)

Disposable repository, real provider, separate collector and verifier, zero-token
measurement from real runtime accounting (A5-13), and independent state
verification of branches/PRs/refs after compensation. Same two-layer shape as
Phase 2/3.
