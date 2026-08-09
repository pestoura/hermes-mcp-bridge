# `HYBRID_ACCEPTED` Criteria (fail-closed)

>
> **V2 · PHASE 8 DOWNSTREAM DESIGN ONLY · NOT IMPLEMENTED · DO_NOT_MERGE**
>
> Requires Phases 3–6 accepted and at least two accepted Phase 7 integrations.
> No resolver code, flag or gate change exists.

1. Phases 3–6 accepted; at least two Phase 7 integrations accepted; recorded by
   commit SHA.
2. Resolver decision tree implemented exactly as specified, with a documented
   one-to-one mapping from tree node to code path.
3. Determinism proven: 100 replays per scenario class with zero mismatches; a
   recorded-input replay reproduces the decision record byte-identically.
4. Reason codes complete and closed; every terminal outcome in the acceptance run
   carries a primary code from the enumeration; unknown-code count = 0.
5. Zero-default agentic proven: no scenario escalates without an explicit
   allowance.
6. Safety invariants I1–I10 each covered by at least one passing negative or
   adversarial test; `P8-01..P8-20` executed with zero failures.
7. Economics evidence recorded against a matched baseline: absolute tokens,
   latency percentiles, provider call counts, deterministic coverage; DIRECT
   paths show zero Hermes LLM tokens and zero upstream LLM calls.
8. Escalation bounded: no run exceeds `MAX_ESCALATIONS_PER_REQUEST`.
9. Audit completeness 100% across all modes, refusals included.
10. Redaction scan of decision records, evidence and metric labels: zero findings.
11. Rollback: disabling HYBRID returns the system to the prior accepted
    deterministic behaviour, verified by a run.
12. V1 isolation intact: exactly 27 tools.
