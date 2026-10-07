"""Lendermarket loan availability monitor.

Mirrors swaper_monitor.py's notification-gate logic exactly, with the
account balance as the same outer gate:
  - balance <= 0 -> nothing to notify, regardless of loans; every segment's
    gate is reset (so a fresh alert fires next time money becomes
    available again).
  - balance > 0 -> per loan segment (configured in LOAN_SEGMENTS below,
    mirroring the filtered listing pages the user actually watches): notify
    once while the segment has loans, only reset that segment's gate once
    it drops back to 0 loans. Fluctuations in the loan count while staying
    above 0 do NOT re-open the gate (avoids spamming on every small change)
    - only an actual return to 0 does, exactly like Swaper's balance.

Login + balance lookup is pure HTTP (no browser needed) - verified
2026-07-18 via a real browser network capture that the whole auth flow
(including the TOTP 2FA challenge) has NO reCAPTCHA or other client-side-JS
requirement, unlike Swaper:
  1. GET  /users/v1/auth/getCsrfToken   -> sets XSRF-TOKEN + users_session
     cookies (Laravel/Sanctum-style CSRF).
  2. POST /users/v1/auth/login          json={"email","password"}, header
     `x-xsrf-token: unquote(cookies["XSRF-TOKEN"])`. Response may require a
     TOTP_CHALLENGE step. The XSRF-TOKEN cookie is refreshed on every
     response - always re-read it from the session right before the next
     call, never reuse a stale value.
  3. POST /users/v1/auth/submitTotpChallenge  json={"code": <TOTP>}, same
     xsrf header pattern. Response contains `data.currentInvestor.investorId`
     - required as an `X-INVESTOR-ID` header on every authenticated call
     below (without it: 401 "Unauthenticated" even with valid cookies/xsrf -
     this was the non-obvious missing piece, found via the response's
     `access-control-allow-headers` listing `X-INVESTOR-ID`).
  4. Authenticated GETs (e.g. the account summary balance) need
     x-xsrf-token (refreshed again) + X-INVESTOR-ID.

Checking loan availability itself only needs the public, unauthenticated
`claims/v1/public/getActiveLoans` endpoint - verified on 2026-07-08 by
comparing its JSON output against the real filtered listing pages:
    https://app.lendermarket.com/fr/listes-des-prets/non-reglemente?...
    https://app.lendermarket.com/fr/listes-des-prets/reglemente?...

Required env vars:
    LENDERMARKET_EMAIL, LENDERMARKET_PASSWORD  -> Lendermarket credentials
    SMTP_HOST, SMTP_USER, SMTP_PASSWORD,       -> outgoing mail server
    EMAIL_TO                                   -> notification recipient
Optional:
    SMTP_PORT (default 587), EMAIL_FROM (default SMTP_USER)
    LENDERMARKET_TOTP_SECRET                   -> base32 secret used to set up
                                                   Google Authenticator, needed
                                                   if 2FA is enabled on the account

NOTE: this module used to also have a one-time "invest-structure
exploration" capture (added 2026-07-23, Playwright-based, emailed the raw
HTML/API structure of a selected lender's listing page) meant to help build
a future Lendermarket auto-invest bot. That bot was built and confirmed
working (see the "Lendermarket real auto-invest" section further below/in
repo memory) - the exploration email is now obsolete and was REMOVED
2026-07-26 per explicit user request ("j'ai plus besoin de recevoir la
structure html, je voulais juste notifier des loans qui ont des prêts et du
solde dispo"). This module is now pure HTTP end-to-end again (no
Playwright/browser dependency at all) - only the loan-availability
notification email and the real auto-invest step remain.
"""

import json
import logging
import os
import time
import traceback
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib import request, parse, error
from urllib.parse import unquote

import pyotp
import requests
from dotenv import load_dotenv

from shared.notifier import send_lendermarket_email, send_lendermarket_invest_summary_email
from shared.google_sheet import get_geo_platform_snapshot
from shared.robot_config import (
    Candidate,
    PlatformConfig,
    build_tracker,
    get_platform_config,
    plan_allocations,
)
from shared.state import load_state, save_state
from shared.session_cache import get_or_refresh_session
from shared.notification_gate import should_notify
from shared.cron_schedule import ensure_schedule, apply_startup_jitter

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("lendermarket_monitor")

LENDERMARKET_EMAIL = os.environ.get("LENDERMARKET_EMAIL")
LENDERMARKET_PASSWORD = os.environ.get("LENDERMARKET_PASSWORD")
LENDERMARKET_TOTP_SECRET = os.environ.get("LENDERMARKET_TOTP_SECRET")
LENDERMARKET_CRON_JOB_ID = os.environ.get("LENDERMARKET_CRON_JOB_ID")

API_BASE = "https://api.lendermarket.com"
CSRF_URL = f"{API_BASE}/users/v1/auth/getCsrfToken"
LOGIN_URL = f"{API_BASE}/users/v1/auth/login"
TOTP_URL = f"{API_BASE}/users/v1/auth/submitTotpChallenge"
LOANS_API_URL = f"{API_BASE}/claims/v1/public/getActiveLoans"
BALANCE_API_URL = f"{API_BASE}/ledger/v1/investor/getInvestorAccountSummary"
# Real invest submission call - see module docstring/repo memory
# ("2026-07-24 ... createInvestment") for how this was captured (safe,
# network-intercepted click-through, no real money spent).
INVEST_URL = f"{API_BASE}/claims/v1/investor/createInvestment"

STATE_FILE = Path(__file__).parent / "lendermarket_state.json"
SESSION_STATE_FILE = Path(__file__).parent / "lendermarket_monitor_session_state.json"
CRON_SCHEDULE_STATE_FILE = Path(__file__).parent / "lendermarket_cron_schedule_state.json"
# Diagnostics for the real auto-invest feature (added 2026-07-24) - full
# request/response detail for every real investment attempt, same idea as
# peerberry_invest_bot.py's DIAGNOSTICS_FILE: never printed to stdout, only
# ever attached (this run's own entries) to the invest summary email.
INVEST_DIAGNOSTICS_FILE = Path(__file__).parent / "lendermarket_invest_diagnostics.log"

# Auto-invest is skipped below this amount per loan (same rationale/default
# as PeerBerry's own invest bot) - the platform's own real minimum per
# investment, confirmed by the user 2026-07-24.
MIN_INVESTMENT_AMOUNT = float(os.environ.get("LENDERMARKET_MIN_INVESTMENT_AMOUNT", "10"))

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://app.lendermarket.com/",
    "Accept": "application/json",
    "Content-Type": "application/json",
}

# Verified against the real filtered listing pages on 2026-07-08.
LOAN_SEGMENTS = [
    {
        "key": "non_reglemente",
        "label": "Prêts non réglementés",
        "regulation_status": "UNREGULATED",
        "lenders": [
            "9babf437-5bf8-41fb-840d-6edf7012e408",
            "9babf437-6970-48e6-8175-62ef53465eba",
            "9babf437-6ccb-4ae2-a22f-c887e9e3696c",
        ],
        "max_remaining_term_in_days": 360,
        "page_url": (
            "https://app.lendermarket.com/fr/listes-des-prets/non-reglemente"
            "?lenders=9babf437-5bf8-41fb-840d-6edf7012e408,"
            "9babf437-6970-48e6-8175-62ef53465eba,"
            "9babf437-6ccb-4ae2-a22f-c887e9e3696c&maxRemainingTermInDays=360"
        ),
    },
    {
        "key": "reglemente",
        "label": "Prêts réglementés",
        "regulation_status": "REGULATED",
        "lenders": [
            "9ffdd9b6-bde3-445b-a3df-f2f57b94afe7",
            "9d501521-54f8-4aa2-b975-6b34f8aac5a0",
        ],
        "max_remaining_term_in_days": 360,
        "page_url": (
            "https://app.lendermarket.com/fr/listes-des-prets/reglemente"
            "?lenders=9ffdd9b6-bde3-445b-a3df-f2f57b94afe7,"
            "9d501521-54f8-4aa2-b975-6b34f8aac5a0&maxRemainingTermInDays=360"
        ),
    },
]

# Per-segment notification gate (see notification_gate.py), shared with
# swaper_monitor.py: notify once while a segment has loans, reset only when
# it drops back to 0 (fluctuations while staying above 0 don't re-open it).
DEFAULT_STATE = {
    "gates": {},
}

# Per-lender filter configs for the invest-structure exploration (added
# 2026-07-23) - one entry per lender the user actually watches for a future
# auto-invest bot, selected via the Google Sheet (see
# Per-lender API filter configs - one entry per lender the auto-invest bot can
# handle, selected via the "config robots" sheet (ACTIF = x, see
# shared/robot_config.py). Verified 2026-07-23 against the
# user's own filtered listing URLs - the `lender_id`s reuse the same UUIDs
# already in LOAN_SEGMENTS above, but each has its own (stricter)
# minInterestRate cutoff the aggregate segments above don't apply.
LENDER_INVEST_FILTERS = {
    "Dineo": {
        "lender_id": "9d501521-54f8-4aa2-b975-6b34f8aac5a0",
        "regulation_status": "REGULATED",
        "min_interest_rate": 8,
        "min_remaining_term_in_days": 1,
        "max_remaining_term_in_days": 360,
        "page_url": (
            "https://app.lendermarket.com/fr/listes-des-prets/reglemente"
            "?minInterestRate=8&minRemainingTermInDays=1&maxRemainingTermInDays=360"
            "&lenders=9d501521-54f8-4aa2-b975-6b34f8aac5a0"
        ),
    },
    "Creditstar Spain": {
        "lender_id": "9babf437-637c-47b4-b0e7-937c30fa587c",
        "regulation_status": "UNREGULATED",
        "min_interest_rate": 10,
        "min_remaining_term_in_days": 1,
        "max_remaining_term_in_days": 360,
        "page_url": (
            "https://app.lendermarket.com/fr/listes-des-prets/non-reglemente"
            "?minInterestRate=10&minRemainingTermInDays=1&maxRemainingTermInDays=360"
            "&lenders=9babf437-637c-47b4-b0e7-937c30fa587c"
        ),
    },
    "Creditstar Sweden": {
        "lender_id": "9babf437-6ccb-4ae2-a22f-c887e9e3696c",
        "regulation_status": "UNREGULATED",
        "min_interest_rate": 9,
        "min_remaining_term_in_days": 1,
        "max_remaining_term_in_days": 360,
        "page_url": (
            "https://app.lendermarket.com/fr/listes-des-prets/non-reglemente"
            "?minInterestRate=9&minRemainingTermInDays=1&maxRemainingTermInDays=360"
            "&lenders=9babf437-6ccb-4ae2-a22f-c887e9e3696c"
        ),
    },
    "Creditstar Denmark": {
        "lender_id": "9babf437-6970-48e6-8175-62ef53465eba",
        "regulation_status": "UNREGULATED",
        "min_interest_rate": 9,
        "min_remaining_term_in_days": 1,
        "max_remaining_term_in_days": 360,
        "page_url": (
            "https://app.lendermarket.com/fr/listes-des-prets/non-reglemente"
            "?minInterestRate=9&minRemainingTermInDays=1&maxRemainingTermInDays=360"
            "&lenders=9babf437-6970-48e6-8175-62ef53465eba"
        ),
    },
    "Creditstar Czech": {
        "lender_id": "9babf437-5bf8-41fb-840d-6edf7012e408",
        "regulation_status": "UNREGULATED",
        "min_interest_rate": 9,
        "min_remaining_term_in_days": 1,
        "max_remaining_term_in_days": 360,
        "page_url": (
            "https://app.lendermarket.com/fr/listes-des-prets/non-reglemente"
            "?minInterestRate=9&minRemainingTermInDays=1&maxRemainingTermInDays=360"
            "&lenders=9babf437-5bf8-41fb-840d-6edf7012e408"
        ),
    },
}

def fetch_active_loans(segment: dict) -> list:
    """Fetch the currently active loans for one segment via the public API,
    with the same lenders/regulationStatus/maxRemainingTermInDays filters
    used by the corresponding listing page."""
    params = [
        ("maxRemainingTermInDays", str(segment["max_remaining_term_in_days"])),
        ("regulationStatus", segment["regulation_status"]),
    ]
    params += [("lenders[]", lender_id) for lender_id in segment["lenders"]]
    url = f"{LOANS_API_URL}?{parse.urlencode(params)}"

    req = request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read())
    except error.HTTPError as exc:
        log.error("Lendermarket API returned HTTP %s for segment '%s'.", exc.code, segment["label"])
        return []
    except Exception:
        log.exception("Failed to fetch loans for segment '%s'.", segment["label"])
        return []

    return payload.get("data") or []


def aggregate_by_lender(loans: list) -> list:
    """Group loans by lender (fournisseur de crédit), returning one entry per
    lender with the loan count, total investable amount, and min/max
    interest rate - sorted by lender name."""
    buckets = {}
    for loan in loans:
        lender_name = (loan.get("lender") or {}).get("displayName") or "Fournisseur inconnu"
        bucket = buckets.setdefault(
            lender_name,
            {"lender": lender_name, "count": 0, "total_amount": 0.0, "min_rate": None, "max_rate": None},
        )

        bucket["count"] += 1

        amount = loan.get("investableAmount") or loan.get("loanAmount")
        try:
            bucket["total_amount"] += float(amount)
        except (TypeError, ValueError):
            pass

        rate = loan.get("interestRate")
        try:
            rate = float(rate)
        except (TypeError, ValueError):
            rate = None
        if rate is not None:
            bucket["min_rate"] = rate if bucket["min_rate"] is None else min(bucket["min_rate"], rate)
            bucket["max_rate"] = rate if bucket["max_rate"] is None else max(bucket["max_rate"], rate)

    return sorted(buckets.values(), key=lambda b: b["lender"])


def fetch_active_loans_for_lender(config: dict, min_interest_rate: float | None = None) -> list:
    """Same public API as fetch_active_loans(), but for a single lender
    with its own minRemainingTermInDays/maxRemainingTermInDays cutoffs (one
    entry of LENDER_INVEST_FILTERS) - used by invest_selected_lenders() (the
    real auto-invest step) to check each selected lender's own availability
    exactly like the user's own filtered listing URLs.

    `min_interest_rate`, if given, OVERRIDES config["min_interest_rate"] -
    the "Taux min" of the lender in the "config robots" sheet is passed here
    by invest_selected_lenders() (the hardcoded value is only a fallback)."""
    rate = min_interest_rate if min_interest_rate is not None else config["min_interest_rate"]
    params = [
        ("minInterestRate", str(rate)),
        ("minRemainingTermInDays", str(config["min_remaining_term_in_days"])),
        ("maxRemainingTermInDays", str(config["max_remaining_term_in_days"])),
        ("regulationStatus", config["regulation_status"]),
        ("lenders[]", config["lender_id"]),
    ]
    url = f"{LOANS_API_URL}?{parse.urlencode(params)}"

    req = request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read())
    except error.HTTPError as exc:
        log.error("Lendermarket API returned HTTP %s while checking lender availability.", exc.code)
        return []
    except Exception:
        log.exception("Failed to fetch active loans for a specific lender.")
        return []

    return payload.get("data") or []


def _match_lender_filter(sheet_name: str, filters: dict) -> str | None:
    """Match a lender name as written in the Google Sheet against
    LENDER_INVEST_FILTERS' keys - same exact/substring, case-insensitive
    matching idea as peerberry_invest_bot._match_selected_originator(), in
    case spelling isn't 100% identical between the sheet and this file.
    Returns the matching filter key, or None if none matched."""
    value = sheet_name.strip().lower()
    if not value:
        return None
    for key in filters:
        if key.strip().lower() == value:
            return key
    for key in filters:
        key_lower = key.strip().lower()
        if key_lower in value or value in key_lower:
            return key
    return None


def _redact_sensitive_headers(headers: dict) -> dict:
    """Same redaction rule as swaper_monitor.py's equivalent - keep header
    names/values needed to see the request shape (content-type, custom API
    headers like x-xsrf-token/X-INVESTOR-ID) but blank out raw cookies/auth
    tokens before they go into an emailed diagnostics attachment.
    """
    redacted = {}
    for name, value in headers.items():
        if name.lower() in ("cookie", "authorization", "set-cookie"):
            redacted[name] = "[REDACTED - sensitive session/auth value, not needed to see the request shape]"
        else:
            redacted[name] = value
    return redacted


def _xsrf_headers(session: requests.Session, investor_id: str | None = None) -> dict:
    headers = dict(_HEADERS)
    xsrf = session.cookies.get("XSRF-TOKEN")
    if xsrf:
        headers["x-xsrf-token"] = unquote(xsrf)
    if investor_id:
        headers["X-INVESTOR-ID"] = investor_id
    return headers


def login(session: requests.Session) -> str:
    """Log into Lendermarket via pure HTTP (email/password + TOTP 2FA if
    enabled) and return the authenticated `investorId`, needed as an
    `X-INVESTOR-ID` header on every subsequent authenticated call.

    See the module docstring for the full CSRF/XSRF + TOTP flow, verified
    2026-07-18 via a real browser network capture.
    """
    if not LENDERMARKET_EMAIL or not LENDERMARKET_PASSWORD:
        raise RuntimeError("LENDERMARKET_EMAIL/LENDERMARKET_PASSWORD environment variables are required.")

    r = session.get(CSRF_URL, headers=_HEADERS, timeout=20)
    r.raise_for_status()

    r = session.post(
        LOGIN_URL,
        json={"email": LENDERMARKET_EMAIL, "password": LENDERMARKET_PASSWORD},
        headers=_xsrf_headers(session),
        timeout=20,
    )
    r.raise_for_status()
    data = r.json().get("data") or {}
    mandatory_steps = (data.get("person") or {}).get("mandatorySteps") or []
    needs_totp = any(step.get("stepName") == "TOTP_CHALLENGE" for step in mandatory_steps)

    if needs_totp:
        if not LENDERMARKET_TOTP_SECRET:
            raise RuntimeError(
                "Lendermarket is asking for a 2FA code but LENDERMARKET_TOTP_SECRET is not set. "
                "Set it to the base32 secret used to configure Google Authenticator."
            )
        log.info("2FA prompt detected, generating and submitting TOTP code...")
        totp = pyotp.TOTP(LENDERMARKET_TOTP_SECRET)

        # Diagnostic only (no secret/code values logged): compare
        # Lendermarket's server-reported clock (Date response header) to
        # our local clock - helps distinguish a genuine clock-skew issue
        # from a wrong LENDERMARKET_TOTP_SECRET if this still fails below.
        server_date_header = r.headers.get("Date")
        if server_date_header:
            try:
                server_time = parsedate_to_datetime(server_date_header)
                skew = (datetime.now(timezone.utc) - server_time).total_seconds()
                log.info("Clock check: local vs. Lendermarket server Date header skew = %.1fs", skew)
            except Exception:
                pass

        # Guard against submitting a code right as its 30s window is about
        # to roll over - the network round-trip can push the server-side
        # check past the boundary and get rejected as "Invalid code
        # provided" even though the code was valid when generated.
        remaining = 30 - (int(time.time()) % 30)
        if remaining < 5:
            time.sleep(remaining + 1)

        # Retrying with a same-window code is a no-op (calling totp.now()
        # again within under a second returns the IDENTICAL code, since
        # it's still the same 30s window - confirmed via a real GitHub
        # Actions failure where the initial attempt and the "retry" were
        # only ~300ms apart and both got rejected). Genuine resilience
        # against boundary-rollover/clock-skew requires trying distinct
        # adjacent-window codes instead.
        now = time.time()
        candidates = [totp.at(now), totp.at(now - 30), totp.at(now + 30)]
        r = None
        for attempt, code in enumerate(candidates, start=1):
            r = session.post(TOTP_URL, json={"code": code}, headers=_xsrf_headers(session), timeout=20)
            if r.status_code != 422:
                break
            log.info("TOTP code rejected (attempt %d/%d)...", attempt, len(candidates))
        if not r.ok:
            raise RuntimeError(
                f"Lendermarket TOTP submission failed (status={r.status_code}): {r.text[:500]}"
            )
        data = r.json().get("data") or {}

    investor_id = (data.get("currentInvestor") or {}).get("investorId")
    if not investor_id:
        raise RuntimeError("Lendermarket login succeeded but no investorId was returned.")
    log.info("Logged in successfully, investorId=%s", investor_id)
    return investor_id


def _fetch_balance_payload(session: requests.Session, investor_id: str) -> dict:
    """Raises on any HTTP error (e.g. 401 on an expired persisted session)."""
    r = session.get(
        BALANCE_API_URL,
        params={"currency": "EUR"},
        headers=_xsrf_headers(session, investor_id),
        timeout=20,
    )
    r.raise_for_status()
    return r.json().get("data") or {}


def _parse_balance(payload: dict) -> float | None:
    try:
        return float(payload.get("investorAvailableBalanceAmount"))
    except (TypeError, ValueError):
        return None


def fetch_account_balance(session: requests.Session, investor_id: str) -> float | None:
    """Fetch the investor's available balance (EUR)."""
    try:
        payload = _fetch_balance_payload(session, investor_id)
    except Exception:
        log.exception("Failed to fetch the Lendermarket account balance.")
        return None
    return _parse_balance(payload)


def fetch_account_pending_payments(session: requests.Session, investor_id: str) -> float:
    """Fetch `investorPendingPaymentsAmount` (EUR): repayments already received
    by the platform but not yet credited to the wallet/statement - part of
    the platform's own `investorAccountValueAmount`. Returns 0.0 on failure."""
    try:
        r = session.get(
            BALANCE_API_URL,
            params={"currency": "EUR"},
            headers=_xsrf_headers(session, investor_id),
            timeout=20,
        )
        r.raise_for_status()
        return float((r.json().get("data") or {}).get("investorPendingPaymentsAmount") or 0.0)
    except Exception:
        log.exception("Failed to fetch the Lendermarket pending payments - assuming 0.0.")
        return 0.0


def login_and_fetch_balance() -> tuple:
    """Log in once and return `(session, investor_id, balance)` - the
    authenticated session/investor_id are kept (unlike the old
    fetch_balance_via_login(), which discarded them) so the SAME login can
    also be reused for the real auto-invest step below (`invest_selected_
    lenders()`), instead of logging in twice per run (extra network
    round-trip + doubles the risk of a TOTP-rollover timing issue on 2FA
    accounts). Returns `(None, None, None)` if credentials are missing or
    login fails."""
    if not LENDERMARKET_EMAIL or not LENDERMARKET_PASSWORD:
        log.warning("LENDERMARKET_EMAIL/PASSWORD not set, skipping account balance lookup.")
        return None, None, None

    session = requests.Session()
    try:
        payload, extra = get_or_refresh_session(
            session, SESSION_STATE_FILE,
            fetch_fn=lambda extra: _fetch_balance_payload(session, extra["investor_id"]),
            login_fn=lambda: (None, {"investor_id": login(session)}),
            platform_name="Lendermarket",
        )
    except Exception:
        log.exception("Failed to log into Lendermarket to fetch the account balance.")
        return None, None, None

    return session, extra["investor_id"], _parse_balance(payload)


def _log_invest_diagnostics(tag: str, **fields) -> None:
    """Append one JSON line of full diagnostic detail to
    INVEST_DIAGNOSTICS_FILE (never printed to stdout/the console log - same
    convention as peerberry_invest_bot.py's _log_diagnostics())."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tag": tag,
        **fields,
    }
    try:
        with INVEST_DIAGNOSTICS_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str, ensure_ascii=False) + "\n")
    except OSError:
        log.exception("Could not write to invest diagnostics file %s", INVEST_DIAGNOSTICS_FILE)


# Cap how much diagnostics text gets attached to the summary email, same
# value/rationale as peerberry_invest_bot.py's equivalent.
MAX_INVEST_DIAGNOSTICS_EMAIL_CHARS = 2_000_000


def _collect_run_invest_diagnostics(since: datetime) -> str | None:
    """Read INVEST_DIAGNOSTICS_FILE and return only the JSON lines written
    at/after `since` (this run's own entries, since the file accumulates
    history across every past run too) - same idea as
    peerberry_invest_bot.py's `_collect_run_diagnostics()`, so the full
    request/response detail (and any error) is attached directly to the
    invest summary email instead of requiring manual access to the runner's
    filesystem. Returns None if the file doesn't exist or this run added
    nothing to it."""
    if not INVEST_DIAGNOSTICS_FILE.exists():
        return None
    lines = []
    try:
        with INVEST_DIAGNOSTICS_FILE.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    entry_time = datetime.fromisoformat(entry["timestamp"])
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
                if entry_time >= since:
                    lines.append(line)
    except OSError:
        log.exception("Could not read invest diagnostics file %s for the summary email attachment.", INVEST_DIAGNOSTICS_FILE)
        return None
    if not lines:
        return None
    text = "\n".join(lines)
    if len(text) > MAX_INVEST_DIAGNOSTICS_EMAIL_CHARS:
        text = text[-MAX_INVEST_DIAGNOSTICS_EMAIL_CHARS:]
        text = "(truncated, showing the last part only)\n" + text
    return text


def _format_amount(amount: float) -> str:
    """Mimic the string Lendermarket's own invest-form number input sends:
    whole numbers with no decimals ("10"), fractional ones trimmed of
    trailing zeros ("33.33") - same convention as peerberry_invest_bot.py's
    equivalent (real capture on 2026-07-24 showed a plain "10" string)."""
    rounded = round(float(amount), 2)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.2f}".rstrip("0").rstrip(".")


def attempt_investment(session: requests.Session, loan_uuid: str, amount: float) -> bool:
    """Real invest submission call - `POST claims/v1/investor/createInvestment`,
    see module docstring/repo memory ("2026-07-24 ... createInvestment") for
    how this was captured (safe, network-intercepted click-through, no real
    money ever spent during that capture). Notably NO `X-INVESTOR-ID` header
    on this specific call (unlike other authenticated Lendermarket calls in
    this file). Both `acceptedLimitedPurposeTerms` and
    `acceptedLimitedRecourseTerms` are sent as `true` (the capture only had
    the first one checked/true - the second, a "Contrat de rachat"
    checkbox, wasn't expanded/checked - but a real bot should accept both
    sets of terms to actually invest properly).
    Always logs the full request+response to INVEST_DIAGNOSTICS_FILE,
    whether it succeeds or fails."""
    payload = {
        "investmentAmount": _format_amount(amount),
        "acceptedLimitedPurposeTerms": True,
        "acceptedLimitedRecourseTerms": True,
        "acceptRisk": "true",
        "loanUuid": loan_uuid,
    }
    try:
        r = session.post(INVEST_URL, json=payload, headers=_xsrf_headers(session), timeout=20)
    except Exception as exc:
        _log_invest_diagnostics(
            "invest_attempt_exception",
            loan_uuid=loan_uuid,
            payload=payload,
            error=str(exc),
            traceback=traceback.format_exc(),
        )
        log.exception("Investment attempt raised an exception for loan %s.", loan_uuid)
        return False

    _log_invest_diagnostics(
        "invest_attempt",
        loan_uuid=loan_uuid,
        payload=payload,
        status=r.status_code,
        response_headers=_redact_sensitive_headers(dict(r.headers)),
        response_body=r.text[:5000],
    )
    if r.ok:
        log.info("Investment attempt for loan %s (%.2f EUR) returned status=%s.", loan_uuid, amount, r.status_code)
        return True
    log.warning(
        "Investment attempt for loan %s (%.2f EUR) FAILED status=%s - full request/response saved to diagnostics.",
        loan_uuid, amount, r.status_code,
    )
    return False


def invest_selected_lenders(
    session: requests.Session,
    balance: float,
    config: PlatformConfig,
    geo_snapshot: dict | None = None,
) -> dict:
    """Real auto-invest step, driven by the "config robots" Google Sheet
    (`config` = get_platform_config("Lendermarket"), see
    shared/robot_config.py for the meaning of each column).

    Only lenders flagged ACTIF and matching a LENDER_INVEST_FILTERS entry
    (`_match_lender_filter()`) are considered. Each one's available loans
    are fetched with the lender's own "Taux min" as the API minInterestRate
    (0 if empty), then every loan is checked against the lender's
    min/max rate, and the whole `balance` is allocated across all the
    candidate loans by `plan_allocations()`: per-lender min/max amount per
    investment, "Pourcentage max du solde par loan" and "Pourcentage max du
    solde par pays" caps (both relative to balance + everything already
    invested, as read from "Répartition géographique" by
    get_geo_platform_snapshot(), counting inactive lenders too), and the
    "Répartition équivalente" flag (equal split between the flagged loans).
    Checked once per run (one-shot bot), caps are also updated after each
    successful investment.

    Returns a stats dict: `balance_before`, `balance_after`,
    `lender_budgets` (amount planned per lender), `invest_attempts`,
    `invest_successes`, `invest_failures`, `total_invested`, `lender_stats`
    (per-lender: `budget`, `country`, `loans_seen`, `attempts`,
    `successes`, `failures`, `invested_amount`, `invested_loans`),
    `country_blocked` / `originator_blocked` (active lenders excluded
    because their country / own cap is already reached),
    `min_interest_rate` (lowest configured min rate, or None),
    `country_threshold_percentage`, `country_status` and
    `originator_cap_status` (refreshed at the end for the summary email)."""
    stats = {
        "balance_before": balance,
        "balance_after": balance,
        "lender_budgets": {},
        "invest_attempts": 0,
        "invest_successes": 0,
        "invest_failures": 0,
        "total_invested": 0.0,
        "lender_stats": {},
        "country_blocked": [],
        "originator_blocked": [],
        "min_interest_rate": config.lowest_min_rate(),
        "country_threshold_percentage": config.country_max_pct,
    }

    tracker = build_tracker(config, balance, geo_snapshot)

    matched = []
    relevant_countries = set()
    for sheet_name in config.active_names():
        filter_key = _match_lender_filter(sheet_name, LENDER_INVEST_FILTERS)
        if filter_key is None:
            log.warning("Active Lendermarket lender '%s' from the config sheet doesn't match any known filter config, skipping it.", sheet_name)
            continue
        country = tracker.country_of(sheet_name)
        if country:
            relevant_countries.add(country)
        if tracker.loan_room(sheet_name) <= 0:
            log.info("Lender '%s' is blocked this run: already at/above its own cap.", sheet_name)
            stats["originator_blocked"].append(sheet_name)
            continue
        if tracker.country_room(country) <= 0:
            log.info("Lender '%s' (country '%s') is blocked this run: already at/above the country cap.", sheet_name, country)
            stats["country_blocked"].append(sheet_name)
            continue
        matched.append((sheet_name, filter_key, country))

    stats["country_status"] = tracker.country_status(relevant_countries)
    stats["originator_cap_status"] = tracker.loan_cap_status()

    candidates = []
    loan_lookup = {}
    for sheet_name, filter_key, country in matched:
        lender_cfg = config.loans[sheet_name]
        loans = fetch_active_loans_for_lender(
            LENDER_INVEST_FILTERS[filter_key], min_interest_rate=lender_cfg.min_rate or 0,
        )
        stats["lender_stats"][sheet_name] = {
            "budget": 0.0,
            "country": country,
            "loans_seen": len(loans),
            "attempts": 0,
            "successes": 0,
            "failures": 0,
            "invested_amount": 0.0,
            "invested_loans": [],
        }
        for loan in loans:
            loan_uuid = loan.get("uuid")
            if not loan_uuid:
                continue
            try:
                available = float(loan.get("investableAmount") or loan.get("loanAmount") or 0)
            except (TypeError, ValueError):
                available = 0.0
            try:
                rate = float(loan.get("interestRate"))
            except (TypeError, ValueError):
                rate = None
            candidates.append(Candidate(loan_uuid, sheet_name, available, rate))
            loan_lookup[loan_uuid] = (sheet_name, rate)

    if not candidates:
        log.info("No available loan for the active Lendermarket lenders %s - nothing to invest this run.", [m[0] for m in matched])
        return stats

    plan = plan_allocations(candidates, tracker, balance, MIN_INVESTMENT_AMOUNT)
    if not plan:
        log.info("%d candidate loan(s), nothing fundable under the configured limits (balance=%.2f EUR).", len(candidates), balance)
        return stats

    for loan_uuid, amount in plan.items():
        sheet_name, _rate = loan_lookup[loan_uuid]
        stats["lender_budgets"][sheet_name] = stats["lender_budgets"].get(sheet_name, 0.0) + amount
        stats["lender_stats"][sheet_name]["budget"] += amount
    log.info("Investment plan (loan uuid -> EUR): %s", plan)

    for loan_uuid, amount in plan.items():
        sheet_name, rate = loan_lookup[loan_uuid]
        lender_stat = stats["lender_stats"][sheet_name]
        stats["invest_attempts"] += 1
        lender_stat["attempts"] += 1
        if attempt_investment(session, loan_uuid, amount):
            stats["invest_successes"] += 1
            stats["total_invested"] += amount
            stats["balance_after"] -= amount
            lender_stat["successes"] += 1
            lender_stat["invested_amount"] += amount
            lender_stat["invested_loans"].append({"amount": amount, "interestRate": rate})
            tracker.add(sheet_name, amount)
        else:
            stats["invest_failures"] += 1
            lender_stat["failures"] += 1

    stats["country_status"] = tracker.country_status(relevant_countries)
    stats["originator_cap_status"] = tracker.loan_cap_status()
    return stats


def run() -> None:
    run_started_at = datetime.now(timezone.utc)
    apply_startup_jitter(CRON_SCHEDULE_STATE_FILE)
    state = load_state(STATE_FILE, DEFAULT_STATE)
    gates = state.setdefault("gates", {})

    # Same rule as Swaper (see notification_gate.py): a segment is only
    # really "available" when there's money to invest AND at least one loan
    # listed. If the balance couldn't be determined (login/fetch failed),
    # don't let that silently gate segments closed - assume money's fine and
    # fall back to loan availability alone. The session/investor_id are kept
    # (not discarded) so the real auto-invest step below can reuse this same
    # login instead of authenticating twice per run.
    session, investor_id, balance = login_and_fetch_balance()
    log.info("Account balance: %s", f"{balance:.2f} €" if balance is not None else "unavailable")

    # Real auto-invest (added 2026-07-24, per explicit user request) - runs
    # BEFORE the segment-availability monitor below (invest first, monitor/
    # notify after), so a matching loan gets a real investment attempt as
    # soon as possible each run instead of after the informational checks.
    # See invest_selected_lenders()'s docstring for the exact budget-
    # splitting rules. The bot stops itself as soon as the balance is <
    # MIN_INVESTMENT_AMOUNT (10 EUR by default) - explicit user request
    # 2026-07-24, nothing left to invest below that. FIXED 2026-09-09 (real
    # GitHub Actions log showed a 0.01 EUR run still doing the Sheet reads
    # for selected lenders/minInterestRate/country+lender caps below before
    # this check): the low-balance stop now happens BEFORE any of those
    # Google Sheet reads, not just before the actual invest call, so a
    # low-balance run skips them entirely instead of reading the Sheet for
    # nothing. The summary email is only sent if something actually
    # happened this run (an investment was attempted, or an unexpected
    # error occurred) - NOT on every run - so this frequent scheduled
    # monitor doesn't spam an email every cycle.
    if session is None or balance is None:
        log.info("Skipping auto-invest: no authenticated session/balance available this run.")
    elif balance < MIN_INVESTMENT_AMOUNT:
        log.info("Auto-invest bot stopping: balance (%.2f EUR) is below the minimum investment amount (%.2f EUR), nothing to invest.", balance, MIN_INVESTMENT_AMOUNT)
    else:
        # The whole robot configuration (active lenders, rate bounds, caps,
        # min/max amounts, equal split) comes from the "config robots"
        # sheet; the amounts already invested (per country/lender) from
        # "Répartition géographique". The config is required (an error is
        # logged and auto-invest skipped); the geographic snapshot is
        # soft-fail: without it caps are computed from the available
        # balance only, so country/loan blocking is disabled for this run.
        try:
            config = get_platform_config("Lendermarket")
        except Exception:
            log.exception("Could not read the Lendermarket configuration from the 'config robots' sheet.")
            config = None

        if config is None:
            pass
        elif not config.active_names():
            log.info("Skipping auto-invest: no Lendermarket lender is active (ACTIF = x) in the 'config robots' sheet.")
        else:
            geo_snapshot = None
            try:
                geo_snapshot = get_geo_platform_snapshot("Lendermarket", "Loanch")
            except Exception:
                log.exception("Could not read the Lendermarket geographic snapshot from the Google Sheet, country/loan caps are computed from the balance only this run.")

            invest_error = None
            try:
                invest_stats = invest_selected_lenders(session, balance, config, geo_snapshot)
            except Exception as exc:
                invest_error = str(exc)
                invest_stats = {"balance_before": balance, "balance_after": balance, "invest_attempts": 0}
                _log_invest_diagnostics("run_error", error=invest_error, traceback=traceback.format_exc())
                log.exception("Unexpected error during the Lendermarket auto-invest step.")

            # The bot runs BEFORE the loan-availability recap below, so the
            # recap/notification email further down must reflect the
            # account's state AFTER this run's investments. Previously this
            # re-fetched the balance from the server right after investing
            # and TRUSTED that live value over the already-accurate computed
            # one - removed 2026-09-19 (explicit user request, real bug
            # report: "le mail que je reçois se base sur les données du
            # compte avant investissement... je ne veux pas recevoir le
            # mail [...] si solde <10€ et que des prêts sont disponible").
            # Lendermarket's balance endpoint appears to lag a moment behind
            # a just-submitted investment (same class of eventual-
            # consistency issue already documented for Swaper in this repo,
            # see swaper_monitor.py's `_extract_balance_from_attempts()`),
            # so that live re-fetch could return the PRE-invest balance and
            # silently overwrite the correct value with it. `balance_after`
            # (from `invest_selected_lenders()`) is fully deterministic - it
            # only decrements by an amount once `attempt_investment()`
            # returned an HTTP success for that exact call - so it's used
            # directly here instead, with no live re-fetch at all.
            balance = invest_stats.get("balance_after", balance)

            if invest_stats.get("invest_attempts", 0) > 0 or invest_error:
                log.info("Auto-invest run finished: %s", invest_stats)
                send_lendermarket_invest_summary_email(
                    invest_stats,
                    error=invest_error,
                    diagnostics_text=_collect_run_invest_diagnostics(run_started_at),
                )
            else:
                log.info("Auto-invest: no fundable loan found this run for the selected lenders.")

    # Same cron-job.org speed-up/slow-down as Swaper (see cron_schedule.py):
    # poll faster while there's money to invest. Skipped when the balance
    # couldn't be determined, rather than guessing and possibly slowing down
    # polling incorrectly.
    if balance is not None:
        mode = "30m" if balance < 10 else "2m"
        ensure_schedule(mode, cron_job_id=LENDERMARKET_CRON_JOB_ID, state_file=CRON_SCHEDULE_STATE_FILE)

    newly_available = {}

    # Below the minimum, every segment is unavailable regardless of its own
    # loan count (see `available` below) - skip the per-segment API call
    # entirely instead of fetching just to log a predetermined SKIP.
    # should_notify() is still called (with count=0) so gates that were open
    # get properly reset now, not silently left stale for next time.
    skip_loan_fetch = balance is not None and balance < 10

    for segment in LOAN_SEGMENTS:
        if skip_loan_fetch:
            count = 0
            log.info("Segment '%s': skipping loan availability check (balance < 10 EUR).", segment["label"])
        else:
            loans = fetch_active_loans(segment)
            count = len(loans)
            log.info("Segment '%s': %d loan(s) currently available.", segment["label"], count)

        available = (balance is None or balance >= 10) and count > 0
        send, was_reset = should_notify(gates, segment["key"], available)

        if was_reset:
            log.info("Segment '%s': resetting notification gate.", segment["label"])

        log.info(
            "Notification decision context: segment='%s', loans_count=%d, available=%s",
            segment["label"],
            count,
            available,
        )

        if send:
            log.info("Notification decision: SEND (reason=loans_available_and_gate_open). loans_count=%d", count)
            newly_available[segment["key"]] = {
                "label": segment["label"],
                "lenders": aggregate_by_lender(loans),
            }
        elif available:
            log.info("Notification decision: SKIP (reason=already_notified_for_current_cycle).")
        else:
            log.info("Notification decision: SKIP (reason=balance < 10 or no loans available).")

    if newly_available:
        log.info("Sending notification for %d segment(s) with newly available loans.", len(newly_available))
        send_lendermarket_email(balance, newly_available)
    else:
        log.info("Nothing new to notify.")

    save_state(STATE_FILE, state)


if __name__ == "__main__":
    run()