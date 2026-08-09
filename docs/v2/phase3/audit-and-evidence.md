# Phase 3 Audit and Evidence Contract

> **V2 · PHASE 3 · PREPARATION ONLY · NOT IMPLEMENTED · NO V1 IMPACT**

## Write-ahead intent record

Before any provider write, a durable record is created:

```text
{
  "schema": "v2.phase3.mutation-audit.1",
  "audit_id": "...",
  "timestamp_utc": "...",
  "principal": "...",
  "operation": "github.create_branch",
  "repository": "owner/repo",
  "capability": "github.write.branch",
  "operation_digest": "...",
  "approval_id": "...",
  "idempotency_key": "...",
  "preconditions_observed": { "base_sha": "..." },
  "policy_decision": "ALLOW_WITH_APPROVAL",
  "policy_version": "...",
  "registry_snapshot_hash": "...",
  "outcome": "PENDING"
}
```

The record is finalized after the call with `outcome` in
`COMMITTED | FAILED_CLEAN | AMBIGUOUS | DENIED`, the provider status class, the
resulting ref/PR identifier, and latency/byte counters.

## Invariants

1. **No write without a prior intent record.** A committed mutation whose audit
   record is missing is an acceptance failure, not a logging bug.
2. **Redaction is fail-closed.** Credential material, tokens, installation IDs
   treated as sensitive, headers and raw provider bodies are never recorded. If
   redaction cannot be proven for a field, the field is omitted.
3. **Integrity.** Mutation evidence bundles reuse the existing V1 HMAC-signed
   result-manifest mechanism; evidence files carry SHA-256 digests recorded in
   the acceptance document, matching the Phase 0/1/2 pattern.
4. **Reconstructability.** From the audit record alone an auditor can answer:
   who, what operation, against which repository, under which policy and
   registry version, with which approval, with what observed preconditions, and
   what the provider did.
5. **Metric cardinality.** Repository, ref and PR identifiers must not be used as
   unbounded metric labels; counters are aggregated by operation and outcome
   class (V2-SEC-024).
6. **Zero-LLM assertion.** As in Phase 2, mutation evidence must show zero
   Hermes token usage for the DIRECT mutation path, measured from real runtime
   accounting rather than asserted.
