"""PeerBerry "Available for investment" balance monitor.

Logs into peerberry.com via pure HTTP (login()/exchange_2fa_code() live here
- moved from diversification/peerberry_diversification.py on 2026-07-16 so
that module can import them from this one instead of the reverse, same
dependency direction as swaper/lendermarket: this monitor has no need for
shared.google_sheet, so it must not import anything from a
*_diversification.py module, which does) and fetches the "Available for
investment" balance shown on the Overview page.

Auth flow verified 2026-07-18 via a real browser network capture - NO
reCAPTCHA, NO CSRF cookie dance needed (much simpler than Lendermarket):
  1. POST https://api.peerberry.com/v1/investor/login  json=
     {"email", "password", "params": null} -> `{"tfa_is_active": true,
     "tfa_token": "<token>"}` if 2FA is enabled (or a direct
     `{"access_token": ...}` if it isn't - not observed on this account,
     tfa_is_active is always true here, so that path is untested).
  2. POST https://api.peerberry.com/v1/investor/login/2fa  json=
     {"code": <TOTP>, "tfa_token": <token from step 1>} -> `{"access_token":
     "<JWT>"}`.
  3. Every subsequent authenticated call just needs `Authorization: Bearer
     <access_token>` - this is the same JWT the real browser also stores in
     an `app_token` cookie via client-side JS, but sending it as a bearer
     header directly (as this module already did before, when reading it out
     of Playwright's cookie jar) works identically and skips the cookie
     entirely.

Balance verified against the real account on 2026-07-15: the Overview page
displays "Available for investment €4.25", sourced from
`GET https://api.peerberry.com/v1/investor/overview` ->
`{"availableMoney": "4.25", "invested": "10023.10", ...}` - `availableMoney`
matched the displayed figure exactly.

Sends an email whenever the balance is >= 10 EUR, but only once per
distinct balance value (anti-spam): if the balance stays exactly the same
as the last notified value across consecutive runs, no new email is sent.
A new email IS sent as soon as the balance changes to a different value
(while still >= 10 EUR) - unlike swaper_monitor.py/lendermarket_monitor.py's
notification_gate.should_notify(), the gate here is NOT reset just because
the balance dips below 10 EUR; it only cares whether the value itself
changed. Persisted in peerberry_state.json (`last_notified_balance`).

Also speeds up/slows down its own cron-job.org schedule based on that same
10 EUR threshold, exactly like swaper_monitor.py/lendermarket_monitor.py:
30 minutes while balance < 10 EUR, 2 minutes while balance >= 10 EUR.

Required env vars:
    PEERBERRY_EMAIL, PEERBERRY_PASSWORD    -> PeerBerry account credentials
    SMTP_HOST, SMTP_USER, SMTP_PASSWORD,   -> outgoing mail server
    EMAIL_TO                               -> notification recipient
Optional:
    SMTP_PORT (default 587), EMAIL_FROM (default SMTP_USER)
    PEERBERRY_TOTP_SECRET                  -> base32 secret used to set up
                                               Google Authenticator, needed
                                               if 2FA is enabled on the account
    CRON_JOB_API_KEY, PEERBERRY_CRON_JOB_ID -> cron-job.org schedule speed-up/
                                                slow-down (see cron_schedule.py);
                                                skipped silently if unset
"""

import os
import sys
import logging
from pathlib import Path

import pyotp
import requests
from dotenv import load_dotenv

load_dotenv()

from shared.notifier import send_peerberry_available_email
from shared.cron_schedule import ensure_schedule
from shared.state import load_state, save_state

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("peerberry_monitor")

API_BASE = "https://api.peerberry.com"
LOGIN_URL = f"{API_BASE}/v1/investor/login"
TFA_URL = f"{API_BASE}/v1/investor/login/2fa"
OVERVIEW_API_URL = f"{API_BASE}/v1/investor/overview"
CRON_SCHEDULE_STATE_FILE = Path(__file__).parent / "peerberry_cron_schedule_state.json"
STATE_FILE = Path(__file__).parent / "peerberry_state.json"

PEERBERRY_EMAIL = os.environ.get("PEERBERRY_EMAIL")
PEERBERRY_PASSWORD = os.environ.get("PEERBERRY_PASSWORD")
PEERBERRY_TOTP_SECRET = os.environ.get("PEERBERRY_TOTP_SECRET")
DEFAULT_STATE = {"last_notified_balance": None}

PEERBERRY_CRON_JOB_ID = os.environ.get("PEERBERRY_CRON_JOB_ID")

MIN_AVAILABLE_TO_NOTIFY = 10.0

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Referer": "https://peerberry.com/",
    "Origin": "https://peerberry.com",
}


def login(session: requests.Session) -> str:
    """Log into PeerBerry via pure HTTP (email/password + TOTP 2FA if
    enabled) and return the `access_token` JWT, used as an `Authorization:
    Bearer <token>` header on every subsequent authenticated call.

    See the module docstring for the full auth flow, verified 2026-07-18 via
    a real browser network capture.
    """
    if not PEERBERRY_EMAIL or not PEERBERRY_PASSWORD:
        raise RuntimeError("PEERBERRY_EMAIL/PEERBERRY_PASSWORD environment variables are required.")

    r = session.post(
        LOGIN_URL,
        json={"email": PEERBERRY_EMAIL, "password": PEERBERRY_PASSWORD, "params": None},
        headers=_HEADERS,
        timeout=20,
    )
    r.raise_for_status()
    data = r.json() or {}

    if data.get("tfa_is_active"):
        if not PEERBERRY_TOTP_SECRET:
            raise RuntimeError(
                "PeerBerry is asking for a 2FA code but PEERBERRY_TOTP_SECRET is not set. "
                "Set it to the base32 secret used to configure Google Authenticator."
            )
        log.info("2FA prompt detected, generating and submitting TOTP code...")
        code = pyotp.TOTP(PEERBERRY_TOTP_SECRET).now()
        r = session.post(
            TFA_URL,
            json={"code": code, "tfa_token": data["tfa_token"]},
            headers=_HEADERS,
            timeout=20,
        )
        if not r.ok:
            raise RuntimeError(f"PeerBerry rejected the TOTP code (status={r.status_code}): {r.text[:200]}")
        data = r.json() or {}

    access_token = data.get("access_token")
    if not access_token:
        raise RuntimeError("PeerBerry login succeeded but no access_token was returned.")
    session.headers["Authorization"] = f"Bearer {access_token}"
    log.info("Logged in successfully.")
    return access_token


def fetch_available_money(session: requests.Session) -> float:
    """Fetch the "Available for investment" balance (`availableMoney`, EUR -
    see module docstring)."""
    r = session.get(OVERVIEW_API_URL, headers=_HEADERS, timeout=20)
    r.raise_for_status()
    payload = r.json() or {}
    try:
        available_money = float(payload.get("availableMoney"))
    except (TypeError, ValueError):
        available_money = 0.0
    return available_money


def run() -> None:
    if not PEERBERRY_EMAIL or not PEERBERRY_PASSWORD:
        log.error("PEERBERRY_EMAIL and PEERBERRY_PASSWORD environment variables are required.")
        sys.exit(1)

    log.info("Starting PeerBerry monitor run (pure HTTP, no browser).")

    session = requests.Session()
    try:
        login(session)
        available_money = fetch_available_money(session)
    except Exception:
        log.exception("Failed to log in or fetch the available-for-investment balance.")
        sys.exit(1)

    log.info("Available for investment: %.2f EUR", available_money)

    # Anti-spam: only send when the balance is >= 10 EUR AND its value
    # actually differs from the last one we notified for (rounded to avoid
    # float noise) - staying at the exact same balance across runs does NOT
    # re-trigger an email, but any change to a different value does, even if
    # the balance dipped below 10 EUR at some point in between.
    state = load_state(STATE_FILE, DEFAULT_STATE)
    rounded_balance = round(available_money, 2)
    last_notified_balance = state.get("last_notified_balance")

    if available_money >= MIN_AVAILABLE_TO_NOTIFY and rounded_balance != last_notified_balance:
        log.info(
            "Balance >= %.2f EUR and different from last notified value (%s) - sending notification email.",
            MIN_AVAILABLE_TO_NOTIFY,
            last_notified_balance,
        )
        send_peerberry_available_email(available_money)
        state["last_notified_balance"] = rounded_balance
        save_state(STATE_FILE, state)
    elif available_money >= MIN_AVAILABLE_TO_NOTIFY:
        log.info("Balance >= %.2f EUR but unchanged since the last notification (%.2f EUR) - skipping.", MIN_AVAILABLE_TO_NOTIFY, last_notified_balance)
    else:
        log.info("Balance < %.2f EUR - not sending an email.", MIN_AVAILABLE_TO_NOTIFY)


if __name__ == "__main__":
    run()
