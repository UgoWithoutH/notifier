"""Generic day-weighted average balance calculator.

Used by every *_diversification.py to compute "solde moyen pondéré
investi"/"solde moyen pondéré non investi" (added to the Google Sheet
2026-09-08, one pair of rows per platform under "Crowdlending"/
"Crowdlending savings"/"Crowdlending agricole"/"Crowdfunding immobilier").
Deliberately kept separate from shared/xirr.py: this has nothing to do
with XIRR/Cash drag and doesn't touch either - it's a plain period-average
calculation, reused as-is for both the invested and non-invested balance.
"""

from datetime import date, timedelta

# Shared Sheet row labels for the two rows written by every
# *_diversification.py - keep these in sync with the actual Sheet cell
# text (matched case-insensitively as a substring, see
# shared/google_sheet.py's find_rows_by_texts_below) so a typo in one
# platform file can't silently stop matching just for that platform.
INVESTED_BALANCE_LABEL = "solde moyen pondéré investi"
NON_INVESTED_BALANCE_LABEL = "solde moyen pondéré non investi"


def compute_time_weighted_average(
    events: list[tuple[date, float]], start_date: date, end_date: date, opening_balance: float = 0.0,
) -> float:
    """Day-weighted average of a running balance over [start_date, end_date]
    (both inclusive).

    `events` : every dated capital movement affecting the balance being
    averaged (e.g. deposits/withdrawals for a cash balance, or new-
    investment/repayment amounts for an invested balance), as
    `(date, signed_delta)` pairs - a POSITIVE delta increases the balance,
    a NEGATIVE delta decreases it. May include events dated before
    `start_date` (needed to carry the running balance INTO the period,
    applied automatically) and after `end_date` (ignored) - callers don't
    need to pre-filter, just pass the platform's full known history.
    Several events on the same date are summed together automatically.

    `opening_balance` : the true balance immediately before the EARLIEST
    known event (typically account inception, where the real balance is
    genuinely 0.0 - the default). Do NOT use this for "the balance right
    before `start_date`" - that must be represented by `events` dated
    before `start_date` instead, otherwise a first reporting month with no
    prior data would silently use the wrong constant for every day before
    the first real event.

    Convention: a movement dated `d` is considered part of the balance
    STARTING `d` itself (inclusive) - the balance used for day `d` already
    reflects it.

    Handles every case correctly:
    - 28/29/30/31-day months and leap years: the denominator is the real
      `(end_date - start_date).days + 1`, never a hardcoded 30.
    - Deposits and withdrawals: any signed delta.
    - Several movements the same day: summed into one delta first.
    - A month with no movement: the running balance stays constant
      (carried in from before `start_date`) for the whole period.
    - The very first month with no previous balance: `opening_balance`
      (0.0 by default) is carried forward for every day before the first
      real event, correctly included in the average (not skipped).

    Returns 0.0 if `end_date < start_date` (degenerate range).
    """
    total_days = (end_date - start_date).days + 1
    if total_days <= 0:
        return 0.0

    deltas_by_date: dict = {}
    for event_date, delta in events:
        if event_date > end_date:
            continue
        deltas_by_date[event_date] = deltas_by_date.get(event_date, 0.0) + delta

    # Fold in every event dated strictly before start_date to get the
    # running balance carried INTO the reporting period.
    running_balance = opening_balance
    for event_date in sorted(d for d in deltas_by_date if d < start_date):
        running_balance += deltas_by_date[event_date]

    total_balance = 0.0
    current = start_date
    while current <= end_date:
        running_balance += deltas_by_date.get(current, 0.0)
        total_balance += running_balance
        current += timedelta(days=1)

    return total_balance / total_days
