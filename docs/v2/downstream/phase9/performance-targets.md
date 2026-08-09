# Performance, Latency and Reliability Targets

>
> **V2 · PHASE 9 DOWNSTREAM DESIGN ONLY · NOT IMPLEMENTED · DO_NOT_MERGE**
>
> Requires `HYBRID_ACCEPTED` and all Phase 7 integrations in the production cut
> accepted. Nothing here declares production readiness.

Targets are **proposed** and calibrated from Phase 0/2/4/5/7 evidence before the
gate. All are measured at the gateway boundary on the real host, with recorded
sample counts; a target without a measured distribution is not accepted.

| Metric | Proposed target | Measurement |
|---|---|---|
| DIRECT read p50 / p95 | ≤ 300 ms / ≤ 1200 ms excluding provider time; provider time reported separately | ≥ 200 samples per capability |
| DIRECT write p95 | ≤ 2000 ms excluding provider time | ≥ 100 samples |
| Resolver decision p99 | ≤ 5 ms | pure function, ≥ 10⁴ samples |
| BATCH throughput | ≥ `BATCH_MAX_NODES` nodes within declared budget without breaching per-provider limits | Phase 4 harness |
| DAG scheduling overhead | ≤ 10% of total plan wall-clock | Phase 5 harness |
| Availability (gateway) | ≥ 99.5% over the acceptance window, excluding provider outages, which are reported separately | Health probe series |
| Error budget policy | Breach freezes new integration promotion until remediated | Recorded decision |
| Token economics | DIRECT/BATCH/DAG/RUNBOOK paths: exactly 0 Hermes LLM tokens | Real accounting source |
| Degradation | Provider outage degrades only that provider's capabilities | Failure-injection evidence |

Percentiles are reported with sample count and window; a single fast run is not
evidence. Cold/warm cache state is recorded.
