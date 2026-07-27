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
from shared.google_sheet import get_selected_lendermarket_lenders
from shared.state import load_state, save_state
from shared.notification_gate import should_notify
from shared.cron_schedule import ensure_schedule

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
# get_selected_lendermarket_lenders()). Verified 2026-07-23 against the
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


def fetch_active_loans_for_lender(config: dict) -> list:
    """Same public API as fetch_active_loans(), but for a single lender
    with its own minInterestRate/minRemainingTermInDays cutoffs (one entry
    of LENDER_INVEST_FILTERS) - used by invest_selected_lenders() (the real
    auto-invest step) to check each selected lender's own availability
    exactly like the user's own filtered listing URLs."""
    params = [
        ("minInterestRate", str(config["min_interest_rate"])),
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
<<<<<<< HEAD

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
        r.raise_for_status()
=======
        # Guard against submitting a code right as its 30s window is about
        # to roll over - the network round-trip can push the server-side
        # check past the boundary and get rejected as "Invalid code
        # provided" even though the code was valid when generated.
        remaining = 30 - (int(time.time()) % 30)
        if remaining < 5:
            time.sleep(remaining + 1)
        code = totp.now()
        r = session.post(TOTP_URL, json={"code": code}, headers=_xsrf_headers(session), timeout=20)
        if r.status_code == 422:
            log.info(
                "TOTP code rejected (status=422, likely rolled over): %s. "
                "Waiting for the next 30s window before retrying with a fresh code.",
                r.text[:300],
            )
            # Regenerating immediately with totp.now() isn't reliable here:
            # if the rejection wasn't actually due to a rollover, it would
            # just resubmit the exact same code and fail again for the same
            # reason. Sleep past the current window's boundary first so the
            # retry is guaranteed to use a genuinely different code.
            remaining = 30 - (int(time.time()) % 30)
            time.sleep(remaining + 1)
            code = totp.now()
            r = session.post(TOTP_URL, json={"code": code}, headers=_xsrf_headers(session), timeout=20)
        if not r.ok:
            raise RuntimeError(
                f"Lendermarket TOTP submission failed (status={r.status_code}): {r.text[:500]}"
            )
>>>>>>> 7b1de0001f94f127249b0975f313b473b3da4b8a
        data = r.json().get("data") or {}

    investor_id = (data.get("currentInvestor") or {}).get("investorId")
    if not investor_id:
        raise RuntimeError("Lendermarket login succeeded but no investorId was returned.")
    log.info("Logged in successfully, investorId=%s", investor_id)
    return investor_id


def fetch_account_balance(session: requests.Session, investor_id: str) -> float | None:
    """Fetch the investor's available balance (EUR)."""
    try:
        r = session.get(
            BALANCE_API_URL,
            params={"currency": "EUR"},
            headers=_xsrf_headers(session, investor_id),
            timeout=20,
        )
        r.raise_for_status()
    except Exception:
        log.exception("Failed to fetch the Lendermarket account balance.")
        return None

    data = r.json().get("data") or {}
    balance = data.get("investorAvailableBalanceAmount")
    try:
        return float(balance)
    except (TypeError, ValueError):
        return None


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
        investor_id = login(session)
    except Exception:
        log.exception("Failed to log into Lendermarket to fetch the account balance.")
        return None, None, None

    balance = fetch_account_balance(session, investor_id)
    return session, investor_id, balance


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


def _compute_loan_shares(budget: float, loans: list, min_investment: float = MIN_INVESTMENT_AMOUNT) -> dict:
    """Split `budget` (one lender's own share of the account balance, see
    `invest_selected_lenders()`) EQUALLY across `loans` (that same lender's
    currently available loans), per explicit user request 2026-07-24: "2
    prêts dispo -> divisé par 2, 3 prêts -> divisé par 3" - a raw equal
    division, NOT PeerBerry's fixed-size-block split.

    Two adjustments on top of a plain `budget / len(loans)`:
    - If the equal share would be below `min_investment` (the platform's
      own real per-investment minimum), fewer loans are funded instead (as
      many as `budget // min_investment` allows, kept in listing order) so
      every funded loan still gets at least `min_investment` - "je ne veux
      pas de reste" (no unusable leftover below the minimum).
    - If a loan's own `investableAmount` is smaller than its equal share
      (there isn't `budget/n` EUR left to invest in that specific loan),
      that loan is capped at what it can actually take and the excess is
      redistributed across the other loans in the same pass (equal share
      recomputed on what's left) - repeated until stable, so unused money
      doesn't sit idle in one loan's slot while another loan could still
      absorb it, again to avoid a leftover.

    Returns `{loan_uuid: amount}` for every loan that ends up funded
    (amount rounded to 2 decimals); loans below `min_investment` after all
    adjustments are simply omitted.
    """
    caps = {}
    for loan in loans:
        loan_uuid = loan.get("uuid")
        if not loan_uuid:
            continue
        try:
            cap = float(loan.get("investableAmount") or loan.get("loanAmount") or 0)
        except (TypeError, ValueError):
            cap = 0.0
        if cap > 0:
            caps[loan_uuid] = cap

    # Keep insertion (listing) order for deterministic drop/keep decisions below.
    active = list(caps.keys())
    remaining = budget
    shares = {}

    while active:
        if remaining < min_investment:
            break

        equal_share = remaining / len(active)
        if equal_share < min_investment:
            max_active = int(remaining // min_investment)
            if max_active <= 0:
                break
            if max_active < len(active):
                active = active[:max_active]
            continue

        capped_any = False
        for loan_uuid in list(active):
            if caps[loan_uuid] < equal_share:
                shares[loan_uuid] = caps[loan_uuid]
                remaining -= caps[loan_uuid]
                active.remove(loan_uuid)
                capped_any = True
        if capped_any:
            continue

        for loan_uuid in active:
            shares[loan_uuid] = round(equal_share, 2)
        break

    return shares


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


def invest_selected_lenders(session: requests.Session, balance: float, selected_lender_names: list) -> dict:
    """Real auto-invest step (added 2026-07-24, per explicit user request):
    for each lender selected in the Google Sheet (matched against
    LENDER_INVEST_FILTERS via `_match_lender_filter()`), the account
    `balance` is split EQUALLY across only the selected lenders that
    CURRENTLY have at least one available loan (a selected lender with 0
    loans right now doesn't consume a share of the balance for nothing -
    explicit user request 2026-07-24), then each lender's own share is
    split again EQUALLY across that lender's own currently available loans
    (see `_compute_loan_shares()` for the min-investment/no-leftover
    rules) - lenders are computed fully independently of each other (no
    cross-lender redistribution once a share is assigned).

    Returns a stats dict: `balance_before`, `balance_after` (running
    balance decremented by every successful investment, for the summary
    email - same idea as peerberry_invest_bot.py's `final_available_
    money`), `lender_budgets`, `invest_attempts`, `invest_successes`,
    `invest_failures`, `total_invested`, `lender_stats` (per-lender:
    `budget`, `loans_seen`, `attempts`, `successes`, `failures`,
    `invested_amount`, `invested_loans`)."""
    stats = {
        "balance_before": balance,
        "balance_after": balance,
        "lender_budgets": {},
        "invest_attempts": 0,
        "invest_successes": 0,
        "invest_failures": 0,
        "total_invested": 0.0,
        "lender_stats": {},
    }

    matched = []
    for sheet_name in selected_lender_names:
        filter_key = _match_lender_filter(sheet_name, LENDER_INVEST_FILTERS)
        if filter_key is None:
            log.warning("Selected Lendermarket lender '%s' from the Google Sheet doesn't match any known filter config, skipping auto-invest for it.", sheet_name)
            continue
        matched.append(filter_key)

    if not matched:
        return stats

    # Fetch each matched lender's currently available loans FIRST - the
    # balance is only divided among lenders that actually have at least one
    # loan right now (per explicit user request 2026-07-24: a selected
    # lender with 0 available loans doesn't "consume" a share of the
    # balance for nothing), not among every selected lender regardless of
    # availability.
    loans_by_lender = {name: fetch_active_loans_for_lender(LENDER_INVEST_FILTERS[name]) for name in matched}
    lenders_with_loans = [name for name, loans in loans_by_lender.items() if loans]

    if not lenders_with_loans:
        log.info("None of the selected Lendermarket lenders %s currently have an available loan - nothing to invest this run.", matched)
        for name in matched:
            stats["lender_stats"][name] = {
                "budget": 0.0, "loans_seen": 0, "attempts": 0, "successes": 0,
                "failures": 0, "invested_amount": 0.0, "invested_loans": [],
            }
        return stats

    lender_budget = balance / len(lenders_with_loans)
    stats["lender_budgets"] = {name: (lender_budget if name in lenders_with_loans else 0.0) for name in matched}

    for lender_name in matched:
        loans = loans_by_lender[lender_name]
        budget_for_lender = lender_budget if lender_name in lenders_with_loans else 0.0
        lender_stat = {
            "budget": budget_for_lender,
            "loans_seen": len(loans),
            "attempts": 0,
            "successes": 0,
            "failures": 0,
            "invested_amount": 0.0,
            "invested_loans": [],
        }
        stats["lender_stats"][lender_name] = lender_stat

        if not loans:
            log.info("Lender '%s': no loan currently available, excluded from the balance split this run.", lender_name)
            continue

        shares = _compute_loan_shares(budget_for_lender, loans, MIN_INVESTMENT_AMOUNT)
        if not shares:
            log.info("Lender '%s': budget=%.2f EUR, %d loan(s) available - nothing fundable (below the %.2f EUR minimum).", lender_name, budget_for_lender, len(loans), MIN_INVESTMENT_AMOUNT)
            continue

        log.info("Lender '%s': budget=%.2f EUR split across %d loan(s): %s", lender_name, budget_for_lender, len(shares), shares)
        for loan_uuid, amount in shares.items():
            stats["invest_attempts"] += 1
            lender_stat["attempts"] += 1
            success = attempt_investment(session, loan_uuid, amount)
            if success:
                stats["invest_successes"] += 1
                stats["total_invested"] += amount
                stats["balance_after"] -= amount
                lender_stat["successes"] += 1
                lender_stat["invested_amount"] += amount
                lender_stat["invested_loans"].append({"loanUuid": loan_uuid, "amount": amount})
            else:
                stats["invest_failures"] += 1
                lender_stat["failures"] += 1

    return stats


def run() -> None:
    run_started_at = datetime.now(timezone.utc)
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

    # Read selected lenders once - reused by both the real auto-invest step
    # below and the one-time-ever invest-exploration capture further down.
    try:
        selected_lender_names = get_selected_lendermarket_lenders()
    except Exception:
        log.exception("Could not read selected Lendermarket lenders from the Google Sheet.")
        selected_lender_names = []

    # Real auto-invest (added 2026-07-24, per explicit user request) - runs
    # BEFORE the segment-availability monitor below (invest first, monitor/
    # notify after), so a matching loan gets a real investment attempt as
    # soon as possible each run instead of after the informational checks.
    # See invest_selected_lenders()'s docstring for the exact budget-
    # splitting rules. The bot stops itself (skips entirely) as soon as the
    # balance is < MIN_INVESTMENT_AMOUNT (10 EUR by default) - explicit
    # user request 2026-07-24, nothing left to invest below that. The
    # summary email is only sent if something actually happened this run
    # (an investment was attempted, or an unexpected error occurred) - NOT
    # on every run - so this frequent scheduled monitor doesn't spam an
    # email every cycle.
    if session is None or balance is None:
        log.info("Skipping auto-invest: no authenticated session/balance available this run.")
    elif balance < MIN_INVESTMENT_AMOUNT:
        log.info("Auto-invest bot stopping: balance (%.2f EUR) is below the minimum investment amount (%.2f EUR), nothing to invest.", balance, MIN_INVESTMENT_AMOUNT)
    elif not selected_lender_names:
        log.info("Skipping auto-invest: no Lendermarket lender selected in the Google Sheet.")
    else:
        invest_error = None
        try:
            invest_stats = invest_selected_lenders(session, balance, selected_lender_names)
        except Exception as exc:
            invest_error = str(exc)
            invest_stats = {"balance_before": balance, "balance_after": balance, "invest_attempts": 0}
            _log_invest_diagnostics("run_error", error=invest_error, traceback=traceback.format_exc())
            log.exception("Unexpected error during the Lendermarket auto-invest step.")

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

    for segment in LOAN_SEGMENTS:
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
