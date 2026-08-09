# Bridge change records

This directory is the canonical JDS-002 ledger for maintenance changes discovered after a candidate or accepted release exists.

Create `CHG-BRIDGE-<NNN>.yaml` when a validation observation, production incident, security finding, compatibility change or engineering review requires a product change.

The record must classify the change as one of:

`HOTFIX`, `BUGFIX`, `HARDENING`, `IMPROVEMENT`, `COMPATIBILITY`, `SECURITY_FIX`, `BREAKING_CHANGE`, or `DOC_ONLY`.

A non-blocking improvement may be `DEFER`red to a later release. A release already accepted under an existing identity is never silently modified.

There are no active Bridge change records at the initial JDS-002 adoption point. Existing historical V2 development PRs are not retroactively rewritten into change records.
