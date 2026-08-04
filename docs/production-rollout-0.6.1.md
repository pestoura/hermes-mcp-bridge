# Production rollout runbook — Hermes MCP Bridge 0.6.1

## Purpose

Use this runbook when promoting the runtime `0.6.1` harness fix for approval ID handling. It adds an operational gate that verifies `hermes_approval_create -> hermes_approval_status` with a strict parser, preventing the false negative caused by extra characters, quotes, or shell coercion.

## Safety boundaries

- Do not modify production `.env`, Cloudflare, RITMO, or Hermes core.
- Do not approve or consume any approval created by this gate.
- Do not use `jq` without `-r`, `repr`, `str(payload)`, or shell parsing to extract `approval_id`.
- Do not rebuild the runtime image unless explicitly instructed.

## Approval gate (mandatory)

```bash
python scripts/approval_smoke.py --url http://127.0.0.1:8765/mcp
```

Required outcome:
- exit code `0`;
- JSON output with `decision=requested`;
- `approval_id` is masked in output; no raw secrets printed.

If the script exits non-zero, stop the rollout and inspect the emitted payload. Do not retry with ad-hoc parsing.

## Smoke inventory

```bash
python scripts/smoke_test.py --url http://127.0.0.1:8765/mcp
```

Expected:
- exactly 26 MCP tools;
- `hermes_approval_create`, `hermes_approval_status`, `hermes_recent_runs` present;
- `schema_version=0.6.1`, bridge `state_registry` `up`.

## Approval ID parser contract

`src/hermes_mcp_bridge/approval_parser.py` defines the canonical parser. Supported shapes:
- `structuredContent` / `structured_content` dict;
- payload dict with `approval_id` or wrapped in `{"result": {...}}`;
- text content with valid JSON.

Rejected:
- strings with surrounding quotes, whitespace, newlines;
- raw JSON serialization, dict/list where `approval_id` is not a plain `str`;
- values not matching `^[A-Za-z0-9][A-Za-z0-9._:\-]{0,159}$`.

## Fallback prohibition

Do not introduce `jq`, `repr`, `str(payload)`, or shell parsing for `approval_id` in scripts or runbooks. Use `extract_approval_id_from_mcp_result` only.
