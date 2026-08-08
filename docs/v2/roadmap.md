# V2 Roadmap

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

The roadmap is gated. A phase is not promoted merely because code exists; its acceptance evidence and prerequisite controls must pass.

## PHASE 0 — AS-IS + Baseline

**Deliverables:** dated current architecture; representative v1 token/latency/API-call benchmark; runtime capability inventory; threat model; hardening findings; trust boundaries.

**Implementation status (2026-08-08):** AS-IS/runtime/security baseline is documented and the connected benchmark/evidence harness is implemented under `scripts/v2_phase0_benchmark.py` plus the fail-closed validator `scripts/validate_v2_phase0_evidence.py`. The phase remains **NOT ACCEPTED** until real Jarvas/Hermes evidence passes the validator; code-only readiness is not promotion evidence.

**Gate:** `BASELINE_ACCEPTED`.

## PHASE 1 — Tool Registry

**Deliverables:** canonical tool schema; capability registry/health model; policy taxonomy/security tiers; capability projection; credential abstraction/capability IDs; policy-as-code test model; capability snapshot hash.

**Gate:** `REGISTRY_ACCEPTED`.

## PHASE 2 — GitHub DIRECT Read-Only MVP

Candidate tools: `github.get_repo`, `github.get_pr`, `github.get_checks`, `github.get_issue`, `github.search`.

Use dedicated least-privilege credentials. Support result shaping. Compare read results against the v1 agentic path in shadow evaluation without duplicating mutations.

**Gate:** `DIRECT_READ_ACCEPTED`.

## PHASE 3 — GitHub Mutations

Candidate operations include create branch and create PR; merge is conditional/governed; delete repository remains DENY by default.

Add mutation-specific idempotency/replay protection, locks/optimistic concurrency where appropriate, plan/operation digests and approvals. Dedicated least-privilege write capability is required.

**Gate:** `DIRECT_MUTATION_ACCEPTED`.

## PHASE 4 — BATCH Engine

Add one-request/many-node execution, bounded parallel worker pool, per-provider/resource/credential limits, budgets, backpressure, adaptive concurrency, circuit breakers, partial-success semantics, deterministic aggregation and result shaping/artifacts.

**Gate:** `BATCH_ACCEPTED`.

## PHASE 5 — DAG Engine

Add explicit dependencies, typed bindings, topological scheduler, deterministic transformation nodes, canonical plan digest, checkpoint/resume, execution leases/heartbeat/recovery, dead-letter/manual-intervention state and replay simulation.

**Gate:** `DAG_ACCEPTED`.

## PHASE 6 — RUNBOOK Registry

Add versioned runbook registry, schema/compile-once canonical IR, tests, capability/credential references, integrity/digest/signing evaluation, promotion workflow and `RB-GITHUB-PR-LIFECYCLE-001` exemplar.

**Gate:** `RUNBOOK_ACCEPTED`.

## PHASE 7 — Additional Integrations

Proposed order: Google/email, Cloudflare, Docker, systemd, Home Assistant projection, Jira, n8n; evaluate other integrations only after evidence. Each integration gets its own capability/credential/policy/sandbox/readiness gate. RITMO is not assumed present until independently confirmed.

**Gate:** per-integration acceptance.

## PHASE 8 — HYBRID Execution

Add explicit escalation reason codes, minimum-context shaping, agentic token/time/escalation budgets, deterministic-first rules and v1/agentic fallback behavior.

**Gate:** `HYBRID_ACCEPTED`.

## PHASE 9 — Production Hardening

Chaos/failure injection, security acceptance, performance/reliability SLOs, credential rotation, artifact/runbook integrity, policy/approval replay tests, provider degradation, rollback validation and end-to-end operational evidence.

**Gate:** `V2_PRODUCTION_READY`.

## Migration controls across phases

- v1 remains operational and semantically unchanged until explicit migration decisions;
- no silent semantic changes;
- feature flags/canary;
- shadow mode only for reads and comparison, never real duplicate mutations;
- rollback to v1/agentic path remains available during migration;
- every gate must trace requirements -> design/ADR -> test/evidence.
