# Target Architecture

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

## Target flow

```text
Client
  |
  v
MCP v2 typed request
  |
  +--> capability snapshot + schema validation
  +--> policy simulation/evaluation
  +--> budgets / quota / resource scope
  +--> approval binding when required
  +--> lock / concurrency controls
  +--> credential capability resolution
  |
  v
Execution mode
  +--> DIRECT typed tool
  +--> BATCH bounded parallel nodes
  +--> DAG dependency scheduler
  +--> RUNBOOK compiled known workflow
  +--> AGENTIC Hermes LLM
  +--> HYBRID deterministic + explicit escalation
  |
  v
Target backend(s)
  |
  v
Shaped result / artifact refs / signed evidence
```

## Deterministic first

Simple known actions should not require Hermes Agent/HY3. Examples include GitHub reads, service status, selected Docker inspection, DNS/tunnel reads, calendar list, email search and Home Assistant state reads when corresponding typed tools are registered and authorized.

V2 should prefer typed domain operations such as `github.merge_pr(...)`, `system.restart_service(...)`, `docker.restart_container(...)` over `execute_shell(command=...)`. Generic shell can remain an internal implementation detail for tightly mediated wrappers but is not the normal projected client surface.

## Safe transformation layer

DAGs may use deterministic transformation nodes (`select`, `filter`, `map`, `count`, `extract`) over typed structured data. Arbitrary `eval`, shell interpolation or user-supplied code is excluded.

## Compile-once runbooks

Promoted runbooks should be validated and compiled into a canonical intermediate representation. Execution uses the compiled representation plus a stored capability snapshot/hash, reducing repeated validation and improving reproducibility.

## Backend abstraction

A tool may use API, CLI wrapper, native Hermes tool, plugin, internal MCP or future connector. Backend choice is registry metadata, not a client secret. The same canonical tool contract should remain stable when a backend is replaced where semantics are equivalent.
