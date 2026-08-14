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
fill_current_month_amounts() (see google_sheet.py) so they can
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
    GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS  -> used to write this month's totals
                                            to the Google Sheet via
                                            fill_current_month_amounts() (see
                                            google_sheet.py)
"""

import re
import logging
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from shared.google_sheet import (
    fill_current_month_amounts,
    fill_current_month_bonus_breakdown,
    fill_geographic_repartition_amounts,
    fill_geographic_repartition_uninvested_amount,
)
from shared.report_date import get_report_now, is_current_month
from shared.xirr import compute_xirr

load_dotenv()

from playwright.sync_api import sync_playwright

from shared.browser_stealth import get_context_options, apply_stealth
from monitors.swaper_monitor import login, SWAPER_EMAIL, SWAPER_PASSWORD, fetch_loans, extract_balance

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("swaper_diversification")

OPEN_INVESTMENTS_URL = "https://swaper.com/en/investments/open-investments"
STATEMENT_PAGE_URL = "https://swaper.com/en/investments/account-statement"
ACCOUNT_ENTRIES_API_URL = "https://swaper.com/rest/public/profile/account-entries"
REFERRAL_BONUS_PAGE_URL = "https://swaper.com/en/bonuses/refer-friends"
STORAGE_STATE_FILE = Path(__file__).parent / "swaper_diversification_storage_state.json"
# Verified live 2026-08-14 (full-history probe): pageSize=1000 returned this
# account's entire history (352 records) in one page - a larger pageSize
# (5000) was REJECTED by the API with HTTP 400 (undocumented server-side
# cap). Kept as a generous page size/safety net even though only one
# calendar month's worth of entries is fetched now (see
# fetch_account_cashflows()).
XIRR_PAGE_SIZE = 1000
MAX_XIRR_PAGES = 20
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


def fetch_current_month_interest_received(page) -> dict:
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

    Also returns this SAME response's `openingBalance`/`closingBalance`
    fields (verified live 2026-08-14: `closingBalance` for a range ending
    today matches the live uninvested "non investi" balance exactly, e.g.
    8.19 EUR both ways) - the real uninvested-cash balance at the start/end
    of the queried range, needed by run() to compute the "Cash drag" row
    (average idle cash this month x the yield the invested capital earned
    this month).
    """
    now = get_report_now(REPORT_TIMEZONE)
    start_date = now.replace(day=1).strftime("%Y-%m-%d")
    end_date = now.strftime("%Y-%m-%d")
    log.info("Requesting account-entries API for booking dates %s to %s...", start_date, end_date)

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
    log.info("Account entries API response: ok=%s status=%s", result.get("ok"), result.get("status"))
    if not result.get("ok"):
        raise RuntimeError(f"Account entries API returned status {result.get('status')}")

    body = result.get("body") or {}
    raw_value = body.get("earnedInterest")
    log.info("Raw 'earnedInterest' value from the account entries API: %r", raw_value)
    try:
        earned_interest = float(raw_value or 0.0)
    except (TypeError, ValueError):
        log.warning("Could not parse 'earnedInterest' value %r as a float - defaulting to 0.0.", raw_value)
        earned_interest = 0.0

    try:
        opening_balance = float(body.get("openingBalance") or 0.0)
        closing_balance = float(body.get("closingBalance") or 0.0)
    except (TypeError, ValueError):
        log.warning("Could not parse openingBalance/closingBalance %r/%r - defaulting to 0.0.", body.get("openingBalance"), body.get("closingBalance"))
        opening_balance = closing_balance = 0.0

    return {"earned_interest": earned_interest, "opening_balance": opening_balance, "closing_balance": closing_balance}


def fetch_account_cashflows(page, start_date: str, end_date: str) -> list:
    """Fetch every real EXTERNAL cashflow (money moving in/out of the
    Swaper account itself, not just reallocated between cash and loans
    within it) within [start_date, end_date], via the same
    `account-entries` API used by fetch_current_month_interest_received()
    (see that function's docstring for the endpoint/auth details). Called
    by run() with THIS CALENDAR MONTH's own range, not the account's full
    history - see run() for why XIRR is now a per-month figure rather than
    a since-inception one (recomputing a since-inception XIRR every month
    would already be an "overall" number each time, making an average
    across months meaningless).

    Needed to reconstruct the cashflow list for the XIRR (money-weighted
    annualized return) calculation: only a DEPOSIT into the account (the
    `FUNDING` transaction type) or a WITHDRAWAL out of it are real external
    cashflows from the investor's own point of view - every other type
    observed on this API (`INVESTMENT`, `REPAYMENT_PRINCIPAL`,
    `REPAYMENT_INTEREST`, `BUYBACK_PRINCIPAL`, `BUYBACK_INTEREST`,
    `EXTENSION_INTEREST`, ...) just moves money between "uninvested cash"
    and "invested in a loan" WITHIN the account, so they must NOT be
    counted as separate cashflows (double-counting them would badly skew
    the result) - the account's final total value already reflects their
    net effect.

    Verified live 2026-08-14 against the real account: this account has 4
    `FUNDING` entries (all deposits, dates 2025-08-25 to 2026-07-03,
    5000.00 EUR total) and no `WITHDRAWAL`-type entry has ever appeared
    (this account has never withdrawn) - matched here via an exact
    case-insensitive "FUNDING" check for deposits and a case-insensitive
    "WITHDRAW" substring check for withdrawals (never actually observed,
    kept as a forward-looking safety net in case this account, or a future
    one, ever does withdraw). Returns a list of
    `{"date": "YYYY-MM-DD", "amount": float, "transactionType": str}`
    dicts - `amount` is always the ABSOLUTE value here (the caller decides
    the sign for XIRR purposes based on `transactionType`, since the raw
    API sign for a withdrawal-type entry has never actually been observed).
    """
    log.info(
        "Requesting the account-entries history (%s to %s) for this month's XIRR cashflow reconstruction...",
        start_date, end_date,
    )

    cashflows = []
    page_number = 1
    total_records = None
    while page_number <= MAX_XIRR_PAGES:
        result = page.evaluate(
            """
            async ([url, startDate, endDate, pageNumber, pageSize]) => {
                const raw = localStorage.getItem('X-XSRF-TOKEN');
                const token = raw ? JSON.parse(raw) : null;
                const res = await fetch(url, {
                    method: 'POST',
                    credentials: 'include',
                    headers: { 'content-type': 'application/json;charset=UTF-8', 'x-xsrf-token': token },
                    body: JSON.stringify({
                        page: pageNumber, pageSize: pageSize, sortOption: null,
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
            [ACCOUNT_ENTRIES_API_URL, start_date, end_date, page_number, XIRR_PAGE_SIZE],
        )
        if not result.get("ok"):
            raise RuntimeError(f"Account entries API returned status {result.get('status')} (page {page_number})")

        body = result.get("body") or {}
        data = body.get("data") or {}
        results = data.get("results") or []
        total_records = data.get("totalRecords")
        log.info("Page %d: %d entrie(s) found (totalRecords=%s).", page_number, len(results), total_records)

        for entry in results:
            transaction_type = (entry.get("transactionType") or "").strip()
            raw_date = entry.get("bookingDate")
            raw_amount = entry.get("amount")
            is_deposit = transaction_type.upper() == "FUNDING"
            is_withdrawal = "WITHDRAW" in transaction_type.upper()
            if not (is_deposit or is_withdrawal) or not raw_date or raw_amount is None:
                continue
            cashflows.append({
                "date": raw_date,
                "amount": abs(float(raw_amount)),
                "transactionType": transaction_type,
            })

        if total_records is None or len(results) == 0:
            break
        if page_number * XIRR_PAGE_SIZE >= total_records:
            break
        page_number += 1
    else:
        log.warning("Hit MAX_XIRR_PAGES (%d) without exhausting totalRecords=%s - cashflow history may be incomplete.", MAX_XIRR_PAGES, total_records)

    log.info("Found %d deposit/withdrawal entrie(s) for XIRR out of totalRecords=%s.", len(cashflows), total_records)
    return cashflows


def fetch_referral_bonus_earned(page) -> float:
    """Fetch the "Earned from referral" figure shown on the Refer Friends
    bonus page (https://swaper.com/en/bonuses/refer-friends).

    Verified against the real account on 2026-07-17: a deeper nav-link
    crawl (going beyond just the account-entries API previously checked)
    found this dedicated page, entirely missed before. The page shows
    "Earned from referral" immediately followed by "0.00 €" as two
    adjacent text nodes (confirmed via a TreeWalker text-node scan) - no
    HTML element/class ties them together, so a TreeWalker is used here
    too, same technique. There is a separate "Loyalty Bonus" feature on
    this page, but it's an interest-RATE boost (+2% p.a. on Wandoo
    Finance/SW Finance loan claims once >=25000 EUR is deposited for 3
    consecutive months) - not a distinct cash figure, so it's not scraped
    here; it's already reflected in the interest rate itself, folded into
    Interest Received.

    IMPORTANT CAVEAT: unlike the interest/statement figures elsewhere in
    this file, this page shows a LIFETIME cumulative total ("Earned from
    referral"), not a "this calendar month" figure - Swaper doesn't expose
    a monthly breakdown for referral bonuses anywhere (no date filter on
    this page, and referral-type transactions never appear in the
    account-entries API regardless of date range). The lifetime total is
    used as-is (currently 0.00 EUR - no referral has ever been credited on
    this account), which is the best real data available; it will need
    revisiting if/when a first referral bonus is ever earned, since this
    total would then stay elevated in every subsequent month's report
    rather than reflecting only that month's new bonus.
    """
    log.info("Reading 'Earned from referral' off the Refer Friends bonus page...")
    raw_value = page.evaluate(
        """
        () => {
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            const texts = [];
            let node;
            while ((node = walker.nextNode())) {
                const t = node.textContent.trim();
                if (t) texts.push(t);
            }
            const idx = texts.findIndex((t) => t === 'Earned from referral');
            return idx !== -1 && idx + 1 < texts.length ? texts[idx + 1] : null;
        }
        """
    )
    log.info("Raw 'Earned from referral' text: %r", raw_value)
    value = _parse_amount(raw_value) if raw_value else None
    if value is None:
        log.warning("Could not find/parse 'Earned from referral' on the bonus page - defaulting to 0.0.")
        return 0.0
    return value


def run(headless: bool = True) -> None:
    if not SWAPER_EMAIL or not SWAPER_PASSWORD:
        log.error("SWAPER_EMAIL and SWAPER_PASSWORD environment variables are required.")
        sys.exit(1)

    # XIRR (like "total"/geographic repartition elsewhere in this repo) is a
    # LIVE-only snapshot metric (it needs TODAY's real total account value
    # as its final cashflow) - it can't be meaningfully backfilled for a
    # past REPORT_DATE month, so it's only ever computed/written for the
    # real current month, decided once up front.
    current_month = is_current_month()

    log.info("Starting Swaper diversification run (headless=%s, storage_state_exists=%s).", headless, STORAGE_STATE_FILE.exists())

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
        except Exception:
            log.exception("Failed to log in or fetch Swaper's loan originator breakdown.")
            browser.close()
            sys.exit(1)

        try:
            log.info("Navigating to the account statement page to fetch this month's Interest Received...")
            page.goto(STATEMENT_PAGE_URL, wait_until="domcontentloaded")
            statement_totals = fetch_current_month_interest_received(page)
            interest_received = statement_totals["earned_interest"]
        except Exception:
            log.exception("Failed to fetch this month's Interest Received - defaulting to 0.0.")
            interest_received = 0.0
            statement_totals = None

        try:
            log.info("Navigating to the Refer Friends bonus page to fetch 'Earned from referral'...")
            page.goto(REFERRAL_BONUS_PAGE_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(1000)
            referral_bonus_earned = fetch_referral_bonus_earned(page)
        except Exception:
            log.exception("Failed to fetch the referral bonus earned - defaulting to 0.0.")
            referral_bonus_earned = 0.0

        try:
            log.info("Navigating to the loans page to fetch the uninvested account balance ('non investi')...")
            loans_payload = fetch_loans(page, [])
            uninvested_balance = extract_balance(loans_payload)
        except Exception:
            log.exception("Failed to fetch the uninvested account balance - 'non investi' will not be updated.")
            uninvested_balance = None

        xirr_cashflow_entries = None
        if current_month:
            try:
                month_start_date = get_report_now(REPORT_TIMEZONE).replace(day=1).strftime("%Y-%m-%d")
                month_end_date = get_report_now(REPORT_TIMEZONE).strftime("%Y-%m-%d")
                log.info("Fetching this month's account-entries to reconstruct this month's XIRR cashflows...")
                xirr_cashflow_entries = fetch_account_cashflows(page, month_start_date, month_end_date)
            except Exception:
                log.exception("Failed to fetch the XIRR cashflow history - XIRR will not be updated.")
                xirr_cashflow_entries = None

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
    log.info("Referral bonus earned (lifetime total): %.2f EUR", referral_bonus_earned)

    # Swaper's account-entries API has no gross/net/withholding-tax
    # breakdown (unlike Afranga/Bienpreter) - interest_received is mapped to
    # both gross_interest_received/net_interest_received since it's the
    # only real figure on hand, withholding_tax defaults to 0.0. Same
    # standardized dict shape as every other *_diversification.py, plus the
    # platform-specific interest_received field kept alongside it.
    # bonus_cashback_contest is now genuinely fetched (see
    # fetch_referral_bonus_earned()) from the Refer Friends bonus page -
    # previously hardcoded to 0.0 based on an insufficiently thorough check
    # of account-entries transactionTypes only, which missed this page
    # entirely. See that function's docstring for the lifetime-vs-monthly
    # caveat.
    amounts = {
        "total": breakdown["total_invested"],
        "gross_interest_received": interest_received,
        "net_interest_received": interest_received,
        "withholding_tax": 0.0,
        "bonus_cashback_contest": referral_bonus_earned,
        "interest_received": interest_received,
    }

    # XIRR is computed for THIS CALENDAR MONTH ONLY (not since account
    # inception) so it can be meaningfully averaged across months to get an
    # "overall" figure - a since-inception XIRR recomputed every month
    # would already BE an overall figure each time, making a later average
    # across months double-count the past and be misleading.
    #
    # A period (monthly) XIRR is obtained the same way any money-weighted
    # return is restricted to a sub-period: the account's total value at
    # the START of the month is treated as if it were "invested" on day 1
    # of the month (a synthetic negative cashflow), the REAL deposit/
    # withdrawal cashflows that happened during the month are kept at
    # their real dates (see fetch_account_cashflows()'s docstring for why
    # every OTHER transaction type is excluded), and today's real total
    # value is the final "as if withdrawn today" positive cashflow - same
    # idea as a since-inception XIRR, just anchored at the start of the
    # month instead of at the account's opening.
    #
    # The start-of-month total value isn't directly observable (Swaper
    # exposes no historical invested-principal figure), so it's derived
    # from the fundamental value identity instead - external cashflows and
    # interest earned are the only two things that change the account's
    # TOTAL value (money moving between "invested" and "uninvested cash"
    # internally, e.g. buying a new loan or a loan being repaid, does not):
    #   end_value = start_value + net_external_cashflows + interest_earned
    # rearranged to:
    #   start_value = end_value - net_external_cashflows - interest_earned
    # NOTE: this ignores a referral bonus credited as cash DURING the month
    # (not covered by interest_received nor by a FUNDING cashflow) - not a
    # concern today since this account's referral bonus has always been
    # 0.0 (see fetch_referral_bonus_earned()'s docstring), but would skew
    # start_value slightly too high the month one is first earned.
    xirr_value = None
    if current_month and xirr_cashflow_entries is not None and uninvested_balance is not None:
        total_account_value = breakdown["total_invested"] + uninvested_balance
        signed_cashflows = []
        net_external_cashflows = 0.0
        for entry in xirr_cashflow_entries:
            try:
                entry_date = datetime.strptime(entry["date"], "%Y-%m-%d").date()
            except ValueError:
                log.warning("Skipping an XIRR cashflow entry with an unparseable date: %r", entry)
                continue
            is_deposit = entry["transactionType"].strip().upper() == "FUNDING"
            signed_amount = -entry["amount"] if is_deposit else entry["amount"]
            signed_cashflows.append((entry_date, signed_amount))
            net_external_cashflows += -signed_amount

        month_start = get_report_now(REPORT_TIMEZONE).replace(day=1).date()
        start_value = total_account_value - net_external_cashflows - interest_received
        if start_value <= 0:
            log.warning(
                "Computed start-of-month account value (%.2f EUR) isn't positive (likely the first month "
                "any money was ever deposited) - omitting the start-of-month synthetic cashflow.",
                start_value,
            )
        else:
            signed_cashflows.append((month_start, -start_value))
        signed_cashflows.append((get_report_now(REPORT_TIMEZONE).date(), total_account_value))

        xirr_value = compute_xirr(signed_cashflows)
        if xirr_value is None:
            log.warning("Could not compute this month's XIRR from %d cashflow(s) - XIRR row will not be updated.", len(signed_cashflows))
        else:
            log.info(
                "Computed this month's XIRR: %.2f%% (start-of-month value %.2f EUR, %d deposit/withdrawal cashflow(s) this month, current total value %.2f EUR).",
                xirr_value * 100, start_value, len(xirr_cashflow_entries), total_account_value,
            )

    # Cash drag: how much this month's return was diluted by cash sitting
    # idle (not invested) instead of earning interest. Defined here as
    # `cash_weight * monthly_yield_rate` (both non-annualized, THIS month
    # only, per the user's own definition - "l'impact sur le mois des sous
    # non investi"):
    #   cash_weight        = avg_idle_cash_this_month / (avg_idle_cash_this_month + total_invested)
    #   monthly_yield_rate = gross_interest_received_this_month / total_invested
    # i.e. the number of percentage points THIS MONTH's return was reduced
    # by, assuming the idle cash would otherwise have earned the same rate
    # as the capital that WAS invested this month. `avg_idle_cash_this_month`
    # = (opening_balance + closing_balance) / 2 from the SAME account-
    # entries response already fetched above for interest_received -
    # closing_balance is the account's real uninvested cash balance
    # (verified live 2026-08-14 to match extract_balance()'s "non investi"
    # figure exactly).
    cash_drag_value = None
    if current_month and statement_totals is not None and breakdown["total_invested"] > 0:
        avg_idle_cash = (statement_totals["opening_balance"] + statement_totals["closing_balance"]) / 2
        cash_weight = avg_idle_cash / (avg_idle_cash + breakdown["total_invested"])
        monthly_yield_rate = interest_received / breakdown["total_invested"]
        cash_drag_value = cash_weight * monthly_yield_rate
        log.info(
            "Computed Cash drag: %.2f%% (avg idle cash %.2f EUR, cash weight %.2f%%, monthly yield %.2f%%).",
            cash_drag_value * 100, avg_idle_cash, cash_weight * 100, monthly_yield_rate * 100,
        )

    # "total" comes from the "Currently Allocated" DOM widget, a LIVE-only
    # snapshot with no date param, and account-entries (the date-ranged
    # interest API) has no balance field (2026-08-06 investigation) - skip
    # total for a backfilled month.
    fill_current_month_amounts(
        platform="Swaper",
        amounts=amounts,
        skip_total=not current_month,
    )

    # Swaper's referral bonus is a "prime" (parrainage/reward), not a
    # cashback or contest - written to its own dedicated sub-row, never to
    # the "Bonus" row itself (a SUM formula over prime/cashback/concours).
    # "Cash drag"/"XIRR" are written alongside it, further down the same
    # block (past fill_current_month_bonus_breakdown()'s default max_rows=6
    # bound - hence the explicit max_rows=12 here, with a safety margin
    # since the user has already inserted a row in this block once) - only
    # included when actually computed, so a failed/skipped computation
    # leaves the existing cell untouched rather than overwriting it with a
    # wrong/zero value.
    bonus_breakdown = {"prime": referral_bonus_earned}
    if xirr_value is not None:
        bonus_breakdown["XIRR"] = xirr_value
    if cash_drag_value is not None:
        bonus_breakdown["Cash drag"] = cash_drag_value
    fill_current_month_bonus_breakdown(
        platform="Swaper",
        breakdown=bonus_breakdown,
        max_rows=12,
    )

    loan_originators = [
        {"name": o["originator"], "amount": o["outstanding"]}
        for o in originators
    ]

    if current_month:
        fill_geographic_repartition_amounts(loan_originators, platform="Swaper")
        if uninvested_balance is not None:
            fill_geographic_repartition_uninvested_amount("Swaper", uninvested_balance)


if __name__ == "__main__":
    # Set headless=False locally (e.g. via `python swaper_diversification.py --show`)
    # to watch the browser and debug the login flow if selectors need adjusting.
    run(headless="--show" not in sys.argv)
