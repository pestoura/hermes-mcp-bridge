# Hermes MCP Bridge — Release 2.0.1 (V2 production activation)

## What 2.0.1 is

`2.0.1` is a **patch release with exactly one purpose**: making the V2
capabilities that `2.0.0` already accepted *functionally active in production*.

`2.0.0` closed Phases 0–9 and shipped every V2 execution lane — DIRECT, BATCH,
DAG, RUNBOOK, INTEGRATIONS, HYBRID — as accepted, tested code. But each lane sat
behind a hardcoded module constant (`BATCH_FEATURE_ENABLED`, `DAG_FEATURE_ENABLED`,
`RUNBOOK_FEATURE_ENABLED`, `PROVIDER_FEATURE_ENABLED`, `HYBRID_FEATURE_ENABLED`),
all `False`, with **no supported way for an operator to turn them on**. Live
reconciliation of production revision `2b1aee114abce5e96d3785c2f89873f06e950b0a`
confirmed the consequence: V2 was *present* but not *activated*.

This release supplies the missing activation mechanism and the acceptance that
proves activation. It is **not** a V2.1 feature scope: no new lane, no new tool,
no widened contract.

## What 2.0.1 is not

- Not a new capability. Every lane activated here was accepted in `2.0.0`.
- Not an agentic expansion. The Hybrid contract is unchanged: deterministic
  preference stays `DIRECT > BATCH > DAG > RUNBOOK > AGENTIC`, and the agentic
  token budget still defaults to **zero**.
- Not a contract change. See the compatibility table below.
- Not a rewrite of `v2.0.0`. That tag is immutable and untouched.

## Compatibility — unchanged, and gate-enforced

| Field | Value | Enforced by |
| --- | --- | --- |
| `bridge_version` / `manifest_version` | `1.0.0` | `pyproject.toml`, `config.py` |
| `contract_version` | `1.0.0` | activation gate check `A-02` |
| `schema_version` (wire + SQLite) | `0.6.1` | activation gate check `A-02` |
| Effective public tools | exactly `27` | activation gate check `A-02` |
| SQLite migration ledger | v10 (no migration added) | full suite |
| Generic shell / HTTP surface | none | activation suite `PA-09` |
| V1 → V2 import direction | none (one-way) | `A-02`, `PA-08` |

## The activation mechanism

### A typed profile, not scattered booleans

`src/hermes_mcp_bridge/v2/production_profile.py` introduces
`V2ProductionProfile`: one frozen, validated record describing which accepted
lanes are active.

- **Fail-closed by construction.** The default profile activates nothing. An
  unknown `BRIDGE_V2_*` variable, a malformed boolean, a negative budget or a
  budget above the accepted ceiling all raise `ProfileConfigError` — none of
  them silently default to "on", and none silently default to "off" either.
- **A master switch that dominates.** `BRIDGE_V2_ENABLED=0` disables every lane
  regardless of the per-capability variables.
- **A structural invariant.** `HYBRID` cannot be enabled without the
  deterministic lanes it is supposed to prefer; a profile that tried would be
  rejected at construction rather than degrade into agentic-first behaviour.
- **Auditable.** `profile.canonical()` and `profile.digest()` give a
  secret-free, canonically hashed view suitable for logs and evidence.

The per-module `*_FEATURE_ENABLED` constants **keep their `False` default**.
They remain the import-time fail-closed posture that the Phase 4–8 gates assert,
and gate check `A-03` fails the release if activation were implemented by
flipping them instead.

### A composition root, not scattered wiring

`src/hermes_mcp_bridge/v2/composition.py` introduces `V2Composition`: the single
sanctioned place where a profile becomes live engines
(`BatchScheduler`, `DagEngine`, `RunbookEngine`, `ProviderGateway`,
`ModeResolver`, `HybridCoordinator`).

- A disabled capability raises `CapabilityDisabled` from the builder. It never
  returns a half-built or permissive object.
- Structural dependencies are enforced: `RUNBOOK` requires `DAG`, because a
  runbook compiles to a plan the DAG engine executes.
- An agentic step supplied with a zero budget is refused rather than parked
  where a later misconfiguration could wake it.
- All dependencies are injected. The composition root decides *whether* a lane
  is wired, never *what* it talks to.

### Configuration surface

| Variable | Meaning | Default |
| --- | --- | --- |
| `BRIDGE_V2_ENABLED` | Master switch; off disables everything | `0` |
| `BRIDGE_V2_DIRECT` | Activate the DIRECT lane | `0` |
| `BRIDGE_V2_BATCH` | Activate the BATCH lane | `0` |
| `BRIDGE_V2_DAG` | Activate the DAG lane | `0` |
| `BRIDGE_V2_RUNBOOK` | Activate the RUNBOOK lane | `0` |
| `BRIDGE_V2_INTEGRATIONS` | Activate the accepted provider integrations | `0` |
| `BRIDGE_V2_HYBRID` | Activate the Hybrid coordinator | `0` |
| `BRIDGE_V2_AGENTIC_TOKEN_BUDGET` | Agentic allowance, ceiling 4096 | `0` |

Booleans accept `1/true/yes/on` and `0/false/no/off`; anything else is refused.

## Rollback

Two levers, neither requiring a code change or an image change:

1. **Disable activation, keep 2.0.1** — set `BRIDGE_V2_ENABLED=0` and restart.
   The runtime returns to the exact `2.0.0` posture. Gate check `A-06` proves
   this equals `DISABLED_PROFILE`.
2. **Revert the release** — redeploy `v2.0.0`. Unchanged and immutable.

The prior V1 baseline remains available as the third, deepest rollback point.

## Acceptance

A new fail-closed gate, `V2_PRODUCTION_ACTIVE`, is produced by
`scripts/validate_v2_production_activation_gate.py`:

| Check | Proves |
| --- | --- |
| `A-00` | Verdict bound to one exact commit and a clean tree |
| `A-01` | Every predecessor gate recorded with `failures=[]` |
| `A-02` | Contract 1.0.0, schema 0.6.1, 27 tools, no V1→V2 import |
| `A-03` | The `*_FEATURE_ENABLED` defaults are still `False` |
| `A-04` | Default activates nothing; production activates all; bad input refuses |
| `A-05` | Each lane is reachable through the composition root, and refuses when disabled |
| `A-06` | The rollback switch restores the released posture |
| `A-07` | Mode preference intact; agentic budget defaults to zero |
| `A-08` | The activation acceptance suite is executed, not assumed |
| `A-09` | Activation module digests recorded for drift detection |

Backing it, `tests/test_v2_production_activation.py` implements `PA-01..PA-24`.
Reachability is proven by *constructing and running the real engine* through
`V2Composition` — a batch that actually completes, a DAG that actually executes,
a runbook that actually invokes, a provider gateway that actually returns
`SUCCESS`, a Hybrid coordinator that actually resolves to DIRECT. Code presence
is never accepted as evidence of activation.

## Artifacts

- Git tag: `v2.0.1` (annotated, immutable, never moved).
- `docs/release-2.0.1.md` — this document.
- `docs/v2/evidence/production-activation-gate.json` — the recorded verdict.
- Changelog entry under `## 2.0.1`.
