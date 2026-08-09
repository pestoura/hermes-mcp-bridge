# ADR-0027 — DAG Replay Format (closes OD-021)

> **V2 · PHASE 5 · IMPLEMENTED BEHIND `DAG_FEATURE_ENABLED` · NO V1 IMPACT**

**Status:** Accepted (Phase 5)

## Context
OD-021 asked what a replay is and what it is allowed to do. Replay is needed for
incident review and for verifying that a plan's deterministic parts behave as
recorded. The hazard is obvious: a "replay" that quietly re-issues provider calls
or re-consumes an approval is not a replay, it is a second execution.

## Decision
A replay is **data, not a recording of transport**: a mapping
`{node_id: shaped_result}` captured from a prior execution's checkpoint, replayed
against the same plan digest. Providers are disabled for the whole run. A replay
therefore performs **zero external calls, consumes zero approvals and writes zero
idempotency keys**; TRANSFORM nodes are recomputed for real, so a divergence
between recorded inputs and recomputed outputs is visible. The execution is
labelled `replay = true` on the checkpoint and on the returned report, and a
node with no recorded result fails rather than falling through to a live call.
Because it carries only shaped results, a replay document contains no credential
material and is subject to the same secret-material rejection as any checkpoint.

## Consequences
Replay is safe to run against production plan documents and is cheap: it exercises
validation, ordering, binding re-validation and transform logic without touching
a provider. It cannot reproduce provider-side behaviour that was never recorded,
which is the correct limitation — Phase 5 does not claim to replay transport.

## Alternatives
* **HTTP cassette / VCR-style transport capture** — rejected: stores raw provider
  traffic, which is exactly where credential material and unshaped PII live.
* **Live re-execution against a staging provider** — rejected: that is an
  execution, with real effects and real approval semantics.
* **Untagged replay** — rejected: a report that cannot be distinguished from a
  real execution is an audit hazard.

## Security implications
Zero external effect by construction, not by policy. The `replay` flag is durable
in the checkpoint so evidence cannot later be mistaken for a real execution
(A5-20).

## Operational implications
Replay documents are derived from checkpoints and inherit their retention and
handling rules.

## Open questions
Whether to include a recorded-vs-recomputed diff report for TRANSFORM nodes as a
first-class artifact; currently a divergence simply surfaces as a different
result.
