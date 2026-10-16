"""Calendar-exact remaining term helpers, shared by every *_diversification.py
that fills the "durée" Google Sheet tab (see
shared.google_sheet.fill_remaining_duration_amounts)."""

from datetime import date, timedelta


def _add_months(start: date, months: int) -> date:
    index = start.year * 12 + start.month - 1 + months
    year, month = divmod(index, 12)
    month += 1
    next_month_start = date(year + (month == 12), month % 12 + 1, 1)
    last_day = (next_month_start - timedelta(days=1)).day
    return date(year, month, min(start.day, last_day))


def _months_and_days_between(start: date, end: date) -> tuple:
    months = (end.year - start.year) * 12 + end.month - start.month
    if _add_months(start, months) > end:
        months -= 1
    return (months, (end - _add_months(start, months)).days)


def remaining_months_and_days(today: date, maturity: date) -> tuple:
    """Calendar-exact remaining term as (whole months, leftover days), e.g.
    2026-10-16 -> 2029-07-15 is (32, 29). (0, 0) on the maturity day itself.
    A maturity already past (late loan) gives the lateness with both
    components negated, e.g. 2026-10-16 vs 2026-08-11 is (-2, -5), so late
    terms sort before every non-late one (most late first)."""
    if maturity == today:
        return (0, 0)
    if maturity < today:
        months, days = _months_and_days_between(maturity, today)
        return (-months, -days)
    return _months_and_days_between(today, maturity)


def overdue_months_and_days(today: date, due: date, default: bool = False):
    """Term of a loan that is late (or in default) since the missed payment
    date `due`, as a duration key for the "durée" sheet: late loans use the
    negated (months, days) of remaining_months_and_days() ("2 mois 5 jours de
    retard"), defaulted ones (months, days, "default") ("2 mois 5 jours de
    défaut"). None if `due` isn't in the past (the platform's date can't
    explain any lateness)."""
    if due >= today:
        return None
    months, days = _months_and_days_between(due, today)
    return (months, days, "default") if default else (-months, -days)
