# BATCH Non-Goals and Prohibited Surfaces

> **V2 · PHASE 4 · DESIGN · unblocked by `DIRECT_MUTATION_ACCEPTED` (a86b26d) · runtime gated behind `BATCH_FEATURE_ENABLED` until `BATCH_ACCEPTED`**

Phase 4 explicitly **does not** introduce, and must never introduce:

1. **No generic shell surface.** No `shell`, `exec`, `run_command`, `bash`,
   subprocess or template-expanded command step. A batch step is a typed
   registry tool and nothing else.
2. **No generic HTTP surface.** No `http_request`, `fetch`, `curl`-equivalent,
   arbitrary URL, arbitrary method or caller-controlled headers. All provider
   traffic goes through existing typed capabilities with their own credential
   and scope binding.
3. **No dynamic tool loading** inside a batch, and no caller-supplied code,
   expression language or templating between steps.
4. **No inter-step data flow.** Step B cannot reference step A's output. Any
   such need is DAG mode, not BATCH. `depends_on` must be empty.
5. **No DAG semantics**: no dependency resolution, topological ordering,
   conditionals or loops.
6. **No transactions, rollback or compensation** across steps.
7. **No automatic retries or backoff** at the batch layer.
8. **No batch-level authorization, credential hoisting or approval reuse.**
9. **No new network listener, endpoint or exporter.** BATCH is a mode on the
   existing surface, not new infrastructure.
10. **No raising of Phase 3 limits.** Batch never increases a per-provider rate
    limit, mutation concurrency, timeout ceiling or scope.
11. **No V1 impact.** The 27-tool V1 surface, contract `1.0.0`,
    schema `0.6.1` are unchanged.

Any proposal to relax items 1–4 is a new ADR and a new threat model, not a
Phase 4 implementation detail.

## Enforcement

A preflight check (Phase 4 analogue of `scripts/v2_phase3_preflight.py`) must
scan the Phase 4 implementation for shell/subprocess/generic-HTTP symbols and
for non-empty `depends_on` handling, and fail closed. That check is written
with the implementation, after `DIRECT_MUTATION_ACCEPTED`.
