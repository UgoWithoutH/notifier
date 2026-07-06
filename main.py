"""Swaper loan monitor.

Logs into swaper.com with Playwright, fetches the available loans and the
uninvested account balance via the site's own API (using the authenticated
browser session so cookies are sent automatically), and sends an email
notification when there is BOTH an uninvested balance > 0 AND at least one
*new* manual loan available to invest in (tracked by loan ID, see state.py -
this catches turnover in the loan list even if the total count never drops
to 0). If the balance is 0, no notification is sent even if loans are
available, and the "already notified" loan IDs are reset so a fresh alert
fires next time money becomes available again.

Required env vars:
    SWAPER_EMAIL, SWAPER_PASSWORD          -> Swaper account credentials
    SMTP_HOST, SMTP_USER, SMTP_PASSWORD,   -> outgoing mail server
    EMAIL_TO                               -> notification recipient
Optional:
    SMTP_PORT (default 587), EMAIL_FROM (default SMTP_USER)
    SWAPER_TOTP_SECRET                     -> base32 secret used to set up
                                               Google Authenticator, needed
                                               if 2FA is enabled on the account
"""

import os
import sys
import logging
from pathlib import Path

import pyotp
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from notifier import send_email
from state import load_state, save_state

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("swaper_monitor")

LOGIN_URL = "https://swaper.com/en/login"
STATE_FILE = Path(__file__).parent / "state.json"
STORAGE_STATE_FILE = Path(__file__).parent / "storage_state.json"
SCREENSHOT_ON_ERROR = Path(__file__).parent / "error_screenshot.png"

SWAPER_EMAIL = os.environ.get("SWAPER_EMAIL")
SWAPER_PASSWORD = os.environ.get("SWAPER_PASSWORD")
SWAPER_TOTP_SECRET = os.environ.get("SWAPER_TOTP_SECRET")


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
    code = pyotp.TOTP(SWAPER_TOTP_SECRET).now()
    otp_input.fill(code)

    try:
        page.get_by_text("Trust this browser").click(timeout=3000)
    except PlaywrightTimeoutError:
        log.warning("Could not find the 'trust this browser' checkbox, continuing anyway.")

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
    page.locator("input[name='email']").fill(SWAPER_EMAIL)
    page.locator("input[name='password']").fill(SWAPER_PASSWORD)
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


def redact_sensitive_fields(page) -> None:
    """Blank out credential/2FA-code/account-balance-looking fields before a
    debug screenshot is taken, so the uploaded artifact can't leak the
    password, a live TOTP code (its input is a plain text field, not
    type=password) or account data.
    """
    try:
        page.evaluate(
            """
            () => {
                const selectors = [
                    "input[name='email']",
                    "input[name='password']",
                    "input[name='code']",
                ];
                for (const sel of selectors) {
                    document.querySelectorAll(sel).forEach(el => { el.value = '***redacted***'; });
                }
                // Blur any focused field so masked dots/values re-render before the screenshot.
                if (document.activeElement) document.activeElement.blur();
            }
            """
        )
    except Exception:
        log.warning("Could not redact sensitive fields before screenshot.")


def run(headless: bool = True) -> None:
    if not SWAPER_EMAIL or not SWAPER_PASSWORD:
        log.error("SWAPER_EMAIL and SWAPER_PASSWORD environment variables are required.")
        sys.exit(1)

    state = load_state(STATE_FILE)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        storage_state = str(STORAGE_STATE_FILE) if STORAGE_STATE_FILE.exists() else None
        context = browser.new_context(storage_state=storage_state)
        page = context.new_page()

        try:
            login(page)
            payload = fetch_loans(page)
        except Exception:
            log.exception("Failed to log in or fetch loans.")
            try:
                redact_sensitive_fields(page)
                page.screenshot(path=str(SCREENSHOT_ON_ERROR), full_page=True)
            except Exception:
                pass
            browser.close()
            sys.exit(1)

        # Persist cookies/local storage so the next run can skip login (and
        # 2FA, thanks to the "trust this browser" checkbox) while the session
        # remains valid.
        context.storage_state(path=str(STORAGE_STATE_FILE))
        browser.close()

    loans = extract_loans(payload)
    balance = extract_balance(payload)

    log.info("Balance: %.2f EUR, %d loan(s) available.", balance, len(loans))

    if balance <= 0:
        if state.get("seen_loan_ids"):
            log.info("Balance back to 0 - resetting seen loans so a fresh alert fires next time money is available.")
        state["seen_loan_ids"] = []
        log.info("Balance is 0 (or unavailable), nothing to notify.")
    else:
        seen_ids = set(state.get("seen_loan_ids", []))
        current_ids = {str(loan.get("id")) for loan in loans}
        new_ids = current_ids - seen_ids
        new_loans = [loan for loan in loans if str(loan.get("id")) in new_ids]

        if new_loans:
            log.info(
                "Balance %.2f EUR + %d new loan(s) since last notification - sending notification email.",
                balance, len(new_loans),
            )
            send_email(balance, new_loans)
        elif loans:
            log.info("Balance positive but no new loan since last notification - skipping email to avoid spam.")
        else:
            log.info("Balance positive but no loan currently available, nothing to notify.")

        state["seen_loan_ids"] = sorted(current_ids)

    save_state(STATE_FILE, state)


if __name__ == "__main__":
    # Set headless=False locally (e.g. via `python main.py --show`) to watch
    # the browser and debug the login flow if selectors need adjusting.
    run(headless="--show" not in sys.argv)
