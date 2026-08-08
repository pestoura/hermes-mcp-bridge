# Execution Modes

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

V2 formalizes six explicit modes.

| Mode | Use | Hermes LLM | Concurrency |
|---|---|---:|---|
| DIRECT | one typed deterministic operation | no | single operation |
| BATCH | N independent typed operations in one request | no by default | bounded parallel |
| DAG | dependency graph with typed bindings | no by default | parallel by topology/limits |
| RUNBOOK | known versioned workflow | no by default | workflow-defined/limited |
| AGENTIC | unknown/new reasoning | yes | Hermes-controlled |
| HYBRID | deterministic path plus explicit escalation | conditional | bounded |

## DIRECT

Policy/schema/credential controls execute a typed operation directly against its backend. Zero Hermes LLM tokens are expected for the operation itself.

## BATCH

One bridge request transports multiple independent operations. Every node retains its own policy, scope, risk class, credential capability, quota, audit and result shaping. A batch-level approval never implicitly authorizes materially different nodes not covered by the approved digest.

## DAG

Nodes declare `depends_on` and typed bindings. The executor validates schema, detects cycles, topologically schedules nodes, runs independent branches concurrently, propagates cancellation/deadlines and emits per-node results.

## RUNBOOK

A deterministic, executable, versioned, validated and auditable workflow. Stable skills may be intentionally promoted to runbooks after review; there is no automatic conversion of all skills.

## AGENTIC

Retains `hermes_prompt` / Hermes Agent for unknown-cause investigation, unstructured interpretation, novel solution design, architectural reasoning, novel planning and other cases where reasoning is the work.

## HYBRID

Deterministic execution happens first. Agentic escalation occurs only when an explicit rule and budget allow it. Example escalation reasons: `UNKNOWN_INTENT`, `UNSUPPORTED_TOOL`, `DIAGNOSIS_REQUIRED`, `LOW_CONFIDENCE_MAPPING`, `UNSTRUCTURED_ANALYSIS_REQUIRED`.

Hybrid controls include maximum escalations, token budget, timeout, context shaping and minimum-necessary context transfer.
