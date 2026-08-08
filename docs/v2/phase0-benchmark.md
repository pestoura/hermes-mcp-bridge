# V2 Phase 0 — Connected Benchmark and Acceptance Evidence

> **V2 · PHASE 0 · EVIDENCE HARNESS · NO V1 SEMANTIC CHANGE**

Phase 0 is complete only when the real v1 path has representative, sanitized evidence for
latency, Hermes/tool/API-call behaviour and LLM token usage. The broad 2026-08-08 audit is
useful architectural evidence but is not a normal-call benchmark and cannot close this gate.

## Gate

The only successful terminal decision for this phase is:

```text
BASELINE_ACCEPTED
```

The validator fails closed as `BASELINE_BLOCKED` unless all of the following are true:

- runtime identity is Bridge `1.0.0`, schema `0.6.1`, with a manifest hash;
- upstream Hermes health is `ok` or `healthy`;
- at least one `read`, one controlled `mutation` and one `agentic` scenario are present;
- every scenario has at least three successful repetitions;
- every repetition has positive end-to-end latency;
- Prometheus deltas include one observed terminal execution and execution tool/upstream-call data;
- the metrics window is not contaminated by another terminal execution;
- every repetition has input/output/total token usage from the Hermes result or a declared provider sidecar;
- evidence stores hashes and aggregates only: no prompt text, output text or secrets.

A code-only merge does **not** promote the phase.

## Files

- `scripts/v2_phase0_benchmark.py` — connected collector.
- `scripts/validate_v2_phase0_evidence.py` — fail-closed gate validator.
- `docs/v2/evidence/phase0-scenarios.example.json` — scenario template.
- `tests/test_v2_phase0_evidence.py` — CI contract tests.

## Collection model

Run the collector on the Jarvas/Hermes host against loopback endpoints:

```bash
python scripts/v2_phase0_benchmark.py \
  --url http://127.0.0.1:8765/mcp \
  --metrics-url http://127.0.0.1:9464/metrics \
  --scenarios /path/to/phase0-scenarios.json \
  --token-usage-file /path/to/token-usage.json \
  --ack-mutation-sandbox \
  --json-out /tmp/hermes-v2-phase0-evidence.json

python scripts/validate_v2_phase0_evidence.py \
  /tmp/hermes-v2-phase0-evidence.json \
  --json-out /tmp/hermes-v2-phase0-gate.json
```

`--token-usage-file` is optional only when Hermes already returns recognizable token usage.
The sidecar must contain only numeric usage plus a short source name; never credentials,
authorization headers, prompts or provider request/response bodies.

## Scenario rules

### Read

Use a stable, read-only operation through the current v1 agentic path. Prefer a resource with
known expected output so semantic success can be independently checked.

### Mutation

The mutation **must target an isolated/disposable resource** and must include its own cleanup
or compensation. The collector refuses a scenario set containing a mutation unless
`--ack-mutation-sandbox` is explicitly supplied.

Do not benchmark a destructive operation against production state merely to satisfy this gate.

### Agentic

Use a reasoning request that is bounded and repeatable. It should not mutate state and should
avoid unrelated tool use.

## Metrics isolation

The collector snapshots the Bridge Prometheus exporter immediately before and after each
sequential run. `bridge_execution_terminal_total` must increase by exactly one. If another
execution terminates inside that window, the sample is marked contaminated and the validator
blocks the gate. Re-run that sample in a quieter/isolated window instead of trying to subtract
unattributed traffic.

The following v1 instrumentation is used where available:

- `bridge_execution_terminal_total`;
- `bridge_execution_tool_calls_sum`;
- `bridge_execution_upstream_calls_sum`;
- `bridge_execution_poll_iterations_sum`;
- `bridge_execution_retries_sum`;
- `bridge_execution_recoveries_sum`.

## Token evidence

Phase 0 must not estimate provider token counts from characters or prompt length. Accepted
sources are:

1. token usage present in the Hermes result; or
2. numeric usage copied/exported from the actual LLM/provider execution record and linked to
   the exact scenario repetition in the sidecar.

This preserves the distinction between measured evidence and approximation.

## Evidence handling

The collector persists:

- scenario ID/category;
- SHA-256 of the prompt, never the prompt;
- success/failure;
- latency;
- output byte count, never output content;
- metric deltas;
- numeric token usage and source;
- Bridge/runtime identity;
- collection/privacy metadata.

The resulting evidence can be retained with the repository/release evidence after review.
Before committing any evidence file, run the validator and inspect it for environment-specific
metadata that should remain local.

## Promotion sequence

```text
collector/test harness GREEN
        ↓
connected Jarvas evidence collected
        ↓
validator PASS
        ↓
BASELINE_ACCEPTED
        ↓
Phase 1 — Tool Registry
```

Until `BASELINE_ACCEPTED` exists as real evidence, Phase 1 may be designed but must not be
promoted as implemented.
