# Phase 6 Preparation Lane — RUNBOOK Registry

> **V2 · PHASE 6 · DESIGN ONLY · NOT IMPLEMENTED · NO V1 IMPACT**
>
> `DAG_ACCEPTED` is **NOT declared**. Phase 4 (`BATCH_ACCEPTED`) and Phase 5
> (`DAG_ACCEPTED`) gates are open. Nothing in this lane may be implemented,
> wired into an executable path, or treated as acceptance. The operational V1
> surface remains exactly **27 tools**, bridge `1.0.0`, schema `0.6.1`,
> contract `1.0.0`.
>
> **DO_NOT_MERGE_UNTIL_DAG_ACCEPTED.**

## Purpose

This lane defines the Runbook Registry — identity, versioning, parameter
schema, capability requirements, policy/approval class, destructive-action
marking, rollback support, timeouts, ownership, evidence policy, admission
validation, immutable version pinning, `plan_digest` binding, least-privilege
and fail-closed invocation — **before** any Phase 6 code exists, so the
registry's security semantics are not invented under delivery pressure.

It is deliberately **docs / ADR / contract / test-plan only**. It adds no
runtime module, no tool, no gate change, and no modification to any existing
document outside `docs/v2/phase6/`.

## Hard preconditions (all must hold before any Phase 6 implementation)

1. `BATCH_ACCEPTED` and `DAG_ACCEPTED` are declared with `failures=[]` from
   connected, fail-closed gates following the Phase 0/1/2 evidence pattern.
2. `DIRECT_MUTATION_ACCEPTED` is declared; a runbook may not be the first place
   a mutation class is exercised.
3. ADR-0010 (Skill vs Runbook), ADR-0012 (approval bound to plan digest),
   ADR-0017 (versioning) and the Phase 6 ADRs ADR-0028..ADR-0031 are promoted
   from Proposed.
4. OD-002 (runbook DSL), OD-018 (canonical serialization) and OD-019 (runbook
   signing) are resolved and recorded; an unresolved OD is a blocker, not a
   default.
5. The Phase 6 test plan (`test-plan.md`) is implemented and failing-closed
   before any runbook is admitted to the registry.

## Documents in this lane

| Document | Scope |
|---|---|
| `registry-identity-and-versioning.md` | Runbook ID grammar, semantic version, `runbook_digest`, immutable pinning, deprecation/yank |
| `parameter-schema.md` | Typed parameter/output schema, canonicalization, forbidden constructs |
| `capability-and-credential-requirements.md` | `requires_capabilities`, credential capability IDs, readiness, least privilege |
| `policy-approval-and-destructive-actions.md` | Policy class, approval class, `destructive_action` marker, resource scopes |
| `rollback-timeouts-and-budgets.md` | `rollback_support`, compensation declaration, `timeout_ms`, budgets, cancellation |
| `ownership-and-evidence.md` | `owner`, review cadence, evidence policy, retention, redaction |
| `admission-validation.md` | Admission pipeline, fail-closed order, rejection reason codes |
| `plan-digest-binding.md` | `runbook_digest` → `plan_digest` → approval binding chain |
| `invocation-model.md` | Fail-closed invocation ordering, least-privilege projection, denial codes |
| `acceptance-criteria.md` | Fail-closed `RUNBOOK_ACCEPTED` criteria (A6-01..A6-22) |
| `test-plan.md` | Planned hermetic + connected test matrix mapped to criteria |
| `migration-dag-to-runbook.md` | Migration path from ad-hoc DAG plans to reusable runbooks |
| `contracts/runbook-manifest-contract.md` | Normative manifest field contract |
| `contracts/RB-GITHUB-PR-LIFECYCLE-001.md` | Exemplar runbook manifest (illustrative, not admitted) |
| `adrs/ADR-0028..ADR-0031` | Phase 6 decisions (Proposed) |

## Reconciliation with existing V2 documents

This lane **does not restate** decisions recorded elsewhere; it refines them for
the runbook case and points back to the source.

| Existing document | Relationship |
|---|---|
| `../adrs/ADR-0010-skill-vs-runbook.md` | Base decision (Skill vs Runbook); this lane supplies the registry, promotion lifecycle and DSL constraints its "Open questions" defer |
| `../adrs/ADR-0004-canonical-tool-registry.md` | Runbook Registry is a **separate** registry that *references* tool registry entries; it does not extend or mutate them |
| `../adrs/ADR-0005-capability-projection.md` | Runbooks are projected under the same authorized-only rules; a runbook is never visible because it exists, only because it is authorized |
| `../adrs/ADR-0007-least-privilege-credentials.md` | Base decision; refined by `capability-and-credential-requirements.md` + ADR-0030 |
| `../adrs/ADR-0011-per-node-policy.md` | Applies unchanged per node; the runbook adds an *aggregate* class that can only be stricter |
| `../adrs/ADR-0012-approval-immutable-plan-digest.md` | Base decision; specialized to the `runbook_digest`→`plan_digest` chain in ADR-0029 |
| `../adrs/ADR-0013-idempotency-replay-protection.md` | Applies to runbook nodes unchanged; runbook adds an execution-level idempotency key |
| `../adrs/ADR-0014-saga-compensation.md` | Base decision; `rollback_support` is a declaration, never an assumption |
| `../adrs/ADR-0017-versioning-backward-compatibility.md` | Governs runbook semantic-version compatibility rules |
| `../adrs/ADR-0021/0022/0023` (Phase 3) | Single-node mutation semantics inherited unchanged by runbook nodes; governed merge stays governed inside a runbook |
| `../architecture/batch-dag-runbooks.md` | Conceptual source; this lane is the normative detail behind its "Runbooks" section |
| `../contracts/runbook-example.md` | Illustrative sketch; superseded in detail (not edited) by `contracts/runbook-manifest-contract.md` |
| `../phase3/` lane | Precedent for lane structure, fail-closed acceptance and two-layer gate |
| `../requirements/functional.md` | V2-FR-006, V2-FR-015 are the primary governing requirements; V2-FR-019/022/025 apply |
| `../requirements/security.md` | V2-SEC-005, V2-SEC-008, V2-SEC-010, V2-SEC-011, V2-SEC-013, V2-SEC-021, V2-SEC-023, V2-SEC-025 |
| `../requirements/traceability-matrix.md` | Existing row `V2-FR-006, V2-FR-015, V2-SEC-010 / ADR-0010 / Runbook Registry / 6 / test_runbook_version_digest_compile / RUNBOOK_ACCEPTED` is the anchor; this lane expands it **without editing that file** |
| `../roadmap.md` Phase 6 | Unchanged; this lane is the detail behind that section |
| `../open-decisions.md` | OD-002, OD-018, OD-019 remain open and are preconditions, not assumptions |

## Explicit non-goals of this lane

- No runbook DSL is selected here; OD-002 stays open. This lane constrains what
  any DSL must satisfy.
- No signing scheme is selected; OD-019 stays open. This lane defines where a
  signature would bind if adopted.
- No runtime module, no `src/` change, no test file, no gate script.
- No change to the V1 27-tool surface and no change to any Phase 0–5 artifact.
