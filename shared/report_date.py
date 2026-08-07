"""Shared helper letting every *_diversification.py script (and
google_sheet.py's current-month-column selection) optionally treat a
caller-supplied date as "today" instead of the real current date.

Wired up via the REPORT_DATE environment variable, itself fed by the
`report_date` workflow_dispatch input on .github/workflows/diversification.yml
(so a manual run of that workflow can be pointed at a specific past/future
date, e.g. to backfill a month whose automated run failed) - falls back to
the real current date when REPORT_DATE is unset/empty, which is the
default/normal behavior for every other trigger (cron-job.org's
workflow_dispatch calls, which don't set this input, and any local run).
Expected format is the French "JJ/MM/AAAA" (e.g. "15/03/2026"), matching
the workflow input's own format, NOT ISO "YYYY-MM-DD".

NOTE (OUTDATED, kept only for history - see 2026-08-06 below): this used to
say monefit_diversification.py's "this month" figures came from
intercepting the Monefit site's own live request with no date-range
control. That was true of the original Playwright version only -
monefit_diversification.py was rewritten to pure HTTP on 2026-07-18 and now
builds its own `dateFrom`/`dateTo` request, fully respecting REPORT_DATE
like every other *_diversification.py.

FEATURE 2026-08-06: `is_current_month()` (below) also gates whether a
platform's "total" (account balance) is written for a REPORT_DATE-backfilled
month, since most platforms' "total" only ever comes from a LIVE-only
snapshot (no historical figure for a past month). Monefit and Go & Grow are
the two confirmed exceptions (their statement endpoints expose a real
per-date/per-entry closing balance) - see their own module docstrings and
`fill_current_month_amounts()`'s `skip_total` param in shared/google_sheet.py.
Every other *_diversification.py still skips "total" entirely for a
backfilled month. lande_diversification.py/mintos_diversification.py are
NOT invoked by diversification.yml's month-range loop at all (Lande is
local-only, Mintos needs MINTOS_PHPSESSID/MINTOS_MW_SESSION_ID refreshed by
hand) but were checked too, for whoever runs them manually with REPORT_DATE
set to a past date: Lande's "total" already comes from its own tax-report
PDF for the SAME requested period (a real historical figure, no skip_total
needed at all - see its module docstring), while Mintos has no such
endpoint (its accounts/978 + user/overview balances are live-only) and
keeps skipping "total"/"en cours prêts"/"en cours obligations" for a
backfilled month, same as every other unsupported platform.

UPDATE 2026-08-07: PeerBerry added as a third confirmed exception -
its account-summary API's own `closingBalance` field (as of the requested
`endDate`) is used as the backfilled month's "total" - see
peerberry_diversification.fetch_current_month_statement_totals(). Loanch
was checked too but has NO confirmed historical balance field (its
statement-report API only returns total_interest/total_bonus, and its only
balance figure, `total_invested` on /api/v1/dashboard, is live-only, same
situation as Mintos) - it still skips "total" for a backfilled month.
"""

import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

REPORT_DATE_ENV_VAR = "REPORT_DATE"
REPORT_DATE_FORMAT = "%d/%m/%Y"  # French "JJ/MM/AAAA", e.g. "15/03/2026"
REPORT_DATE_MONTHS_ENV_VAR = "REPORT_DATE_MONTHS"  # comma-separated REPORT_DATE values


def get_report_date() -> date:
    """Return the date to treat as "today": REPORT_DATE (env var, French
    "JJ/MM/AAAA", e.g. "15/03/2026") if set and non-empty, else the real
    current date."""
    raw = os.environ.get(REPORT_DATE_ENV_VAR, "").strip()
    if not raw:
        return date.today()
    return datetime.strptime(raw, REPORT_DATE_FORMAT).date()


def get_report_now(tz: ZoneInfo) -> datetime:
    """Same as get_report_date(), but returns a tz-aware datetime (at
    midnight) in the given timezone - a drop-in replacement for
    `datetime.now(tz)` in every *_diversification.py's "this month" date
    range calculations."""
    d = get_report_date()
    return datetime(d.year, d.month, d.day, tzinfo=tz)


def is_current_month() -> bool:
    """True if get_report_date() falls in the REAL current (wall-clock)
    month/year - False when REPORT_DATE points at a past/future month
    (e.g. a month-range backfill run). Used to skip writing "live snapshot"
    figures (account balance/total, geographic repartition) for a
    backfilled month, since those aren't real historical data for that
    month - only the date-range-computed interest/bonus figures are."""
    report = get_report_date()
    today = date.today()
    return (report.year, report.month) == (today.year, today.month)


def get_report_date_list() -> list[str]:
    """Returns the REPORT_DATE ("JJ/MM/AAAA") values to process, one per
    call to a *_diversification.py's run(), for the *_get_session.py
    manual-login helpers that support backfilling several months in one
    run while logging in/capturing the session only ONCE (see
    run_manual_platform.ps1's -StartMonth/-EndMonth). Reads
    REPORT_DATE_MONTHS (comma-separated) if set/non-empty, else falls back
    to a single-item list built from REPORT_DATE (possibly ""), matching
    get_report_date()'s own single-date fallback to today."""
    raw = os.environ.get(REPORT_DATE_MONTHS_ENV_VAR, "").strip()
    if raw:
        return [d.strip() for d in raw.split(",") if d.strip()]
    return [os.environ.get(REPORT_DATE_ENV_VAR, "").strip()]
