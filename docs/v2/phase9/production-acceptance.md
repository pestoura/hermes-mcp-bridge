# `V2_PRODUCTION_READY` Criteria (fail-closed)

>
> **V2 · PHASE 9 · implemented and validated by `scripts/validate_v2_phase9_production_gate.py`**
>
> Requires `HYBRID_ACCEPTED` and all Phase 7 integrations in the production cut
> accepted. Nothing here declares production readiness.

All must hold, each with retained sanitized evidence and SHA-256 digests, on one
identified commit and artifact digest.

1. `HYBRID_ACCEPTED` declared; every Phase 7 integration in the production cut
   individually accepted; all prior gates recorded by SHA.
2. Performance targets measured with recorded distributions and sample counts;
   no target claimed without data.
3. Failure-injection catalogue `F-01..F-20` executed; every case fail-closed with
   a stable reason code; duplicate mutations 0.
4. Chaos scenarios `C-01..C-08` executed; RTO ≤ 5 min, audit RPO 0, duplicate
   mutations 0, unknown outcomes all surfaced and resolvable.
5. Audit completeness 100% by independent reconciliation; digest chain verified;
   redaction scan zero findings.
6. Observability: metric/label contract enforced, adversarial cardinality test
   passes, ceiling respected, alerts defined and firing correctly in test.
7. Secret scanning: tree, history window and artifacts scanned with a recorded
   scanner/database version; zero verified findings; `scanned=false` is a fail.
8. Supply chain: SBOM generated and retained, dependencies pinned with hashes,
   vulnerability scan recorded with no unresolved High/Critical outside recorded
   time-boxed exceptions, provenance and immutable digests recorded.
9. Rollback drill executed within 15 minutes with verification evidence; at least
   rollback options 1 and 2 drilled; credential rotation drill executed for every
   domain; restore drill verified from backup.
10. Policy/approval replay tests pass: historical approvals cannot be reused
    against a changed digest; replayed idempotency keys produce zero second side
    effects.
11. V1 preserved: exactly 27 tools, bridge/schema versions recorded, no V1 module
    imports V2 runtime paths.
12. Operational documentation complete: runbooks for each failure class, on-call
    alert meanings, rollback procedure, credential rotation procedure.

Explicitly **not** acceptance: green CI alone, a successful demo, tests written
but not executed, or targets stated without measured distributions.
