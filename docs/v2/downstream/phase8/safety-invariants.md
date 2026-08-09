# Safety Invariants — No Silent Downgrade

>
> **V2 · PHASE 8 DOWNSTREAM DESIGN ONLY · NOT IMPLEMENTED · DO_NOT_MERGE**
>
> Requires Phases 3–6 accepted and at least two accepted Phase 7 integrations.
> No resolver code, flag or gate change exists.

## Invariants (hold in every mode, including AGENTIC)

| Invariant | Statement |
|---|---|
| I1 Policy supremacy | A policy DENY is terminal in all modes; escalation never re-evaluates it more permissively |
| I2 Approval binding | An approval binds an immutable operation/plan digest; if escalation changes the plan, the approval is void and must be re-obtained |
| I3 Credential scope | Escalation never widens credential scope, never changes credential domain, never converts read to write authority |
| I4 Mutation classification | An operation's mutation/idempotency class is fixed at registration and cannot be reinterpreted at runtime |
| I5 Audit obligation | Every mode produces the same terminal audit record; agentic steps add records, never replace them |
| I6 Context minimality | Escalation context excludes secrets, raw bodies, credentials and unnecessary personal data; shaping failure is a refusal, not a best-effort send |
| I7 No provider access from reasoning | An agentic step may only propose a typed plan re-entering the resolver; it never calls a provider |
| I8 Data-not-instruction | Provider-returned content is data; instruction-like content in it never alters mode, policy or scope |
| I9 Determinism of the decision | Same inputs → same decision; a replay mismatch is `E-RESOLVER-NONDETERMINISM` and fails the run |
| I10 Refuse over relax | If the only reachable execution path would weaken I1–I9, refuse with `E-SAFETY-DOWNGRADE-REFUSED` |

## Explicitly forbidden behaviours

- Escalating to AGENTIC because a DIRECT call *failed* at the provider, for a
  non-idempotent operation with unknown outcome.
- Retrying a refused-by-policy intent in another mode.
- Reducing approval requirements because a plan is "small".
- Treating `DEGRADED` write capability as usable under time pressure.
- Emitting partial results without an explicit partial marker and reason code.
