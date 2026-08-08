# V2 Protocol Baseline

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

This file is a compact protocol-level index for the architecture baseline. It does not replace v1 wire schema `0.6.1` and does not declare a final v2 schema version.

## Planned request concepts

Every v2 request should have explicit schema/protocol versioning, principal/trust context, request/correlation identifier, execution mode and enforceable budgets. BATCH/DAG/RUNBOOK nodes have stable node IDs, canonical tool names/versions, typed arguments, resource scopes and result-shaping requests.

Mutating nodes add or derive idempotency semantics, lock/resource identity and approval/plan-digest requirements. Long workflows may carry checkpoint/lease/resume state.

## Planned response concepts

Execution responses expose request/execution identifiers, aggregate status, per-node states/results/errors, explicit partial failure, policy/approval evidence, artifact references/provenance and signed/sanitized result manifest metadata.

## Canonical states

`SUCCESS`, `FAILED`, `PARTIAL_SUCCESS`, `CANCELLED`, `TIMED_OUT`, `COMPENSATED`, `MANUAL_INTERVENTION_REQUIRED`.

Capability health: `AVAILABLE`, `DEGRADED`, `UNAVAILABLE`, `UNAUTHORIZED`.

Policy simulation/decision: `ALLOW`, `DENY`, `APPROVAL_REQUIRED` (mapping to existing v1 policy terminology must be decided consistently before implementation).

Retry class: `RETRY_SAFE`, `RETRY_CONDITIONAL`, `NO_RETRY`.

## Protocol invariants

- typed bindings only; no arbitrary eval/shell interpolation;
- deterministic canonical serialization for digests;
- per-node governance even inside one request;
- credentials represented by capability IDs and never serialized;
- large results may become artifact refs;
- execution can record a capability manifest hash;
- unknown/missing policy/action fails closed;
- no mutation is duplicated for shadow comparison;
- AGENTIC/HYBRID escalation is explicit, budgeted and reason-coded.

## Historical note

A historical local `docs/protocol.md` / commit `8f6b2c1` was reported as existing outside GitHub. GitHub `main` on this baseline date did not contain that file/commit. A read-only recovery attempt through the Hermes runtime was rate-limited before content could be retrieved. This baseline therefore records the complete v2 protocol requirements supplied for this documentation task without claiming byte-for-byte recovery of that local file. The local work is not deleted or overwritten by this GitHub branch.
