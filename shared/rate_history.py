"""Daily weighted rate / weighted duration and their monthly averages, used by the
"taux" Google Sheet tab (see shared.rate_sheet.fill_rate_amounts).

Every run records, for the day it runs, per loan originator (or for the whole platform):
- the amount-weighted average yield (%) of its loans ;
- the amount-weighted average base duration (months) of its loans.
Each day is stored as four sums so that a month can be aggregated exactly (amount-weighted over every
recorded day, not a mean of means): [sum(amount x rate), sum(amount with a rate), sum(amount x months),
sum(amount with a base duration)]. They are kept in a small JSON state file per platform (persisted between
GitHub Actions runs with actions/cache, like shared.duration_status_history's files).
"""

import json
import re
import unicodedata
from datetime import date
from pathlib import Path

from shared.duration_status_history import (  # noqa: F401 - partagés avec l'onglet "durée"
    DEFAULT_HORIZON_MONTHS,
    PLATFORM_KEY,
    STATE_DIR,
    _window_start,
    month_label,
    parse_horizon_months,
    parse_month_label,
    window_months,
)
from shared.remaining_duration import term_in_months


def state_path(platform: str) -> Path:
    ascii_name = unicodedata.normalize("NFKD", platform.replace("&", "and")).encode("ascii", "ignore").decode()
    return STATE_DIR / f"{re.sub(r'[^a-z0-9]', '', ascii_name.lower())}_rate_history_state.json"


def load_history(path: Path) -> tuple:
    """(days, names): days = {"YYYY-MM-DD": {key: [rate_sum, rate_base, duration_sum, duration_base]}},
    names = {key: display name}. Both empty if the file is missing/unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, {}
    days, names = data.get("days", {}), data.get("names", {})
    return (days if isinstance(days, dict) else {}), (names if isinstance(names, dict) else {})


def save_history(path: Path, days: dict, names: dict) -> None:
    path.write_text(json.dumps({"days": days, "names": names}, indent=1, sort_keys=True), encoding="utf-8")


def loan_sums(loans) -> list | None:
    """[(base_term, amount, rate_percent or None)] -> [rate_sum, rate_base, duration_sum, duration_base], or
    None when there is no loan with a positive amount."""
    rate_sum = rate_base = duration_sum = duration_base = 0.0
    for base_term, amount, rate in loans:
        if amount <= 0:
            continue
        duration_sum += amount * term_in_months(base_term)
        duration_base += amount
        if rate is not None:
            rate_sum += amount * rate
            rate_base += amount
    if duration_base <= 0:
        return None
    return [round(rate_sum, 6), round(rate_base, 6), round(duration_sum, 6), round(duration_base, 6)]


def record_day(days: dict, names: dict, day: date, sums_by_key: dict, names_by_key: dict, today: date, horizon_months: int) -> None:
    """Stores `day`'s sums ({key: [..4 sums..]}) and the display names, and drops the days older than the
    horizon window (and the names no longer present in any remaining day)."""
    days[day.isoformat()] = {key: list(sums) for key, sums in sums_by_key.items()}
    names.update({key: name for key, name in names_by_key.items() if key in sums_by_key})
    oldest = _window_start(today, horizon_months).isoformat()
    for stale in [d for d in days if d < oldest]:
        del days[stale]
    tracked = {key for sums in days.values() for key in sums}
    for key in [key for key in names if key not in tracked]:
        del names[key]


def tracked_originators(path: Path, today: date, horizon_months: int) -> dict:
    """{key: display name} of the originators that still have data recorded inside the horizon window, so their
    rows stay in the sheet even when they no longer have any loan."""
    days, names = load_history(path)
    oldest = _window_start(today, horizon_months).isoformat()
    keys = {key for day, sums in days.items() if day >= oldest for key in sums if key != PLATFORM_KEY}
    return {key: names.get(key, key) for key in keys}


def monthly_samples(days: dict, key: str, year: int, month: int) -> list:
    """[(day, sums)] recorded for `key` during the month, in date order."""
    prefix = f"{year:04d}-{month:02d}-"
    return sorted((day, sums[key]) for day, sums in days.items() if day.startswith(prefix) and key in sums)


def monthly_average(days: dict, key: str, year: int, month: int):
    """Amount-weighted average over the recorded days of the month: (rate in %, duration in months), each None
    when no loan of the month carries it; None when nothing was recorded."""
    samples = [sums for _, sums in monthly_samples(days, key, year, month)]
    if not samples:
        return None
    totals = [sum(sample[i] for sample in samples) for i in range(4)]
    rate = totals[0] / totals[1] if totals[1] > 0 else None
    duration = totals[2] / totals[3] if totals[3] > 0 else None
    return rate, duration
