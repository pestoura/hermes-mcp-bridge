# ADR-0019 — Execution Sandbox Boundaries

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

**Status:** Proposed

## Context
Direct typed execution can bypass the agent but must not inherit unrestricted host authority.

## Decision
Wrappers constrain executable, arguments, path roots, working directory, environment variables and network destinations. systemd uses service allowlists; filesystem uses explicit roots; network uses egress policy; Docker should prefer mediated socket proxy/least-authority APIs; generic shell is not projected.

## Consequences
Safer direct execution at the cost of wrapper/sandbox engineering.

## Alternatives
Raw host shell/filesystem/Docker socket; containerize everything without per-tool policy.

## Security implications
Primary mitigation for shell injection, path traversal, SSRF, secret exfiltration and host takeover.

## Operational implications
Sandbox rules need environment-specific configuration, tests and diagnostics.

## Open questions
Concrete sandbox technology/isolation boundary for Phase 2/3 and later privileged integrations.
