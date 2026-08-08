# Phase 2 — Connected GitHub DIRECT Acceptance

> **V2 · CONNECTED GATE DEFINED · EVIDENCE NOT YET COLLECTED · `DIRECT_READ_ACCEPTED` NOT DECLARED**

This document defines the evidence that must be collected on the **actual
Jarvas/Hermes runtime** before the GitHub DIRECT read-only path may be promoted.
Repository CI and hermetic MockTransport tests are prerequisites, not substitutes
for this gate.

## Gate principle

```text
repo-side DIRECT core GREEN
        ↓
actual Jarvas discovery
        ↓
dedicated github.read provider verified
        ↓
explicit canary/feature wiring
        ↓
5 operations × 3 repetitions
        ↓
DIRECT execution + V1 agentic shadow read
        ↓
normalized semantic comparison
        ↓
zero Hermes upstream/LLM use on DIRECT
        ↓
fail-closed evidence validator
        ↓
DIRECT_READ_ACCEPTED
```

The gate is implemented by:

```text
scripts/validate_v2_phase2_direct_read_evidence.py
```

It cannot generate or infer evidence. It only validates an externally collected
connected evidence document.

## Required runtime identity

Evidence must prove:

- Bridge `1.0.0`;
- schema `0.6.1`;
- V1 tool count remains exactly `27`;
- the actual Jarvas host was reached;
- the V1 path is healthy;
- the DIRECT feature/canary path is explicitly enabled;
- V1 semantics remain unchanged;
- the exact 40-hex commit carrying the DIRECT core is recorded.

## GitHub provider gate

The connected discovery must identify the actual Jarvas-side GitHub provider
without printing or retaining its secret.

Accepted provider types for this gate:

- GitHub App installation credential;
- fine-grained GitHub token.

A classic/broad PAT is not accepted as Phase 2 least-privilege evidence.

Required capability:

```text
github.read
```

Required exact read permissions:

```json
{
  "checks": "read",
  "issues": "read",
  "metadata": "read",
  "pull_requests": "read"
}
```

The evidence must also record:

- `authenticated = true`;
- `least_privilege = true`;
- `broad_pat = false`;
- no unexpected permissions;
- exact repository scopes, with no wildcards;
- GitHub base URL `https://api.github.com`;
- GitHub REST API version `2026-03-10`.

No credential value, secret path, environment dump or authorization header may
be retained.

## Required samples

Exactly three connected repetitions are required for each tool:

- `github.get_repo`;
- `github.get_pr`;
- `github.get_checks`;
- `github.get_issue`;
- `github.search`.

Total required samples: **15**.

Each sample must identify the exact repository and prove it belongs to the
provider's declared repository scope.

### DIRECT side

Every DIRECT sample must prove:

- success;
- positive latency;
- exactly **one GitHub provider API call**;
- `hermes_upstream_calls = 0`;
- Hermes LLM input/output/total tokens = exactly `0`;
- positive raw provider bytes;
- positive returned bytes;
- returned bytes do not exceed raw bytes;
- no mutation observed;
- no redirect followed.

This is the core proof that a deterministic read did not silently fall back to
Hermes/LLM execution.

### V1 shadow side

The same read intent must also run through the existing V1 agentic path, without
performing a mutation.

Every shadow sample must prove:

- success;
- positive latency;
- real Hermes token accounting, not an estimate;
- positive total token usage;
- a named real accounting source;
- no mutation observed.

The V1 path remains the comparison baseline only; the DIRECT execution must not
invoke it internally.

### Semantic comparison

DIRECT and V1 results may differ in raw provider representation, so comparison
must be performed over the same normalized field set used by the DIRECT result
shaper.

Evidence must retain only digests and the boolean comparison result:

```text
semantic_match = true
direct_normalized_sha256 = <64 hex>
v1_normalized_sha256     = <same 64 hex>
```

Raw prompt/output text is not required and must not be retained by the
acceptance evidence.

## Window isolation

Each sample must declare:

```text
connected_jarvas = true
contaminated_window = false
```

If unrelated activity makes provider-call, Hermes-call or token attribution
ambiguous, the sample is rejected and must be recollected. The validator does
not average away contaminated observations.

## Aggregate requirements

For 15 accepted samples the aggregate must prove:

```text
sample_count                    = 15
successful_samples              = 15
semantic_matches                = 15
direct_provider_api_calls       = 15
direct_hermes_upstream_calls    = 0
direct_hermes_llm_tokens        = 0
v1_shadow_hermes_llm_tokens     > 0
mutations_observed              = 0
contaminated_windows            = 0
```

Each of the five tool IDs must have exactly 3 repetitions numbered 1–3.

## Privacy contract

The retained evidence must explicitly state:

```json
{
  "credential_values_stored": false,
  "environment_dump_stored": false,
  "outputs_stored": false,
  "prompts_stored": false,
  "secret_paths_stored": false
}
```

The validator also rejects evidence containing forbidden secret-bearing keys
such as raw token/authorization/password/private-key fields.

## Validator result

Connected evidence is validated with:

```bash
python scripts/validate_v2_phase2_direct_read_evidence.py \
  /path/to/phase2-connected-evidence.json \
  --json-out /path/to/phase2-connected-gate.json
```

Success is exactly:

```json
{
  "failures": [],
  "gate": "DIRECT_READ_ACCEPTED",
  "source_commit": "<40-hex-connected-source-sha>"
}
```

Any missing/invalid property returns `DIRECT_READ_BLOCKED` and a non-zero exit
code.

## Current blocker

The repository side can define and test this contract, but it cannot satisfy the
connected gate without access to the actual Jarvas GitHub provider/credential
path and the explicit canary wiring. ChatGPT's own GitHub connector is a
different trust/credential boundary and is not valid evidence for this gate.
