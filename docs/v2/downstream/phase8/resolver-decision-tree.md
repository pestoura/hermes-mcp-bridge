# Deterministic Resolver Decision Tree

>
> **V2 · PHASE 8 DOWNSTREAM DESIGN ONLY · NOT IMPLEMENTED · DO_NOT_MERGE**
>
> Requires Phases 3–6 accepted and at least two accepted Phase 7 integrations.
> No resolver code, flag or gate change exists.

## Properties

- **Pure:** mode is a function of (typed request, registry/capability snapshot,
  policy result, declared budgets, runbook index). No wall-clock, no randomness,
  no LLM input.
- **Total:** every input yields exactly one terminal outcome: a mode or a refusal.
- **Recorded:** every evaluation emits one mode decision with one primary reason
  code and an ordered list of rejected-branch codes.
- **Replayable:** given the recorded inputs and snapshot digests, the decision
  reproduces byte-identically.

## Normative order

```text
S0  validate typed request                  -> refuse E-REQ-INVALID
S1  policy evaluate                         -> DENY: refuse E-POLICY-DENY
                                               APPROVAL_REQUIRED: hold, then continue
S2  is the intent expressible as exactly one registered typed tool
    with all arguments concretely bound and all targets in scope?
      yes -> S3
      no  -> S5
S3  is that tool's capability READY (write) / READY|DEGRADED (read)?
      yes -> MODE = DIRECT            reason R-DIRECT-EXACT
      no  -> S5 (record R-REJ-DIRECT-NOT-READY)
S4  (reserved: DIRECT with bounded provider retry, idempotent classes only)
S5  does a registered, version-pinned RUNBOOK match the intent
    with all inputs concretely bound?
      yes -> MODE = RUNBOOK           reason R-RUNBOOK-MATCH
      no  -> S6 (record R-REJ-NO-RUNBOOK)
S6  is the intent a set of N>1 homogeneous, independent, typed operations
    with no inter-operation data dependency?
      yes and N <= BATCH_MAX_NODES -> MODE = BATCH   reason R-BATCH-INDEPENDENT
      yes and N >  BATCH_MAX_NODES -> refuse E-BUDGET-NODES
      no  -> S7 (record R-REJ-NOT-INDEPENDENT)
S7  can the intent be expressed as a typed DAG with explicit dependencies,
    all nodes registered, all bindings typed, plan digest computable?
      yes and depth/width within budget -> MODE = DAG  reason R-DAG-TYPED-PLAN
      no  -> S8 (record R-REJ-NOT-PLANNABLE)
S8  agentic gate (all must hold, else refuse):
      a. request declares an agentic allowance (opt-in, not default)
      b. agentic budget available: tokens, wall-clock, escalation count
      c. no unresolved T3/write intent that lacks an approval binding
      d. policy permits agentic for this scope
      e. minimum-context shaping succeeds (no secret, no raw bodies)
    all true  -> MODE = AGENTIC       reason R-AGENTIC-<precise cause>
    any false -> refuse E-AGENTIC-<blocking condition>
```

## Escalation is one-way and bounded

Escalation moves only downward in determinism (DIRECT → BATCH/DAG/RUNBOOK →
AGENTIC) and at most `MAX_ESCALATIONS_PER_REQUEST` times. A HYBRID run may return
to deterministic execution *only* by emitting a new typed plan that itself
re-enters the tree at S0 with the same invariants and a decremented budget; an
agentic step may never execute a provider call directly.

## Partial determinism

A HYBRID request executes its deterministic segment first and escalates only the
residual, unresolved sub-intent. Results already obtained deterministically are
never recomputed agentically. The recorded decision includes the deterministic
coverage ratio (`deterministic_nodes / total_nodes`).
