# Phase 9 ADRs

| ADR | Title | Status |
|---|---|---|
| ADR-0039 | Observability labels are drawn from closed, bounded enumerations | Accepted |
| ADR-0040 | A production cut requires SBOM, pinned supply chain, clean secret scan and rehearsed rollback | Accepted |

The downstream design lane reserved `ADR-0029` and `ADR-0031` for these two
decisions; those numbers were taken by accepted Phase 5/6 work, so Phase 9 uses
0039-0040. `ADR-0030` (evidence-complete audit for every terminal outcome) is
implemented by the Phase 9 audit chain and is covered by the accepted
`ADR-0035` integration audit chain rather than a new record.
