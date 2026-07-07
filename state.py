"""Persistence helpers for tracking the loan-monitor's state across runs.

Stores a small dict in a JSON file, notably:
- `notified_for_positive_cycle`: whether a notification has already been sent
    for the current positive-balance cycle.
- `cycle_balance_marker`: last seen rounded balance while in a positive cycle.

The notification gate is reset when balance goes back to 0 or when the balance
value changes, which avoids spam while still allowing a fresh alert on a new
funding level.
"""

import json
import logging
from pathlib import Path

log = logging.getLogger("swaper_monitor")

DEFAULT_STATE = {
    "notified_for_positive_cycle": False,
    "cycle_balance_marker": None,
    "cron_schedule_mode": None,
}


def load_state(state_file: Path) -> dict:
    if not state_file.exists():
        return dict(DEFAULT_STATE)
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        merged = {**DEFAULT_STATE, **data}

        # Backward-compatible migration from legacy boolean flags.
        if merged.get("cron_schedule_mode") is None:
            if merged.get("cron_set_30m_for_zero_balance"):
                merged["cron_schedule_mode"] = "30m"
            elif merged.get("cron_set_2m_for_positive_balance"):
                merged["cron_schedule_mode"] = "2m"

        return merged
    except (json.JSONDecodeError, OSError):
        log.warning("Could not read state file %s, starting fresh.", state_file)
        return dict(DEFAULT_STATE)


def save_state(state_file: Path, state: dict) -> None:
    state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
