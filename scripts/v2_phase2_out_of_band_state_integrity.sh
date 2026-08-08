#!/usr/bin/env bash
# Out-of-band state-integrity probe scheduler (dry-run by default).
#
# Repo-side foundation only: this wrapper never declares or satisfies the
# Phase 2 connected gate. Without both EXECUTE_OUT_OF_BAND=YES and an explicit
# --confirm flag it only prints the sanitized plan produced by the Python
# planner. Real scheduling is an operator action taken after the Hermes control
# run has ended.
set -Eeuo pipefail
umask 077

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PLANNER="$HERE/v2_phase2_out_of_band_state_integrity.py"
PYTHON="${OOB_PYTHON:-python3}"

UNIT_NAME="${OOB_UNIT_NAME:-hermes-v2-oob-state-integrity}"
DELAY_SECONDS="${OOB_DELAY_SECONDS:-60}"
TIMEOUT_SECONDS="${OOB_TIMEOUT_SECONDS:-120}"
STATE_DB=""
RESULT=""
WORKDIR="${OOB_WORKING_DIRECTORY:-$HOME}"
CONFIRM='no'

blocked() {
  printf '{"state":"FAILED","reason":"%s"}\n' "$1" >&2
  exit 2
}

usage() {
  cat <<'USAGE'
usage: v2_phase2_out_of_band_state_integrity.sh --state-db PATH --result PATH
                                                [--working-directory PATH]
                                                [--unit-name NAME]
                                                [--delay-seconds N]
                                                [--timeout-seconds N]
                                                [--confirm]

Dry-run unless EXECUTE_OUT_OF_BAND=YES and --confirm are both supplied.
No secret material may be passed to this script; it accepts paths only.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --state-db) STATE_DB="${2:-}"; shift 2 ;;
    --result) RESULT="${2:-}"; shift 2 ;;
    --working-directory) WORKDIR="${2:-}"; shift 2 ;;
    --unit-name) UNIT_NAME="${2:-}"; shift 2 ;;
    --delay-seconds) DELAY_SECONDS="${2:-}"; shift 2 ;;
    --timeout-seconds) TIMEOUT_SECONDS="${2:-}"; shift 2 ;;
    --confirm) CONFIRM='yes'; shift ;;
    -h|--help) usage; exit 0 ;;
    *) blocked 'OOB_ARGUMENT_UNKNOWN' ;;
  esac
done

[[ -n "$STATE_DB" ]] || blocked 'OOB_STATE_DB_MISSING'
[[ -n "$RESULT" ]] || blocked 'OOB_RESULT_MISSING'
[[ -f "$PLANNER" ]] || blocked 'OOB_PLANNER_MISSING'

PLAN_JSON="$("$PYTHON" "$PLANNER" plan \
  --unit-name "$UNIT_NAME" \
  --state-db "$STATE_DB" \
  --result "$RESULT" \
  --working-directory "$WORKDIR" \
  --delay-seconds "$DELAY_SECONDS" \
  --timeout-seconds "$TIMEOUT_SECONDS")" || blocked 'OOB_PLAN_FAILED'

printf '%s\n' "$PLAN_JSON"

if [[ "${EXECUTE_OUT_OF_BAND:-}" != 'YES' || "$CONFIRM" != 'yes' ]]; then
  printf '{"mode":"DRY_RUN","executed":false}\n'
  exit 0
fi

command -v systemd-run >/dev/null 2>&1 || blocked 'OOB_SYSTEMD_RUN_MISSING'

# Idempotent cleanup of a previous transient unit before scheduling a new one.
systemctl --user stop "${UNIT_NAME}.timer" >/dev/null 2>&1 || true
systemctl --user stop "${UNIT_NAME}.service" >/dev/null 2>&1 || true

systemd-run --user \
  --unit="$UNIT_NAME" \
  --collect \
  --on-active="${DELAY_SECONDS}s" \
  --timer-property=AccuracySec=1s \
  --property=Type=oneshot \
  --property=Restart=no \
  --property=UMask=0077 \
  --property=RuntimeMaxSec="$TIMEOUT_SECONDS" \
  --property=WorkingDirectory="$WORKDIR" \
  --property=NoNewPrivileges=yes \
  --property=StandardOutput=null \
  --property=StandardError=journal \
  -- "$PYTHON" "$PLANNER" measure --state-db "$STATE_DB" --result "$RESULT" \
  || blocked 'OOB_SCHEDULE_FAILED'

printf '{"mode":"SCHEDULED","executed":true,"unit":"%s"}\n' "$UNIT_NAME"
