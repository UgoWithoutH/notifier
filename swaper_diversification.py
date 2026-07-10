"""Swaper portfolio "loan originator breakdown" fetcher.

Same family as afranga_diversification.py / peerberry_diversification.py /
lendermarket_diversification.py / loanch_diversification.py: logs into
swaper.com (reusing swaper_monitor.login(), which already handles
email/password + TOTP 2FA - not duplicated here) and reads the "Loan
Originator Breakdown" widget on the Open Investments page
(https://swaper.com/en/investments/open-investments), which shows one
percentage per loan originator (e.g. "Wandoo Finance Group 14.44%", "SW
Finance 85.56%") plus the total currently allocated/invested amount (e.g.
"5076.18 €"). The per-originator EUR amount isn't shown directly, so it's
computed as `total_invested * percentage / 100`, per the user's own
instructions. No email is sent - the amounts are just logged and handed to
update_google_sheet() (currently a skeleton, see its docstring) so they can
be filled into a Google Sheet, mirroring the other *_diversification.py
scripts.

Widget markup verified against the real account on 2026-07-09 (a Recharts
pie chart + legend, both under `.statistics-pie-card`):
- The card whose `.title` is "Loan Originator Breakdown" contains a
  `.statistics-pie-bottom-container` with one `.statistics-legend-container`
  per originator: `<div class="statistics-pill-container">...<pill/>Wandoo
  Finance Group</div><div>14.44%</div>` - the originator name is the
  `.statistics-pill-container`'s own text (after the empty colored-pill
  div), the percentage is the container's second child `<div>`.
- The total invested amount is in a separate `.statistics-value-container`
  widget: `<div class="amount-text">5076.18 €</div><div
  class="value-text">Currently Allocated</div>` - found via the
  `.value-text` div whose text is "Currently Allocated", value read from
  its previous sibling `.amount-text`.

Also fetches this calendar month's "Interest Received" from the Account
Statement page (https://swaper.com/en/investments/account-statement) - see
fetch_current_month_interest_received() below, same idea as
loanch_diversification.fetch_current_month_statement_totals().

Required env vars:
    SWAPER_EMAIL, SWAPER_PASSWORD      -> Swaper account credentials (shared
                                           with swaper_monitor.py)
Optional:
    SWAPER_TOTP_SECRET                  -> base32 secret used to set up
                                            Google Authenticator, needed if
                                            2FA is enabled on the account
    GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS  -> only needed once update_google_sheet()
                                            below is filled in (see google_sheet.py)
"""

import re
import logging
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

from playwright.sync_api import sync_playwright

from browser_stealth import get_context_options, apply_stealth
from swaper_monitor import login, SWAPER_EMAIL, SWAPER_PASSWORD

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("swaper_diversification")

OPEN_INVESTMENTS_URL = "https://swaper.com/en/investments/open-investments"
STATEMENT_PAGE_URL = "https://swaper.com/en/investments/account-statement"
ACCOUNT_ENTRIES_API_URL = "https://swaper.com/rest/public/profile/account-entries"
STORAGE_STATE_FILE = Path(__file__).parent / "swaper_diversification_storage_state.json"
# Swaper's own "This Month" quick filter (verified 2026-07-10 by capturing its
# request) uses the CURRENT calendar month up to TODAY (bookingDateFrom = 1st
# of the month, bookingDateTo = today) - not the full month like Loanch's
# equivalent filter. Pin the timezone explicitly (rather than relying on the
# executing machine's local clock, e.g. UTC on a CI runner) so "today"/"this
# month" are computed in the account's own local time.
REPORT_TIMEZONE = ZoneInfo("Europe/Paris")


def _parse_amount(text: str):
    """Parse a currency-formatted amount (e.g. "5076.18 €", "5 076.18 €")
    into a float, without assuming a fixed locale - whichever of ',' or '.'
    appears last is treated as the decimal separator, the other (or
    repeats of it) as thousands separators."""
    if not text:
        return None
    cleaned = text.replace("\xa0", " ").strip()
    cleaned = re.sub(r"[^\d.,\s-]", "", cleaned).replace(" ", "")
    if not cleaned:
        return None

    has_comma, has_dot = "," in cleaned, "." in cleaned
    if has_comma and has_dot:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif has_comma:
        last_part = cleaned.rsplit(",", 1)[-1]
        if len(last_part) == 2:
            cleaned = cleaned.replace(",", "", cleaned.count(",") - 1).replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")

    try:
        return float(cleaned)
    except ValueError:
        return None


def fetch_breakdown(page) -> dict:
    """Navigate to the Open Investments page and read the "Loan Originator
    Breakdown" widget (per-originator percentages) and the "Currently
    Allocated" total invested amount. See module docstring for the
    verified selectors."""
    page.goto(OPEN_INVESTMENTS_URL, wait_until="networkidle")
    page.wait_for_selector(".statistics-pie-bottom-container", timeout=30000)
    page.wait_for_timeout(1000)  # let the chart/legend finish rendering

    raw = page.evaluate(
        """
        () => {
            const cards = Array.from(document.querySelectorAll('.statistics-pie-card'));
            const card = cards.find((c) => c.querySelector('.title') && c.querySelector('.title').textContent.includes('Loan Originator Breakdown'));
            const originators = [];
            if (card) {
                const legends = card.querySelectorAll('.statistics-pie-bottom-container .statistics-legend-container');
                legends.forEach((legend) => {
                    const nameEl = legend.querySelector('.statistics-pill-container');
                    const percentEl = nameEl ? nameEl.nextElementSibling : null;
                    if (nameEl && percentEl) {
                        originators.push({ name: nameEl.textContent.trim(), percentage: percentEl.textContent.trim() });
                    }
                });
            }

            let totalInvested = null;
            const valueText = Array.from(document.querySelectorAll('.value-text')).find((el) => el.textContent.trim() === 'Currently Allocated');
            if (valueText) {
                const amountEl = valueText.previousElementSibling;
                totalInvested = amountEl ? amountEl.textContent.trim() : null;
            }

            return { originators, totalInvested };
        }
        """
    )

    log.info("Raw values read from the Open Investments page: %r", raw)

    if not raw.get("originators"):
        raise RuntimeError("Could not find the 'Loan Originator Breakdown' widget on the Open Investments page.")
    if not raw.get("totalInvested"):
        raise RuntimeError("Could not find 'Currently Allocated' on the Open Investments page.")

    total_invested = _parse_amount(raw["totalInvested"])
    if total_invested is None:
        raise RuntimeError(f"Could not parse the total invested amount out of {raw['totalInvested']!r}.")

    originators = []
    for o in raw["originators"]:
        percentage = _parse_amount(o["percentage"])
        if percentage is None:
            raise RuntimeError(f"Could not parse the percentage out of {o['percentage']!r} for {o['name']!r}.")
        originators.append({"originator": o["name"], "percentage": percentage})

    return {"total_invested": total_invested, "originators": originators}


def compute_amounts(breakdown: dict) -> list:
    """Compute each originator's invested amount as
    `total_invested * percentage / 100`, sorted by amount descending."""
    total_invested = breakdown["total_invested"]
    amounts = [
        {"originator": o["originator"], "outstanding": round(total_invested * o["percentage"] / 100, 2)}
        for o in breakdown["originators"]
    ]
    amounts.sort(key=lambda o: o["outstanding"], reverse=True)
    return amounts


def fetch_current_month_interest_received(page) -> float:
    """Fetch this calendar month's "Interest Received" total, as shown on
    the Account Statement page's transactions summary
    (https://swaper.com/en/investments/account-statement), via the same
    `account-entries` API the page's own "This Month" quick filter uses.

    Verified against the real account on 2026-07-10:

    1. Clicking the "This Month" quick filter on the Account Statement tab
       sends `POST https://swaper.com/rest/public/profile/account-entries`
       with a JSON body including `bookingDateFrom`/`bookingDateTo` set to
       the 1st of the current month through TODAY (not the full calendar
       month like Loanch's equivalent filter) - reproduced here the same
       way. "Last Month" was also captured for comparison and confirmed to
       use the full previous month's first/last day instead.
    2. The response's `earnedInterest` field (12.19 for July 2026) matched
       the "Interest Received" figure shown in the summary card exactly
       (the other cards - "Bought Loans", "Sold Loans", "Deducted Taxes" -
       map to the response's `investments`/`soldInvestments`/`taxes` fields
       respectively, not used here since only Interest Received was asked
       for).
    3. This endpoint is CSRF-protected (plain `fetch(..., {credentials:
       'include'})` alone gets HTTP 403 "Forbidden") - unlike every other
       *_diversification.py's API calls so far. The required
       `X-XSRF-TOKEN` header value is NOT in a readable cookie (it's not
       exposed via `document.cookie` at all despite the header's name) -
       it's mirrored into `localStorage['X-XSRF-TOKEN']` (a JSON-quoted
       string) by the site's own JS, read from there instead.
    """
    now = datetime.now(REPORT_TIMEZONE)
    start_date = now.replace(day=1).strftime("%Y-%m-%d")
    end_date = now.strftime("%Y-%m-%d")

    result = page.evaluate(
        """
        async ([url, startDate, endDate]) => {
            const raw = localStorage.getItem('X-XSRF-TOKEN');
            const token = raw ? JSON.parse(raw) : null;
            const res = await fetch(url, {
                method: 'POST',
                credentials: 'include',
                headers: { 'content-type': 'application/json;charset=UTF-8', 'x-xsrf-token': token },
                body: JSON.stringify({
                    page: 1, pageSize: 9, sortOption: null,
                    interestRateFrom: null, interestRateTo: null,
                    remainingTermMonthsFrom: null, remainingTermMonthsTo: null,
                    availableInvestmentAmountFrom: null, availableInvestmentAmountTo: null,
                    countryCodes: [], amountFrom: null, amountTo: null, filtered: false,
                    transactionTypes: [], bookingDateFrom: startDate, bookingDateTo: endDate,
                }),
            });
            return { ok: res.ok, status: res.status, body: await res.json().catch(() => null) };
        }
        """,
        [ACCOUNT_ENTRIES_API_URL, start_date, end_date],
    )
    if not result.get("ok"):
        raise RuntimeError(f"Account entries API returned status {result.get('status')}")

    body = result.get("body") or {}
    return float(body.get("earnedInterest") or 0.0)


def update_google_sheet(originators: list, interest_received: float) -> None:
    """Skeleton: write the per-originator invested amounts and this month's
    Interest Received into the Google Sheet. Mirrors
    loanch_diversification.update_google_sheet() - not implemented yet on
    purpose, fill in the actual cell/row mapping once you know which cells
    should hold which value, e.g.:

        from google_sheet import get_latest_dashboard_worksheet, SPREADSHEET_ID
        worksheet = get_latest_dashboard_worksheet(SPREADSHEET_ID)
        for o in originators:
            ...  # look up the right cell for o["originator"] and write o["outstanding"]
        ...  # look up the right cell for interest_received

    Left as a no-op for now so running this script never requires
    GOOGLE_SHEET_ID/GOOGLE_CREDENTIALS to be set.
    """
    log.info(
        "update_google_sheet() is not implemented yet - skipping (%d originator(s), "
        "interest_received=%.2f available).",
        len(originators), interest_received,
    )


def run(headless: bool = True) -> None:
    if not SWAPER_EMAIL or not SWAPER_PASSWORD:
        log.error("SWAPER_EMAIL and SWAPER_PASSWORD environment variables are required.")
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
            breakdown = fetch_breakdown(page)
            page.goto(STATEMENT_PAGE_URL, wait_until="domcontentloaded")
            interest_received = fetch_current_month_interest_received(page)
        except Exception:
            log.exception("Failed to log in or fetch Swaper's diversification/statement data.")
            browser.close()
            sys.exit(1)

        # Persist cookies/local storage so the next run can skip login (and
        # 2FA) while the session remains valid.
        context.storage_state(path=str(STORAGE_STATE_FILE))
        browser.close()

    originators = compute_amounts(breakdown)
    log.info(
        "Total invested: %.2f EUR across %d loan originator(s):",
        breakdown["total_invested"], len(originators),
    )
    for o in originators:
        log.info("  %s: %.2f EUR", o["originator"], o["outstanding"])

    log.info("This month's Interest Received: %.2f EUR", interest_received)

    update_google_sheet(originators, interest_received)


if __name__ == "__main__":
    # Set headless=False locally (e.g. via `python swaper_diversification.py --show`)
    # to watch the browser and debug the login flow if selectors need adjusting.
    run(headless="--show" not in sys.argv)
