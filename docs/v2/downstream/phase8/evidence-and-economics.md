# Token, Cost and Latency Evidence Contract

>
> **V2 · PHASE 8 DOWNSTREAM DESIGN ONLY · NOT IMPLEMENTED · DO_NOT_MERGE**
>
> Requires Phases 3–6 accepted and at least two accepted Phase 7 integrations.
> No resolver code, flag or gate change exists.

## Measurement source

Token accounting uses the **real** Hermes accounting source already validated in
Phase 0/2 (per-session model usage in the Hermes state store), never model
self-report and never estimation. Provider API calls are counted at the provider
boundary. Latency is measured at the gateway boundary and per stage.

## Per-request economics record

| Field | Definition |
|---|---|
| `mode` | Terminal mode or `REFUSED` |
| `primary_reason_code` | Single code |
| `rejected_branches[]` | Ordered rejection codes |
| `deterministic_nodes` / `total_nodes` | Coverage numerator/denominator |
| `deterministic_coverage` | Ratio |
| `direct_tokens` | Must be `0` for fully deterministic paths |
| `agentic_tokens` | input/output/cache/reasoning, separated |
| `escalation_count` | Integer, ≤ `MAX_ESCALATIONS_PER_REQUEST` |
| `provider_api_calls` | Per provider |
| `latency_ms` | Total and per stage (resolve, policy, credential, provider, shape) |
| `cost_estimate` | Derived from tokens × recorded model price snapshot; the price snapshot id is recorded so the figure is reproducible |
| `budget_outcome` | `within` / `exhausted` / `refused` |

## Comparison contract

HYBRID benefit is claimed only against a **matched** V1/agentic baseline for the
same scenario set: same targets, same repetitions, same host, uncontaminated
metric windows, cleanup residual 0 — the pattern accepted in Phases 0 and 2.
A benefit claim states absolute token counts, not only percentages, and reports
the scenario count.

## Anti-gaming rules

- A refusal is never counted as a token saving.
- A partial result is never compared against a complete baseline result;
  semantic-match count is reported alongside.
- Latency improvements exclude cache-warm effects unless the baseline was equally
  warmed, which is recorded.
- Zero-token claims require the connected evidence to show zero upstream Hermes
  LLM calls, not merely zero recorded tokens.
