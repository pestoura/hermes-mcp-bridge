# V2 Roadmap

> **V2 · IMPLEMENTATION IN PROGRESS · PHASES 0–2 ACCEPTED · NO IMPACT ON V1**

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

**Gate:** `BASELINE_ACCEPTED` — satisfied. Issue #43 closed.

## PHASE 1 — Tool Registry

**Status (2026-08-08): ACCEPTED.** The canonical registry core is implemented as
an isolated additive package (`src/hermes_mcp_bridge/v2/`) and remains unwired
to the V1 server/tool registration path. The V1 27-tool contract remains
unchanged. The integrated `main` commit
`4bc999084b88cc5ef5346f21c9f2e09717c63568` passed the deterministic acceptance
collector and the fail-closed validator with `failures=[]` and
`REGISTRY_ACCEPTED`.

**Deliverables:** canonical tool schema; capability registry/health model; policy taxonomy/security tiers; capability projection; credential abstraction/capability IDs; policy-as-code test model; capability snapshot hash.

**Implemented:** canonical `ToolDefinition` with enforced invariants;
`CapabilityRegistry`/`ToolRegistry` with duplicate checks and fail-closed
lookups; the seven-state `CapabilityState` readiness model; deterministic
canonical JSON plus SHA-256 `capability_snapshot_hash`; fail-closed
policy-as-code with `ALLOW`/`DENY`/`APPROVAL_REQUIRED` and stable reason codes;
deterministic static capability projection; credential broker **contract** with
an in-memory test broker only; audit-safe canonical serialization that excludes
free-text editorial metadata and materialized credential values.

**Acceptance scope:** the gate covers the Phase 1 requirements mapped in
`docs/v2/requirements/traceability-matrix.md`: registry schema conformance,
deterministic/versioned capability snapshots, capability health semantics,
authorized-only projection, fail-closed policy evaluation, credential
capability/redaction contract, malicious metadata/secret-field negative tests,
and V1 isolation/regression evidence.

**Acceptance evidence:** GitHub Actions CI #189 (`31254605844`) completed
successfully for the integrated commit. The blocking draft release
`phase1-registry-evidence-4bc999084b88cc5ef5346f21c9f2e09717c63568` is targeted
to that exact SHA and retains:

- `phase1-registry-acceptance.json` — SHA-256
  `ab66fc6dd872d2f184dafea6566dfcba178d7328f87eda9f9e319da8f030c20a`;
- `phase1-registry-gate.json` — SHA-256
  `4acaa6699b5374176f8b63b5be40d05b0fabbcfbea8624c5a3beb8f48ab78d1a`;
- repository retention manifest:
  `docs/v2/evidence/phase1-registry-acceptance-release-20260808.json`.

**Explicitly deferred:** registry persistence/storage/signing (open questions of
ADR-0004); a real credential backend (OD-005); principal/tenant authorization
(OD-007); dynamic capability discovery/projection (OD-012/OD-013); internal MCP
proxying details (OD-014); and the later durable policy engine/format beyond the
Phase 1 rule model (OD-017). `REGISTRY_ACCEPTED` does not claim these as
implemented.

**Gate:** `REGISTRY_ACCEPTED` — **satisfied**.

## PHASE 2 — GitHub DIRECT Read-Only MVP

**Status (2026-08-09): ACCEPTED.** The repository-side DIRECT read core remains
deliberately unwired from the V1 MCP server and the V1 contract remains exactly
27 tools. Both required gates passed on the accepted source commit
`818c56a467ed00b1412a219c78e3c68007848df3`: the inner semantic/economics gate
returned `DIRECT_READ_ACCEPTED` with `failures=[]`, and the OUTER out-of-band
integrity/provenance gate returned `overall_status=ACCEPTED` with `reasons=[]`.

**Implemented core:**

- typed `DIRECT`, read-only T1 definitions for `github.get_repo`,
  `github.get_pr`, `github.get_checks`, `github.get_issue` and the deliberately
  repository-scoped `github.search`;
- exact repository allow-list with no wildcard authority;
- fail-closed order: tool classification → exact repository scope →
  policy/capability/`github.read` readiness → authorization material → provider;
- separate `GitHubAuthorizationProvider` execution boundary so the Phase 1
  status-only broker never returns secret material;
- fixed `https://api.github.com` GET path, redirects disabled and environment
  proxy inheritance disabled;
- endpoint-specific normalization/result allow-lists and bounded canonical
  result byte budgets;
- stable redacted errors for auth, forbidden/rate-limit, not-found/gone,
  invalid request, redirects, upstream failures and invalid JSON/shape;
- repository-constrained issue/PR search whose qualifiers are built by code,
  rejecting caller qualifier/boolean injection;
- hermetic tests proving scope/policy/readiness denial can produce zero
  authorization resolution and zero HTTP requests; scope denial additionally
  proves zero credential-readiness broker calls;
- no Hermes client/prompt/agent invocation in the DIRECT core.

**Core CI status:** the accepted source commit
`818c56a467ed00b1412a219c78e3c68007848df3` passed GitHub Actions run
`31299055311` (`CI`, conclusion `success`). Subsequent `main` commits are
documentation and CI-matrix work only and do not alter Phase 2 runtime
behaviour, so the accepted source commit is retained unchanged.

**Acceptance evidence (accepted source commit, connected Jarvas host):**

| Field | Recorded value |
| --- | --- |
| Bridge / schema version | `1.0.0` / `0.6.1` |
| Credential provider | `github_app`, least-privilege, `broad_pat=false` |
| Samples / successes / semantic matches | 15 / 15 / 15 |
| Provenance pass / fail | 15 / 0 |
| `direct_total_tokens` | 0 |
| `agentic_total_tokens` | 13784 |
| `token_reduction_percent` | 100.0 |
| DIRECT provider API calls | 15 |
| Upstream Hermes direct LLM calls | 0 |
| Mutations observed / contaminated windows | 0 / 0 |
| Real-state row deltas (`sessions`/`messages`/`session_model_usage`) | 0 / 0 / 0 |
| Shadow witness positive / writers restored | `true` / `true` |

Retained, sanitized documents and their SHA-256 digests are indexed in
`docs/v2/evidence/README.md`:
`phase2-final-evidence-20260809.json` and `phase2-final-manifest-20260809.json`.

**Requirements satisfied for `DIRECT_READ_ACCEPTED`:**

1. fresh discovery on the actual Jarvas host of GitHub tooling, credential
   sources, scopes and repository access, without printing secret values;
2. dedicated least-privilege `github.read` capability provisioned as a GitHub
   App installation credential;
3. provider health/authentication probe against the authorized repository set;
4. explicit feature/canary wiring without changing V1 semantics;
5. shadow comparison of the five DIRECT reads against the V1 agentic path;
6. latency, provider API calls and token economics measured, proving **zero
   Hermes LLM token usage** on DIRECT execution;
7. connected, fail-closed acceptance evidence retained;
8. OUTER out-of-band state-integrity/provenance run executed, with its evidence
   and manifest retained.

Availability/authorization must not be inferred from the ChatGPT GitHub
connector. See `docs/v2/architecture/github-direct-read.md`.

**Gate:** `DIRECT_READ_ACCEPTED` — **declared**. OUTER `overall_status` —
**ACCEPTED**. Issue #51 is closed by this promotion.

## PHASE 3 — GitHub Mutations

Candidate operations include create branch and create PR; merge is conditional/governed; delete repository remains DENY by default.

Add mutation-specific idempotency/replay protection, locks/optimistic concurrency where appropriate, plan/operation digests and approvals. Dedicated least-privilege write capability is required.

**Preparation lane (design only, no implementation):** `docs/v2/phase3/` holds
the mutation threat model, credential split, `create_branch`/`create_pr`
semantics, governed merge and destructive-operation exclusion, idempotency and
optimistic concurrency, operation digest/approval model, audit and evidence
contract, rollback/compensation rules, fail-closed acceptance criteria and the
planned mutation test matrix. Supporting decisions: ADR-0020..ADR-0023
(Proposed). No Phase 3 tool, code path or gate change exists, and none may be
introduced before `DIRECT_READ_ACCEPTED`.

**Gate:** `DIRECT_MUTATION_ACCEPTED` — **declared** at
`8fc8363a3eb31db99c18afb39fcd78bde011e2b6` with `failures: []`. Evidence:
`docs/v2/evidence/phase3-direct-mutation-acceptance.json`; runner and criteria
mapping: `docs/v2/phase3/promotion.md`.

## PHASE 4 — BATCH Engine

Add one-request/many-node execution, bounded parallel worker pool, per-provider/resource/credential limits, budgets, backpressure, adaptive concurrency, circuit breakers, partial-success semantics, deterministic aggregation and result shaping/artifacts.

**Gate:** `BATCH_ACCEPTED` — **declared** with `failures: []` and a live
`max_observed_inflight` of 2 proving real parallel execution. Evidence:
`docs/v2/evidence/phase4-batch-acceptance.json`; runner and semantics:
`docs/v2/phase4/promotion.md`. The runtime ships behind
`BATCH_FEATURE_ENABLED = False` and is not wired to MCP.

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
