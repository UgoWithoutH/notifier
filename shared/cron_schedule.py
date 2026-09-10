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

Re-added 2026-09-09 (was previously disabled/commented out for Swaper).
`ensure_schedule()` unconditionally rebuilds a freshly-jittered minutes list
and PATCHes it to cron-job.org on EVERY call (no "already in this mode,
skip" check) - the actual minutes list for each mode is never a fixed,
perfectly regular cadence ([0, 30] / every 2 minutes), a random jitter (in
whole minutes) is added to the base 2min/30min interval on every rebuild,
so the external trigger doesn't fire at an obviously robotic, perfectly-even
cadence. See JITTER_RANGE_MINUTES_2M / JITTER_RANGE_MINUTES_30M below -
deliberately hardcoded constants (not env vars) so they're trivial to tweak
directly in code.

Required env var (missing -> calls are logged and skipped, never raise):
    CRON_JOB_API_KEY
Optional:
    CRON_JOB_TIMEZONE (default Europe/Paris)
"""

import json
import logging
import os
import random
from pathlib import Path
from urllib import request, error

from shared.state import load_state, save_state

log = logging.getLogger("cron_schedule")

CRON_JOB_API_KEY = os.environ.get("CRON_JOB_API_KEY")
CRON_JOB_TIMEZONE = os.environ.get("CRON_JOB_TIMEZONE", "Europe/Paris")

DEFAULT_STATE = {"cron_schedule_mode": None, "cron_schedule_minutes": None}

# Base interval (in minutes) for each mode, before jitter is added -
# solde >= 10 -> fast poll ("2m"), solde < 10 -> slow poll ("30m").
BASE_INTERVAL_MINUTES = {"2m": 2, "30m": 30}

# Random jitter (whole minutes, INCLUSIVE range) added on top of each mode's
# own base interval every time a schedule is actually (re)built - two
# SEPARATE ranges, one per mode, per explicit user request ("deux random
# différent... chacun aurait leur fourchette"). E.g. (0, 3) for "2m" means
# each step in the built minutes list is somewhere between 2 and 5 minutes
# apart, never a perfectly even "every 2 minutes" heartbeat. Adjust these
# two constants directly to change the randomness range - if
# JITTER_RANGE_MINUTES_30M ever grows large enough that a "30m" schedule can
# produce >=6 firings/hour, also revisit `_infer_mode_from_minutes()`'s
# count-based threshold below.
JITTER_RANGE_MINUTES_2M = (0, 3)
JITTER_RANGE_MINUTES_30M = (0, 10)


def _build_jittered_minutes(mode: str) -> list:
    """Builds an irregularly-spaced list of minutes-of-hour (0-59) for the
    given mode: starts at 0, then repeatedly advances by (that mode's base
    interval + a freshly-drawn random jitter from its own range) until past
    59 - deliberately NOT a perfectly even cadence, see the module docstring
    and JITTER_RANGE_MINUTES_* constants above."""
    base = BASE_INTERVAL_MINUTES[mode]
    jitter_range = JITTER_RANGE_MINUTES_2M if mode == "2m" else JITTER_RANGE_MINUTES_30M
    minutes = []
    m = 0
    while m < 60:
        minutes.append(m)
        step = base + random.randint(*jitter_range)
        m += max(step, 1)
    return minutes


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
    """Unconditionally (re)builds a freshly-jittered minutes list for `mode`
    ("30m" or "2m") and PATCHes it to cron-job.org every single call - no
    "already in this mode, skip" check anymore (per explicit user request),
    since skipping meant the schedule kept the SAME stale jittered minutes
    list run after run instead of actually re-randomizing it each time."""
    if mode not in BASE_INTERVAL_MINUTES:
        raise ValueError(f"Unknown cron schedule mode: {mode!r}")

    state = load_state(state_file, DEFAULT_STATE)

    old_minutes = state.get("cron_schedule_minutes")
    log.info("Cron timer BEFORE update: minutes=%s (last known mode=%s).", old_minutes, state.get("cron_schedule_mode"))

    new_minutes = _build_jittered_minutes(mode)
    log.info("Cron decision: updating to mode=%s (new minutes=%s).", mode, new_minutes)
    if _patch_schedule(cron_job_id, new_minutes):
        state["cron_schedule_mode"] = mode
        state["cron_schedule_minutes"] = new_minutes
        save_state(state_file, state)
        log.info("Cron timer AFTER update: minutes=%s (mode=%s) - was minutes=%s.", new_minutes, mode, old_minutes)
    else:
        log.warning("Cron decision: UPDATE failed (target_mode=%s, timer unchanged: minutes=%s).", mode, old_minutes)



def set_job_enabled(cron_job_id: str, enabled: bool) -> bool:
    """Enable/disable a cron-job.org job outright (its `job.enabled` flag),
    as opposed to `ensure_schedule()` which only ever changes HOW OFTEN an
    enabled job fires. Used by monitors that now poll continuously inside
    a single long-running invocation (e.g. swaper_monitor.py's invest loop,
    added 2026-08-01) - the external cron-job.org trigger is disabled for
    the duration of that loop (no need for it to fire a second, overlapping
    run) and re-enabled once the loop stops, success or timeout. Returns
    True on a confirmed API success, False otherwise (missing API key/job
    id, or the request itself failed) - callers should treat False as
    "best effort, not guaranteed" and log accordingly, never raise.
    """
    if not CRON_JOB_API_KEY or not cron_job_id:
        log.info("CRON_JOB_API_KEY or cron job id missing, skipping cron-job.org enable/disable.")
        return False

    endpoint = f"https://api.cron-job.org/jobs/{cron_job_id}"
    payload = {"job": {"enabled": enabled}}
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
                log.info("cron-job.org job %s %s.", cron_job_id, "enabled" if enabled else "disabled")
                return True
            log.warning("cron-job.org enable/disable returned unexpected HTTP status %s.", resp.status)
            return False
    except error.HTTPError as exc:
        details = ""
        try:
            details = exc.read().decode("utf-8", errors="ignore")
        except Exception:
            pass
        log.warning("cron-job.org enable/disable failed (HTTP %s). Response: %s", exc.code, details[:400])
    except Exception:
        log.exception("cron-job.org enable/disable failed.")

    return False
