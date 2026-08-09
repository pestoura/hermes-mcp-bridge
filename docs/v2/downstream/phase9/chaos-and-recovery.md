# Chaos, Degradation and Recovery

>
> **V2 · PHASE 9 DOWNSTREAM DESIGN ONLY · NOT IMPLEMENTED · DO_NOT_MERGE**
>
> Requires `HYBRID_ACCEPTED` and all Phase 7 integrations in the production cut
> accepted. Nothing here declares production readiness.

## Scenarios

| # | Scenario | Objective |
|---|---|---|
| C-01 | Kill the gateway process during a write | Recover with zero duplicate side effects; unknown outcomes listed |
| C-02 | Kill during a DAG run | Resume from checkpoint or dead-letter; no node executed twice |
| C-03 | Single provider fully down for 15 min | Only that provider degrades; other capabilities keep serving |
| C-04 | Two providers degraded simultaneously | No cascading failure; resolver refuses affected intents cleanly |
| C-05 | Sustained rate limiting | Backpressure and adaptive concurrency hold; no unbounded queue growth |
| C-06 | Audit sink outage for 5 min | Writes refused, reads degraded and marked, backlog resolved without loss |
| C-07 | Credential rotation under load | No failed-open; in-flight either completes or fails closed |
| C-08 | Restart storm (3 restarts in 5 min) | State integrity preserved; no duplicate mutations |

## Objectives

- **RTO** (gateway restored): ≤ 5 minutes, manual restart acceptable.
- **RPO** (audit/evidence): 0 lost terminal records for write operations.
- **Duplicate mutation count** across all chaos runs: exactly 0.
- **Unknown outcomes**: allowed, but each must be surfaced, audited and
  manually resolvable; silent unknowns are a failure.

## Degraded-mode contract

Degraded means: reduced capability set, explicit markers on results, refusal of
write intents on affected providers. Degraded never means: relaxed policy,
skipped approval, unaudited execution or best-effort secret handling.
