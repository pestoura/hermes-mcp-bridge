# Phase 3 Fail-Closed Acceptance Criteria — `DIRECT_MUTATION_ACCEPTED`

> **V2 · PHASE 3 · PREPARATION ONLY · NOT IMPLEMENTED · NO V1 IMPACT**
>
> This document defines what a future gate must prove. It does not declare,
> pre-approve or partially satisfy any gate.

## Structure

The gate follows the Phase 2 two-layer pattern:

- **INNER**: connected, deterministic collector run against a real disposable
  repository, producing machine-checked evidence.
- **OUTER**: out-of-band verification that the real repository state matches the
  evidence, executed independently of the collector.

Both must return `failures=[]`. Either layer failing means NOT ACCEPTED.

## Mandatory criteria

| ID | Criterion |
|---|---|
| A3-01 | `DIRECT_READ_ACCEPTED` was declared before any Phase 3 code was merged; the Phase 3 branch's merge-base proves the ordering |
| A3-02 | V1 remains exactly 27 tools, bridge `1.0.0`, schema `0.6.1`, contract `1.0.0`; regression evidence retained |
| A3-03 | Write capabilities are separate from `github.read`; probed permissions equal the intended set exactly (no superset) |
| A3-04 | `Administration` permission absent; no code path can emit a repository-deletion request (static + runtime assertion) |
| A3-05 | Every mutation is preceded by a write-ahead audit record; zero committed mutations without one |
| A3-06 | Approval binding proven: digest mismatch, expiry, wrong scope, reuse and concurrent consumption all DENY |
| A3-07 | Idempotency proven: a repeated identical request produces exactly one provider mutation |
| A3-08 | Optimistic concurrency proven: head/base drift between approval and execution produces a DENY or provider `409`, never a silent write |
| A3-09 | Scope enforcement proven: an out-of-scope repository produces zero credential resolution and zero HTTP requests |
| A3-10 | Merge governance proven: default-branch merge DENY, missing required checks DENY, unverifiable protection state DENY |
| A3-11 | Compensation proven for `create_branch` and `create_pr`; residual object count after cleanup is 0 |
| A3-12 | Unsafe compensation dead-letters to manual intervention rather than attempting a write |
| A3-13 | Zero Hermes LLM tokens consumed on the DIRECT mutation path, measured from real runtime accounting |
| A3-14 | No secret material in results, logs, traces, metric labels or evidence files; redaction scan clean |
| A3-15 | Rate-limit / `Retry-After` handling produces no duplicate write attempts |
| A3-16 | Every criterion above traces to a named test in `test-matrix.md` and to a requirement in `../requirements/security.md` |

## Fail-closed discipline

- An unevaluated criterion counts as a failure, not as "not applicable".
- Mock, CI-only or simulated evidence never substitutes for the connected run.
- The gate script must emit a single machine-readable verdict and retained,
  digest-recorded evidence, matching the established Phase 0/1/2 evidence chain.
