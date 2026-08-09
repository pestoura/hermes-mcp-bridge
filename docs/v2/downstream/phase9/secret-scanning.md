# Secret Scanning

>
> **V2 · PHASE 9 DOWNSTREAM DESIGN ONLY · NOT IMPLEMENTED · DO_NOT_MERGE**
>
> Requires `HYBRID_ACCEPTED` and all Phase 7 integrations in the production cut
> accepted. Nothing here declares production readiness.

## Scope

1. Working tree of the release commit.
2. Commit history window covering the release range.
3. Generated artifacts: evidence documents, audit exports, SBOM, logs captured
   during acceptance, metric label snapshots.
4. Container image layers and build context, if an image is part of the cut.

## Gates

- Zero verified findings is required. A suspected finding must be triaged and
  either proven a false positive with recorded justification or remediated.
- Any real leaked credential triggers immediate rotation before the gate can be
  reconsidered; the rotation itself is evidence.
- Scanner name, version and ruleset digest are recorded; a scanner that cannot
  run (missing database or offline) yields `scanned=false`, which is **not** a
  pass.

## Structural prevention (must be proven by test, not only by scanning)

- Audit-safe canonical serialization rejects secret-shaped fields.
- Credential handles are request-scoped and non-serializable.
- Metric labels are closed enumerations.
- Error normalization strips provider payloads.
