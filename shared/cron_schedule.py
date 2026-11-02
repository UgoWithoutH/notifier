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

Intervals (2026-11): the interval of a schedule is no longer limited to the
legacy "2m"/"30m"/"24h" modes - `ensure_schedule_interval()` takes any number
of minutes (typically read from the "config robots" sheet through
robot_config.schedule_minutes(), written as "5min", "1h 5min", "2j"... see
parse_duration_minutes()). The last applied interval lives in the state file
("cron_schedule_interval_minutes"; older files only have the mode label).
cron-job.org only takes minutes/hours/days LISTS, so only some intervals are
exact (divisors of 60 min, whole hours dividing 24, whole days); any other
value is snapped to the nearest exact one with a warning.
"""

import json
import logging
import os
import random
import re
import time
from pathlib import Path
from urllib import request, error

from shared.state import load_state, save_state

log = logging.getLogger("cron_schedule")

CRON_JOB_API_KEY = os.environ.get("CRON_JOB_API_KEY")
CRON_JOB_TIMEZONE = os.environ.get("CRON_JOB_TIMEZONE", "Europe/Paris")

DEFAULT_STATE = {
    "cron_schedule_mode": None,
    "cron_schedule_minutes": None,
    "cron_schedule_interval_minutes": None,
    # True/False = the balance state ("solde suffisant" / "insuffisant") the
    # recorded interval was chosen for; None = unknown (older state files).
    "cron_schedule_sufficient": None,
}

# Legacy fixed modes (still used by PeerBerry): name -> interval in minutes.
BASE_INTERVAL_MINUTES = {"2m": 2, "30m": 30}

# Daily (and multi-day) schedules fire at DAILY_HOUR:00 (cron-job.org timezone).
DAILY_MODE = "24h"
DAILY_HOUR = 8
DAY_MINUTES = 1440
MAX_INTERVAL_DAYS = 31
SCHEDULE_MODES = (*BASE_INTERVAL_MINUTES, DAILY_MODE)

# Startup jitter (see apply_startup_jitter()): a random sleep of 0..N whole
# minutes, N = interval // 2 capped at MAX_JITTER_MINUTES (so a 2 min cadence
# gets 0-1 min, a 30 min or slower cadence 0-15 min).
MAX_JITTER_MINUTES = 15

# Duration format accepted in the "config robots" sheet: one or more
# "<number><unit>" parts, e.g. "5min", "1h 5min", "2j", "1j 2h 30min", "1h30".
# A number without unit counts as minutes ("90" = 90 min, "1h30" = 1h 30min).
_UNIT_MINUTES = {
    "m": 1, "mn": 1, "min": 1, "mins": 1, "minute": 1, "minutes": 1,
    "h": 60, "hr": 60, "heure": 60, "heures": 60, "hour": 60, "hours": 60,
    "j": DAY_MINUTES, "d": DAY_MINUTES, "jour": DAY_MINUTES, "jours": DAY_MINUTES,
    "day": DAY_MINUTES, "days": DAY_MINUTES,
}
_DURATION_PART = re.compile(r"(\d+(?:[.,]\d+)?)\s*([a-z]*)")


def parse_duration_minutes(raw) -> int | None:
    """'5min' -> 5, '1h 5min' -> 65, '2j' -> 2880, '1h30' -> 90, '90' -> 90.
    Empty, zero or unparsable text -> None."""
    text = str(raw or "").casefold().replace("\u00a0", " ").replace("\u202f", " ").strip()
    if not text:
        return None
    parts = list(_DURATION_PART.finditer(text))
    leftover = _DURATION_PART.sub(" ", text)
    if not parts or re.sub(r"\b(et|and)\b|[\s+,&]", "", leftover):
        return None
    total = 0.0
    for part in parts:
        unit = part.group(2)
        if unit and unit not in _UNIT_MINUTES:
            return None
        total += float(part.group(1).replace(",", ".")) * _UNIT_MINUTES.get(unit, 1)
    minutes = round(total)
    return minutes if minutes > 0 else None


def format_duration(minutes: int) -> str:
    """65 -> '1h 5min', 2880 -> '2j' (same format as parse_duration_minutes)."""
    days, rest = divmod(int(minutes), DAY_MINUTES)
    hours, mins = divmod(rest, 60)
    return " ".join(
        f"{value}{unit}" for value, unit in ((days, "j"), (hours, "h"), (mins, "min")) if value
    ) or "0min"


def _schedulable_intervals() -> list:
    """Intervals cron-job.org can run EXACTLY: a divisor of 60 min, a whole
    number of hours dividing 24, or a whole number of days (<= 31)."""
    minutes = [n for n in range(1, 61) if 60 % n == 0]
    hours = [60 * n for n in range(2, 25) if 24 % n == 0]
    days = [DAY_MINUTES * n for n in range(2, MAX_INTERVAL_DAYS + 1)]
    return sorted({*minutes, *hours, *days})


SCHEDULABLE_INTERVALS = _schedulable_intervals()


def snap_interval_minutes(minutes: int) -> int:
    """Nearest interval cron-job.org's minutes/hours/days lists can express
    exactly (e.g. 7 min -> 6, 45 min -> 30, 100 min -> 120); ties go to the
    shorter interval."""
    return min(SCHEDULABLE_INTERVALS, key=lambda value: (abs(value - minutes), value))


def interval_from_mode(mode) -> int | None:
    """Interval in minutes of a mode label ('2m', '30m', '24h', '1h 5min')."""
    return parse_duration_minutes(mode)


def _build_schedule(interval: int) -> dict:
    """minutes/hours/mdays lists for a (schedulable) interval. Every key is
    always sent so leaving a pattern restores the others to 'every' (-1)."""
    if interval < 60:
        return {"minutes": list(range(0, 60, interval)), "hours": [-1], "mdays": [-1]}
    if interval < DAY_MINUTES:
        step = interval // 60
        return {"minutes": [0], "hours": [-1] if step == 1 else list(range(0, 24, step)), "mdays": [-1]}
    days = interval // DAY_MINUTES
    mdays = [-1] if days == 1 else list(range(1, 32, days))
    return {"minutes": [0], "hours": [DAILY_HOUR], "mdays": mdays}


def apply_startup_jitter(state_file: Path, wait_range: tuple | None = None) -> None:
    """Sleeps once, for a random whole-minute duration drawn from `wait_range`
    (low, high in minutes, "Temps attente avant démarrage" bornes of the
    "config robots" sheet) when given,
    else from the jitter range of the LAST KNOWN cron interval (read from `state_file` -
    the same one passed to `ensure_schedule_interval()`; falls back to the
    30 min range if none has ever been recorded yet). Meant to be called
    once, right at the very start of a monitor's own `run()` (before
    login/any real work) - this is where the anti-robotic-cadence
    randomness lives, since the cron-job.org schedule itself is a plain
    fixed interval (see module docstring)."""
    if wait_range is not None:
        low, high = wait_range
        delay_minutes = random.randint(low, high)
        if delay_minutes <= 0:
            return
        log.info("Startup wait: sleeping %sm (random within %s-%s) before proceeding...", delay_minutes, format_duration(low), format_duration(high))
        time.sleep(delay_minutes * 60)
        return
    state = load_state(state_file, DEFAULT_STATE)
    interval = _last_interval(state) or 30
    delay_minutes = random.randint(0, min(MAX_JITTER_MINUTES, interval // 2))
    if delay_minutes <= 0:
        return
    log.info("Startup jitter: sleeping %sm (interval=%s) before proceeding...", delay_minutes, format_duration(interval))
    time.sleep(delay_minutes * 60)


def _last_interval(state: dict) -> int | None:
    """Last interval applied on cron-job.org, from the state (older state
    files only hold the mode label, e.g. '2m' / '30m' / '24h')."""
    value = state.get("cron_schedule_interval_minutes")
    if isinstance(value, int) and value > 0:
        return value
    return interval_from_mode(state.get("cron_schedule_mode"))


# One short retry on a 429 (cron-job.org's own per-account rate limit,
# hit occasionally since Swaper/Lendermarket can both call this API in a
# short window) before giving up and leaving the schedule unchanged.
RATE_LIMIT_RETRY_DELAY_SECONDS = 15


def _patch_schedule(cron_job_id: str, schedule: dict) -> bool:
    if not CRON_JOB_API_KEY or not cron_job_id:
        log.info("CRON_JOB_API_KEY or cron job id missing, skipping cron-job.org update.")
        return False

    endpoint = f"https://api.cron-job.org/jobs/{cron_job_id}"
    payload = {"job": {"schedule": {"timezone": CRON_JOB_TIMEZONE, **schedule}}}

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
    """Legacy fixed modes ("2m", "30m", "24h"), see ensure_schedule_interval()."""
    if mode not in SCHEDULE_MODES:
        raise ValueError(f"Unknown cron schedule mode: {mode!r}")
    ensure_schedule_interval(interval_from_mode(mode), cron_job_id, state_file)


def ensure_schedule_interval(
    interval_minutes: int, cron_job_id: str, state_file: Path, sufficient: bool | None = None,
) -> None:
    """PATCHes cron-job.org's schedule to a plain fixed interval ONLY when it
    differs from the last interval recorded in `state_file` - staying on the
    same interval across runs makes zero API calls, to stay well under
    cron-job.org's 100-requests/day account cap (re-added 2026-09-10, after
    the brief 2026-09-09 "always rebuild+PATCH" design blew through that
    budget). An interval cron-job.org cannot express exactly (e.g. 7 min) is
    snapped to the nearest one that it can (see snap_interval_minutes()).
    `sufficient` (balance state the interval was chosen for) is recorded in
    the state file so a later run can re-check the right sheet value before
    its balance is known."""
    interval = snap_interval_minutes(max(1, int(interval_minutes)))
    if interval != interval_minutes:
        log.warning(
            "Cron interval %s cannot be scheduled exactly on cron-job.org, using %s instead.",
            format_duration(interval_minutes), format_duration(interval),
        )

    state = load_state(state_file, DEFAULT_STATE)
    current = _last_interval(state)

    if current == interval:
        log.info("Cron decision: already every %s, skipping cron-job.org API call.", format_duration(interval))
        if sufficient is not None and state.get("cron_schedule_sufficient") != sufficient:
            state["cron_schedule_sufficient"] = sufficient
            save_state(state_file, state)
        return

    old_minutes = state.get("cron_schedule_minutes")
    log.info("Cron timer BEFORE update: minutes=%s (last known interval=%s).", old_minutes, format_duration(current) if current else None)

    schedule = _build_schedule(interval)
    log.info(
        "Cron decision: interval changed %s -> %s, updating cron-job.org (new schedule=%s).",
        format_duration(current) if current else None, format_duration(interval), schedule,
    )
    if _patch_schedule(cron_job_id, schedule):
        state["cron_schedule_mode"] = format_duration(interval)
        state["cron_schedule_interval_minutes"] = interval
        state["cron_schedule_minutes"] = schedule["minutes"]
        if sufficient is not None:
            state["cron_schedule_sufficient"] = sufficient
        save_state(state_file, state)
        log.info("Cron timer AFTER update: every %s (minutes=%s) - was minutes=%s.", format_duration(interval), schedule["minutes"], old_minutes)
    else:
        log.warning("Cron decision: UPDATE failed (target interval=%s, timer unchanged: minutes=%s).", format_duration(interval), old_minutes)



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
