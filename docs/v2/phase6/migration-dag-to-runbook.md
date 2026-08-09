# Migration Path — From DAG Plans to Reusable Runbooks

> **V2 · PHASE 6 · DESIGN ONLY · NOT IMPLEMENTED · NO V1 IMPACT**

## Problem

After `DAG_ACCEPTED`, callers can submit ad-hoc DAG plans. Ad-hoc plans are
flexible but are authored per request, reviewed per request (if at all), and
carry no stable identity, no owner, no version and no test attestation.
A runbook is the governed, reusable form of a plan that has proven itself.

Migration must be **explicit and reviewed**. There is no automatic promotion of
observed DAG plans into runbooks, and no bulk conversion (ADR-0010).

## Stages

### Stage 0 — Ad-hoc DAG (baseline)

Caller submits a plan; the engine validates, computes `plan_digest`, applies
per-node policy and approvals, executes, records evidence. Nothing is
registered.

**Controls in force:** per-node policy, approval bound to `plan_digest`,
idempotency, audit, evidence.
**Missing:** identity, version, owner, tests, reuse, admission-time capability
exactness, destructive-marker computation, review cadence.

### Stage 1 — Candidate identification

A plan shape is a migration candidate when it satisfies **all** of:

1. it has executed successfully at least N times (recommended N ≥ 5) with the
   same node set and edge set;
2. its variation across executions is confined to declared-parameterizable
   values (repository, branch name, title) — not to graph shape;
3. it has a nameable business purpose and a willing owner;
4. its mutating nodes are already Phase 3-governed operations.

Evidence for (1) and (2) comes from retained execution evidence, not from
memory or anecdote.

### Stage 2 — Parameter extraction

The varying values are lifted into a closed typed parameter schema
(`parameter-schema.md`). Anything that varied but cannot be typed and bounded
is a blocker: either it becomes a bounded `enum`/`resource_ref`, or the
candidate is rejected. Free-form strings feeding external mutations are not
acceptable parameters.

The graph itself becomes fixed. If the graph must vary, it is not one runbook —
it is either several runbooks or a genuine ad-hoc plan.

### Stage 3 — Manifest authoring

The author writes the manifest: identity, version `1.0.0`, parameter/output
schema, node list with **pinned** tool versions, policy class, approval class,
`destructive_action`, `rollback_support`, timeouts, budgets, owner, review
cadence.

Rule: **the runbook's controls must be at least as strict as the strictest
observed ad-hoc execution.** Migration may tighten, never loosen. A promotion
that would weaken any control is rejected (`test_promotion_cannot_weaken_controls`).

### Stage 4 — Threat and security review

The mutation threat model from Phase 3 is re-applied to the composed workflow,
because composition creates threats that single nodes do not have: intermediate
state visible to later nodes, partial-commit windows, and compensation ordering.
Reviewers are the owner plus security for anything `MUTATING_HIGH` or
destructive.

### Stage 5 — Tests

The planned tests for this runbook are written and pass against the exact
digest: schema negatives, policy/approval negatives, idempotency, compensation,
and a connected run against a disposable resource. Test attestation is an
admission stage (`admission-validation.md`, stage 14) — a runbook cannot be
admitted on the strength of prose review alone.

### Stage 6 — Admission

The manifest goes through the full admission pipeline. Capability set,
destructive marker and policy aggregate are **computed and compared**; the
author's declarations are checked, not trusted. On success the runbook is
`ADMITTED` with a `runbook_digest`.

### Stage 7 — Staged promotion

`ADMITTED → ACTIVE` requires explicit authorization. For
`destructive_action = true`, a non-production staged promotion and a successful
staged execution are prerequisites.

### Stage 8 — Shadow / equivalence check

Where the operation is read-only, the runbook may run in shadow against the
ad-hoc path and be compared (V2-NFR-020). Mutations are **never** duplicated in
shadow. For mutating runbooks, equivalence is proven structurally instead: the
runbook plan and the reference DAG plan must produce the **same** `plan_digest`
for equivalent inputs under the shared canonical serialization (A6-25). This is
why OD-018 must give DAG and runbook the same serializer.

### Stage 9 — Deprecate the ad-hoc path

Once the runbook is `ACTIVE`, the equivalent ad-hoc plan shape may be
discouraged and, where policy allows, denied for that workflow — so that the
governed form is the only form. This is a policy decision per workflow, not a
global switch, and it must not remove the ad-hoc capability itself (V2-NFR-009:
the V1/agentic rollback path stays available).

## Migration invariants

| Invariant | Meaning |
|---|---|
| No automatic promotion | A plan never becomes a runbook without a human-authored manifest and review |
| No control weakening | Runbook controls ≥ strictest observed ad-hoc controls |
| No graph inference | The graph is authored, not learned from traffic |
| No unbounded parameters | Everything caller-varying is typed, bounded and scoped |
| No shadow mutations | Shadow comparison is reads only |
| No gate implication | A successful migration is not `RUNBOOK_ACCEPTED` |

## Reverse path

If a runbook proves wrong or unsafe it is **yanked**, not edited. Callers fall
back to the governed ad-hoc DAG path (or to V1), which remains available. A
corrected runbook is a new version, admitted from scratch through the same
pipeline.

## Skill → Runbook

The `Skill → Promote → Runbook` path from ADR-0010 lands in this same pipeline:
a skill is prose for an agent, so promotion means an author transcribes the
stable procedure into a manifest at Stage 3 and then follows Stages 4–8
unchanged. A skill is never compiled into a runbook mechanically, and a skill
carries no authority into the registry.
