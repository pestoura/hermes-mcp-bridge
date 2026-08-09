# Phase 6 Fail-Closed Acceptance Criteria — `RUNBOOK_ACCEPTED`

> **V2 · PHASE 6 · DESIGN ONLY · NOT IMPLEMENTED · NO V1 IMPACT**
>
> This document defines what a future gate must prove. It does not declare,
> pre-approve or partially satisfy any gate.

## Structure

The gate follows the Phase 2/3 two-layer pattern:

- **INNER** — connected, deterministic collector run against a real disposable
  repository, producing machine-checked evidence.
- **OUTER** — out-of-band verification, independent of the collector, that the
  real external state and the registry state match the evidence.

Both must return `failures=[]`. Either layer failing means NOT ACCEPTED.

## Mandatory criteria

| ID | Criterion |
|---|---|
| A6-01 | `BATCH_ACCEPTED`, `DAG_ACCEPTED` and `DIRECT_MUTATION_ACCEPTED` were declared before any Phase 6 code was merged; the branch merge-base proves the ordering |
| A6-02 | V1 remains exactly 27 tools, bridge `1.0.0`, schema `0.6.1`, contract `1.0.0`; regression evidence retained |
| A6-03 | Admission is deterministic: compiling the same manifest twice yields byte-identical IR and identical `runbook_digest` |
| A6-04 | Admission performs zero network calls and zero credential resolutions, proven by instrumentation |
| A6-05 | `(runbook_id, version)` is immutable: re-admission with a different digest is rejected and nothing is overwritten; registry events are append-only |
| A6-06 | Unpinned or floating tool/runbook references are rejected; every admitted node pins an exact version |
| A6-07 | Invocation without the expected `runbook_digest` is denied; digest mismatch denies with zero credential resolution and zero HTTP requests |
| A6-08 | Yanked runbooks are non-invocable immediately, with no grace period; in-flight executions cancel at the next node boundary and are compensated or dead-lettered |
| A6-09 | Parameter schema is closed: unknown properties, oversize payloads, out-of-constraint values and secret-shaped parameters are all rejected |
| A6-10 | No unsafe binding is admissible: templating, expressions, `eval`, environment or filesystem lookup are rejected; static type compatibility is enforced |
| A6-11 | The computed capability set equals the declared set exactly; a superset and a subset both fail; no administrative capability is referenceable |
| A6-12 | Declared `policy_class` is at least the computed aggregate; a weaker declaration is rejected; a missing per-node policy entry denies |
| A6-13 | `destructive_action` is computed and compared; under-declaration is rejected; `true` forces `approval_class ≥ DUAL`, `NO_RETRY` and write-ahead audit |
| A6-14 | `rollback_support` is declared for every mutating node; `AUTOMATIC` is backed by a registered, tested, independently governed compensation; `NOT_SUPPORTED` requires recorded irreversibility acceptance |
| A6-15 | Unsafe compensation dead-letters to `MANUAL_INTERVENTION_REQUIRED` without attempting a write; partial compensation reports `COMPENSATED_PARTIAL` with the exact residual list |
| A6-16 | Runbook and per-node timeouts are present, bounded and internally consistent; deadlines propagate; timeout on a mutating runbook enters compensation, never a silent retry |
| A6-17 | Budgets cannot be widened by the caller; `max_agentic_escalations` and `max_agentic_tokens` default to 0 and a deterministic runbook consumes exactly 0 Hermes LLM tokens, measured from real runtime accounting |
| A6-18 | Approval binding proven: digest mismatch, expiry, wrong scope, self-approval, reuse and concurrent consumption all deny; exactly one winner under race |
| A6-19 | Execution idempotency proven: a repeat of `(idempotency_key, plan_digest)` produces exactly zero additional provider mutations and returns the recorded result; the same key with a different digest conflicts |
| A6-20 | Every mutating node has a write-ahead audit record preceding it; zero committed mutations without one; audit store is append-only |
| A6-21 | Every runbook has a resolvable owner and a bounded review cadence; overdue review denies invocation for `MUTATING_HIGH` or `destructive_action = true` |
| A6-22 | Evidence is complete, integrity protected and tamper detectable; redaction scan is clean across results, logs, traces, metric labels and artifacts; unprovable fields are withheld, not masked |
| A6-23 | Projection is authorized-only: an unauthorized caller cannot detect the existence of a restricted runbook |
| A6-24 | The `RB-GITHUB-PR-LIFECYCLE-001` exemplar is admitted, promoted through a staged path, executed end to end against a disposable repository, and fully cleaned up with residual object count 0 |
| A6-25 | Migration equivalence proven: the reference DAG plan and its promoted runbook produce identical `plan_digest` under the shared canonical serialization for equivalent inputs |
| A6-26 | Every criterion above traces to a named test in `test-plan.md` and to a requirement in `../requirements/functional.md` / `../requirements/security.md` |

## Traceability anchor

The existing matrix row is
`V2-FR-006, V2-FR-015, V2-SEC-010 | ADR-0010 | Runbook Registry | 6 |
test_runbook_version_digest_compile | RUNBOOK_ACCEPTED`.

This lane expands that row without editing `../requirements/traceability-matrix.md`.
The expansion to be applied **when Phase 6 implementation starts**:

| Requirement(s) | ADR / design | Component | Test/evidence |
|---|---|---|---|
| V2-FR-006, V2-SEC-010 | ADR-0010/0024 | Runbook Registry — identity/versioning/pinning | `test_runbook_version_digest_compile`, `test_runbook_immutable_pin_*` |
| V2-FR-015 | ADR-0028 | Admission validator | `test_runbook_admission_*` |
| V2-SEC-005, V2-SEC-011 | ADR-0012/0025 | Plan digest + approval | `test_runbook_plan_digest_*`, `test_runbook_approval_*` |
| V2-SEC-025, V2-FR-014 | ADR-0007/0026 | Capability/credential requirements | `test_runbook_capability_*` |
| V2-SEC-021 | ADR-0014/0027 | Rollback and destructive marking | `test_runbook_rollback_*`, `test_runbook_destructive_*` |
| V2-SEC-008, V2-SEC-023 | ADR-0015 | Evidence and redaction | `test_runbook_evidence_*` |
| V2-FR-019 | ADR-0009 | Cancellation/deadline propagation | `test_runbook_cancellation_*` |
| V2-FR-025, V2-NFR-009 | ADR-0017 | V1 isolation and rollback availability | `test_runbook_v1_isolation` |

## Fail-closed discipline

- An unevaluated criterion counts as a failure, not as "not applicable".
- Mock, CI-only or simulated evidence never substitutes for the connected run.
- The gate script emits a single machine-readable verdict plus retained,
  digest-recorded evidence, matching the Phase 0/1/2 evidence chain.
- Declaring `RUNBOOK_ACCEPTED` requires both layers `failures=[]`; nothing in
  this lane may be cited as partial satisfaction.
