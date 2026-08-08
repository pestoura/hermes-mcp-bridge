# Connected Phase 2 blocker — shadow-home preparation

Date: 2026-08-08

Observed on the real Jarvas connected acceptance run after PR #61 and post-merge CI #230:

```text
{"gate":"DIRECT_READ_BLOCKED","reason":"SHADOW_HOME_PREPARATION_FAILED"}
launcher_rc=2
```

This does not prove the underlying preparation cause. The current launcher invokes `v2_phase2_prepare_shadow_home.py` with stdout discarded and collapses every non-zero exit from that helper to the generic `SHADOW_HOME_PREPARATION_FAILED` code.

The helper itself already emits a sanitized machine-readable contract:

```text
{"status":"SHADOW_HOME_BLOCKED","reason":"<SAFE_CODE>"}
```

No credential values, paths, environment dumps, prompts or raw provider output are required to identify the real blocker.

`DIRECT_READ_ACCEPTED` remains undeclared. Phase 3 remains blocked until the real 15-sample connected gate passes.
