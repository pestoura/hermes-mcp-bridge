# Observability and Token Economics

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

V2 extends existing logging/metrics with execution-mode, node, scheduler, shaping and LLM economics.

## Request counters

`direct_requests_total`, `batch_requests_total`, `dag_requests_total`, `runbook_requests_total`, `agentic_requests_total`, `hybrid_requests_total`.

## Node counters

`node_execution_total`, `node_success_total`, `node_failure_total`, `node_retry_total`, `node_cancel_total`.

## Latency

Measure request, node, queue, policy, approval and credential-resolution latency with p50/p95/p99. Track active requests/nodes and queue depth.

## Result shaping

`result_bytes_raw`, `result_bytes_returned`, `result_reduction_ratio`.

## LLM

`agentic_input_tokens`, `agentic_output_tokens`, `agentic_total_tokens`, `agentic_escalations_total`.

Key derived indicators: percentage of requests executed without Hermes LLM, agentic escalation rate, estimated tokens saved and direct-vs-agentic latency ratio.

The 2026-08-08 broad audit token count is baseline evidence only, not a normal-call benchmark. Phase 0 must establish representative read/mutation/agentic benchmarks before claims are made.

## Tracing

Adopt W3C Trace Context end-to-end: client request -> bridge -> policy -> scheduler -> node -> backend -> result. Do not place tokens, passwords, full prompts or unnecessary sensitive personal data into span attributes/labels. Cardinality must be bounded.

## Result manifest

Per execution capture execution/request/plan identifiers and digests, mode, node statuses, policy decisions, approvals, timestamps, artifact refs, result digest and signature status. Evidence should support audit without preserving secrets.
