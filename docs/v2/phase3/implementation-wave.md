# Phase 3 Implementation Wave — Lane Definition and File Ownership

> **V2 · PHASE 3 · IMPLEMENTATION WAVE PLAN**
>
> Precondition satisfied: `DIRECT_READ_ACCEPTED` (Phase 2 promotion PR #82 merged,
> issue #51 closed, postmerge CI success). The Phase 3 design lane (PR #73) is
> merged, so ADR-0020..ADR-0023 and `docs/v2/phase3/*` are the binding design
> input for this wave.
>
> **V1 invariants are untouched by every lane:** bridge `1.0.0`, schema `0.6.1`,
> exactly 27 operational tools, HMAC policy surface unchanged. No lane may add a
> generic shell / arbitrary-command public surface.

## Wave shape

Six implementation lanes plus a Controller lane. Lanes are ordered by dependency
but are designed so that L1..L4 can proceed concurrently after the Controller
publishes the shared enum/error surface (C-1). Each lane owns disjoint files
except for the explicitly listed shared touchpoints, which are Controller-owned.

| Lane | Title | Depends on | Blocking for |
|---|---|---|---|
| C | Controller / integration | — | all |
| L1 | Write credential + capability split | C-1 | L5, L6 |
| L2 | Mutation registry + typed contracts (`create_branch`, `create_pr`) | C-1 | L3, L5 |
| L3 | Approval, operation digest, idempotency, concurrency, locks | C-1 | L5 |
| L4 | Mutation audit / provenance / evidence (fail-closed, write-ahead) | C-1 | L5 |
| L5 | Mutation executor: preflight ordering, TOCTOU revalidation, read-back, INDETERMINATE | L1..L4 | L6 |
| L6 | Governed merge + destructive exclusion (`delete_repository` DENY) | L1, L2, L5 | gate |

## Lane C — Controller / integration (this lane)

Scope: bootstrap only in this run. Owns cross-lane surfaces so lanes never edit
the same file.

Files owned:

- `src/hermes_mcp_bridge/v2/enums.py` (new mutation enum members only)
- `src/hermes_mcp_bridge/v2/errors.py` (new mutation error types)
- `src/hermes_mcp_bridge/v2/__init__.py` (re-exports)
- `docs/v2/phase3/implementation-wave.md` (this file)
- `docs/v2/roadmap.md`, `docs/v2/requirements/traceability-matrix.md` (conflict-prone; Controller-only)
- `scripts/v2_phase3_preflight.py` (canonical preflight, Controller-owned)

Responsibilities: publish C-1 (shared enums/errors), sequence merges, arbitrate
file-ownership conflicts, run the integration suite, own the final
`DIRECT_MUTATION_ACCEPTED` gate wiring.

## Lane L1 — Write credential and capability split

Implements `credential-split.md` + ADR-0020. `github.write` capability distinct
from `github.read`; no capability may satisfy both a read tool and a write tool.
Exact-permission probing (superset ⇒ NOT_READY), `Administration` grant ⇒
NOT_READY. Broker returns status only; write material never serialized.

Files owned:
- `src/hermes_mcp_bridge/v2/github_write_credentials.py` (new)
- `src/hermes_mcp_bridge/v2/github_readiness.py` (extend: exact permission match)
- `tests/test_v2_phase3_write_credentials.py` (new)

Criteria: A3-03, A3-04 (permission half), A3-14.

## Lane L2 — Mutation registry and typed contracts

Implements `mutation-semantics.md`. Typed definitions for `github.create_branch`
and `github.create_pr`: strict JSON schemas (`additionalProperties: false`),
`MutationClass`, `IdempotencySemantics`, `ApprovalRequirement`, retry class,
result shaping. Explicit per-operation policy rules — a missing rule is DENY by
construction (existing `MISSING_POLICY_RULE`). No wildcard rules.

Files owned:
- `src/hermes_mcp_bridge/v2/github_mutation_registry.py` (new)
- `tests/test_v2_phase3_mutation_registry.py` (new)

Must not edit `github_registry.py` (read lane) — the mutation registry composes it.

Criteria: A3-09 (schema/scope shape), A3-16.

## Lane L3 — Approval, digest, idempotency, concurrency

Implements ADR-0021/ADR-0022 and `approval-and-digest.md`,
`idempotency-and-concurrency.md`. Canonical `operation_digest` over
(tool, normalized args, policy snapshot, scope, expected head); approval bound
to digest, single-use, expiring, atomically consumed; idempotency key scoped by
principal+repo+digest; lease/`IN_PROGRESS` semantics; optimistic concurrency via
`expected_head_sha`.

Files owned:
- `src/hermes_mcp_bridge/v2/mutation_digest.py` (new)
- `src/hermes_mcp_bridge/v2/mutation_idempotency.py` (new)
- `tests/test_v2_phase3_digest_and_approval.py` (new)
- `tests/test_v2_phase3_idempotency_concurrency.py` (new)

Reuses `canonical.py` (read-only) and top-level `locks.py`/`_file_lock.py`
(read-only; any change request goes to Controller).

Criteria: A3-06, A3-07, A3-08.

## Lane L4 — Mutation audit, provenance, evidence

Implements `audit-and-evidence.md`. Write-ahead audit record persisted and
durable **before** any provider call; fail-closed if the record cannot be
written (no mutation attempted); redaction of all secret-bearing fields;
provenance chain compatible with the Phase 0/1/2 evidence/digest chain.

Files owned:
- `src/hermes_mcp_bridge/v2/mutation_audit.py` (new)
- `tests/test_v2_phase3_mutation_audit.py` (new)

Criteria: A3-05, A3-14.

## Lane L5 — Mutation executor

The only lane allowed to issue write HTTP. Fixed, non-reorderable preflight:

1. scope check (out-of-scope ⇒ zero credential resolution, zero HTTP)
2. registry lookup (unknown operation ⇒ DENY)
3. per-operation policy decision (missing rule ⇒ DENY)
4. capability/credential readiness (write capability only)
5. approval verification against `operation_digest`
6. idempotency lookup / lease acquisition
7. TOCTOU revalidation of expected head/base immediately before the call
8. write-ahead audit record
9. provider call
10. read-back verification of the created object
11. result shaping / evidence emission, lease release

Ambiguity (timeout, connection reset, unverifiable read-back) ⇒ `INDETERMINATE`
with mandatory reconciliation read; never a blind retry. `Retry-After` handling
must not duplicate a write.

Files owned:
- `src/hermes_mcp_bridge/v2/github_mutations.py` (new; write executor)
- `tests/test_v2_phase3_github_mutations.py` (new, hermetic, zero network)

Must not edit `github_direct.py` (read executor).

Criteria: A3-07, A3-08, A3-09, A3-15, plus INDETERMINATE semantics.

## Lane L6 — Governed merge and destructive exclusion

Implements `governed-merge.md` + ADR-0023. `github.merge_pr` conditional on
gates: required checks GREEN, protection state verifiable (unverifiable ⇒ DENY),
default-branch merge DENY unless explicitly scoped, distinct approver required.
`delete_repository` DENY by default: no registry entry, and a static assertion
that no code path can emit `DELETE /repos/{owner}/{repo}`.

Files owned:
- `src/hermes_mcp_bridge/v2/github_governed_merge.py` (new)
- `tests/test_v2_phase3_governed_merge.py` (new)
- `tests/test_v2_phase3_destructive_exclusion.py` (new)

Criteria: A3-04 (destructive half), A3-10.

## Canonical tests and preflight

- Hermetic suite: `tests/test_v2_phase3_*.py`, zero real network, run in CI.
- Acceptance shape: `tests/test_v2_phase3_mutation_acceptance.py` (Controller).
- Preflight: `scripts/v2_phase3_preflight.py` — static, offline, fail-closed.
  Asserts V1 invariants (bridge 1.0.0 / schema 0.6.1 / 27 tools), absence of any
  repository-deletion request string, and read/write capability disjointness.
  Exit non-zero ⇒ the wave is blocked.
- Connected gate (later, Controller): INNER collector against a disposable
  repository + OUTER out-of-band verification, both `failures=[]`.

## Integration strategy

1. Controller merges C-1 (enums/errors/preflight) first.
2. L1..L4 open independent PRs against `main`; each must pass the hermetic suite
   and the preflight.
3. L5 rebases after L1..L4 are merged; it is the first PR allowed to reference
   the write path end-to-end.
4. L6 last, then the Controller wires `DIRECT_MUTATION_ACCEPTED`.
5. Any lane needing a file it does not own raises a Controller request instead
   of editing it.
