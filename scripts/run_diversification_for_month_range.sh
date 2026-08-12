#!/usr/bin/env bash
# Runs a *_diversification.py module once per month in an inclusive
# MM/AAAA..MM/AAAA range, setting REPORT_DATE to the LAST day of each month
# (so each run's "this month to date" fetch logic captures the whole
# month). Falls back to a single run with REPORT_DATE unset (today) when
# both start/end are empty, preserving the old single-date behavior.
#
# A single month's run can fail transiently (e.g. a platform's 2FA/TOTP
# endpoint rejecting a fresh login due to its own anti-abuse rate limiting
# after several logins in a row - seen for real with Loanch, all 3 already-
# resilient TOTP candidate codes rejected within ~150ms) - retried once
# after a longer cooldown, then the month is SKIPPED (not fatal) so every
# other month in the range still gets attempted. The script's own exit code
# still reflects whether any month ultimately failed, so a GitHub Actions
# run still shows red when that happens.
set -uo pipefail
RETRY_COOLDOWN_SECONDS=60
any_month_failed=0

MODULE="$1"
START_MONTH="${2:-}"
END_MONTH="${3:-}"

if [[ -z "$START_MONTH" && -z "$END_MONTH" ]]; then
  echo "No month range given - running $MODULE once for the current date."
  python -m "$MODULE"
  exit $?
fi

if [[ -z "$START_MONTH" || -z "$END_MONTH" ]]; then
  echo "start_month and end_month must both be set (or both left empty)." >&2
  exit 1
fi

MONTH_RE='^(0[1-9]|1[0-2])/[0-9]{4}$'
if [[ ! "$START_MONTH" =~ $MONTH_RE ]]; then
  echo "start_month '$START_MONTH' is not in MM/AAAA format." >&2
  exit 1
fi
if [[ ! "$END_MONTH" =~ $MONTH_RE ]]; then
  echo "end_month '$END_MONTH' is not in MM/AAAA format." >&2
  exit 1
fi

start_mm="${START_MONTH%%/*}"
start_yyyy="${START_MONTH##*/}"
end_mm="${END_MONTH%%/*}"
end_yyyy="${END_MONTH##*/}"

current="${start_yyyy}-${start_mm}-01"
end="${end_yyyy}-${end_mm}-01"

current_epoch=$(date -d "$current" +%s)
end_epoch=$(date -d "$end" +%s)

if [[ "$current_epoch" -gt "$end_epoch" ]]; then
  echo "start_month ($START_MONTH) is after end_month ($END_MONTH)." >&2
  exit 1
fi

while [[ "$current_epoch" -le "$end_epoch" ]]; do
  last_day=$(date -d "${current} +1 month -1 day" +%d/%m/%Y)
  month_label=$(date -d "$current" +%m/%Y)
  echo "=== Running $MODULE for $month_label (REPORT_DATE=$last_day) ==="
  if ! REPORT_DATE="$last_day" python -m "$MODULE"; then
    echo "[FAILED] $MODULE failed for $month_label - waiting ${RETRY_COOLDOWN_SECONDS}s (possible transient anti-abuse/rate-limit rejection) then retrying once..." >&2
    sleep "$RETRY_COOLDOWN_SECONDS"
    if ! REPORT_DATE="$last_day" python -m "$MODULE"; then
      echo "[FAILED] $MODULE failed again for $month_label - skipping this month, continuing with the rest of the range." >&2
      any_month_failed=1
    fi
  fi
  current=$(date -d "${current} +1 month" +%Y-%m-01)
  current_epoch=$(date -d "$current" +%s)
  if [[ "$current_epoch" -le "$end_epoch" ]]; then
    # Each iteration logs into the platform fresh (incl. a real 2FA/TOTP
    # submission for platforms that need it) - looping many months
    # back-to-back with zero delay looks like a rapid-fire login pattern
    # that some platforms' anti-abuse/rate-limiting can start rejecting
    # (even genuinely correct TOTP codes), not just a one-off flake.
    sleep 10
  fi
done

if [[ "$any_month_failed" -ne 0 ]]; then
  echo "One or more months failed even after a retry - see the [FAILED] lines above." >&2
  exit 1
fi
