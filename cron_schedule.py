"""Shared cron-job.org schedule coordinator.

Swaper and Lendermarket each run in their own GitHub Actions workflow,
triggered by their own separate cron-job.org job (see
.github/workflows/swaper.yml and .github/workflows/lendermarket.yml), and
both want the same behavior: poll faster while there's a positive balance to
invest (to catch fleeting loan availability), and slower otherwise. This
module owns the mechanics of talking to cron-job.org's API and remembering
which schedule is currently applied - the job ID and the state file are
passed in by each caller since they're per-monitor, only the API key and the
patching mechanics are shared here.

Required env var (missing -> calls are logged and skipped, never raise):
    CRON_JOB_API_KEY
Optional:
    CRON_JOB_TIMEZONE (default Europe/Paris)
"""

import json
import logging
import os
from pathlib import Path
from urllib import request, error

from state import load_state, save_state

log = logging.getLogger("cron_schedule")

CRON_JOB_API_KEY = os.environ.get("CRON_JOB_API_KEY")
CRON_JOB_TIMEZONE = os.environ.get("CRON_JOB_TIMEZONE", "Europe/Paris")

DEFAULT_STATE = {"cron_schedule_mode": None}

# Minute-of-hour lists accepted by cron-job.org's schedule.minutes field.
SCHEDULES = {
    "30m": [0, 30],
    "2m": list(range(0, 60, 2)),
}


def _patch_schedule(cron_job_id: str, minutes: list) -> bool:
    if not CRON_JOB_API_KEY or not cron_job_id:
        log.info("CRON_JOB_API_KEY or cron job id missing, skipping cron-job.org update.")
        return False

    endpoint = f"https://api.cron-job.org/jobs/{cron_job_id}"
    payload = {"job": {"schedule": {"timezone": CRON_JOB_TIMEZONE, "minutes": minutes}}}
    req = request.Request(
        endpoint,
        method="PATCH",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {CRON_JOB_API_KEY}",
            "Content-Type": "application/json",
        },
    )

    try:
        with request.urlopen(req, timeout=20) as resp:
            if 200 <= resp.status < 300:
                return True
            log.warning("cron-job.org update returned unexpected HTTP status %s.", resp.status)
            return False
    except error.HTTPError as exc:
        details = ""
        try:
            details = exc.read().decode("utf-8", errors="ignore")
        except Exception:
            pass
        log.warning("cron-job.org update failed (HTTP %s). Response: %s", exc.code, details[:400])
    except Exception:
        log.exception("cron-job.org update failed.")

    return False


def ensure_schedule(mode: str, cron_job_id: str, state_file: Path) -> None:
    """Make sure cron-job.org's job `cron_job_id` is set to the given
    schedule mode ("30m" or "2m"). Only calls the API when the state
    persisted in `state_file` says it isn't already set that way, so
    repeated calls with the same mode are cheap no-ops."""
    if mode not in SCHEDULES:
        raise ValueError(f"Unknown cron schedule mode: {mode!r}")

    state = load_state(state_file, DEFAULT_STATE)
    current_mode = state.get("cron_schedule_mode")

    log.info("Cron decision context: current_mode=%s, target_mode=%s", current_mode, mode)

    if current_mode == mode:
        log.info("Cron already marked as %s in local state, skipping update.", mode)
        return

    log.info("Cron decision: UPDATE requested (from=%s to=%s).", current_mode, mode)
    if _patch_schedule(cron_job_id, SCHEDULES[mode]):
        state["cron_schedule_mode"] = mode
        save_state(state_file, state)
        log.info("Cron decision: UPDATE success (new_mode=%s).", mode)
    else:
        log.warning("Cron decision: UPDATE failed (target_mode=%s).", mode)
