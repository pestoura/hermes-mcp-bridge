# V2 Phase 2 — progressive tool disclosure in the shadow

> **REMEDIATION · NOT AN ACCEPTANCE DECLARATION**
>
> This document records a real connected blocker and its fix. It does not
> declare Phase 2 accepted. V1 and the frozen 27-tool contract are unchanged.

## Observed blocker

A real connected Jarvas run of the pinned inner launcher failed closed with:

```text
{"gate":"DIRECT_READ_BLOCKED","reason":"PROVENANCE_UNAUTHORIZED_TOOL_CALL"}
```

The outer out-of-band runner had previously collapsed the same failure into the
generic `FINAL_INNER_LAUNCHER_FAILED`, so the underlying cause was invisible in
the sanitized final marker.

## Root cause

Current Hermes ships **progressive tool disclosure** (`tools.tool_search`,
default `enabled: "auto"`). When at least one deferrable tool exists, every
MCP/plugin tool is removed from the model-facing tools array and replaced by
three bridge tools:

```text
tool_search   tool_describe   tool_call
```

Core Hermes tools are never deferred — but in this shadow home *every*
authorized tool is an MCP tool. The default therefore deferred all five
`phase2-read` tools, and the shadow agent reached GitHub through the generic
bridge instead of the authorized surface.

`tool_provenance` then did exactly what it is designed to do: the recovered
tool call was not in the authorized set, so it raised
`PROVENANCE_UNAUTHORIZED_TOOL_CALL`. The gate was correct; the shadow was not a
faithful V1 comparison path.

## Fix

1. `scripts/v2_phase2_prepare_shadow_home.py` writes
   `tools.tool_search.enabled = "off"` into the disposable shadow config. This
   only removes an indirection layer; it does not widen the tool surface, and
   the exact five-tool `include` allowlist is unchanged.
2. `scripts/v2_phase2_probe_shadow_runtime.py` proves deferral is inert by
   loading the **installed** `tools.tool_search.load_config()` under the shadow
   HOME, rather than trusting the config it just wrote. A non-`off` value fails
   closed with the stable code `SHADOW_TOOL_DEFERRAL_ACTIVE`.
3. The sanitized isolation contract gains one new boolean,
   `tool_deferral_disabled`, and the schema is bumped to
   `hermes-v2-phase2-shadow-isolation/3`. The field is required: a payload that
   omits it fails validation rather than validating silently.

## Non-goals

* No acceptance criterion is relaxed. Exact `_get_platform_tools()` proof, zero
  enabled native toolsets, exact `phase2-read` MCP config, 15-sample semantic
  matching, provenance and the OUTER state-integrity gate all remain mandatory.
* No path, token, prompt or raw output enters evidence.
