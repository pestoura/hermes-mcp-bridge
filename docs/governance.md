# Governance and Multi-Agent Control — 0.5.0

Bridge contract version: **0.5.0**.

## Scope and boundaries

The bridge validates, persists, and communicates governance policy. It does not pretend to execute multi-agent capabilities that Hermes upstream has not confirmed.

## Orchestration contract

Public modes: `auto`, `single`, `parallel`, `pipeline`, `review`.

Upstream effective modes: `auto`, `explicit` only.

Inputs using `auto|explicit` remain valid. `explicit` is treated as an explicit policy without breaking existing callers.

Agent card fields:
- `orchestration_contract_modes`: all requested modes.
- `upstream_effective_modes`: modes confirmed upstream.

Capability manifest `upstream_support`:
- `requested`: bridge-supported contract modes.
- `effective`: modes Hermes has confirmed.
- `unsupported`: requested modes not confirmed upstream.

## Policy engine

Decisions: `ALLOW`, `DENY`, `REQUIRE_APPROVAL`.

Rules are declarative and deterministic. No eval/exec/code execution. No dangerous regex or template execution.

Default posture: allow low-risk reads; mutations default to `REQUIRE_APPROVAL`.

High-risk trust labels with mutation default to `REQUIRE_APPROVAL`.

## Tool trust metadata

`ExtendedToolManifest` includes:
- `trust_level`
- `mutation_class`
- `reversible`
- `idempotency_class`
- `approval_requirement`
- `attestation_status`

Tool manifests are honest about supported capabilities.

## Approvals

Approvals are persistent, single-use by default, and transactional.

Statuses: `requested`, `approved`, `rejected`, `expired`, `consumed`, `stale`.

Identity assurance is `caller_asserted` until upstream provides verifiable identity.

## Provenance

Claims use `OBSERVED`, `DERIVED`, `INFERRED`, `UNVERIFIED`.

Result manifests include sanitized metadata only: never prompt text, raw outputs, or tool arguments.

HMAC-SHA256 signing is mandatory in `production` / `security_required`
(`BRIDGE_SECURITY_MODE`). Without a usable key in those modes the bridge is
fail-closed: `hermes_readiness` reports `security_posture.status=not_ready`.
Only in an explicitly declared `development` / `dev` / `test` mode may manifests
stay `signature_status=unsigned`, and that state is always reported, never
silent.

The caller-supplied `__canonical__` digest override was removed in 0.9.0: the
canonical digest is always computed from the payload.

## Policy model (0.9.0)

The policy is loaded explicitly by `hermes_mcp_bridge.policy.load_policy()`:

| Precedence | Source | Env |
| --- | --- | --- |
| 1 | inline JSON | `BRIDGE_POLICY_JSON` |
| 2 | file | `BRIDGE_POLICY_PATH` |
| 3 | built-in safe policy | — |

Fail-closed rules:

- invalid JSON, invalid schema, or a configured-but-missing file => policy is
  **not** loaded, `security_posture` is `not_ready`, and every enforcement call
  denies;
- a policy declaring neither read-only nor mutating actions is only accepted
  with `unknown_action_decision=DENY` (no permissive empty policy);
- an action declared both read-only and mutating is rejected;
- an action unknown to the active policy is DENY by default;
- any evaluation error or unrecognised decision inside enforcement is DENY.

`policy.classify_action()` is the single source of truth for read-only vs
mutating classification; `server._mutation_from_action()` delegates to it, so
there is no second hard-coded list.

The built-in safe policy explicitly allows health/status/list/manifest/
readiness/capabilities/agent-card and the other genuinely read-only tools, and
classifies every state-changing or executing tool as mutating (approval
required). `hermes_prompt` / `hermes_submit` remain read-only in the normal
path, so 0.8.x callers are unaffected; they escalate to REQUIRE_APPROVAL only
when the envelope is untrusted or the caller declares a mutation.

A versioned, secret-free production policy ships at
`config/policies/production.json`.

## Approval consumption (0.9.0)

There is no `hermes_approval_consume` tool — the surface stays at 27 tools.
Consumption is internal and atomic: `ApprovalRegistry.consume()` verifies
`decision`, `consumed_at`, `expires_at`, action binding and resource
fingerprint inside a single `BEGIN IMMEDIATE` transaction, and the conditional
`UPDATE ... WHERE consumed_at IS NULL AND decision='approved'` guarantees a
single winner under concurrency. An expired approval is transitioned to
`expired` and rejected with `ApprovalExpiredError`.

`hermes_execute_approved_plan` consumes the approval in the authorized
execution path, with `require_fingerprint=True`. Registry `ApprovalRecord` and
plan-layer `PlanApproval` are different models; `plans.plan_approval_from_record()`
is the explicit adapter and raises a structured `ApprovalAdapterError`
(`error=approval_binding_invalid`) when the record is not bound to a
`plan_id`/`plan_hash`. No hidden fields are invented.

## Migration from 0.4

`run_mappings` table is preserved. `approvals` table is added with idempotent creation.

Bridge version bump: `0.5.0`. Backward compatibility for the original 9 tools is preserved; 5 governance tools were added.
