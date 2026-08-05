# Hermes MCP Bridge 1.0.0 — stable contract

## Status

The `1.0.0` line freezes the existing 27-tool MCP surface as the first stable
public contract. It does not enable metrics, tracing export, retry or the
circuit breaker in production, and it does not change the wire or SQLite schema.

- contract version: `1.0.0`;
- wire/schema version: `0.6.1`;
- required tools: `27`;
- canonical snapshot: `contracts/1.0.0.json`;
- canonical snapshot SHA-256:
  `543213dedc2928466e5ec8ed9e2d4f9a464c7204cef737584a2a7e774c378e2d`.

The snapshot contains no secrets, runtime paths, prompts, results or deployment
state.

## Compatibility guarantees for 1.x

A release in the `1.x` series must not, without a new major version:

- remove or rename a required tool;
- change an existing required input from optional to mandatory;
- narrow an accepted input domain in a way that breaks valid 1.0 clients;
- remove a documented output field or change its meaning incompatibly;
- reuse an existing error identifier for a different condition;
- change the wire/schema version without a documented migration and rollback;
- change `hermes_readiness.version_added` from `0.8.0`.

A minor release may add optional inputs, additive output fields or new tools.
Clients must ignore unknown additive fields. A patch release may correct defects
without changing the public contract.

## Tool stability

The required set is stable as a catalog. Individual capability-manifest entries
retain their existing `stability`, `read_only`, `effective_mode` and
`depends_on_upstream` metadata. Promoting an experimental operation to stable is
additive. Making a stable operation experimental is incompatible.

## Schema policy

`SCHEMA_VERSION` remains `0.6.1`. The 1.0 version is a contract and release
boundary, not a database migration. Any future schema movement requires:

1. an explicit migration;
2. compatibility tests from the previous production baseline;
3. an isolated restore test;
4. a rollback decision that accounts for irreversible state changes.

## Validation

CI must prove all of the following:

- package, runtime and contract versions are `1.0.0`;
- the runtime registers exactly the required 27 tools;
- the `1.0.0` tool set is identical to `0.9.0`;
- `contracts/1.0.0.json` equals the generated canonical snapshot;
- the generated snapshot digest matches the documented digest;
- the capability manifest uses contract/manifest version `1.0.0`;
- `hermes_readiness` remains present with `version_added=0.8.0`;
- schema version remains `0.6.1`.

## Deprecation policy

A public operation or field must be documented as deprecated before removal.
Removal is permitted only in a later major release. Deprecation must include:

- the affected operation or field;
- the replacement or migration path;
- the first version carrying the warning;
- the earliest major version in which removal may occur.

Deprecation warnings and logs must not contain prompts, results, credentials,
tokens, cookies or secret material.

## Production gate

Development and isolated validation of `1.0.0` may proceed while the `0.9.0`
single-slot acceptance remains pending. Production promotion of `1.0.0` remains
blocked until an equivalent single-slot RITMO/Hermes lifecycle test proves:

- one dedicated read-only run;
- no duplicate submission after SSE/polling convergence;
- recovery after one controlled restart;
- no stuck run or lease;
- SQLite integrity;
- redacted JSON logs;
- exact image and contract identity.
