# Result Shaping and Artifacts

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

Reducing returned context is a central v2 objective. Typed tools should support bounded projections such as `select/fields`, `exists`, `count`, `first`, `latest`, `top-N`, `metadata-only`, pagination and cursor semantics.

Example: a GitHub search finding 127 items should be able to return only count + top 5 + selected fields when that satisfies the request.

## Large results

Large payloads should be stored as artifacts/evidence rather than injected into LLM context. The client receives a bounded reference containing fields such as `artifact_ref`, digest, size, content type, creation/expiry timestamps and non-sensitive metadata. Later requests may retrieve selected parts.

## Deterministic aggregation

BATCH/DAG outputs should be joinable and aggregatable without an LLM when schemas are compatible. Safe transformation operators include select/filter/map/count/extract, never arbitrary code/eval.

## Secret-aware schemas

Fields should support classifications such as `PUBLIC`, `INTERNAL`, `SENSITIVE`, `SECRET`. `SECRET` is never serialized to the client. Redaction is fail-closed.

## Provenance

Each result should identify tool, tool version, backend, execution ID, timestamp and digest without revealing credentials. Raw and returned byte counts enable `result_reduction_ratio` measurement.
