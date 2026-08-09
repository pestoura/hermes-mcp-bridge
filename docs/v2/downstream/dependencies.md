# Downstream Dependency and Merge-Order Contract

>
> **V2 · DOWNSTREAM DESIGN ONLY · NOT IMPLEMENTED · DO_NOT_MERGE UNTIL PREDECESSORS ACCEPTED**
>
> This subtree is design-only. No runtime file, gate, tool surface or policy path
> is changed by it. Predecessor gates `DIRECT_MUTATION_ACCEPTED` (Phase 3),
> `BATCH_ACCEPTED` (Phase 4), `DAG_ACCEPTED` (Phase 5) and `RUNBOOK_ACCEPTED`
> (Phase 6) are **not** accepted at the time of writing. The operational V1
> surface remains exactly **27 tools**, bridge `1.0.0`, schema `0.6.1`.

## Hard predecessor chain

```text
Phase 3 DIRECT_MUTATION_ACCEPTED
  -> Phase 4 BATCH_ACCEPTED
     -> Phase 5 DAG_ACCEPTED
        -> Phase 6 RUNBOOK_ACCEPTED
           -> Phase 7 per-integration acceptance
              -> Phase 8 HYBRID_ACCEPTED
                 -> Phase 9 V2_PRODUCTION_READY
```

## What may proceed in parallel

| Activity | Allowed before predecessors accepted? |
|---|---|
| Design docs in this lane | Yes |
| ADR text proposals (state `Proposed`) | Yes |
| Test-matrix definition (names, intent, negative cases) | Yes |
| Interface sketches expressed as documentation | Yes |
| Any `src/` or `tests/` change | No |
| Registering a Phase 7 provider/tool | No |
| Any resolver code path or flag | No |
| Roadmap/traceability edits | No — Controller-owned |
| Merging this PR | No — `DO_NOT_MERGE` |

## Minimum unblocking conditions per lane

**Phase 7** requires: `RUNBOOK_ACCEPTED`; a resolved OD-005 (real credential
backend) and OD-007 (principal/tenant authorization); a per-provider
least-privilege credential provisioned and health-probed; provider sandbox
boundaries from `../architecture/../security/trust-boundaries.md` extended and
re-accepted. RITMO is **not assumed to exist**; its lane stays `BLOCKED_UNCONFIRMED`
until independently confirmed on the host.

**Phase 8** requires: at least Phases 3–6 accepted plus **two** accepted Phase 7
integrations, so resolver behaviour is exercised across more than one provider
class; Phase 0 baseline plus Phase 2/4/5 economics evidence available for
comparison.

**Phase 9** requires: Phase 8 accepted; all Phase 7 integrations that will ship
in the production cut accepted; evidence retention path unchanged.

## Merge order

This PR is a draft and stays draft. When Phase 7 opens, the Controller may merge
this lane **as documentation only**, or cherry-pick `phase7/` first and keep
`phase8/` and `phase9/` unmerged. No partial merge may add a runtime file.
