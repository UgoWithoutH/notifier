"""PeerBerry "Available for investment" balance monitor.

Logs into peerberry.com (reusing peerberry_diversification.login(), which
already handles email/password + TOTP 2FA - not duplicated here) and fetches
the "Available for investment" balance shown on the Overview page
(https://peerberry.com/en/client/overview).

Verified against the real account on 2026-07-15: the Overview page displays
"Available for investment â‚¬4.25", which is sourced from
`GET https://api.peerberry.com/v1/investor/overview` ->
`{"availableMoney": "4.25", "invested": "10023.10", ...}` - `availableMoney`
matched the displayed figure exactly. Captured the same way
swaper_monitor.fetch_loans() captures its own API call: by navigating to the
real page and waiting for its own request/response, so auth headers/cookies
are exactly what the browser itself sends (no need to reconstruct a bearer
token like peerberry_diversification.py does for the originators/account
summary endpoints).

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

from dotenv import load_dotenv

load_dotenv()

from playwright.sync_api import sync_playwright

from shared.notifier import send_peerberry_available_email
from shared.browser_stealth import get_context_options, apply_stealth
from shared.cron_schedule import ensure_schedule
from shared.state import load_state, save_state
from diversification.peerberry_diversification import login, PEERBERRY_EMAIL, PEERBERRY_PASSWORD

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("peerberry_monitor")

OVERVIEW_URL = "https://peerberry.com/en/client/overview"
OVERVIEW_API_URL = "https://api.peerberry.com/v1/investor/overview"
STORAGE_STATE_FILE = Path(__file__).parent / "peerberry_storage_state.json"
CRON_SCHEDULE_STATE_FILE = Path(__file__).parent / "peerberry_cron_schedule_state.json"
STATE_FILE = Path(__file__).parent / "peerberry_state.json"
DEFAULT_STATE = {"last_notified_balance": None}

PEERBERRY_CRON_JOB_ID = os.environ.get("PEERBERRY_CRON_JOB_ID")

MIN_AVAILABLE_TO_NOTIFY = 10.0


def fetch_available_money(page) -> float:
    """Navigate to the Overview page and capture its own overview API
    response to read the "Available for investment" balance
    (`availableMoney`, EUR - see module docstring)."""
    log.info("Navigating to the overview page and capturing the overview API response...")
    with page.expect_response(
        # Exact match only - matching by prefix would also catch sibling
        # endpoints like ".../overview/originators" or ".../overview/profit/...".
        lambda r: r.url.split("?", 1)[0] == OVERVIEW_API_URL and r.request.method == "GET"
    ) as response_info:
        page.goto(OVERVIEW_URL, wait_until="domcontentloaded")
    response = response_info.value
    if not response.ok:
        raise RuntimeError(f"Overview API returned status {response.status}")

    payload = response.json()
    try:
        available_money = float(payload.get("availableMoney"))
    except (TypeError, ValueError):
        available_money = 0.0
    return available_money


def run(headless: bool = True) -> None:
    if not PEERBERRY_EMAIL or not PEERBERRY_PASSWORD:
        log.error("PEERBERRY_EMAIL and PEERBERRY_PASSWORD environment variables are required.")
        sys.exit(1)

    log.info("Starting PeerBerry monitor run (headless=%s, storage_state_exists=%s).", headless, STORAGE_STATE_FILE.exists())

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        storage_state = str(STORAGE_STATE_FILE) if STORAGE_STATE_FILE.exists() else None
        context = browser.new_context(
            storage_state=storage_state,
            locale="en-US",
            **get_context_options(),
        )
        apply_stealth(context)
        page = context.new_page()

        try:
            login(page)
            available_money = fetch_available_money(page)
        except Exception:
            log.exception("Failed to log in or fetch the available-for-investment balance.")
            browser.close()
            sys.exit(1)

        # Persist cookies/local storage so the next run can skip login (and
        # 2FA) while the session remains valid.
        context.storage_state(path=str(STORAGE_STATE_FILE))
        browser.close()

    log.info("Available for investment: %.2f EUR", available_money)

    # Same cron-job.org speed-up/slow-down as Swaper/Lendermarket (see
    # cron_schedule.py): poll faster while there's money to invest.
    if available_money < MIN_AVAILABLE_TO_NOTIFY:
        ensure_schedule("30m", cron_job_id=PEERBERRY_CRON_JOB_ID, state_file=CRON_SCHEDULE_STATE_FILE)
    else:
        ensure_schedule("2m", cron_job_id=PEERBERRY_CRON_JOB_ID, state_file=CRON_SCHEDULE_STATE_FILE)

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
    # Set headless=False locally (e.g. via `python peerberry_monitor.py --show`)
    # to watch the browser and debug the login flow if selectors need adjusting.
    run(headless="--show" not in sys.argv)
