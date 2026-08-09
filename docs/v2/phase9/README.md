# Phase 9 — Production Hardening (design lane)

>
> **V2 · PHASE 9 · implemented and validated by `scripts/validate_v2_phase9_production_gate.py`**
>
> Requires `HYBRID_ACCEPTED` and all Phase 7 integrations in the production cut
> accepted. Nothing here declares production readiness.

| Document | Scope |
|---|---|
| `performance-targets.md` | Latency/throughput/reliability SLOs and measurement method |
| `failure-injection.md` | Deterministic fault catalogue and expected fail-closed behaviour |
| `chaos-and-recovery.md` | Chaos scenarios, recovery objectives, degraded modes |
| `audit-completeness.md` | 100% terminal-record proof and tamper evidence |
| `observability.md` | Metric/label contract with bounded cardinality |
| `secret-scanning.md` | Secret scanning scope, gates and response |
| `supply-chain-sbom.md` | SBOM, pinning, provenance, image and dependency scanning |
| `rollback-drills.md` | Timed rollback and credential-rotation drills |
| `production-acceptance.md` | Fail-closed `V2_PRODUCTION_READY` criteria |
