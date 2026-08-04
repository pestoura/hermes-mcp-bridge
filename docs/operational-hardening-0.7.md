# Operational hardening — Hermes MCP Bridge

This feature branch adds canonical backup/restore and secret rotation utilities for the bridge state database and environment secrets.

## Scope

- `src/hermes_mcp_bridge/state_backup.py` — online SQLite backup and restore.
- `src/hermes_mcp_bridge/secret_rotation.py` — secret discovery, rotation planning, and safe application.
- `tests/test_state_backup.py` — backup/restore regression tests.
- `tests/test_secret_rotation.py` — secret rotation regression tests.
- `docs/state-backup.md`, `docs/secret-rotation.md` — runbooks.
- `README.md` — operational notes.

## Status

WIP. Do not merge. See campaign checklist in `docs/`.

## Checklist

- [ ] Full gate: compileall, ruff, pytest, git diff --check.
- [ ] Secret audit: no plaintext keys, full hashes, or `.env` in diff.
- [ ] CI green on this branch.
- [ ] PM/registry approval before merge.

## Notes

- No production service changes, `.env` rewrites, or restarts in this branch.
- All secrets are handled as digests; values are never logged or asserted by tests.
