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
"""

import os
import random
import sys
import time
import logging
import json
from urllib import request, error
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
CRON_JOB_API_KEY = os.environ.get("CRON_JOB_API_KEY")
CRON_JOB_ID = os.environ.get("CRON_JOB_ID")
CRON_JOB_TIMEZONE = os.environ.get("CRON_JOB_TIMEZONE", "UTC")
EVERY_30_MINUTES = [0, 30]
EVERY_2_MINUTES = list(range(0, 60, 2))

# A small pool of realistic, current desktop Chrome UA/viewport combos - the
# default Playwright/Chromium UA advertises "HeadlessChrome" (or a mismatched
# version), one of the easiest bot signals. Picking one combo at random per
# run (instead of always sending the exact same fingerprint from the same
# GitHub Actions IP range) makes traffic correlation slightly harder.
BROWSER_PROFILES = [
    {
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "viewport": {"width": 1366, "height": 768},
    },
    {
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        ),
        "viewport": {"width": 1536, "height": 864},
    },
    {
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "viewport": {"width": 1440, "height": 900},
    },
    {
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "viewport": {"width": 1920, "height": 1080},
    },
]

# Patched into every page before any site JS runs, to undo the most common
# "is this a real browser?" checks (navigator.webdriver, empty plugins list,
# missing chrome.runtime object).
STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
window.chrome = window.chrome || { runtime: {} };
const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
if (originalQuery) {
    window.navigator.permissions.query = (parameters) => (
        parameters.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : originalQuery(parameters)
    );
}
"""


def human_pause(min_seconds: float = 0.4, max_seconds: float = 1.2) -> None:
    """Sleep for a small random duration to avoid the instant, uniformly
    timed actions that give away scripted (non-human) interactions."""
    time.sleep(random.uniform(min_seconds, max_seconds))


def human_mouse_wander(page, moves: int = None) -> None:
    """Move the mouse through a few random points and scroll a bit, so the
    page isn't left completely idle/static the way a script would leave it -
    some bot-detection scripts specifically watch for the total absence of
    mousemove/scroll events during a session."""
    try:
        viewport = page.viewport_size or {"width": 1280, "height": 720}
        for _ in range(moves or random.randint(2, 4)):
            x = random.randint(0, max(viewport["width"] - 1, 1))
            y = random.randint(0, max(viewport["height"] - 1, 1))
            page.mouse.move(x, y, steps=random.randint(5, 15))
            human_pause(0.1, 0.4)
        page.mouse.wheel(0, random.randint(150, 500))
    except Exception:
        pass  # purely cosmetic, never fail the run because of it


def human_type(locator, text: str) -> None:
    """Type into a field one character at a time with randomized per-key
    delay, instead of `.fill()` which sets the value instantly - an easy
    signal for behavioral bot detection."""
    locator.click()
    for char in text:
        locator.type(char, delay=random.uniform(60, 180))


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
    human_type(otp_input, code)
    human_pause()

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


def set_cron_to_every_30_minutes() -> bool:
    """Patch cron-job.org schedule to run at minute 0 and 30 each hour."""
    if not CRON_JOB_API_KEY or not CRON_JOB_ID:
        log.info("CRON_JOB_API_KEY or CRON_JOB_ID missing, skipping cron-job.org update.")
        return False

    endpoint = f"https://api.cron-job.org/jobs/{CRON_JOB_ID}"
    payload = {
        "job": {
            "schedule": {
                "timezone": CRON_JOB_TIMEZONE,
                "minutes": EVERY_30_MINUTES,
            }
        }
    }
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
                log.info("cron-job.org schedule set to every 30 minutes.")
                return True
            log.warning("cron-job.org update returned unexpected HTTP status %s.", resp.status)
            return False
    except error.HTTPError as exc:
        details = ""
        try:
            details = exc.read().decode("utf-8", errors="ignore")
        except Exception:
            pass
        log.warning(
            "cron-job.org update failed (HTTP %s). Response: %s",
            exc.code,
            details[:400],
        )
    except Exception:
        log.exception("cron-job.org update failed.")

    return False


def set_cron_to_every_2_minutes() -> bool:
    """Patch cron-job.org schedule to run every 2 minutes."""
    if not CRON_JOB_API_KEY or not CRON_JOB_ID:
        log.info("CRON_JOB_API_KEY or CRON_JOB_ID missing, skipping cron-job.org update.")
        return False

    endpoint = f"https://api.cron-job.org/jobs/{CRON_JOB_ID}"
    payload = {
        "job": {
            "schedule": {
                "timezone": CRON_JOB_TIMEZONE,
                "minutes": EVERY_2_MINUTES,
            }
        }
    }
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
                log.info("cron-job.org schedule set to every 2 minutes.")
                return True
            log.warning("cron-job.org update returned unexpected HTTP status %s.", resp.status)
            return False
    except error.HTTPError as exc:
        details = ""
        try:
            details = exc.read().decode("utf-8", errors="ignore")
        except Exception:
            pass
        log.warning(
            "cron-job.org update failed (HTTP %s). Response: %s",
            exc.code,
            details[:400],
        )
    except Exception:
        log.exception("cron-job.org update failed.")

    return False


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
        browser = p.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
            ],
        )
        storage_state = str(STORAGE_STATE_FILE) if STORAGE_STATE_FILE.exists() else None
        profile = random.choice(BROWSER_PROFILES)
        context = browser.new_context(
            storage_state=storage_state,
            user_agent=profile["user_agent"],
            viewport=profile["viewport"],
            locale="en-US",
            timezone_id="Europe/Paris",
        )
        context.add_init_script(STEALTH_INIT_SCRIPT)
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

    log.info("Balance %s, %d loan(s) available.", "positive" if balance > 0 else "zero/unavailable", len(loans))

    # TEMPORARY DEBUG: force-send a recap email regardless of balance/new
    # loans, to validate the SMTP pipeline end-to-end. Triggered via the
    # `force_test_email` workflow_dispatch input. Remove once confirmed working.
    force_test_email = os.environ.get("FORCE_TEST_EMAIL", "").lower() == "true"
    if force_test_email:
        log.info("FORCE_TEST_EMAIL is set - sending a forced test recap email.")
        send_email(balance, loans)

    current_cron_mode = state.get("cron_schedule_mode")
    target_cron_mode = "30m" if balance <= 0 else "2m"
    log.info(
        "Cron decision context: balance=%.2f, current_mode=%s, target_mode=%s",
        balance,
        current_cron_mode,
        target_cron_mode,
    )

    if balance <= 0:
        if current_cron_mode != "30m":
            log.info("Cron decision: UPDATE requested (from=%s to=30m).", current_cron_mode)
            if set_cron_to_every_30_minutes():
                state["cron_schedule_mode"] = "30m"
                log.info("Cron decision: UPDATE success (new_mode=30m).")
            else:
                log.warning("Cron decision: UPDATE failed (target_mode=30m).")
        else:
            log.info("Cron already marked as 30m in local state, skipping update.")

        if state.get("notified_for_positive_cycle"):
            log.info("Balance back to 0 - resetting notification gate for the next positive cycle.")
        state["notified_for_positive_cycle"] = False
        state["cycle_balance_marker"] = None
        log.info("Balance is 0 (or unavailable), nothing to notify.")
    else:
        if current_cron_mode != "2m":
            log.info("Cron decision: UPDATE requested (from=%s to=2m).", current_cron_mode)
            if set_cron_to_every_2_minutes():
                state["cron_schedule_mode"] = "2m"
                log.info("Cron decision: UPDATE success (new_mode=2m).")
            else:
                log.warning("Cron decision: UPDATE failed (target_mode=2m).")
        else:
            log.info("Cron already marked as 2m in local state, skipping update.")

        rounded_balance = round(balance, 2)
        previous_marker = state.get("cycle_balance_marker")
        balance_changed = previous_marker != rounded_balance
        if balance_changed:
            if previous_marker is not None:
                log.info("Balance changed - resetting notification gate for this new balance level.")
            state["notified_for_positive_cycle"] = False
            state["cycle_balance_marker"] = rounded_balance

        already_notified = bool(state.get("notified_for_positive_cycle", False))

        log.info(
            "Notification decision context: balance_positive=true, loans_count=%d, balance_changed=%s, already_notified=%s, force_test_email=%s",
            len(loans),
            balance_changed,
            already_notified,
            force_test_email,
        )

        if loans and not already_notified and not force_test_email:
            log.info(
                "Notification decision: SEND (reason=balance_positive_and_loans_and_gate_open). loans_count=%d",
                len(loans),
            )
            send_email(balance, loans)
            state["notified_for_positive_cycle"] = True
        elif loans:
            if force_test_email:
                log.info("Notification decision: SKIP normal cycle send (reason=force_test_email_already_sent).")
            else:
                log.info("Notification decision: SKIP (reason=already_notified_for_current_cycle).")
        else:
            if state.get("notified_for_positive_cycle"):
                log.info("Loan count dropped to 0 - resetting notification gate.")
            state["notified_for_positive_cycle"] = False
            log.info("Notification decision: SKIP (reason=no_available_loans).")

    save_state(STATE_FILE, state)


if __name__ == "__main__":
    # Random startup delay (up to 1 minute) so scheduled runs don't always
    # fire at exactly the same second, making the traffic look less bot-like.
    delay = random.uniform(0, 60)
    log.info("Startup jitter: sleeping for %.1f seconds before starting.", delay)
    time.sleep(delay)

    # Set headless=False locally (e.g. via `python main.py --show`) to watch
    # the browser and debug the login flow if selectors need adjusting.
    run(headless="--show" not in sys.argv)
