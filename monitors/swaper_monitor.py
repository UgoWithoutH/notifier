"""Swaper loan monitor.

Logs into swaper.com with Playwright, fetches the available loans and the
uninvested account balance via the site's own API (using the authenticated
browser session so cookies are sent automatically), and sends an email
notification only once per positive-balance cycle: when balance is > 0 and
at least one manual loan is available. No further notifications are sent while
balance stays > 0 at the same amount. The notification gate is reset when
balance drops back to 0 or when the balance amount changes.

Required env vars:
    SWAPER_EMAIL, SWAPER_PASSWORD          -> Swaper account credentials
    SMTP_HOST, SMTP_USER, SMTP_PASSWORD,   -> outgoing mail server
    EMAIL_TO                               -> notification recipient
Optional:
    SMTP_PORT (default 587), EMAIL_FROM (default SMTP_USER)
    SWAPER_TOTP_SECRET                     -> base32 secret used to set up
                                               Google Authenticator, needed
                                               if 2FA is enabled on the account

Invest-structure exploration (added 2026-07-23): whenever loans are available
(same condition that triggers the normal notification email above), this
module ALSO captures diagnostic data meant to help figure out, later, how to
auto-invest on Swaper (mirroring monitors/peerberry_invest_bot.py, which does
this for real already) - the real invest HTTP request/HTML structure is NOT
known yet, and can't be explored right now since no loans are available at
the time this was written. `capture_invest_exploration_state()`'s response/
request listeners (registered on the already-logged-in Playwright `page`,
only AFTER login() has fully completed - see the security lesson in repo
memory about not registering broad interceptors during a login flow) collect
every `/rest/` API call observed while browsing the loans page, plus a
truncated HTML dump of that page. NO invest/confirm button is ever clicked -
purely passive capture, same real-money safety boundary already established
for the PeerBerry exploration (see repo memory: don't click a real invest/
confirm control without the user explicitly accepting that risk). The result
is emailed (see shared.notifier.send_swaper_invest_exploration_email()) as a
JSON attachment whenever the normal notification email is also sent, so the
user can pass that file back to build the real auto-invest bot once genuine
loan/invest markup has actually been observed.
"""

import json
import os
import random
import sys
import time
import logging
from datetime import datetime, timezone
from pathlib import Path

import pyotp
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from shared.notifier import send_swaper_email, send_swaper_invest_exploration_email
from shared.state import load_state, save_state
from shared.cron_schedule import ensure_schedule
from shared.notification_gate import should_notify
from shared.browser_stealth import get_context_options, apply_stealth, human_pause, human_mouse_wander, human_type

DEFAULT_STATE = {
    "gates": {},
}

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("swaper_monitor")

LOGIN_URL = "https://swaper.com/en/login"
STATE_FILE = Path(__file__).parent / "swaper_state.json"
STORAGE_STATE_FILE = Path(__file__).parent / "swaper_storage_state.json"
CRON_SCHEDULE_STATE_FILE = Path(__file__).parent / "cron_schedule_state.json"

SWAPER_EMAIL = os.environ.get("SWAPER_EMAIL")
SWAPER_PASSWORD = os.environ.get("SWAPER_PASSWORD")
SWAPER_TOTP_SECRET = os.environ.get("SWAPER_TOTP_SECRET")
SWAPER_CRON_JOB_ID = os.environ.get("SWAPER_CRON_JOB_ID")



def handle_two_factor(page) -> None:
    """If Swaper prompts for a Google Authenticator code after submitting
    credentials, generate a fresh TOTP code from SWAPER_TOTP_SECRET (the same
    base32 secret that was scanned into the authenticator app) and submit it.

    Verified against the real 2FA screen on 2026-07-06: the code input is
    `input[name='code']`, there's a "Trust this browser and skip 2FA for 30
    days" checkbox (ticked here so the persisted session, see
    STORAGE_STATE_FILE, avoids repeating 2FA on the next scheduled runs), and
    the same "Log In" div is reused to submit the code.
    """
    otp_input = page.locator("input[name='code']")
    try:
        otp_input.wait_for(timeout=8000)
    except PlaywrightTimeoutError:
        return  # no 2FA prompt shown, nothing to do

    if not SWAPER_TOTP_SECRET:
        raise RuntimeError(
            "Swaper is asking for a 2FA code but SWAPER_TOTP_SECRET is not set. "
            "Set it to the base32 secret used to configure Google Authenticator."
        )

    log.info("2FA prompt detected, generating and submitting TOTP code...")
    totp = pyotp.TOTP(SWAPER_TOTP_SECRET)
    code = totp.now()
    human_type(otp_input, code)
    human_pause()

    try:
        page.get_by_text("Trust this browser").click(timeout=3000)
    except PlaywrightTimeoutError:
        log.warning("Could not find the 'trust this browser' checkbox, continuing anyway.")

    # Guard against the 30s TOTP window rolling over while we were typing
    # the code / clicking "trust this browser" - suspected cause of a
    # GitHub Actions failure on 2026-07-09 where the whole 2FA sequence
    # completed without any Playwright error, but the page never left
    # /login (i.e. the code was silently rejected as stale). If a
    # freshly-generated code differs from what's currently filled in,
    # clear the field and retype it right before submitting.
    fresh_code = totp.now()
    if fresh_code != code:
        log.info("TOTP code rolled over to a new 30s window while typing, retyping the fresh code.")
        otp_input.fill("")
        human_type(otp_input, fresh_code)
        human_pause()

    page.locator("div.button.clickable", has_text="Log In").click()


def login(page) -> None:
    """Log in to Swaper using the credentials (and TOTP secret, if 2FA is
    enabled) from env vars.

    Selectors verified against the real login form on 2026-07-06: the email
    and password inputs have stable `name` attributes, but the submit control
    is a styled `<div class="button clickable ... disabled">` (not a real
    `<button>`), which loses its `disabled` class once both fields are filled.
    """
    log.info("Navigating to login page...")
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    human_mouse_wander(page)

    # Dismiss the Cookiebot consent banner if it shows up.
    try:
        page.get_by_role("button", name="Allow all cookies").click(timeout=5000)
    except PlaywrightTimeoutError:
        pass

    # If a previous session was restored (see STORAGE_STATE_FILE) and is still
    # valid, Swaper redirects away from /login immediately - nothing else to do.
    if "/login" not in page.url:
        log.info("Reused a previous session, already logged in at %s", page.url)
        return

    log.info("Filling in credentials...")
    human_type(page.locator("input[name='email']"), SWAPER_EMAIL)
    human_pause()
    human_type(page.locator("input[name='password']"), SWAPER_PASSWORD)
    human_pause()
    page.locator("div.button.clickable", has_text="Log In").click()

    handle_two_factor(page)

    # Wait until we've left the login page (successful redirect to dashboard).
    page.wait_for_url(lambda url: "/login" not in url, timeout=20000)
    log.info("Logged in successfully, current URL: %s", page.url)


def fetch_loans(page) -> dict:
    """Fetch the loans list by navigating to the real /en/loans page and
    capturing its own API call, so the CSRF token (`x-xsrf-token` header,
    derived from a cookie) and `referer` are exactly what the API expects -
    a manually reconstructed fetch() call gets rejected with 403 otherwise.

    Verified against a real account on 2026-07-06: the page issues
    `POST /rest/public/loans` with body
    `{"page": 1, "pageSize": 13, "groups": [], "amountFrom": null,
    "amountTo": null, "status": null}` and returns
    `{accountBalance, totalRecords, page, results}`. This only fetches the
    first page (13 items) - fine for "is anything newly available" style
    monitoring, but increase pageSize / paginate if you need the full list.
    """
    log.info("Navigating to the loans page and capturing the loans API response...")
    with page.expect_response(
        lambda r: r.url.endswith("/rest/public/loans") and r.request.method == "POST"
    ) as response_info:
        page.goto("https://swaper.com/en/loans", wait_until="domcontentloaded")
    response = response_info.value
    if not response.ok:
        raise RuntimeError(f"Loans API returned status {response.status}")
    human_mouse_wander(page)
    return response.json()


def extract_loans(payload) -> list:
    """Normalize the API response into a flat list of loan dicts.

    Verified response shape: {"accountBalance": ..., "totalRecords": ...,
    "page": ..., "results": [{"id": ..., "number": ..., "status": ...,
    "amount": ..., "interestRatePerYear": ..., "term": {...}, ...}, ...]}
    """
    if isinstance(payload, dict):
        items = payload.get("results") or []
    elif isinstance(payload, list):
        items = payload
    else:
        items = []

    return [item for item in items if item.get("id") is not None or item.get("loanId") is not None]


def extract_balance(payload) -> float:
    """Extract the uninvested account balance (EUR) from the API response."""
    if isinstance(payload, dict):
        balance = payload.get("accountBalance")
        if isinstance(balance, (int, float)):
            return float(balance)
    return 0.0


# Caps how large the JSON exploration diagnostics attachment can get (a
# runaway/huge loans page HTML dump or a chatty set of captured API calls
# shouldn't produce an unreasonably large email).
MAX_INVEST_EXPLORATION_CHARS = 500_000
# How much of the loans page's main HTML is kept - enough to see a loan
# row's real markup (any Invest button/form) without dumping unrelated
# <head>/script/style bytes.
MAX_LOANS_PAGE_HTML_CHARS = 200_000


def _record_api_response(captured: list, response) -> None:
    """`page.on("response", ...)` handler used by run() to passively capture
    Swaper's own `/rest/` API traffic while browsing the loans page, for the
    invest-structure exploration diagnostics (see module docstring). Only
    ever registered AFTER login() has fully completed; also explicitly
    skips anything login/auth-related as a second safety layer (belt-and-
    braces, per the repo's security lesson about page.route()/page.on()
    interceptors and credentials) even though that should never happen at
    this point in the flow.
    """
    url = response.url
    if "swaper.com" not in url or "/rest/" not in url:
        return
    lower_url = url.lower()
    if any(keyword in lower_url for keyword in ("login", "auth", "password")):
        return
    entry = {"method": response.request.method, "url": url, "status": response.status}
    try:
        entry["body"] = response.text()[:20000]
    except Exception:
        entry["body"] = None
    captured.append(entry)


def run(headless: bool = True) -> None:
    if not SWAPER_EMAIL or not SWAPER_PASSWORD:
        log.error("SWAPER_EMAIL and SWAPER_PASSWORD environment variables are required.")
        sys.exit(1)

    state = load_state(STATE_FILE, DEFAULT_STATE)
    gates = state.setdefault("gates", {})

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
            ],
        )
        storage_state = str(STORAGE_STATE_FILE) if STORAGE_STATE_FILE.exists() else None
        context = browser.new_context(
            storage_state=storage_state,
            locale="en-US",
            timezone_id="Europe/Paris",
            **get_context_options(),
        )
        apply_stealth(context)
        page = context.new_page()

        captured_api_calls = []
        loans_page_html = None

        try:
            login(page)
            # Registered only AFTER login() has fully completed (see
            # _record_api_response()'s docstring) - passively captures the
            # /rest/ API traffic fired while fetch_loans() navigates to the
            # loans page, for the invest-structure exploration diagnostics.
            page.on("response", lambda response: _record_api_response(captured_api_calls, response))
            payload = fetch_loans(page)
            try:
                loans_page_html = page.evaluate(
                    "() => (document.querySelector('main') || document.body).outerHTML"
                )
            except Exception:
                log.exception("Could not capture the loans page HTML for the invest-exploration diagnostics.")
        except Exception:
            log.exception("Failed to log in or fetch loans.")
            browser.close()
            sys.exit(1)

        # Persist cookies/local storage so the next run can skip login (and
        # 2FA, thanks to the "trust this browser" checkbox) while the session
        # remains valid.
        context.storage_state(path=str(STORAGE_STATE_FILE))
        browser.close()

    loans = extract_loans(payload)
    balance = extract_balance(payload)

    # Build the invest-structure exploration diagnostics (only meaningful
    # when there's actually at least one loan to look at) - see module
    # docstring. Cheap to build unconditionally (data already fetched/
    # captured above); only actually emailed below if loans exist AND the
    # normal notification email also fires this run.
    invest_exploration_text = None
    if loans:
        if loans_page_html and len(loans_page_html) > MAX_LOANS_PAGE_HTML_CHARS:
            loans_page_html = loans_page_html[:MAX_LOANS_PAGE_HTML_CHARS] + "\n... (truncated)"
        diagnostics = {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "loans_count": len(loans),
            "loans_api_payload": payload,
            "captured_api_calls": captured_api_calls,
            "loans_page_html": loans_page_html,
        }
        invest_exploration_text = json.dumps(diagnostics, indent=2, ensure_ascii=False, default=str)
        if len(invest_exploration_text) > MAX_INVEST_EXPLORATION_CHARS:
            invest_exploration_text = invest_exploration_text[:MAX_INVEST_EXPLORATION_CHARS] + "\n... (truncated)"

    log.info("Balance %s, %d loan(s) available.", "positive" if balance > 0 else "zero/unavailable", len(loans))

    # TEMPORARY DEBUG: force-send a recap email regardless of balance/new
    # loans, to validate the SMTP pipeline end-to-end. Triggered via the
    # `force_test_email` workflow_dispatch input. Remove once confirmed working.
    force_test_email = os.environ.get("FORCE_TEST_EMAIL", "").lower() == "true"
    if force_test_email:
        log.info("FORCE_TEST_EMAIL is set - sending a forced test recap email.")
        send_swaper_email(balance, loans)
        if invest_exploration_text:
            send_swaper_invest_exploration_email(len(loans), invest_exploration_text)

    if balance < 10:
        ensure_schedule("30m", cron_job_id=SWAPER_CRON_JOB_ID, state_file=CRON_SCHEDULE_STATE_FILE)
    else:
        ensure_schedule("2m", cron_job_id=SWAPER_CRON_JOB_ID, state_file=CRON_SCHEDULE_STATE_FILE)

    # Same rule for both monitors (see notification_gate.py): only really
    # "available" when there's money to invest AND at least one loan listed.
    available = balance >= 10 and bool(loans)
    send, was_reset = should_notify(gates, "swaper", available, record=not force_test_email)

    if was_reset:
        log.info("Resetting notification gate (balance < 10 or no loans available).")

    log.info(
        "Notification decision context: balance=%.2f, loans_count=%d, available=%s, force_test_email=%s",
        balance,
        len(loans),
        available,
        force_test_email,
    )

    if send and not force_test_email:
        log.info("Notification decision: SEND (reason=balance >= 10 and loans available and gate open).")
        send_swaper_email(balance, loans)
        if invest_exploration_text:
            log.info("Sending Swaper invest-structure exploration diagnostics email (%d loan(s) available).", len(loans))
            send_swaper_invest_exploration_email(len(loans), invest_exploration_text)
    elif available:
        if force_test_email:
            log.info("Notification decision: SKIP normal cycle send (reason=force_test_email_already_sent).")
        else:
            log.info("Notification decision: SKIP (reason=already_notified_for_current_cycle).")
    else:
        log.info("Notification decision: SKIP (reason=balance < 10 or no loans available).")

    save_state(STATE_FILE, state)


if __name__ == "__main__":
    # Random startup delay (up to 1 minute) so scheduled runs don't always
    # fire at exactly the same second, making the traffic look less bot-like.
    delay = random.uniform(0, 60)
    log.info("Startup jitter: sleeping for %.1f seconds before starting.", delay)
    time.sleep(delay)

    # Set headless=False locally (e.g. via `python swaper_monitor.py --show`) to
    # watch the browser and debug the login flow if selectors need adjusting.
    run(headless="--show" not in sys.argv)
