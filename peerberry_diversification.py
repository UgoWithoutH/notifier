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
from pathlib import Path

import pyotp
from dotenv import load_dotenv

load_dotenv()

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from browser_stealth import get_context_options, apply_stealth, human_pause, human_mouse_wander, human_type

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("peerberry_diversification")

LOGIN_URL = "https://peerberry.com/en/client/"
ORIGINATORS_API_URL = "https://api.peerberry.com/v1/investor/overview/originators"
STORAGE_STATE_FILE = Path(__file__).parent / "peerberry_diversification_storage_state.json"

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

    if not result.get("ok"):
        raise RuntimeError(
            f"Originators API request failed (status={result.get('status')}, error={result.get('error')})"
        )

    return result.get("body") or []


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


def update_google_sheet(originators: list) -> None:
    """Skeleton: write the per-loan-originator amounts into the Google Sheet.

    Not implemented yet on purpose - fill in the actual cell/row mapping
    once you know which cells in which sheet should hold which originator's
    amount. `google_sheet.py` already provides `get_latest_dashboard_worksheet()`
    (picks the most recent "Dashboard <year>" tab) as a starting point, e.g.:

        from google_sheet import get_latest_dashboard_worksheet, SPREADSHEET_ID
        worksheet = get_latest_dashboard_worksheet(SPREADSHEET_ID)
        for o in originators:
            ...  # look up the right cell for o["originator"] and write o["amount"]

    Left as a no-op for now so running this script never requires
    GOOGLE_SHEET_ID/GOOGLE_CREDENTIALS to be set.
    """
    log.info("update_google_sheet() is not implemented yet - skipping (%d originator(s) available).", len(originators))


def run(headless: bool = True) -> None:
    if not PEERBERRY_EMAIL or not PEERBERRY_PASSWORD:
        log.error("PEERBERRY_EMAIL and PEERBERRY_PASSWORD environment variables are required.")
        sys.exit(1)

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

        # Persist cookies/local storage so the next run can skip login (and
        # 2FA) while the session remains valid.
        context.storage_state(path=str(STORAGE_STATE_FILE))
        browser.close()

    originators = normalize_originators(payload)
    log.info("Fetched distribution for %d loan originator(s).", len(originators))
    for o in originators:
        log.info("  %s (%s, %s): %.2f EUR (%.2f%%)", o["originator"], o["company"], o["iso2"], o["amount"], o["part"])

    update_google_sheet(originators)


if __name__ == "__main__":
    # Set headless=False locally (e.g. via `python peerberry_monitor.py --show`)
    # to watch the browser and debug the login flow if selectors need adjusting.
    run(headless="--show" not in sys.argv)
