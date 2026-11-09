"""Day-by-day balance reconstruction for platforms whose statement endpoint
only returns aggregates for a queried date range (Iuvo, Lendermarket,
Monefit) but accepts a single-day range.

Querying the endpoint once per calendar day (cached permanently in a
per-platform state file, only the last couple of days are re-fetched on
each run) gives real dates for every deposit/withdrawal and real daily
balances, instead of the coarser "(opening+closing)/2 per month" and "net
flow on the 15th" approximations.

Each platform supplies a `fetch_day(day) -> dict` returning a JSON-ready
record with these keys:

- `net_deposit`  : deposits - withdrawals of that day.
- `value_change` : change of the TOTAL account value caused by the day's
                   statement (net deposit + interest + bonus - fees).
                   Only needed when the platform has no `total_close`.
- `total_close`  : (optional) whole-account value at the end of the day,
                   when the platform reports it directly (Monefit).
- `wallet_close` : (optional) uninvested wallet at the end of the day, or
                   None when the platform shows nothing for that day (Iuvo
                   returns 0.0 for a day without any row - it must then be
                   forward-filled from the last known day).
- `wallet_open`  : (optional) opening wallet, only used for a consistency
                   warning.

`build_daily_balances()` then rebuilds one value per day: total (backward
from today's live total unless `total_close` is available, same principle
as the monthly `_invested_balance_at_month_end()` helpers), wallet
(forward-filled) and invested (= total - wallet).
"""

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

from shared.state import load_state, save_state
from shared.weighted_average import compute_time_weighted_average

log = logging.getLogger("daily_balances")

DAILY_CACHE_SCHEMA_VERSION = 1
_ACTIVITY_KEYS = ("deposits", "withdrawals", "opening_balance", "closing_balance")


def first_active_day(monthly_summaries: dict, activity_keys=_ACTIVITY_KEYS) -> date | None:
    """First day of the first cached "YYYY-MM" month showing any activity (None if there is none)."""
    keys = [k for k, s in (monthly_summaries or {}).items() if any(s.get(name) for name in activity_keys)]
    if not keys:
        return None
    year, month = (int(part) for part in min(keys).split("-"))
    return date(year, month, 1)


def refresh_daily_cache(
    state_file: Path, fetch_day: Callable[[date], dict], first_day: date, last_day: date,
    refetch_last_days: int = 2, save_every: int = 15,
) -> dict[date, dict]:
    """Return `{day: record}` for every day in [first_day, last_day], fetching only the days not already
    cached (the last `refetch_last_days` days are always re-fetched: the current day is still partial).
    Progress is saved every `save_every` fetched days, so an interrupted run resumes where it stopped;
    any exception from `fetch_day` is re-raised after saving what was fetched so far."""
    state = load_state(state_file, {"days": {}})
    cached = (state.get("days") or {}) if state.get("schema_version") == DAILY_CACHE_SCHEMA_VERSION else {}
    refetch_from = last_day - timedelta(days=refetch_last_days - 1)
    # Days after last_day (a backfilled past month) stay in the file untouched.
    days = {k: v for k, v in cached.items() if date.fromisoformat(k) < refetch_from or date.fromisoformat(k) > last_day}

    missing = [
        first_day + timedelta(days=n) for n in range((last_day - first_day).days + 1)
        if (first_day + timedelta(days=n)).isoformat() not in days
    ]
    log.info("Daily cache %s: %d day(s) cached, %d to fetch.", state_file.name, len(days), len(missing))

    def _save() -> None:
        save_state(state_file, {"schema_version": DAILY_CACHE_SCHEMA_VERSION, "days": dict(sorted(days.items()))})

    fetched = 0
    try:
        for day in missing:
            days[day.isoformat()] = fetch_day(day)
            fetched += 1
            if fetched % save_every == 0:
                _save()
    finally:
        if fetched:
            _save()
    return {date.fromisoformat(k): v for k, v in days.items() if date.fromisoformat(k) <= last_day}


@dataclass
class DailyBalances:
    total: dict[date, float]
    wallet: dict[date, float]
    invested: dict[date, float]
    net_deposits: dict[date, float]
    inception_day: date  # first day with a positive net deposit

    def average(self, name: str, start: date, end: date) -> float | None:
        """Day-weighted average of the "total"/"wallet"/"invested" series over [start, end] (inclusive,
        end-of-day values). None if the whole period precedes the account's first deposit."""
        if end < self.inception_day:
            return None
        series = getattr(self, name)
        events = []
        previous = 0.0
        for day in sorted(series):
            events.append((day, series[day] - previous))
            previous = series[day]
        return compute_time_weighted_average(events, start, end)

    def cashflows(self, upto: date) -> list[tuple[date, float]]:
        """Real-dated XIRR flows (money put in is negative), without the final terminal value."""
        return [(d, -v) for d, v in sorted(self.net_deposits.items()) if d <= upto and abs(v) >= 0.005]


def build_daily_balances(days: dict[date, dict], last_day: date, live_total: float | None = None) -> DailyBalances | None:
    """Build the daily total/wallet/invested series from `days` (see module docstring). `live_total` (the
    account's total value on `last_day`) is required unless every record carries `total_close`."""
    if not days:
        return None
    first_day = min(days)
    all_days = [first_day + timedelta(days=n) for n in range((last_day - first_day).days + 1)]
    net_deposits = {d: days[d]["net_deposit"] for d in all_days if d in days}
    deposit_days = [d for d, v in net_deposits.items() if v > 0.005]
    if not deposit_days:
        return None
    inception_day = min(deposit_days)

    if all(days[d].get("total_close") is not None for d in days) and last_day in days:
        total, last_known = {}, 0.0
        for d in all_days:
            last_known = days[d]["total_close"] if d in days else last_known
            total[d] = last_known
    else:
        if live_total is None:
            raise ValueError("live_total is required when the daily records carry no total_close.")
        total = {last_day: live_total}
        for d in reversed(all_days[1:]):
            total[d - timedelta(days=1)] = total[d] - (days[d]["value_change"] if d in days else 0.0)
        residual = total[inception_day - timedelta(days=1)] if inception_day > first_day else total[first_day]
        log.info(
            "Daily reconstruction: total value just before the first deposit (%s) = %.2f EUR (0 if the ledger is complete).",
            inception_day, residual,
        )

    wallet: dict[date, float] = {}
    last_wallet = 0.0
    mismatches = 0
    for d in all_days:
        record = days.get(d) or {}
        if record.get("wallet_close") is not None:
            opening = record.get("wallet_open")
            if opening is not None and abs(opening - last_wallet) > 0.05:
                mismatches += 1
            last_wallet = record["wallet_close"]
        wallet[d] = last_wallet
    if mismatches:
        log.warning("Daily wallet: %d day(s) whose opening balance differs from the previous known closing balance.", mismatches)

    # Before the first deposit nothing is held: the backward walk would otherwise leave a small residual.
    for d in all_days:
        if d < inception_day:
            total[d] = 0.0
            wallet[d] = 0.0
    invested = {d: max(total[d] - wallet[d], 0.0) for d in all_days}
    return DailyBalances(total=total, wallet=wallet, invested=invested, net_deposits=net_deposits, inception_day=inception_day)


def previous_month_range(day: date) -> tuple[date, date]:
    """(first day, last day) of the calendar month before `day`'s month."""
    end = day.replace(day=1) - timedelta(days=1)
    return end.replace(day=1), end
