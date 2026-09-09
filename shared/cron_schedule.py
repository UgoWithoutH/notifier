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

Re-added 2026-09-09 (was previously disabled/commented out for Swaper) with
two explicit additions requested by the user:
1. Before deciding whether a PATCH is needed, `ensure_schedule()` tries to
   read the job's REAL current schedule straight from cron-job.org's own API
   (`_fetch_current_schedule()`) instead of trusting only the local
   `state_file` cache - the cache is used ONLY as a fallback when that live
   check fails (missing API key/job id, network error, etc.).
2. The actual minutes list for each mode is no longer a fixed, perfectly
   regular cadence ([0, 30] / every 2 minutes) - a random jitter (in whole
   minutes) is added to the base 2min/30min interval every time the
   schedule is (re)built, so the external trigger doesn't fire at an
   obviously robotic, perfectly-even cadence forever. See
   JITTER_RANGE_MINUTES_2M / JITTER_RANGE_MINUTES_30M below - deliberately
   hardcoded constants (not env vars) so they're trivial to tweak directly
   in code.

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


def _infer_mode_from_minutes(minutes: list) -> str | None:
    """Best-effort guess of which mode ("2m"/"30m") a real, possibly-
    jittered `minutes` list (read back from cron-job.org's own API via
    `_fetch_current_schedule()`) corresponds to - the exact list can't be
    compared for equality since it's randomized on every rebuild. A "2m"
    schedule fires roughly 12-30 times/hour; a "30m" one only 1-3 times/hour
    - a simple count-based threshold safely tells them apart given the
    jitter ranges above."""
    if not minutes:
        return None
    return "2m" if len(minutes) >= 6 else "30m"


def _fetch_current_schedule(cron_job_id: str) -> list | None:
    """Best-effort GET of the job's REAL, currently-configured
    `schedule.minutes` straight from cron-job.org's own API - preferred over
    the local state-file cache when available (the cache could in principle
    drift from reality, e.g. someone edited the schedule by hand in the
    cron-job.org console). Returns None (never raises) if the API key/job id
    is missing or the request fails for any reason - callers must fall back
    to the cache in that case."""
    if not CRON_JOB_API_KEY or not cron_job_id:
        return None

    endpoint = f"https://api.cron-job.org/jobs/{cron_job_id}"
    req = request.Request(
        endpoint,
        method="GET",
        headers={"Authorization": f"Bearer {CRON_JOB_API_KEY}"},
    )
    try:
        with request.urlopen(req, timeout=20) as resp:
            if 200 <= resp.status < 300:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("jobDetails", {}).get("schedule", {}).get("minutes")
            log.warning("cron-job.org job lookup returned unexpected HTTP status %s.", resp.status)
    except Exception:
        log.warning("cron-job.org job lookup failed, falling back to the local cache.", exc_info=True)
    return None


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
    schedule mode ("30m" or "2m"). Prefers a LIVE check of the job's real
    current schedule (via `_fetch_current_schedule()`) to decide whether an
    update is actually needed; falls back to the `state_file` cache's own
    `cron_schedule_mode` if that live check isn't possible (missing API
    key/job id, network error). Only calls the PATCH endpoint when the
    (live or cached) current mode differs from `mode`, so repeated calls
    with the same mode are cheap no-ops - when a PATCH does happen, the
    actual minutes list is freshly randomized (see
    `_build_jittered_minutes()`/JITTER_RANGE_MINUTES_* above), not a fixed
    cadence."""
    if mode not in BASE_INTERVAL_MINUTES:
        raise ValueError(f"Unknown cron schedule mode: {mode!r}")

    state = load_state(state_file, DEFAULT_STATE)
    cached_mode = state.get("cron_schedule_mode")

    live_minutes = _fetch_current_schedule(cron_job_id)
    if live_minutes is not None:
        current_mode = _infer_mode_from_minutes(live_minutes)
        log.info(
            "Cron decision context (live-verified): schedule minutes=%s -> current_mode=%s, target_mode=%s",
            live_minutes, current_mode, mode,
        )
    else:
        current_mode = cached_mode
        log.info(
            "Cron decision context (cache fallback, live check unavailable): current_mode=%s, target_mode=%s",
            current_mode, mode,
        )

    if current_mode == mode:
        log.info("Cron already effectively in %s mode, skipping update.", mode)
        return

    new_minutes = _build_jittered_minutes(mode)
    log.info("Cron decision: UPDATE requested (from=%s to=%s, new minutes=%s).", current_mode, mode, new_minutes)
    if _patch_schedule(cron_job_id, new_minutes):
        state["cron_schedule_mode"] = mode
        state["cron_schedule_minutes"] = new_minutes
        save_state(state_file, state)
        log.info("Cron decision: UPDATE success (new_mode=%s).", mode)
    else:
        log.warning("Cron decision: UPDATE failed (target_mode=%s).", mode)



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
