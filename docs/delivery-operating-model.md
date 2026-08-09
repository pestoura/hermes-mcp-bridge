# Hermes MCP Bridge — Delivery Operating Model

## Purpose

This document defines how Hermes MCP Bridge work is delivered under JDS-001. It complements product architecture, compatibility, security, deployment and version-specific acceptance documents.

Permanent progression rule:

```text
GREEN | PASS | SUPPORTED | ACCEPTED
                 ↓
        CONTINUE AUTOMATICALLY
```

A gate that did not execute is not GREEN.

## Delivery objective

Optimize for the **next usable, versionable Bridge baseline**, not raw feature volume.

For V2, prioritize end-to-end value in this order when applicable:

```text
DIRECT
  >
BATCH
  >
DAG / RUNBOOK
  >
AGENTIC
```

Deterministic work should move out of unnecessary LLM paths whenever contracts, policy and evidence allow it.

## WIP and execution strategy

The repository does not require a fixed number of agents or lanes.

Use parallel work only when outcomes are materially independent and reduce critical-path time. Current portfolio guidance is an upper bound, not a target:

```text
active development WIP <= 5–6 lanes
```

Use fewer lanes whenever dependencies, shared contracts or integration pressure make more concurrency counterproductive.

A lane may be executed by a human, agent, automation, CI job or other implementation mechanism.

## Integration Controller role

For concurrent delivery, one role owns shared-state reconciliation and integration throughput. It is not necessarily an agent.

Responsibilities:

- reconcile `main`, PRs, CI, releases, contracts and runtime evidence;
- identify the critical path and next usable baseline;
- keep WIP bounded;
- classify failures;
- repair/route deterministic failures;
- integrate GREEN work;
- revalidate compatibility and `main`;
- start new work only when useful capacity exists;
- preserve truthful support/version claims.

A failed lane does not freeze unrelated work unless it reveals a shared protocol, security, ABI, deployment or `main` defect.

## Walking skeleton and vertical slices

Prefer a thin complete execution path before broad infrastructure.

A Bridge V2 slice should prove as much of this chain as its scope requires:

```text
MCP request
   → tool/capability resolution
   → policy
   → execution mode
   → runtime adapter
   → shaped result
   → provenance/evidence
```

For multi-operation features:

```text
request
   → plan
   → per-node policy
   → bounded execution
   → per-node result/error
   → aggregate/shaping
   → provenance
```

Do not count isolated registries, planners or adapters as delivered until a useful end-to-end capability consumes them.

## Gate staging

Use cheap deterministic gates before expensive integration/runtime work:

```text
compile / format / lint
        ↓
type / schema / contract validation
        ↓
targeted tests
        ↓
security / secret / policy invariants
        ↓
full test suite
        ↓
package / image / SBOM
        ↓
isolated integration acceptance
        ↓
controlled runtime smoke / deployment acceptance
```

Heavy jobs should depend on successful fast gates where practical. Avoid equivalent duplicate `push` + `pull_request` pipelines when repository protection permits.

## Failure handling

### Deterministic failure

```text
FAIL → inspect → root cause → patch → targeted retest → continue
```

No blind retries.

### Integration/product failure

Examples include protocol mismatch, schema regression, result-shaping defect, per-node policy error, lifecycle bug or adapter incompatibility. Isolate the affected work while unrelated GREEN paths continue.

### Global blocker

Freeze promotion for issues such as:

- public ABI/contract break without versioning;
- fail-open policy;
- HMAC/replay/integrity regression;
- secret/credential exposure;
- broken `main`;
- production-path regression;
- destructive ambiguity;
- required human/security decision;
- insufficient evidence for support/promotion.

## Definition of Delivery

A Bridge capability is delivered only when the applicable chain is proven:

```text
IMPLEMENTED
+ CONTRACTED
+ TESTED
+ POLICY-VALIDATED
+ SECURITY-VALIDATED
+ INTEGRATED
+ EVIDENCED
+ RUNTIME-ACCEPTED WHEN CLAIMED
= DELIVERED
```

Code existence or synthetic tests do not imply production support.

## Version and compatibility rules

- preserve V1/public behavior unless a breaking change is explicitly versioned;
- prefer adapters from old surface to new internals over duplicated implementations;
- keep rollback to the previous known-good release available for production promotion;
- release evidence must identify commit, protocol/tool/schema versions, gates and rollback path;
- use merge queue or equivalent serialized validation where several concurrent GREEN PRs could invalidate one another.

## Product-specific delivery sequence

Select from live repository state, but favor independently useful baselines such as:

```text
V2 execution core
      ↓
first DIRECT real capability
      ↓
BATCH / parallel safe operations
      ↓
DAG / runbook execution
      ↓
result shaping / artifacts / provenance
      ↓
expanded deterministic adapters
      ↓
production acceptance
```

Do not reimplement capabilities already integrated simply because an older plan still lists them.

## Runtime acceptance

When Hermes/Jarvas runtime access is available, validate read-only/low-risk evidence first:

```text
health
readiness
tool inventory
protocol compatibility
RITMO lifecycle where applicable
single-path smoke
```

Never invent runtime support when the live gate was not observed.

## Resume rule

A resumed execution session first reconciles:

```text
main + HEAD + PRs + CI + releases + contracts + roadmap + runtime evidence
```

Conversation memory is advisory only.

## Permanent algorithm

```text
DISCOVER
   ↓
RECONCILE LIVE STATE
   ↓
IDENTIFY NEXT USABLE BASELINE
   ↓
SELECT MINIMUM USEFUL WORK SET
   ↓
BOUNDED PARALLEL IMPLEMENTATION
   ↓
FAST GATES
   ↓
FAIL? ── yes ──→ FIX / RETEST
   │
   no
   ↓
INTEGRATE
   ↓
FULL / SECURITY / RUNTIME GATES
   ↓
BASELINE GREEN
   ↓
VERSION + EVIDENCE
   ↓
NEXT BASELINE
```
