# V2 Roadmap

> **V2 · IMPLEMENTATION IN PROGRESS · PHASE 0 ACCEPTED · NO IMPACT ON V1**

The roadmap is gated. A phase is not promoted merely because code exists; its acceptance evidence and prerequisite controls must pass.

## PHASE 0 — AS-IS + Baseline

**Deliverables:** dated current architecture; representative v1 token/latency/API-call benchmark; runtime capability inventory; threat model; hardening findings; trust boundaries.

**Status (2026-08-08): ACCEPTED.** AS-IS/runtime/security baseline is documented, the connected benchmark harness `scripts/v2_phase0_benchmark.py` was executed against the real v1 runtime, and the fail-closed validator `scripts/validate_v2_phase0_evidence.py` returned `BASELINE_ACCEPTED` with zero failures.

**Runtime validated:** Bridge `1.0.0`, schema `0.6.1`, upstream Hermes `ok`, base commit `f0b7e72f6bdf42e82712f3d2e8182ff937ae9509`.

**Result:** 3 categories (`read`, `mutation`, `agentic`) × 3 repetitions = 9/9 successful, 0 failures, 0 contaminated metric windows, `bridge_execution_terminal_total` delta of exactly 1 per repetition, 519,048 real LLM tokens accounted, mutation cleanup residual count 0, privacy controls PASS (no prompts, outputs or secrets stored).

**Evidence:**

| File | SHA-256 |
| --- | --- |
| `docs/v2/evidence/phase0-connected-baseline-20260808.json` | `ea6cc080891d133fa835a4e852f69dd124feaeccc38befe24293395020667559` |
| `docs/v2/evidence/phase0-connected-baseline-gate-20260808.json` | `8db24e8436fcac4b2a9eae2f41ad5b8e9c5194a0f174a3b0f3f67e612e0f9997` |

See `docs/v2/evidence/README.md` for the full index and traceability chain.

**Gate:** `BASELINE_ACCEPTED` — satisfied. Issue #43 is ready to close.

## PHASE 1 — Tool Registry

**Status: IMPLEMENTATION IN PROGRESS.** The canonical registry core landed as an
isolated additive package (`src/hermes_mcp_bridge/v2/`) with unit tests. It is
**not** wired into the V1 server or tool registration path and the V1 27-tool
surface is unchanged. `REGISTRY_ACCEPTED` is **not** declared: no acceptance
evidence has been produced, no gate validator has been run and no registry
implementation is promoted by this document.

**Deliverables:** canonical tool schema; capability registry/health model; policy taxonomy/security tiers; capability projection; credential abstraction/capability IDs; policy-as-code test model; capability snapshot hash.

**Implemented so far:** canonical `ToolDefinition` with enforced invariants;
`CapabilityRegistry`/`ToolRegistry` with duplicate checks and fail-closed
lookups; the seven-state `CapabilityState` readiness model; deterministic
canonical JSON plus SHA-256 `capability_snapshot_hash`; fail-closed
policy-as-code with `ALLOW`/`DENY`/`APPROVAL_REQUIRED` and stable reason codes;
deterministic static capability projection; credential broker **contract** with
an in-memory test broker only.

**Not yet done (required before the gate):** registry persistence and signing
(OD-003), a real credential backend (OD-005), the principal/tenant model
(OD-007), dynamic projection and discovery (OD-012/OD-013), and the Phase 1
acceptance evidence + validator.

**Gate:** `REGISTRY_ACCEPTED` — **not satisfied**.

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
