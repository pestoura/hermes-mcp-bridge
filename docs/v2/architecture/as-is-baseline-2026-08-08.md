# AS-IS Baseline — 2026-08-08

> **V2 · PLANNED · NOT IMPLEMENTED · NO IMPACT ON V1**

This file records facts supplied from the 2026-08-08 read-only runtime audit. Credential values are intentionally excluded.

## Hermes Agent

- observed version: `0.20.0`;
- observed default model: `tencent/hy3:free`;
- provider: Nous;
- native toolsets, skills and authenticated integrations are present;
- one internal MCP server was confirmed: `home-assistant` at the audited Home Assistant MCP endpoint;
- approximately 24 Home Assistant MCP tools were observed;
- no internal Hermes MCP server was confirmed for GitHub, Docker, Grafana, Cloudflare or RITMO.

Observed active native toolsets (18): browser, clarify, code_execution, computer_use, context_engine, cronjob, delegation, file, homeassistant, image_gen, memory, session_search, skills, terminal, todo, tts, vision, web. Approximately 250 skills were observed across GitHub/DevOps, n8n, Jira, GitLab, Cloudflare, GCP, Google/Workspace, filesystem/log/network/service operations and others.

## Hermes MCP Bridge

- observed bridge version: `1.0.0`;
- wire schema: `0.6.1`;
- security mode: production;
- ChatGPT currently sees the v1 control-plane tools rather than Hermes terminal/filesystem/CLI/API tools directly.

The observed external surface included: `hermes_submit`, `hermes_prompt`, `hermes_wait`, `hermes_status`, `hermes_stop`, `hermes_health`, `hermes_readiness`, `hermes_recent_runs`, `hermes_capabilities`, `hermes_agent_card`, policy/approval tools, plan/approved execution, checkpoints/continue, saga tools, locks, quotas and result manifests.

## GitHub as-is

GitHub is **not** an internal Hermes MCP server in the audited runtime. The observed pattern is approximately:

```text
Hermes Agent -> GitHub skill -> HY3 -> terminal -> gh/git -> GitHub
```

`gh` was authenticated using a PAT and `GITHUB_TOKEN` was available to scripts. Values are not recorded. Multiple GitHub/DevOps skills were present.

**SKILL != TOOL**: a skill is knowledge/procedure for an agent; it is not itself a deterministic executable function.

## Authenticated integration mechanisms observed

Mechanisms only, never values: GitHub PAT/gh/GITHUB_TOKEN; Home Assistant bearer/MCP; Google OAuth2/refresh token for Gmail/Calendar/Drive; Jira Cloud API token; Cloudflare API token; GCP service accounts; Outlook/SPMS OAuth; n8n API key; IMAP/SMTP; Telegram bot token; Hermes API key; LLM provider credential pool.

The status model is explicitly:

```text
configured != healthy != authorized-for-v2
```

## RITMO

Runtime RITMO integration was **NOT CONFIRMED / NOT PRESENT IN AUDITED RUNTIME**. Documentation/dashboard references do not establish an active MCP, API, token or plugin integration.

## Existing deterministic precedent

Jobs such as `google-token-keepalive.sh` and `hermes-auto-update-safe.sh` were observed using cron -> script -> result without HY3. V2 generalizes this already-valid deterministic execution principle.
