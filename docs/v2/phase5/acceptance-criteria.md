# Phase 5 Fail-Closed Acceptance Criteria — `DAG_ACCEPTED`

> **V2 · PHASE 5 · DESIGN ONLY · NOT IMPLEMENTED · NO V1 IMPACT**
>
> Defines what a future gate must prove. Declares nothing, pre-approves nothing,
> and does not partially satisfy any gate.

## Structure

Two layers, as in Phases 2 and 3:

- **INNER** — connected deterministic collector against a real disposable
  repository plus a hermetic suite; machine-checked evidence, `failures=[]`.
- **OUTER** — independent out-of-band verification that real provider state and
  persisted store state match the evidence, executed separately from the
  collector, `reasons=[]`.

Either layer failing ⇒ NOT ACCEPTED.

## Mandatory criteria

| ID | Criterion |
|---|---|
| A5-01 | `DIRECT_MUTATION_ACCEPTED` **and** `BATCH_ACCEPTED` were declared before any Phase 5 execution code was merged; branch merge-base proves the ordering |
| A5-02 | V1 remains exactly 27 tools, bridge `1.0.0`, schema `0.6.1`, contract `1.0.0`; regression evidence retained |
| A5-03 | No shell, subprocess, generic HTTP or arbitrary-URL surface exists in any node type; static scan + runtime assertion prove it |
| A5-04 | No arbitrary expression/template/eval path in bindings or transforms; transform ops are exactly the accepted closed set |
| A5-05 | Cycle detection proven: cyclic, self-dependent, unknown-dependency, duplicate-edge, unreachable-node and depth/fan-out-exceeding plans are rejected with stable reason codes, deterministically, with zero credential resolution and zero HTTP |
| A5-06 | Binding type safety proven: unknown field, type mismatch, undeclared edge, oversize value and control-field target all reject statically; runtime re-validation rejects hostile provider values |
| A5-07 | `plan_digest` determinism proven: node reordering, `depends_on` reordering, whitespace and editorial metadata do not change the digest; tool/arg/edge/budget/policy/digest_version changes do |
| A5-08 | Approval binding proven: digest mismatch, expiry, wrong scope, reuse and concurrent consumption all DENY; exactly one concurrent consumption succeeds |
| A5-09 | Per-node policy proven: every node evaluated independently; a missing policy entry DENIES; a denied node performs zero credential resolution and zero HTTP; plan-level configuration cannot widen a node |
| A5-10 | Bounded parallelism proven: observed concurrency never exceeds `min(plan, engine, provider, credential)` limits; mutating nodes on the same resource never overlap; deterministic dispatch order for a fixed plan |
| A5-11 | Compensation proven in reverse topological order with read-back verification; unsafe/ambiguous compensation performs zero writes and dead-letters; residual object count after a successful compensation run is 0; retained effects are enumerated explicitly |
| A5-12 | `INDETERMINATE` behaviour proven: no auto-retry, no auto-compensation, dependents skipped, durable checkpoint written before recovery, plan status never better than `INDETERMINATE`, `unknown_effects` always reported |
| A5-13 | Zero Hermes LLM tokens consumed on the DAG path, measured from real runtime accounting |
| A5-14 | No secret material in results, bindings, transforms, checkpoints, audit records, logs, traces, metric labels or evidence files; redaction scan clean |
| A5-15 | Checkpoint/resume proven: kill mid-plan and mid-mutation; resume duplicates zero mutations, re-consumes zero approvals, and re-evaluates policy (a now-denied node is skipped, never executed under the stale decision) |
| A5-16 | Lease/fencing proven: two engine instances cannot both dispatch a node; stale `fence_token` writes are rejected; lease expiry makes a plan recoverable, not cancelled |
| A5-17 | Checkpoint integrity proven: tampered record, digest mismatch or unsupported state schema dead-letters and never resumes execution |
| A5-18 | Budget/backpressure proven: node count, parallelism, external-call, wall-time, result-byte and checkpoint-byte ceilings each terminate the plan explicitly rather than silently trimming |
| A5-19 | Partial success proven: `continue_independent` completes unrelated branches and reports a complete per-node status map; `fail_fast` skips all unstarted nodes with `UPSTREAM_ABORT` |
| A5-20 | `dry_run` proven: full validation, per-node policy decisions and digest reported with zero credential resolution and zero external calls; a `dry_run` output cannot be replayed as an approval |
| A5-21 | Replay simulation proven observably distinct: `replay=true` in every audit record, zero external calls, zero approval consumption, zero idempotency writes |
| A5-22 | Every criterion traces to a named test in `test-plan.md` and to a requirement in `../requirements/functional.md` / `../requirements/security.md` |

## Fail-closed discipline

- A criterion with no executed test is NOT satisfied. A test name is not
  evidence.
- Evidence must be produced by the accepted source commit and retained with
  SHA-256 digests in `../evidence/`.
- No criterion may be waived to promote the gate. Reducing scope means removing
  the capability, not the criterion.
- `DAG_ACCEPTED` claims exactly these criteria and nothing more. Open decisions
  OD-003, OD-018, OD-021, OD-024 must be **closed** (not merely acknowledged)
  before the gate can be declared, because each changes observable semantics.
