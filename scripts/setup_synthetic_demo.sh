#!/bin/zsh
# Seed the isolated, synthetic-only data store used by the recorded demo.
set -euo pipefail

if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
  print -u2 "ANTHROPIC_API_KEY is required. Export it in this terminal, then run this script again."
  exit 1
fi

ROOT="${PERSONAL_RECORDS_HOME:-/private/tmp/personal-records-demo-20260716}"
RECORDS_BIN="${RECORDS_BIN:-$(cd "$(dirname "$0")/.." && pwd)/.venv/bin/records}"
export PERSONAL_RECORDS_HOME="$ROOT"

"$RECORDS_BIN" ingest examples/motor_policy_schedule.txt
"$RECORDS_BIN" ingest examples/motor_renewal_quote.txt
"$RECORDS_BIN" ingest examples/motor_policy_wording.txt
"$RECORDS_BIN" ingest examples/multicover_renewal_invitation.txt

print "\n--- Demo state ---"
"$RECORDS_BIN" policies
"$RECORDS_BIN" renewals
"$RECORDS_BIN" review
