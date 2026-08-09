# Proposed ADRs for Phases 7–9 (text only, state: Proposed)

>
> **V2 · DOWNSTREAM DESIGN ONLY · NOT IMPLEMENTED · DO_NOT_MERGE UNTIL PREDECESSORS ACCEPTED**
>
> This subtree is design-only. No runtime file, gate, tool surface or policy path
> is changed by it. Predecessor gates `DIRECT_MUTATION_ACCEPTED` (Phase 3),
> `BATCH_ACCEPTED` (Phase 4), `DAG_ACCEPTED` (Phase 5) and `RUNBOOK_ACCEPTED`
> (Phase 6) are **not** accepted at the time of writing. The operational V1
> surface remains exactly **27 tools**, bridge `1.0.0`, schema `0.6.1`.

These are proposals. They are **not** placed in `docs/v2/adrs/` and are not
numbered authoritatively until the Controller promotes them; the numbers below
are reservations continuing from the accepted ADR-0023.

## ADR-0024 — Integration providers are plugins behind a closed boundary

Context: Phase 7 adds Google/email, Cloudflare, Docker, systemd, Home Assistant,
Jira, n8n, Grafana and possibly RITMO. Ad-hoc provider code in the executor would
make policy and credential handling provider-specific.

Decision: every integration is a **provider plugin** implementing one closed
interface (`describe`, `health`, `execute`) and is loaded from an explicit
in-repo allow-list. No dynamic/remote plugin loading, no entry-point scanning, no
network-sourced code. A provider never receives the registry, policy engine,
audit sink or another provider's credential handle.

Consequences: uniform fail-closed ordering; provider faults are containable;
per-provider acceptance gates become possible; third-party plugin ecosystems are
explicitly out of scope for V2.

## ADR-0025 — Capability discovery is declarative and classified direct-read / direct-write

Decision: a provider declares capabilities statically in its manifest; the
gateway classifies each as `DIRECT_READ`, `DIRECT_WRITE` or `UNSUPPORTED`.
Runtime probing may only *demote* a capability (READY -> DEGRADED/UNAVAILABLE),
never promote or introduce one. Discovery output is a deterministic, hashed
snapshot extending the accepted Phase 1 `capability_snapshot_hash`.

## ADR-0026 — Per-provider credential isolation domains

Decision: each provider gets its own credential capability namespace
(`<provider>.read`, `<provider>.write`), its own broker domain and its own
scope allow-list. Cross-provider credential resolution is denied at the broker,
not at the caller. Secret material never crosses the plugin boundary as a value
when a request-scoped authorization object can be used instead.

## ADR-0027 — Deterministic resolver with a fixed decision tree

Decision: execution-mode selection is a pure function of the typed request,
registry/capability state, policy result and declared budgets. Same inputs ->
same mode and same reason code. The LLM never selects the mode. `AGENTIC` is only
reachable via an explicit terminal branch with a recorded reason code.

## ADR-0028 — No silent safety downgrade

Decision: escalation may relax *determinism*, never *safety*. Approval
requirements, credential scope, policy DENY, digest binding and audit obligations
are invariant across modes. A path that would require weaker controls is refused
with `E-SAFETY-DOWNGRADE-REFUSED`, not downgraded.

## ADR-0029 — Bounded-cardinality observability labels

Decision: metric labels are drawn from closed enumerations (provider, capability
id, mode, outcome class, reason code). Free-text, repository names, user ids,
paths, branch names and request ids are never labels; they belong to audit
records and exemplars only.

## ADR-0030 — Evidence-complete audit for every terminal outcome

Decision: every request produces exactly one terminal audit record, including
refusals and internal errors. Missing audit is a failure of the request, not a
best-effort side effect: if the audit sink cannot accept the record, the
operation fails closed (write path) or is reported degraded and refused (read
path with mutation intent).

## ADR-0031 — Release integrity: SBOM, pinned supply chain and rollback drills

Decision: a production cut requires a generated SBOM, pinned and hash-verified
dependencies, a clean secret scan of the release tree and history window, and a
rehearsed, timed rollback to the previous accepted artifact.
