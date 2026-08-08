# Trust and Execution Boundaries

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

## Boundaries

1. Client -> Bridge: untrusted input, authenticated principal still to be formalized.
2. Bridge -> policy/registry: trusted control data must be versioned/integrity protected.
3. Bridge -> credential broker: only capability IDs cross; secrets stay server-side.
4. Bridge -> tool backend: typed parameters, destination/path/executable restrictions.
5. Internal MCP/plugin -> registry: metadata is not automatically trusted.
6. Tool -> result layer: outputs are untrusted data until schema/redaction/provenance validation.
7. Artifact store: integrity and retention controls required.
8. Agentic path: LLM recommendations cannot bypass deterministic policy/approval.

## Sandbox strategy

Typed wrappers should limit executable, arguments, paths, network destinations, environment variables, working directory and file access.

- Docker: prefer a mediated surface such as docker-socket-proxy over raw Docker socket exposure.
- systemd: explicit service allowlist.
- filesystem: explicit root paths and operation classes.
- network: explicit egress policy/destination allowlist.
- shell: not a normal projected external tool.

## Safe defaults

Unknown action -> DENY; missing credential -> FAIL; missing policy -> DENY; schema mismatch -> FAIL; ambiguous binding -> FAIL; unsupported compensation -> do not assume rollback.
