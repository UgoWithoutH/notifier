"""Afranga portfolio diversification (by loan originator) fetcher.

Logs into afranga.com (email/password + a single-field Google Authenticator
TOTP code, unlike Swaper/Lendermarket/PeerBerry's multi-box inputs) and
fetches every active investment from the "My investments"
(https://afranga.com/profile/my-investments) page, then groups them by loan
originator and sums the outstanding (remaining) investment amount per
originator. No email is sent - the amounts are just logged and handed to
update_google_sheet() (currently a skeleton, see its docstring) so they can
be filled into a Google Sheet, mirroring peerberry_diversification.py /
lendermarket_diversification.py.

Unlike PeerBerry/Lendermarket, Afranga has no JSON API for this - "My
investments" is an old-school server-rendered (Laravel + jQuery) page: the
table is populated by
`GET https://afranga.com/profile/my-investments/refresh?<filters>&limit=N`
which returns an HTML fragment (a `<table id="myInvestmentsTable">`), not
JSON. Verified against the real account on 2026-07-09:
- Requires a `_token` CSRF query param, read from `input[name='_token']` on
  the my-investments page itself (a hidden field of the filter form).
- `limit` must be one of the dropdown's allowed values (25/50/100/250) -
  250 is used here to fetch everything in one call; larger values return
  HTTP 400. No offset/page param was found, so accounts with more than 250
  active investments would be truncated (logged as a warning if the exact
  cap is hit - fine for now, revisit if/when the portfolio grows that big).
- Each `<tr>` has the loan originator as an `<img alt="X logo">` (never as
  plain text) in the 5th `<td>`, and the outstanding amount as
  "€ 1 234.56"-formatted text in the 11th `<td>` ("Outstanding Investment"
  column - the remaining/still-invested capital, as opposed to "Invested
  Amount" which is the original amount before any repayments). The last row
  is always a "Total:" summary row, not a real investment, and is skipped.
- The HTML fragment is parsed with the browser's own `DOMParser` (via
  `page.evaluate`), not a Python HTML parser - avoids adding a new
  dependency (e.g. BeautifulSoup) just for this.

Required env vars:
    AFRANGA_EMAIL, AFRANGA_PASSWORD    -> Afranga account credentials
Optional:
    AFRANGA_TOTP_SECRET                -> base32 secret used to set up
                                           Google Authenticator, needed if
                                           2FA is enabled on the account
    GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS -> only needed once update_google_sheet()
                                           below is filled in (see google_sheet.py)
"""

import os
import re
import sys
import logging
from pathlib import Path

import pyotp
from dotenv import load_dotenv

load_dotenv()

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from browser_stealth import get_context_options, apply_stealth, human_pause, human_mouse_wander, human_type

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("afranga_diversification")

LOGIN_URL = "https://afranga.com/login"
MY_INVESTMENTS_URL = "https://afranga.com/profile/my-investments"
REFRESH_URL = "https://afranga.com/profile/my-investments/refresh"
STORAGE_STATE_FILE = Path(__file__).parent / "afranga_diversification_storage_state.json"
MAX_LIMIT = 250  # largest value offered by the page's own rows-per-page dropdown

AFRANGA_EMAIL = os.environ.get("AFRANGA_EMAIL")
AFRANGA_PASSWORD = os.environ.get("AFRANGA_PASSWORD")
AFRANGA_TOTP_SECRET = os.environ.get("AFRANGA_TOTP_SECRET")


def dismiss_cookie_banner(page) -> None:
    """Dismiss the cookie consent dialog if it shows up."""
    for label in ["Accept all", "Accept necessary"]:
        try:
            page.get_by_role("button", name=label).click(timeout=3000)
            return
        except PlaywrightTimeoutError:
            continue


def handle_two_factor(page) -> None:
    """If Afranga prompts for a TOTP code after submitting credentials,
    generate one from AFRANGA_TOTP_SECRET and fill it in.

    Verified against the real 2FA screen on 2026-07-09: a single text field
    ("Enter the code from Google Authenticator:") and a "Verify" button -
    unlike the multi-box inputs used by Swaper/Lendermarket/PeerBerry.
    """
    otp_input = page.get_by_role("textbox", name="Enter the code from Google Authenticator:")
    try:
        otp_input.wait_for(timeout=8000)
    except PlaywrightTimeoutError:
        return  # no 2FA prompt shown, nothing to do

    if not AFRANGA_TOTP_SECRET:
        raise RuntimeError(
            "Afranga is asking for a 2FA code but AFRANGA_TOTP_SECRET is not set. "
            "Set it to the base32 secret used to configure Google Authenticator."
        )

    log.info("2FA prompt detected, generating and submitting TOTP code...")
    code = pyotp.TOTP(AFRANGA_TOTP_SECRET).now()
    human_type(otp_input, code)
    human_pause()

    try:
        # Defensive, same as the fix applied to lendermarket_monitor.py on
        # 2026-07-09: if the form auto-submits once the code is filled in,
        # this click would otherwise race against the navigation and hang.
        page.get_by_role("button", name="Verify").click(timeout=5000)
    except PlaywrightTimeoutError:
        log.info("'Verify' button not found/clickable - the code likely auto-submitted already.")


def login(page) -> None:
    """Log in to Afranga using AFRANGA_EMAIL/PASSWORD (and
    AFRANGA_TOTP_SECRET if 2FA is enabled).

    Selectors verified against the real login form on 2026-07-09:
    `input[name='email']` / `input[name='password']` and a "Log in" button.
    Note: the 2FA step (if shown) stays on the same /login URL (only the
    page title/content changes to "2FA Verification"), so the "already
    logged in" check below only makes sense right after the initial
    navigation - before that URL could have changed to anything else, i.e.
    it can't be used to detect 2FA completion, only the final `for` loop
    (which just polls until the URL isn't /login anymore, however we got
    there) can.
    """
    log.info("Navigating to login page...")
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    dismiss_cookie_banner(page)
    human_mouse_wander(page)

    # If a previous session was restored (see STORAGE_STATE_FILE) and is
    # still valid, Afranga redirects away from /login immediately - nothing
    # else to do.
    page.wait_for_timeout(1000)
    if page.url.rstrip("/") != LOGIN_URL.rstrip("/"):
        log.info("Reused a previous session, already logged in at %s", page.url)
        return

    log.info("Filling in credentials...")
    human_type(page.locator("input[name='email']"), AFRANGA_EMAIL)
    human_pause()
    human_type(page.locator("input[name='password']"), AFRANGA_PASSWORD)
    human_pause()
    page.get_by_role("button", name="Log in").click()

    handle_two_factor(page)

    for _ in range(40):
        if page.url.rstrip("/") != LOGIN_URL.rstrip("/"):
            break
        page.wait_for_timeout(500)
    else:
        raise RuntimeError(f"Still on the login page after submitting credentials/2FA: {page.url}")
    log.info("Logged in successfully, current URL: %s", page.url)


def fetch_investments(page) -> list:
    """Fetch every active investment by calling the same HTML-fragment
    endpoint the "My investments" page itself uses (see module docstring
    for the verified shape/quirks). Parses the response with the browser's
    own DOMParser and returns a list of {"originator", "outstanding"} dicts
    (amounts as floats), skipping the trailing "Total:" summary row.
    """
    csrf_token = page.locator("input[name='_token']").first.get_attribute("value")
    if not csrf_token:
        raise RuntimeError("Could not find the CSRF _token on the My investments page.")

    result = page.evaluate(
        """
        async ([refreshUrl, token, maxLimit]) => {
            const params = new URLSearchParams({
                _token: token,
                'interest_rate_percent[from]': '', 'interest_rate_percent[to]': '',
                'period[from]': '', 'period[to]': '',
                'created_at[from]': '', 'created_at[to]': '',
                'invested_amount[from]': '', 'invested_amount[to]': '',
                'loan[type]': '', 'loan[early_repayment_possible]': '',
                limit: String(maxLimit),
            });
            const res = await fetch(`${refreshUrl}?${params.toString()}`, { credentials: 'include' });
            const html = await res.text();
            if (!res.ok) {
                return { ok: false, status: res.status, rows: [] };
            }
            const doc = new DOMParser().parseFromString(html, 'text/html');
            const rows = Array.from(doc.querySelectorAll('#myInvestmentsTable tbody tr'));
            const investments = rows.map((row) => {
                const cells = row.querySelectorAll('td');
                const loanIdText = cells[1] ? cells[1].textContent.replace('Loan ID', '').trim() : '';
                const originatorImg = cells[4] ? cells[4].querySelector('img') : null;
                const originator = originatorImg ? originatorImg.getAttribute('alt') : null;
                const outstandingText = cells[10] ? cells[10].textContent.replace('Outstanding Investment', '').trim() : '';
                return { loanIdText, originator, outstandingText };
            });
            return { ok: true, status: res.status, rows: investments };
        }
        """,
        [REFRESH_URL, csrf_token, MAX_LIMIT],
    )

    if not result.get("ok"):
        raise RuntimeError(f"My investments refresh endpoint returned status {result.get('status')}")

    rows = result.get("rows") or []
    # The trailing "Total:" row has no loan ID / originator - skip it.
    investments = [r for r in rows if r.get("loanIdText") and r.get("originator")]

    if len(investments) >= MAX_LIMIT:
        log.warning(
            "Fetched exactly the max page size (%d) - the portfolio may have more investments than "
            "this endpoint can return in one call (no pagination param is known to work here).",
            MAX_LIMIT,
        )

    return investments


def _parse_amount(text: str) -> float:
    """Parse a "€ 1 234.56"-formatted amount into a float."""
    if not text:
        return 0.0
    cleaned = text.replace("€", "").replace("\xa0", "").replace(" ", "").strip()
    cleaned = re.sub(r"[^\d.]", "", cleaned)
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def aggregate_by_originator(investments: list) -> list:
    """Group investments by loan originator and sum the outstanding
    (remaining) investment amount for each - one entry per originator,
    sorted by amount descending."""
    totals = {}
    for inv in investments:
        originator = re.sub(r"\s*logo$", "", inv.get("originator") or "Unknown", flags=re.IGNORECASE).strip()
        amount = _parse_amount(inv.get("outstandingText"))
        totals[originator] = totals.get(originator, 0.0) + amount

    originators = [{"originator": name, "outstanding": amount} for name, amount in totals.items()]
    originators.sort(key=lambda o: o["outstanding"], reverse=True)
    return originators


def update_google_sheet(originators: list) -> None:
    """Skeleton: write the per-originator outstanding amounts into the
    Google Sheet. Mirrors peerberry_diversification.update_google_sheet() /
    lendermarket_diversification.update_google_sheet() - not implemented
    yet on purpose, fill in the actual cell/row mapping once you know which
    cells should hold which originator's amount, e.g.:

        from google_sheet import get_latest_dashboard_worksheet, SPREADSHEET_ID
        worksheet = get_latest_dashboard_worksheet(SPREADSHEET_ID)
        for o in originators:
            ...  # look up the right cell for o["originator"] and write o["outstanding"]

    Left as a no-op for now so running this script never requires
    GOOGLE_SHEET_ID/GOOGLE_CREDENTIALS to be set.
    """
    log.info("update_google_sheet() is not implemented yet - skipping (%d originator(s) available).", len(originators))


def run(headless: bool = True) -> None:
    if not AFRANGA_EMAIL or not AFRANGA_PASSWORD:
        log.error("AFRANGA_EMAIL and AFRANGA_PASSWORD environment variables are required.")
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
            page.goto(MY_INVESTMENTS_URL, wait_until="domcontentloaded")
            investments = fetch_investments(page)
        except Exception:
            log.exception("Failed to log in or fetch Afranga investments.")
            browser.close()
            sys.exit(1)

        # Persist cookies/local storage so the next run can skip login (and
        # 2FA) while the session remains valid.
        context.storage_state(path=str(STORAGE_STATE_FILE))
        browser.close()

    originators = aggregate_by_originator(investments)
    log.info("Fetched %d active investment(s) across %d loan originator(s).", len(investments), len(originators))
    for o in originators:
        log.info("  %s: %.2f EUR", o["originator"], o["outstanding"])

    update_google_sheet(originators)


if __name__ == "__main__":
    # Set headless=False locally (e.g. via `python afranga_diversification.py --show`)
    # to watch the browser and debug the login flow if selectors need adjusting.
    run(headless="--show" not in sys.argv)
