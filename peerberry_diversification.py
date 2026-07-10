"""PeerBerry portfolio "distribution by loan originators" fetcher.

Logs into peerberry.com with Playwright (email/password + TOTP 2FA, same
idea as swaper_monitor.py / lendermarket_monitor.py) and fetches the
per-loan-originator investment breakdown that's shown on the Overview page
under Investments > "Loan originators" (amount invested + % of the
portfolio, one row per originator). No email is sent - the amounts are just
logged and handed to update_google_sheet() (currently a skeleton, see its
docstring) so they can be filled into a Google Sheet.

The breakdown itself is NOT re-fetched via a dedicated API call when
switching that dropdown on the site - it's already loaded once and only
becomes visible via `GET https://api.peerberry.com/v1/investor/overview/originators`,
which fires the first time that view is selected. Verified against the real
account on 2026-07-09: response is a JSON array of
`{"originator": "Lendplus ZA", "originatorId": 56, "company": "Aventus Group",
"companyId": 1, "iso2": "ZA", "amount": "1091.02", "part": "10.90"}`. This is
called directly (via the browser's own `fetch()`, so it reuses the
authenticated session) instead of clicking through the dropdown, using an
`Authorization: Bearer <token>` header built from the `app_token` cookie set
at login - a plain `credentials: 'include'` fetch gets rejected by CORS (the
API's `Access-Control-Allow-Origin` is a wildcard, which browsers refuse to
pair with credentialed requests).

Also fetches this calendar month's "Interest income" from the Account
Summary page (https://peerberry.com/en/client/statement/account-summary) -
see fetch_current_month_interest_income() below, same idea as
swaper_diversification.fetch_current_month_interest_received().

Required env vars:
    PEERBERRY_EMAIL, PEERBERRY_PASSWORD    -> PeerBerry account credentials
Optional:
    PEERBERRY_TOTP_SECRET                  -> base32 secret used to set up
                                               Google Authenticator, needed
                                               if 2FA is enabled on the account
    GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS     -> only needed once update_google_sheet()
                                               below is filled in (see google_sheet.py)
"""

import os
import sys
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pyotp
from dotenv import load_dotenv

load_dotenv()

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from browser_stealth import get_context_options, apply_stealth, human_pause, human_mouse_wander, human_type

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("peerberry_diversification")

LOGIN_URL = "https://peerberry.com/en/client/"
ORIGINATORS_API_URL = "https://api.peerberry.com/v1/investor/overview/originators"
STATEMENT_URL = "https://peerberry.com/en/client/statement/account-summary"
ACCOUNT_SUMMARY_API_URL = "https://api.peerberry.com/v2/investor/account-summary"
STORAGE_STATE_FILE = Path(__file__).parent / "peerberry_diversification_storage_state.json"
# The Account Summary page's default "This month" period (verified 2026-07-10
# by capturing its own request) = 1st of the current month through TODAY, not
# the full calendar month - same semantics as Swaper/Afranga/Lendermarket's
# equivalents. Pin the timezone explicitly rather than relying on the
# executing machine's local clock (e.g. UTC on a CI runner).
REPORT_TIMEZONE = ZoneInfo("Europe/Paris")

PEERBERRY_EMAIL = os.environ.get("PEERBERRY_EMAIL")
PEERBERRY_PASSWORD = os.environ.get("PEERBERRY_PASSWORD")
PEERBERRY_TOTP_SECRET = os.environ.get("PEERBERRY_TOTP_SECRET")


def dismiss_cookie_banner(page) -> None:
    """Dismiss the Cookiebot consent dialog if it shows up.

    Verified on 2026-07-09: a normal Playwright click on the "Allow all"
    button can silently no-op (the dialog stays in the DOM, still
    intercepting clicks on the login form underneath it), so this falls
    back to removing the dialog element outright via JS if it's still
    present a moment after the click.
    """
    try:
        page.locator("#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll").click(timeout=5000, force=True)
    except PlaywrightTimeoutError:
        return  # banner never appeared, nothing to do

    page.wait_for_timeout(500)
    page.evaluate(
        """
        () => {
            const dialog = document.getElementById('CybotCookiebotDialog');
            if (dialog) dialog.remove();
        }
        """
    )


def handle_two_factor(page) -> None:
    """If PeerBerry prompts for a TOTP code after submitting credentials,
    generate one from PEERBERRY_TOTP_SECRET and fill it in.

    Verified against the real 2FA screen on 2026-07-09: 6 separate one-digit
    inputs named `s1`..`s6`; filling the last one auto-submits the form (no
    explicit "verify" button to click).
    """
    first_box = page.locator("input[name='s1']")
    try:
        first_box.wait_for(timeout=8000)
    except PlaywrightTimeoutError:
        return  # no 2FA prompt shown, nothing to do

    if not PEERBERRY_TOTP_SECRET:
        raise RuntimeError(
            "PeerBerry is asking for a 2FA code but PEERBERRY_TOTP_SECRET is not set. "
            "Set it to the base32 secret used to configure Google Authenticator."
        )

    log.info("2FA prompt detected, generating and submitting TOTP code...")
    code = pyotp.TOTP(PEERBERRY_TOTP_SECRET).now()
    for i, digit in enumerate(code, start=1):
        human_type(page.locator(f"input[name='s{i}']"), digit)

    page.wait_for_timeout(1500)
    error_text = page.locator("text=Auth code is invalid")
    if error_text.count() > 0:
        raise RuntimeError("PeerBerry rejected the TOTP code (invalid/expired).")


def login(page) -> None:
    """Log in to PeerBerry using PEERBERRY_EMAIL/PASSWORD (and
    PEERBERRY_TOTP_SECRET if 2FA is enabled).

    Selectors verified against the real login form on 2026-07-09:
    `input[name='email']` / `input[name='password']` and a
    `button[type='submit']` labeled "Log in". The submit button is clicked
    via JS (`element.click()` through `page.evaluate`) rather than
    Playwright's normal click - the latter's actionability check
    ("visible, enabled and stable") kept timing out, apparently because of
    an overlapping chat-widget element, even though the button is genuinely
    clickable.
    """
    log.info("Navigating to login page...")
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    dismiss_cookie_banner(page)
    human_mouse_wander(page)

    # If a previous session was restored (see STORAGE_STATE_FILE) and is
    # still valid, PeerBerry redirects straight to /overview - nothing else
    # to do.
    page.wait_for_timeout(1000)
    if page.url.rstrip("/") != LOGIN_URL.rstrip("/"):
        log.info("Reused a previous session, already logged in at %s", page.url)
        return

    log.info("Filling in credentials...")
    human_type(page.locator("input[name='email']"), PEERBERRY_EMAIL)
    human_pause()
    human_type(page.locator("input[name='password']"), PEERBERRY_PASSWORD)
    human_pause()
    page.evaluate("document.querySelector(\"button[type='submit']\").click()")

    handle_two_factor(page)

    # This is a client-rendered SPA: the redirect away from the login URL
    # happens via client-side routing (pushState), not always a real
    # navigation event, and by the time we get here it may have already
    # happened - so poll the current URL instead of using
    # page.wait_for_url(), which can miss/outlast it.
    for _ in range(40):
        if page.url.rstrip("/") != LOGIN_URL.rstrip("/"):
            break
        page.wait_for_timeout(500)
    else:
        raise RuntimeError(f"Still on the login page after submitting credentials/2FA: {page.url}")
    log.info("Logged in successfully, current URL: %s", page.url)


def fetch_originator_distribution(page) -> list:
    """Fetch the per-loan-originator investment breakdown via PeerBerry's own
    API, using the `app_token` JWT cookie set at login as a bearer token
    (see module docstring for why a plain cookie-based fetch doesn't work).
    """
    log.info("Requesting originators API...")
    result = page.evaluate(
        """
        async (url) => {
            const match = document.cookie.match(/(?:^|; )app_token=([^;]+)/);
            const token = match ? decodeURIComponent(match[1]) : null;
            if (!token) {
                return { ok: false, status: 0, body: null, error: 'app_token cookie not found' };
            }
            const res = await fetch(url, { headers: { Authorization: 'Bearer ' + token } });
            return { ok: res.ok, status: res.status, body: await res.json().catch(() => null) };
        }
        """,
        ORIGINATORS_API_URL,
    )

    log.info("Originators API response: ok=%s status=%s", result.get("ok"), result.get("status"))
    if not result.get("ok"):
        raise RuntimeError(
            f"Originators API request failed (status={result.get('status')}, error={result.get('error')})"
        )

    body = result.get("body") or []
    log.info("Originators API returned %d raw entry(ies).", len(body))
    return body


def normalize_originators(payload: list) -> list:
    """Parse the raw API payload into {"originator", "company", "iso2",
    "amount", "part"} dicts with numeric amount/part, sorted by amount
    descending."""
    originators = []
    for entry in payload:
        try:
            amount = float(entry.get("amount"))
        except (TypeError, ValueError):
            amount = 0.0
        try:
            part = float(entry.get("part"))
        except (TypeError, ValueError):
            part = 0.0
        originators.append(
            {
                "originator": entry.get("originator") or "Unknown",
                "company": entry.get("company"),
                "iso2": entry.get("iso2"),
                "amount": amount,
                "part": part,
            }
        )
    originators.sort(key=lambda o: o["amount"], reverse=True)
    return originators


def fetch_current_month_interest_income(page) -> float:
    """Fetch this calendar month's "Interest income" total, as shown on the
    Account Summary page (https://peerberry.com/en/client/statement/account-summary).

    Verified against the real account on 2026-07-10: the page's default
    "This month" period (opening date = 1st of the current month, closing
    date = today) triggers `GET
    https://api.peerberry.com/v2/investor/account-summary?period=&startDate=<1st>&endDate=<today>`,
    returning `{"openingBalance": "6.20", "closingBalance": "0.98",
    "operations": {"DEPOSIT": "5000.00", "INVESTMENT": "-6578.71",
    "INTEREST": "9.88", "PRINCIPAL": "1563.61"}}` - `operations.INTEREST`
    matched the page's displayed "Interest income +€9.88" exactly (matches
    the user-supplied reference value). Like the originators endpoint (see
    module docstring), this is on api.peerberry.com so it needs the
    `app_token` cookie sent as an `Authorization: Bearer` header (a plain
    `credentials: 'include'` fetch fails CORS).
    """
    now = datetime.now(REPORT_TIMEZONE)
    start_date = now.replace(day=1).strftime("%Y-%m-%d")
    end_date = now.strftime("%Y-%m-%d")
    log.info("Requesting account-summary API for %s to %s...", start_date, end_date)

    result = page.evaluate(
        """
        async ([url, startDate, endDate]) => {
            const match = document.cookie.match(/(?:^|; )app_token=([^;]+)/);
            const token = match ? decodeURIComponent(match[1]) : null;
            if (!token) {
                return { ok: false, status: 0, body: null, error: 'app_token cookie not found' };
            }
            const qs = new URLSearchParams({ period: '', startDate, endDate }).toString();
            const res = await fetch(`${url}?${qs}`, { headers: { Authorization: 'Bearer ' + token } });
            return { ok: res.ok, status: res.status, body: await res.json().catch(() => null) };
        }
        """,
        [ACCOUNT_SUMMARY_API_URL, start_date, end_date],
    )

    log.info("Account summary API response: ok=%s status=%s", result.get("ok"), result.get("status"))
    if not result.get("ok"):
        raise RuntimeError(
            f"Account summary API request failed (status={result.get('status')}, error={result.get('error')})"
        )

    operations = (result.get("body") or {}).get("operations") or {}
    log.info("Raw 'operations' block from the account summary API: %r", operations)
    try:
        interest_income = float(operations.get("INTEREST") or 0.0)
    except (TypeError, ValueError):
        log.warning("Could not parse 'INTEREST' value %r as a float - defaulting to 0.0.", operations.get("INTEREST"))
        interest_income = 0.0

    log.info("Parsed this month's Interest income: %.2f EUR", interest_income)
    return interest_income


def update_google_sheet(originators: list, interest_income: float) -> None:
    """Skeleton: write the per-loan-originator amounts and this month's
    Interest income into the Google Sheet.

    Not implemented yet on purpose - fill in the actual cell/row mapping
    once you know which cells in which sheet should hold which originator's
    amount. `google_sheet.py` already provides `get_latest_dashboard_worksheet()`
    (picks the most recent "Dashboard <year>" tab) as a starting point, e.g.:

        from google_sheet import get_latest_dashboard_worksheet, SPREADSHEET_ID
        worksheet = get_latest_dashboard_worksheet(SPREADSHEET_ID)
        for o in originators:
            ...  # look up the right cell for o["originator"] and write o["amount"]
        ...  # look up the right cell for interest_income

    Left as a no-op for now so running this script never requires
    GOOGLE_SHEET_ID/GOOGLE_CREDENTIALS to be set.
    """
    log.info(
        "update_google_sheet() is not implemented yet - skipping (%d originator(s), "
        "interest_income=%.2f available).",
        len(originators), interest_income,
    )


def run(headless: bool = True) -> None:
    if not PEERBERRY_EMAIL or not PEERBERRY_PASSWORD:
        log.error("PEERBERRY_EMAIL and PEERBERRY_PASSWORD environment variables are required.")
        sys.exit(1)

    log.info("Starting PeerBerry diversification run (headless=%s, storage_state_exists=%s).", headless, STORAGE_STATE_FILE.exists())

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
            payload = fetch_originator_distribution(page)
        except Exception:
            log.exception("Failed to log in or fetch the loan originator distribution.")
            browser.close()
            sys.exit(1)

        try:
            log.info("Navigating to the account summary page to fetch this month's Interest income...")
            page.goto(STATEMENT_URL, wait_until="domcontentloaded")
            interest_income = fetch_current_month_interest_income(page)
        except Exception:
            log.exception("Failed to fetch this month's Interest income - defaulting to 0.0.")
            interest_income = 0.0

        # Persist cookies/local storage so the next run can skip login (and
        # 2FA) while the session remains valid.
        context.storage_state(path=str(STORAGE_STATE_FILE))
        browser.close()

    originators = normalize_originators(payload)
    log.info("Fetched distribution for %d loan originator(s).", len(originators))
    for o in originators:
        log.info("  %s (%s, %s): %.2f EUR (%.2f%%)", o["originator"], o["company"], o["iso2"], o["amount"], o["part"])

    log.info("This month's Interest income: %.2f EUR", interest_income)

    update_google_sheet(originators, interest_income)


if __name__ == "__main__":
    # Set headless=False locally (e.g. via `python peerberry_monitor.py --show`)
    # to watch the browser and debug the login flow if selectors need adjusting.
    run(headless="--show" not in sys.argv)
