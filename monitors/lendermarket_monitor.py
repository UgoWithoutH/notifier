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
"""

import json
import logging
import os
import time
from pathlib import Path
from urllib import request, parse, error
from urllib.parse import unquote

import pyotp
import requests
from dotenv import load_dotenv

from shared.notifier import send_lendermarket_email
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

STATE_FILE = Path(__file__).parent / "lendermarket_state.json"
CRON_SCHEDULE_STATE_FILE = Path(__file__).parent / "lendermarket_cron_schedule_state.json"

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
DEFAULT_STATE = {"gates": {}}


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
            log.info("TOTP code rejected (likely rolled over), retrying once with a fresh code.")
            code = totp.now()
            r = session.post(TOTP_URL, json={"code": code}, headers=_xsrf_headers(session), timeout=20)
        r.raise_for_status()
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


def fetch_balance_via_login() -> float | None:
    if not LENDERMARKET_EMAIL or not LENDERMARKET_PASSWORD:
        log.warning("LENDERMARKET_EMAIL/PASSWORD not set, skipping account balance lookup.")
        return None

    session = requests.Session()
    try:
        investor_id = login(session)
        return fetch_account_balance(session, investor_id)
    except Exception:
        log.exception("Failed to log into Lendermarket to fetch the account balance.")
        return None


def run() -> None:
    state = load_state(STATE_FILE, DEFAULT_STATE)
    gates = state.setdefault("gates", {})

    # Same rule as Swaper (see notification_gate.py): a segment is only
    # really "available" when there's money to invest AND at least one loan
    # listed. If the balance couldn't be determined (login/fetch failed),
    # don't let that silently gate segments closed - assume money's fine and
    # fall back to loan availability alone.
    balance = fetch_balance_via_login()
    log.info("Account balance: %s", f"{balance:.2f} €" if balance is not None else "unavailable")

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
