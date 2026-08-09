# ADR-0040 - Release integrity: SBOM, pinned supply chain and rehearsed rollback

- Status: Accepted (Phase 9)
- Supersedes: the `ADR-0031` proposal in the downstream design lane.
- Related: ADR-0017 (versioning and backward compatibility), ADR-0019 (execution sandbox boundaries).

## Context

A production cut is only as trustworthy as the artifact it ships and the
operator's proven ability to undo it. Claims such as "dependencies are pinned"
or "we can roll back" are worthless as prose: they must be recomputed at gate
time from the repository and from executed drills.

## Decision

A production cut requires, all machine-checked by
`scripts/validate_v2_phase9_production_gate.py`:

- a generated SBOM and build provenance whose defaults are non-promotable;
- every runtime dependency carrying an upper bound, and the container base image
  pinned by `sha256:` digest rather than a mutable tag;
- a secret scan over the working tree, a bounded history window and the
  generated artifacts, reporting `scanned=true` with zero findings — a scan that
  could not complete its declared scope is a failure, never a silent pass;
- executed rollback, credential-rotation and audit-restore drills with recorded
  elapsed times inside their RTO, and an audit RPO of zero records.

## Consequences

- Release evidence is reproducible from the commit under test and is bound to it
  by `--require-sha`.
- An unpinned dependency, a floating base image or an unscannable tree blocks
  promotion rather than degrading it to a warning.
- Rollback is a rehearsed, timed procedure with published runbooks, not an
  improvisation during an incident.
