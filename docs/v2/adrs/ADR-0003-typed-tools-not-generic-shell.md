# ADR-0003 — Typed Tools Instead of Generic Shell Exposure

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

**Status:** Proposed

## Context
A generic shell exposes broad executable, argument, path and environment authority and makes fine-grained policy difficult.

## Decision
Project domain-typed operations (for example `github.merge_pr`, `system.restart_service`, `docker.restart_container`) rather than unrestricted `execute_shell(command)`.

## Consequences
More wrappers/schemas to maintain; much stronger policy, audit and validation.

## Alternatives
Generic shell with prompt guidance; raw CLI pass-through.

## Security implications
Reduces shell injection, parameter smuggling and privilege ambiguity; internal wrappers still require strict escaping/allowlists.

## Operational implications
Tool coverage grows incrementally; unsupported operations may use agentic mode without expanding projected shell authority.

## Open questions
Which narrowly-scoped internal command runner primitives are acceptable beneath wrappers.
