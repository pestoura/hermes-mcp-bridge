# Compensation and Saga Semantics (Design)

> **V2 · PHASE 5 · DESIGN ONLY · NOT IMPLEMENTED · NO V1 IMPACT**

Phase 5 executes distributed effects without distributed transactions. The
model is a saga with **conservative, registry-declared** compensations. There is
no rollback of a provider-side commit; there is only an explicitly declared
inverse operation, or manual intervention.

## Principles

1. **Compensation is declarative, not inferred.** A node can only compensate
   with an entry from the registry compensation table (Phase 3
   `../phase3/rollback-and-compensation.md`). The engine never synthesizes an
   inverse.
2. **Compensation is itself a governed mutation.** It is policy-evaluated,
   credential-checked, idempotency-keyed, write-ahead audited and scope-checked
   exactly like a forward node. It is not a privileged path.
3. **Unsafe compensation dead-letters.** If the inverse cannot be proven safe
   (state drifted, effect no longer matches the recorded shape, third party
   modified it), the engine performs **no write** and routes to manual
   intervention. Phase 3 A3-12 preserved.
4. **`INDETERMINATE` is never compensated automatically.** Compensating an
   effect that may not exist can itself be destructive.
5. **No compensation for read nodes.** Reads have no effects to undo.

## Ordering

Compensation runs in **reverse topological order** over the set of nodes with
`status == SUCCESS` and a non-null `effect_ref`, restricted to the affected
subgraph:

```text
compensation_set = { n : n.status == SUCCESS
                       and n.effect_ref != null
                       and n.compensation != null
                       and n is an ancestor-or-self of the failure frontier
                         (rollback_policy scope) }
order = reverse(topological_order(compensation_set))
```

Nodes on branches unrelated to the failure are compensated only if
`rollback_policy` is plan-wide (`compensate_on_failure` with
`failure_policy: fail_fast`). Under `continue_independent`, independent
successful branches are **retained**, and the plan reports `PARTIAL`.

Compensation is sequential by default. Parallel compensation is allowed only
across disjoint resource lock keys and never for nodes with a declared
dependency relationship.

## Per-node compensation states

| State | Meaning |
|---|---|
| `COMPENSATION_PENDING` | Selected, not yet attempted |
| `COMPENSATED` | Inverse committed and verified by a read-back |
| `COMPENSATION_SKIPPED` | Effect already absent; verified, no write performed |
| `COMPENSATION_UNSAFE` | Precondition drift detected; no write performed; dead-letter |
| `COMPENSATION_FAILED` | Inverse attempted and failed; dead-letter |
| `COMPENSATION_INDETERMINATE` | Inverse outcome unknown; dead-letter, no retry |

Verification by read-back is mandatory: a compensation is only `COMPENSATED`
after a read confirms the expected absence/restoration. "Provider returned 200"
is not sufficient evidence.

## Preconditions before any inverse write

For each compensation, all must hold or the node becomes `COMPENSATION_UNSAFE`:

1. `effect_ref` is present and matches the recorded shape on a fresh read.
2. The effect has not been modified since creation by another actor (compare
   recorded provider revision/sha/timestamp).
3. The effect is still within the plan's authorized resource scope.
4. The compensating operation is registry-declared for this exact forward
   operation and is non-destructive in the Phase 3 taxonomy.
5. Repository/branch protection state is verifiable (unverifiable = unsafe,
   Phase 3 A3-10 precedent).

Example, `github.create_branch` → delete the created ref **only if** the ref
still points at exactly the sha the forward node created, is not the default
branch, is not protected, and has no open PR referencing it. Any deviation:
`COMPENSATION_UNSAFE`.

Example, `github.create_pr` → close the created PR (never delete the branch as
a side effect, never force-push). If the PR was merged, compensation is
impossible: `COMPENSATION_UNSAFE`, dead-letter, explicit operator report.

## Idempotency of compensation

```text
comp_key = H( plan_digest || node_id || "compensate" || effect_ref )
```

Written write-ahead. A crash mid-compensation resumes by reconciling against
`comp_key` and the effect's current state, using the same read-only
reconciliation rules as forward nodes.

## Reporting

The plan result always enumerates, explicitly and per node: committed effects
retained, effects compensated, effects that could not be compensated, and
effects whose status is unknown. A plan never reports a clean failure while
leaving unreported side effects. This is a gate criterion (A5-11).

## Requirements traced

V2-FR-010, V2-FR-011, V2-SEC-004, ADR-0014, Phase 3
`../phase3/rollback-and-compensation.md`.
