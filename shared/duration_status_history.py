"""Daily "normaux / retards / défauts" shares and their monthly averages, used by
the "durée" Google Sheet tab (see shared.google_sheet.fill_remaining_duration_amounts).

Every run records, for the day it runs, the share (fraction of the outstanding
amount) of normal / late / defaulted loans per loan originator (or for the
whole platform). Those daily shares are kept in a small JSON state file per
platform (persisted between GitHub Actions runs with actions/cache, like the
XIRR cashflow state files) and averaged per calendar month for the sheet.

Duration terms follow shared.google_sheet's convention: (months, days) >= 0 is
a normal loan, a negative term is a late loan, (months, days, "default") is a
defaulted loan.
"""

import json
import logging
import re
import unicodedata
from datetime import date
from pathlib import Path

log = logging.getLogger("duration_status_history")

DEFAULT_HORIZON_MONTHS = 12
PLATFORM_KEY = "__platform__"
STATE_DIR = Path(__file__).resolve().parents[1] / "diversification"

FRENCH_MONTHS = (
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
)

_YEARS_RE = re.compile(r"(\d+)\s*(?:ans?|années?|annees?)\b", re.IGNORECASE)
_MONTHS_RE = re.compile(r"(\d+)\s*mois\b", re.IGNORECASE)
_BARE_NUMBER_RE = re.compile(r"^\s*(\d+)\s*$")


def parse_horizon_months(text: str):
    """"1 an" -> 12, "1 an 6 mois" -> 18, "18 mois" -> 18, "18" -> 18. None if unreadable or zero."""
    text = (text or "").strip()
    bare = _BARE_NUMBER_RE.match(text)
    if bare:
        months = int(bare.group(1))
    else:
        years = sum(int(n) for n in _YEARS_RE.findall(text))
        months = years * 12 + sum(int(n) for n in _MONTHS_RE.findall(text))
    return months or None


def month_label(year: int, month: int) -> str:
    return f"{FRENCH_MONTHS[month - 1]} {year}"


def parse_month_label(text: str):
    """"octobre 2026" -> (2026, 10); None if the text isn't a month label."""
    match = re.match(r"^\s*([^\W\d_]+)\s+(\d{4})\s*$", text or "")
    if not match or match.group(1).casefold() not in FRENCH_MONTHS:
        return None
    return int(match.group(2)), FRENCH_MONTHS.index(match.group(1).casefold()) + 1


def window_months(today: date, horizon_months: int) -> list:
    """The `horizon_months` calendar months ending with today's, newest first, as (year, month)."""
    index = today.year * 12 + today.month - 1
    return [(year, month + 1) for year, month in (divmod(index - offset, 12) for offset in range(horizon_months))]


def _window_start(today: date, horizon_months: int) -> date:
    year, month = window_months(today, horizon_months)[-1]
    return date(year, month, 1)


def classify_term(term: tuple) -> str:
    """"default", "late" or "normal"."""
    if len(term) > 2:
        return "default"
    return "late" if term[0] < 0 or term[1] < 0 else "normal"


def status_shares(durations: dict):
    """{term: amount} -> (normal, late, default) fractions of the total, or None if the total isn't positive."""
    totals = {"normal": 0.0, "late": 0.0, "default": 0.0}
    for term, amount in durations.items():
        totals[classify_term(term)] += float(amount)
    total = sum(totals.values())
    if total <= 0:
        return None
    return (totals["normal"] / total, totals["late"] / total, totals["default"] / total)


def merge_durations(durations_by_key: dict) -> dict:
    merged = {}
    for durations in durations_by_key.values():
        for term, amount in durations.items():
            merged[term] = merged.get(term, 0.0) + float(amount)
    return merged


def state_path(platform: str) -> Path:
    ascii_name = unicodedata.normalize("NFKD", platform.replace("&", "and")).encode("ascii", "ignore").decode()
    return STATE_DIR / f"{re.sub(r'[^a-z0-9]', '', ascii_name.lower())}_duration_status_state.json"


def load_history(path: Path) -> tuple:
    """(days, names): days = {"YYYY-MM-DD": {key: [normal, late, default]}}, names = {key: display name}.
    Both empty if the file is missing/unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, {}
    days, names = data.get("days", {}), data.get("names", {})
    return (days if isinstance(days, dict) else {}), (names if isinstance(names, dict) else {})


def save_history(path: Path, days: dict, names: dict) -> None:
    path.write_text(json.dumps({"days": days, "names": names}, indent=1, sort_keys=True), encoding="utf-8")


def record_day(days: dict, names: dict, day: date, shares_by_key: dict, names_by_key: dict, today: date, horizon_months: int) -> None:
    """Stores `day`'s shares ({key: (normal, late, default)}) and the display names, and drops the days older
    than the horizon window (and the names no longer present in any remaining day)."""
    days[day.isoformat()] = {key: list(shares) for key, shares in shares_by_key.items()}
    names.update({key: name for key, name in names_by_key.items() if key in shares_by_key})
    oldest = _window_start(today, horizon_months).isoformat()
    for stale in [d for d in days if d < oldest]:
        del days[stale]
    tracked = {key for shares in days.values() for key in shares}
    for key in [key for key in names if key not in tracked]:
        del names[key]


def tracked_originators(path: Path, today: date, horizon_months: int) -> dict:
    """{key: display name} of the originators that still have shares recorded inside the horizon window, so
    their rows stay in the sheet even when they no longer have any loan."""
    days, names = load_history(path)
    oldest = _window_start(today, horizon_months).isoformat()
    keys = {key for day, shares in days.items() if day >= oldest for key in shares if key != PLATFORM_KEY}
    return {key: names.get(key, key) for key in keys}


def monthly_samples(days: dict, key: str, year: int, month: int) -> list:
    """[(day, [normal, late, default])] recorded for `key` during the month, in date order."""
    prefix = f"{year:04d}-{month:02d}-"
    return sorted((day, shares[key]) for day, shares in days.items() if day.startswith(prefix) and key in shares)


def monthly_average(days: dict, key: str, year: int, month: int):
    """Mean of each daily share of `key` over the recorded days of the month: (normal, late, default) or None."""
    samples = [shares for _, shares in monthly_samples(days, key, year, month)]
    if not samples:
        return None
    return tuple(sum(sample[i] for sample in samples) / len(samples) for i in range(3))
