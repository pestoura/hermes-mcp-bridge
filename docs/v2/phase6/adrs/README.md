# Phase 6 Architectural Decision Records

> **V2 · PHASE 6 · DESIGN ONLY · NOT IMPLEMENTED · NO V1 IMPACT**

All Phase 6 ADRs are **Proposed**. None may be treated as accepted, and none may
be implemented before `DAG_ACCEPTED`.

They are held in this lane rather than in `../../adrs/` to keep the Phase 6
design isolated until the DAG gate is declared; on acceptance they are intended
to be promoted into the main ADR set with their numbers unchanged.

| ADR | Decision topic | Refines |
|---|---|---|
| ADR-0028 | Compile-once canonical runbook IR and fail-closed admission | ADR-0004, ADR-0010 |
| ADR-0029 | Runbook digest, plan digest and approval binding | ADR-0012, ADR-0021 |
| ADR-0030 | Runbook least privilege by computed capability set | ADR-0006, ADR-0007, Phase 3 credential split |
| ADR-0031 | Computed `destructive_action` marker and declared rollback support | ADR-0014, ADR-0023 |

Numbers 0024–0027 continue the existing sequence, which ends at ADR-0023
(Phase 3). No existing ADR file is modified by this lane.
