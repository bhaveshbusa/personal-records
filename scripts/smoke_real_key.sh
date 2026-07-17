#!/usr/bin/env bash
# Full real-key regression — the complete Phase 2R checklist in one command,
# against the live model:
#
#   1. policy_schedule twin  -> accepted (PolicyFiled), shows in `policies`
#   2. same file again       -> duplicate (never re-calls the LLM)
#   3. renewal quote         -> accepted, links to the schedule's entity
#   4. ask: quote comparison -> quoted-vs-current with delta (2R.5 gate)
#   5. MultiCover golden     -> review; confirm -> motor+home in `renewals`
#   6. policy wording        -> stored+indexed; ask windscreen (2R.4 gate)
#   7. discard schedule + MultiCover -> policy retracts; prior quote revives
#   8. records eval          -> first real per-stage accuracy numbers
#
# Runs against a THROWAWAY data home (PERSONAL_RECORDS_HOME=$(mktemp -d)) —
# your real ~/.personal-records/ is never touched. The temp home is kept at
# the end so you can inspect events/telemetry; delete it when done.
#
# Usage:  bash scripts/smoke_real_key.sh          (from the repo root)
# Needs:  ANTHROPIC_API_KEY set; ~40 LLM calls on claude-sonnet-5 (cents).

set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

bold() { printf '\n\033[1m%s\033[0m\n' "$*"; }
die()  { printf '\nSMOKE FAILED — %s\n' "$*" >&2; exit 1; }

# --- Preflight -------------------------------------------------------------
[ -f "evals/cases.json" ] || die "run from the repo root (evals/cases.json not found)"
[ -n "${ANTHROPIC_API_KEY:-}" ] || die "ANTHROPIC_API_KEY is not set"

if [ -e .git/index.lock ] || [ -e .git/HEAD.lock ]; then
  printf 'note: stale git lock files present (left by a sandboxed session).\n'
  printf '      git will not work until you: rm -f .git/HEAD.lock .git/index.lock .git/objects/maintenance.lock\n'
  printf '      (does not affect this smoke run)\n'
fi

if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi
python3 -c "import anthropic" 2>/dev/null \
  || die "the 'anthropic' package is not importable — activate the venv or: pip install -e ."

export PYTHONPATH=src
export PERSONAL_RECORDS_HOME
PERSONAL_RECORDS_HOME="$(mktemp -d /tmp/records-smoke.XXXXXX)"
printf 'Throwaway data home: %s\n' "$PERSONAL_RECORDS_HOME"
printf 'Model: %s\n' "${RECORDS_MODEL:-claude-sonnet-5 (default)}"

records() { python3 -m records.cli.main "$@"; }

# run <expect-substring> <cmd...>: run, echo output, assert, return output
OUT=""
run() {
  local expect="$1"; shift
  OUT="$("$@" 2>&1)"
  printf '%s\n' "$OUT" | sed 's/^/  | /'
  case "$OUT" in
    *"$expect"*) ;;
    *) die "expected output to contain '$expect' (command: $*)" ;;
  esac
}
last_doc_id() { printf '%s\n' "$OUT" | head -1 | awk '{print $NF}'; }

# --- 1. policy_schedule twin -> accepted -----------------------------------
bold "1/8  ingest policy_schedule twin (expect: accepted, PolicyFiled)"
run "accepted" records ingest examples/motor_policy_schedule.txt
SCHEDULE_DOC_ID="$(last_doc_id)"
run "SwiftSure" records policies

# --- 2. duplicate ingest ----------------------------------------------------
bold "2/8  ingest the same file again (expect: duplicate, zero LLM calls)"
run "duplicate" records ingest examples/motor_policy_schedule.txt

# --- 3. renewal quote links to the schedule's entity ------------------------
bold "3/8  ingest renewal quote (expect: accepted — +7.5% is inside the ±40% band)"
run "accepted" records ingest examples/motor_renewal_quote.txt
QUOTE_DOC_ID="$(last_doc_id)"
run "motor" records renewals

# --- 4. quote comparison (2R.5 gate) ----------------------------------------
bold "4/8  ask: how does this quote compare? (expect: quoted vs current + delta)"
run "" records ask "how does this quote compare to my current policy?"

# --- 5. MultiCover golden case (the reason this system exists) ---------------
bold "5/8  ingest MultiCover invitation (expect: review — multi-line, zero events)"
run "review" records ingest examples/multicover_renewal_invitation.txt
MULTICOVER_DOC_ID="$(last_doc_id)"
bold "     confirm from review (expect: motor + home RenewalAccepted)"
run "confirmed" records review --confirm "$MULTICOVER_DOC_ID"
run "home" records renewals

# --- 6. wording Q&A (2R.4 gate) ----------------------------------------------
bold "6/8  ingest policy wording (expect: stored + indexed)"
run "stored" records ingest examples/motor_policy_wording.txt
bold "     ask the windscreen question (expect: covered, §1.1 verbatim citation)"
run "1.1" records ask "am I covered for a cracked windscreen?"
bold "     ask an out-of-scope question (expect: refusal, not a guess)"
run "" records ask "am I covered for jetski damage?"

# --- 7. discard retracts (2R.2 gate) -----------------------------------------
bold "7/8  discard the schedule (expect: gone from policies; log keeps both)"
run "retracted" records discard "$SCHEDULE_DOC_ID" --reason "smoke: retraction check"
run "no current policy records" records policies
bold "     discard MultiCover (expect: earlier RenewalProposed becomes visible again)"
run "retracted" records discard "$MULTICOVER_DOC_ID" --reason "smoke: reveal prior proposal"
run "${QUOTE_DOC_ID:0:12}" records renewals

# --- 8. eval harness — the real per-stage numbers (2R.6) ---------------------
bold "8/8  records eval (15 cases, ~30 LLM calls — the first live measurement)"
if records eval; then EVAL_RC=0; else EVAL_RC=$?; fi
[ -f evals/results.csv ] || die "eval did not write evals/results.csv"

# --- Summary ------------------------------------------------------------------
bold "SMOKE RUN COMPLETE"
CALLS="$(wc -l < "$PERSONAL_RECORDS_HOME/telemetry.jsonl" 2>/dev/null | tr -d ' ')"
printf '  LLM calls (steps 1-7, telemetry): %s\n' "${CALLS:-?}"
printf '  Eval results: evals/results.csv (exit %s — nonzero means some case failed a stage;\n' "$EVAL_RC"
printf '    that is a finding about the model/prompts, not a broken run — read the CSV)\n'
printf '  Inspect the throwaway home, then delete it:\n'
printf '    ls %s\n' "$PERSONAL_RECORDS_HOME"
printf '    rm -rf %s\n' "$PERSONAL_RECORDS_HOME"
