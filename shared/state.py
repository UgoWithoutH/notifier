"""Generic JSON state persistence, shared by the loan monitors (Swaper,
Lendermarket, ...) to track state across runs (e.g. notification gates,
session markers) without spamming on every run.

Each monitor keeps its own state file and its own default dict; this module
only handles the read/merge/write mechanics.
"""

import json
import logging
from pathlib import Path

log = logging.getLogger("state")


def load_state(state_file: Path, default: dict) -> dict:
    """Load a JSON state dict, merged over `default` so missing/new keys are
    backfilled. Falls back to a fresh copy of `default` if the file is
    missing or unreadable."""
    if not state_file.exists():
        return dict(default)
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        return {**default, **data}
    except (json.JSONDecodeError, OSError):
        log.warning("Could not read state file %s, starting fresh.", state_file)
        return dict(default)


def save_state(state_file: Path, state: dict) -> None:
    state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")

