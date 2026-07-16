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

NOTE: monefit_diversification.py's "this month" interest/statement figures
are fetched by letting the Monefit site make its OWN request (already
defaulting to its live "current month") and intercepting the response -
there's no query-param-based date range to redirect there (see that
module's docstring), so REPORT_DATE only affects WHICH Google Sheet
column monefit's real (always current-month-to-date) figures get written
to, not what data is actually fetched. Every other *_diversification.py
computes its own explicit date range and fully respects REPORT_DATE.
"""

import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

REPORT_DATE_ENV_VAR = "REPORT_DATE"
REPORT_DATE_FORMAT = "%d/%m/%Y"  # French "JJ/MM/AAAA", e.g. "15/03/2026"


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
