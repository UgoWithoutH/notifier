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

Re-added 2026-09-09 (was previously disabled/commented out for Swaper),
then REWORKED 2026-09-10 (cron-job.org's account is capped at 100 API
requests/day, and the earlier design burned through that budget fast):
`ensure_schedule()` now only PATCHes cron-job.org when `mode` actually
differs from the last known mode in `state_file` - staying in the same mode
across runs costs zero API calls. The cron-job.org schedule itself is now a
plain, FIXED interval per mode (every 2min / every 30min, no jitter) - the
anti-robotic-cadence randomness moved to `apply_startup_jitter()` instead,
a short random sleep each monitor calls once at the very start of its own
`run()`, sized from the SAME JITTER_RANGE_MINUTES_2M/30M ranges (now in
minutes of SLEEP, not minutes added to the cron timer).

Required env var (missing -> calls are logged and skipped, never raise):
    CRON_JOB_API_KEY
Optional:
    CRON_JOB_TIMEZONE (default Europe/Paris)
"""

import json
import logging
import os
import random
import time
from pathlib import Path
from urllib import request, error

from shared.state import load_state, save_state

log = logging.getLogger("cron_schedule")

CRON_JOB_API_KEY = os.environ.get("CRON_JOB_API_KEY")
CRON_JOB_TIMEZONE = os.environ.get("CRON_JOB_TIMEZONE", "Europe/Paris")

DEFAULT_STATE = {"cron_schedule_mode": None, "cron_schedule_minutes": None}

# Base interval (in minutes) for each mode - solde >= 10 -> fast poll
# ("2m"), solde < 10 -> slow poll ("30m"). The cron-job.org schedule itself
# is now built at exactly this interval, no jitter (see apply_startup_jitter()
# below for where the randomness moved to).
BASE_INTERVAL_MINUTES = {"2m": 2, "30m": 30}

# Random sleep (whole minutes, INCLUSIVE range), applied ONCE at the start
# of a monitor's own run() via apply_startup_jitter() - two SEPARATE ranges,
# one per mode, per explicit user request ("deux random différent... chacun
# aurait leur fourchette"). No longer added to the cron-job.org schedule
# itself (that's now a plain fixed interval, to avoid rebuilding/re-PATCHing
# it on every run just to re-randomize it). Adjust these two constants
# directly to change the sleep range.
JITTER_RANGE_MINUTES_2M = (0, 3)
JITTER_RANGE_MINUTES_30M = (0, 10)


def _build_fixed_minutes(mode: str) -> list:
    """Builds a plain, regularly-spaced list of minutes-of-hour (0-59) for
    the given mode, at exactly that mode's own base interval - no jitter
    (see the module docstring for why the randomness moved elsewhere)."""
    base = BASE_INTERVAL_MINUTES[mode]
    return list(range(0, 60, base))


def apply_startup_jitter(state_file: Path) -> None:
    """Sleeps once, for a random whole-minute duration drawn from the
    jitter range of the LAST KNOWN cron schedule mode (read from
    `state_file` - the same one passed to `ensure_schedule()`; falls back
    to the "30m" range if no mode has ever been recorded yet). Meant to be
    called once, right at the very start of a monitor's own `run()`
    (before login/any real work) - this is where the anti-robotic-cadence
    randomness now lives, since the cron-job.org schedule itself is a
    plain fixed interval (see module docstring)."""
    state = load_state(state_file, DEFAULT_STATE)
    mode = state.get("cron_schedule_mode") or "30m"
    jitter_range = JITTER_RANGE_MINUTES_2M if mode == "2m" else JITTER_RANGE_MINUTES_30M
    delay_minutes = random.randint(*jitter_range)
    if delay_minutes <= 0:
        return
    log.info("Startup jitter: sleeping %sm (mode=%s) before proceeding...", delay_minutes, mode)
    time.sleep(delay_minutes * 60)


# One short retry on a 429 (cron-job.org's own per-account rate limit,
# hit occasionally since Swaper/Lendermarket can both call this API in a
# short window) before giving up and leaving the schedule unchanged.
RATE_LIMIT_RETRY_DELAY_SECONDS = 15


def _patch_schedule(cron_job_id: str, minutes: list) -> bool:
    if not CRON_JOB_API_KEY or not cron_job_id:
        log.info("CRON_JOB_API_KEY or cron job id missing, skipping cron-job.org update.")
        return False

    endpoint = f"https://api.cron-job.org/jobs/{cron_job_id}"
    payload = {"job": {"schedule": {"timezone": CRON_JOB_TIMEZONE, "minutes": minutes}}}

    for attempt in range(1, 3):
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
            if exc.code == 429 and attempt == 1:
                log.warning(
                    "cron-job.org update rate-limited (HTTP 429), retrying once in %ss...",
                    RATE_LIMIT_RETRY_DELAY_SECONDS,
                )
                time.sleep(RATE_LIMIT_RETRY_DELAY_SECONDS)
                continue
            log.warning("cron-job.org update failed (HTTP %s). Response: %s", exc.code, details[:400])
            return False
        except Exception:
            log.exception("cron-job.org update failed.")
            return False

    return False


def ensure_schedule(mode: str, cron_job_id: str, state_file: Path) -> None:
    """PATCHes cron-job.org's schedule to a fixed `mode` ("30m" or "2m")
    ONLY when it differs from the last known mode in `state_file` - staying
    in the same mode across runs makes zero API calls, to stay well under
    cron-job.org's 100-requests/day account cap (re-added 2026-09-10, after
    the brief 2026-09-09 "always rebuild+PATCH" design blew through that
    budget)."""
    if mode not in BASE_INTERVAL_MINUTES:
        raise ValueError(f"Unknown cron schedule mode: {mode!r}")

    state = load_state(state_file, DEFAULT_STATE)
    current_mode = state.get("cron_schedule_mode")

    if current_mode == mode:
        log.info("Cron decision: already in mode=%s, skipping cron-job.org API call.", mode)
        return

    old_minutes = state.get("cron_schedule_minutes")
    log.info("Cron timer BEFORE update: minutes=%s (last known mode=%s).", old_minutes, current_mode)

    new_minutes = _build_fixed_minutes(mode)
    log.info("Cron decision: mode changed %s -> %s, updating cron-job.org (new minutes=%s).", current_mode, mode, new_minutes)
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
