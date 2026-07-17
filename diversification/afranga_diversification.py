"""Afranga portfolio diversification (by loan originator) fetcher.

Logs into afranga.com (email/password + a single-field Google Authenticator
TOTP code, unlike Swaper/Lendermarket/PeerBerry's multi-box inputs) and
fetches every active investment from the "My investments"
(https://afranga.com/profile/my-investments) page, then groups them by loan
originator and sums the outstanding (remaining) investment amount per
originator. No email is sent - the amounts are just logged and handed to
fill_current_month_amounts() (see google_sheet.py) so they can
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

Also fetches this calendar month's "Gross interest received" and
"Withholding Tax" from the Account Statement page
(https://afranga.com/profile/account-statement) - see
fetch_current_month_statement_totals() below, same idea as
swaper_diversification.fetch_current_month_interest_received() /
loanch_diversification.fetch_current_month_statement_totals().

Required env vars:
    AFRANGA_EMAIL, AFRANGA_PASSWORD    -> Afranga account credentials
Optional:
    AFRANGA_TOTP_SECRET                -> base32 secret used to set up
                                           Google Authenticator, needed if
                                           2FA is enabled on the account
    GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS -> used to write this month's totals to
                                           the Google Sheet via
                                           fill_current_month_amounts() (see
                                           google_sheet.py)
"""

from shared.browser_stealth import get_context_options, apply_stealth, human_pause, human_mouse_wander, human_type
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
import os
import re
import sys
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pyotp
from dotenv import load_dotenv

from shared.google_sheet import fill_current_month_amounts, fill_current_month_bonus_breakdown, fill_geographic_repartition_amounts
from shared.report_date import get_report_now

load_dotenv()


logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("afranga_diversification")

LOGIN_URL = "https://afranga.com/login"
MY_INVESTMENTS_URL = "https://afranga.com/profile/my-investments"
REFRESH_URL = "https://afranga.com/profile/my-investments/refresh"
STATEMENT_PAGE_URL = "https://afranga.com/profile/account-statement"
STATEMENT_REFRESH_URL = "https://afranga.com/profile/account-statement/refresh"
BONUS_CASHBACK_HISTORY_URL = "https://afranga.com/profile/bonus-cashback/history"
STORAGE_STATE_FILE = Path(__file__).parent / \
    "afranga_diversification_storage_state.json"
MAX_LIMIT = 250  # largest value offered by the page's own rows-per-page dropdown
# Afranga's own "Current Month" quick filter on the Account Statement page
# (verified 2026-07-10 by capturing its request) uses the CURRENT calendar
# month up to TODAY (createdAt[from] = 1st of the month, createdAt[to] =
# today) - same "this month, not the full month" semantics as Swaper's
# equivalent filter. Pin the timezone explicitly (rather than relying on the
# executing machine's local clock, e.g. UTC on a CI runner) so "today"/"this
# month" are computed in the account's own local time.
REPORT_TIMEZONE = ZoneInfo("Europe/Paris")

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
    and a "Verify" button - unlike the multi-box inputs used by
    Swaper/Lendermarket/PeerBerry. Originally located via the accessible
    name "Enter the code from Google Authenticator:" (the heading text
    above the field), but re-verified on 2026-07-15 that this heading is
    NOT actually associated with the input as its accessible name - the
    input's real accessible name is its "Verification code" placeholder.
    The old locator silently never matched (get_by_role() timed out and
    handle_two_factor() treated that as "no 2FA prompt shown"), leaving the
    code never submitted and the login stuck on /login. Located by
    placeholder instead, which matches the real DOM.
    """
    otp_input = page.get_by_placeholder("Verification code")
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
        log.info(
            "'Verify' button not found/clickable - the code likely auto-submitted already.")


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
        raise RuntimeError(
            f"Still on the login page after submitting credentials/2FA: {page.url}")
    log.info("Logged in successfully, current URL: %s", page.url)


def fetch_investments(page) -> list:
    """Fetch every active investment by calling the same HTML-fragment
    endpoint the "My investments" page itself uses (see module docstring
    for the verified shape/quirks). Parses the response with the browser's
    own DOMParser and returns a list of {"originator", "outstanding"} dicts
    (amounts as floats), skipping the trailing "Total:" summary row.
    """
    csrf_token = page.locator(
        "input[name='_token']").first.get_attribute("value")
    if not csrf_token:
        raise RuntimeError(
            "Could not find the CSRF _token on the My investments page.")
    log.info("Requesting My investments refresh endpoint (limit=%d)...", MAX_LIMIT)

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

    log.info("My investments refresh endpoint response: ok=%s status=%s",
             result.get("ok"), result.get("status"))
    if not result.get("ok"):
        raise RuntimeError(
            f"My investments refresh endpoint returned status {result.get('status')}")

    rows = result.get("rows") or []
    log.info("Parsed %d raw row(s) from the My investments table (before filtering the Total row).", len(rows))
    # The trailing "Total:" row has no loan ID / originator - skip it.
    investments = [r for r in rows if r.get(
        "loanIdText") and r.get("originator")]
    log.info("%d row(s) remain after filtering out the trailing Total row.", len(
        investments))

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
    cleaned = text.replace("€", "").replace(
        "\xa0", "").replace(" ", "").strip()
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
        originator = re.sub(r"\s*logo$", "", inv.get("originator")
                            or "Unknown", flags=re.IGNORECASE).strip()
        amount = _parse_amount(inv.get("outstandingText"))
        totals[originator] = totals.get(originator, 0.0) + amount

    originators = [{"originator": name, "outstanding": amount}
                   for name, amount in totals.items()]
    originators.sort(key=lambda o: o["outstanding"], reverse=True)
    return originators


def fetch_current_month_statement_totals(page) -> dict:
    """Fetch this calendar month's "Gross interest received" and
    "Withholding Tax" totals, as shown in the "Transaction Summary" panel
    of the Account Statement page (https://afranga.com/profile/account-statement),
    via the same HTML-fragment endpoint the page's own "Current Month" quick
    filter uses (same idea as
    swaper_diversification.fetch_current_month_interest_received() /
    loanch_diversification.fetch_current_month_statement_totals()).

    Verified against the real account on 2026-07-10:

    1. Clicking "Current Month" on the Account Statement page sends
       `GET https://afranga.com/profile/account-statement/refresh?_token=...&createdAt[from]=<1st of month, dd.mm.yyyy>&createdAt[to]=<today, dd.mm.yyyy>`
       - reproduced here the same way (needs the same `_token` CSRF param
       as fetch_investments(), read from the same `input[name='_token']`
       field, present on this page too).
    2. The response is an HTML fragment (like the my-investments refresh
       endpoint) whose "Transaction Summary" panel has one `<div
       class="row">` per summary line, each with a `.text-18-400-gray`
       label `<div>` and a sibling `.text-18-400` value `<div>` (e.g.
       "Deposited funds" / "€ 500.00"). The "Gross interest received" row's
       label actually reads "Gross interest received (net € X.XX)" (the net
       amount is baked into the label itself) - matched here via
       `startswith` rather than an exact string. "Withholding Tax" matches
       exactly. Confirmed against June 2026 data: Gross interest received =
       5.51 EUR, Withholding Tax = 0.57 EUR.
    3. Both rows are only rendered when non-zero (e.g. they were entirely
       absent when fetching the empty first-10-days-of-July range during
       exploration) - a missing row is treated as 0.0, not an error.
    """
    now = get_report_now(REPORT_TIMEZONE)
    start_date = now.replace(day=1).strftime("%d.%m.%Y")
    end_date = now.strftime("%d.%m.%Y")

    csrf_token = page.locator(
        "input[name='_token']").first.get_attribute("value")
    if not csrf_token:
        raise RuntimeError(
            "Could not find the CSRF _token on the Account statement page.")
    log.info("Requesting Account statement refresh endpoint for %s to %s...",
             start_date, end_date)

    result = page.evaluate(
        """
        async ([refreshUrl, token, from_, to_]) => {
            const params = new URLSearchParams({ _token: token, 'createdAt[from]': from_, 'createdAt[to]': to_ });
            const res = await fetch(`${refreshUrl}?${params.toString()}`, { credentials: 'include' });
            const html = await res.text();
            if (!res.ok) {
                return { ok: false, status: res.status, rows: [] };
            }
            const doc = new DOMParser().parseFromString(html, 'text/html');
            const rows = Array.from(doc.querySelectorAll('.row')).map((row) => {
                const label = row.querySelector('.text-18-400-gray');
                const value = row.querySelector('.text-18-400, .text-18-500, .text-18-600');
                return label ? { label: label.textContent.trim(), value: value ? value.textContent.trim() : null } : null;
            }).filter(Boolean);
            return { ok: true, status: res.status, rows };
        }
        """,
        [STATEMENT_REFRESH_URL, csrf_token, start_date, end_date],
    )
    log.info("Account statement refresh endpoint response: ok=%s status=%s",
             result.get("ok"), result.get("status"))
    if not result.get("ok"):
        raise RuntimeError(
            f"Account statement refresh endpoint returned status {result.get('status')}")

    rows = result.get("rows") or []
    log.info("Found %d summary row(s) in the Transaction Summary panel: %r", len(
        rows), [r.get("label") for r in rows])

    gross_interest_received = 0.0
    withholding_tax = 0.0
    for row in rows:
        label = row.get("label") or ""
        if label.startswith("Gross interest received"):
            gross_interest_received = _parse_amount(row.get("value"))
        elif label == "Withholding Tax":
            withholding_tax = _parse_amount(row.get("value"))

    log.info(
        "Parsed statement totals: gross_interest_received=%.2f, withholding_tax=%.2f",
        gross_interest_received, withholding_tax,
    )
    return {"gross_interest_received": gross_interest_received, "withholding_tax": withholding_tax}


def fetch_current_month_bonus_cashback(page) -> float:
    """Fetch this calendar month's paid Bonus Cashback total, by scraping
    the "Recent Completed Campaigns" table on
    https://afranga.com/profile/bonus-cashback/history and summing the
    "Bonus Paid" amount of every row whose "Payout Date" falls within the
    current calendar month (1st of the month through today).

    Discovered on 2026-07-17 via a deeper nav-link crawl (going beyond the
    Account Statement page previously checked) - completely missed before:
    a dedicated "Bonus Cashback Campaigns" page (linked from the profile
    overview page), with lifetime totals ("Total Earned"/"Total Paid"/
    "Pending") plus a per-campaign history table. No JSON API backs this
    page (only marketing/analytics beacons were seen in a network capture)
    - it's server-rendered HTML, so the table itself is scraped here, same
    general approach as afranga_diversification's Transaction Summary
    panel above.

    Unlike Swaper's referral bonus page (a single lifetime total with no
    per-event date), each completed campaign here DOES have its own real
    "Payout Date" (e.g. "2025-09-24"), so a genuine "this month" figure can
    be computed by filtering on it - no lifetime-vs-monthly ambiguity here.
    Verified against the real account on 2026-07-17: only one completed
    campaign exists so far (Lendivo, paid out 2025-09-24, 5.00 EUR), so this
    month's total is correctly 0.00 - a real computed result, not a
    hardcoded assumption.
    """
    now = get_report_now(REPORT_TIMEZONE)
    start_date = now.replace(day=1).date()
    end_date = now.date()
    log.info("Requesting Bonus Cashback history page to sum this month's (%s to %s) paid bonuses...", start_date, end_date)

    rows = page.evaluate(
        """
        () => {
            const table = document.querySelector('table');
            if (!table) return [];
            const headers = Array.from(table.querySelectorAll('thead th')).map(th => th.textContent.trim());
            const bonusPaidIdx = headers.indexOf('Bonus Paid');
            const payoutDateIdx = headers.indexOf('Payout Date');
            return Array.from(table.querySelectorAll('tbody tr')).map((tr) => {
                const cells = tr.querySelectorAll('td');
                const bonusPaidCell = cells[bonusPaidIdx];
                const payoutDateCell = cells[payoutDateIdx];
                return {
                    bonusPaid: bonusPaidCell ? bonusPaidCell.textContent.replace(/\\s+/g, ' ').trim() : null,
                    payoutDate: payoutDateCell ? payoutDateCell.textContent.replace(/\\s+/g, ' ').trim() : null,
                };
            });
        }
        """
    )
    log.info("Found %d completed campaign row(s) in the Bonus Cashback history table: %r", len(rows), rows)

    total = 0.0
    for row in rows:
        payout_date_match = re.search(r"\d{4}-\d{2}-\d{2}", row.get("payoutDate") or "")
        if not payout_date_match:
            continue
        payout_date = datetime.strptime(payout_date_match.group(), "%Y-%m-%d").date()
        if start_date <= payout_date <= end_date:
            total += _parse_amount(row.get("bonusPaid"))

    log.info("This month's paid Bonus Cashback total: %.2f EUR", total)
    return total


def run(headless: bool = True) -> None:
    if not AFRANGA_EMAIL or not AFRANGA_PASSWORD:
        log.error(
            "AFRANGA_EMAIL and AFRANGA_PASSWORD environment variables are required.")
        sys.exit(1)

    log.info("Starting Afranga diversification run (headless=%s, storage_state_exists=%s).",
             headless, STORAGE_STATE_FILE.exists())

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        storage_state = str(
            STORAGE_STATE_FILE) if STORAGE_STATE_FILE.exists() else None
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

        try:
            log.info(
                "Navigating to the account statement page to fetch this month's statement totals...")
            page.goto(STATEMENT_PAGE_URL, wait_until="domcontentloaded")
            statement_totals = fetch_current_month_statement_totals(page)
        except Exception:
            log.exception(
                "Failed to fetch this month's Gross interest received/Withholding Tax - defaulting both to 0.0."
            )
            statement_totals = {
                "gross_interest_received": 0.0, "withholding_tax": 0.0}

        try:
            log.info(
                "Navigating to the Bonus Cashback history page to fetch this month's paid bonuses...")
            page.goto(BONUS_CASHBACK_HISTORY_URL, wait_until="domcontentloaded")
            bonus_cashback = fetch_current_month_bonus_cashback(page)
        except Exception:
            log.exception(
                "Failed to fetch this month's Bonus Cashback total - defaulting to 0.0.")
            bonus_cashback = 0.0

        # Persist cookies/local storage so the next run can skip login (and
        # 2FA) while the session remains valid.
        context.storage_state(path=str(STORAGE_STATE_FILE))
        browser.close()

    originators = aggregate_by_originator(investments)
    log.info("Fetched %d active investment(s) across %d loan originator(s).", len(
        investments), len(originators))
    for o in originators:
        log.info("  %s: %.2f EUR", o["originator"], o["outstanding"])

    statement_totals["total"] = sum(o["outstanding"] for o in originators)
    statement_totals["net_interest_received"] = (
        statement_totals["gross_interest_received"] -
        statement_totals["withholding_tax"]
    )
    # bonus_cashback_contest is now genuinely fetched (see
    # fetch_current_month_bonus_cashback()) from the dedicated Bonus
    # Cashback history page - previously hardcoded to 0.0 based on an
    # insufficiently thorough check of the Transaction Summary panel only,
    # which missed this page entirely.
    statement_totals["bonus_cashback_contest"] = bonus_cashback
    log.info(
        "This month's statement totals: total=%.2f EUR, gross_interest_received=%.2f EUR, "
        "net_interest_received=%.2f EUR, withholding_tax=%.2f EUR",
        statement_totals["total"], statement_totals["gross_interest_received"],
        statement_totals["net_interest_received"], statement_totals["withholding_tax"],
    )

    fill_current_month_amounts(
        platform="Afranga",
        amounts=statement_totals
    )

    # Afranga's bonus feature is literally called "Bonus Cashback
    # Campaigns" - a "cashback", not a prime/concours - written to its own
    # dedicated sub-row, never to the "Bonus" row itself (a SUM formula
    # over prime/cashback/concours).
    fill_current_month_bonus_breakdown(
        platform="Afranga",
        breakdown={"cashback": statement_totals["bonus_cashback_contest"]},
    )

    loan_originators = [
        {"name": o["originator"], "amount": o["outstanding"]}
        for o in originators
    ]

    fill_geographic_repartition_amounts(loan_originators)


if __name__ == "__main__":
    # Set headless=False locally (e.g. via `python afranga_diversification.py --show`)
    # to watch the browser and debug the login flow if selectors need adjusting.
    run(headless="--show" not in sys.argv)
