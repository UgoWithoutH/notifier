"""Persistence helpers for tracking the loan-monitor's state across runs.

Stores a small dict in a JSON file, notably `seen_loan_ids`: the loan IDs
already notified about while the uninvested balance was > 0. This is reset
to empty whenever the balance drops back to 0 (money is no longer available
to invest, so past listings become irrelevant), and lets us detect a genuinely
new loan even if the total available count never actually reaches 0 (e.g. one
loan disappears exactly as a different one appears).
"""

import json
import logging
from pathlib import Path

log = logging.getLogger("swaper_monitor")

DEFAULT_STATE = {"seen_loan_ids": []}


def load_state(state_file: Path) -> dict:
    if not state_file.exists():
        return dict(DEFAULT_STATE)
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        return {**DEFAULT_STATE, **data}
    except (json.JSONDecodeError, OSError):
        log.warning("Could not read state file %s, starting fresh.", state_file)
        return dict(DEFAULT_STATE)


def save_state(state_file: Path, state: dict) -> None:
    state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
