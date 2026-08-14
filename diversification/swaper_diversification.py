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
from shared.state import load_state, save_state
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
# Cache of every deposit/withdrawal cashflow ever fetched for the XIRR
# calculation (see get_cached_account_cashflows() below) - avoids
# re-fetching the account's ENTIRE history from the account-entries API on
# every monthly run; only entries since the last run's cutoff are fetched
# and merged in. The XIRR itself is still recomputed from scratch (see
# compute_xirr()) over the FULL merged list every run - XIRR is a root of a
# non-linear equation over all historical cashflows, it cannot be derived
# from last month's XIRR value plus just this month's new flows.
XIRR_CASHFLOWS_STATE_FILE = Path(__file__).parent / "swaper_xirr_cashflows_state.json"
XIRR_CASHFLOWS_STATE_DEFAULT = {"cashflows": [], "last_fetched_date": None}
# Verified live 2026-08-14 (full-history probe): pageSize=1000 returned this
# account's entire history (352 records) in one page - a larger pageSize
# (5000) was REJECTED by the API with HTTP 400 (undocumented server-side
# cap). Kept as a generous page size/safety net for accounts with more
# history than this one.
XIRR_PAGE_SIZE = 1000
MAX_XIRR_PAGES = 20
# XIRR is a since-inception money-weighted return (not per-month) - this
# start date is early enough to cover any real account's full history.
XIRR_HISTORY_START_DATE = "2000-01-01"
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
    by run() with the account's FULL history (XIRR_HISTORY_START_DATE to
    today) - XIRR is a since-inception money-weighted return, not a
    per-month one.

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
        "Requesting the account-entries history (%s to %s) for the since-inception XIRR cashflow reconstruction...",
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


def get_cached_account_cashflows(page, end_date: str) -> list:
    """Return every deposit/withdrawal cashflow since account inception,
    fetching from the account-entries API only the entries NOT already
    cached locally (in XIRR_CASHFLOWS_STATE_FILE), instead of re-fetching
    the account's full history on every run.

    IMPORTANT: this only optimizes AWAY the redundant API calls - the XIRR
    itself must still be computed from the FULL merged list every time
    (see compute_xirr()). XIRR is the root of a non-linear equation over
    every historical cashflow's own date/amount; there is no way to derive
    a new XIRR from last month's XIRR value plus just this month's new
    cashflows without silently dropping the date-weighting of every past
    cashflow, which would produce a wrong number.

    Re-fetches starting from the cached `last_fetched_date` itself (not the
    day after) so an entry booked on that same day, added on Swaper's side
    after the previous run already fetched it, isn't missed - duplicates
    are then dropped by de-duplicating on (date, amount, transactionType).
    """
    state = load_state(XIRR_CASHFLOWS_STATE_FILE, XIRR_CASHFLOWS_STATE_DEFAULT)
    cached_cashflows = state["cashflows"]
    start_date = state["last_fetched_date"] or XIRR_HISTORY_START_DATE

    log.info(
        "Found %d cached XIRR cashflow(s) (last fetched up to %s) - fetching only new entries from %s to %s...",
        len(cached_cashflows), state["last_fetched_date"], start_date, end_date,
    )
    new_cashflows = fetch_account_cashflows(page, start_date, end_date)

    seen = set()
    merged = []
    for entry in cached_cashflows + new_cashflows:
        key = (entry["date"], entry["amount"], entry["transactionType"])
        if key in seen:
            continue
        seen.add(key)
        merged.append(entry)

    save_state(XIRR_CASHFLOWS_STATE_FILE, {"cashflows": merged, "last_fetched_date": end_date})
    log.info("XIRR cashflow cache now holds %d entrie(s) (was %d before this run).", len(merged), len(cached_cashflows))
    return merged


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
                today_date = get_report_now(REPORT_TIMEZONE).strftime("%Y-%m-%d")
                log.info("Fetching the since-inception XIRR cashflows (cached where possible)...")
                xirr_cashflow_entries = get_cached_account_cashflows(page, today_date)
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

    # XIRR is a since-inception money-weighted return: every real
    # deposit/withdrawal ever made (see fetch_account_cashflows()'s
    # docstring for why every OTHER transaction type is excluded) is a
    # signed cashflow at its real date, plus today's real total account
    # value as the final "as if withdrawn today" positive cashflow.
    xirr_value = None
    # Bonus's own share of XIRR (percentage points, same scale as xirr_value
    # itself) - isolated by recomputing XIRR with the lifetime referral bonus
    # (already baked into the account's live balance) subtracted from the
    # final "as if withdrawn today" cashflow; the difference vs. the real
    # XIRR is how many points came from the bonus. Feeds the pie-chart
    # breakdown requested alongside "Cash drag"/"XIRR" below.
    bonus_xirr_contribution = None
    if current_month and xirr_cashflow_entries is not None and uninvested_balance is not None:
        total_account_value = breakdown["total_invested"] + uninvested_balance
        signed_cashflows = []
        for entry in xirr_cashflow_entries:
            try:
                entry_date = datetime.strptime(entry["date"], "%Y-%m-%d").date()
            except ValueError:
                log.warning("Skipping an XIRR cashflow entry with an unparseable date: %r", entry)
                continue
            is_deposit = entry["transactionType"].strip().upper() == "FUNDING"
            signed_amount = -entry["amount"] if is_deposit else entry["amount"]
            signed_cashflows.append((entry_date, signed_amount))

        today_date = get_report_now(REPORT_TIMEZONE).date()
        signed_cashflows.append((today_date, total_account_value))

        xirr_value = compute_xirr(signed_cashflows)
        if xirr_value is None:
            log.warning("Could not compute XIRR from %d cashflow(s) - XIRR row will not be updated.", len(signed_cashflows))
        else:
            log.info(
                "Computed since-inception XIRR: %.2f%% (%d deposit/withdrawal cashflow(s), current total value %.2f EUR).",
                xirr_value * 100, len(xirr_cashflow_entries), total_account_value,
            )

            if referral_bonus_earned:
                cashflows_without_bonus = signed_cashflows[:-1] + [
                    (today_date, total_account_value - referral_bonus_earned)
                ]
                xirr_without_bonus = compute_xirr(cashflows_without_bonus)
                if xirr_without_bonus is not None:
                    bonus_xirr_contribution = xirr_value - xirr_without_bonus
                    log.info("Bonus's own share of XIRR: %.2f points.", bonus_xirr_contribution * 100)
            else:
                bonus_xirr_contribution = 0.0

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
    # Cash drag/taxes' own share of XIRR, on the same annualized percentage-
    # point scale as XIRR itself (unlike "Cash drag" above, which is a
    # monthly-only figure) - linearly annualized (*12, same convention as
    # this Sheet's own "Rendement %" formula) since only this month's idle-
    # cash figures are available, not a real since-inception average. Feeds
    # the pie-chart breakdown requested alongside bonus_xirr_contribution.
    cash_drag_xirr_contribution = None
    taxes_xirr_contribution = None
    if current_month and statement_totals is not None and breakdown["total_invested"] > 0:
        avg_idle_cash = (statement_totals["opening_balance"] + statement_totals["closing_balance"]) / 2
        cash_weight = avg_idle_cash / (avg_idle_cash + breakdown["total_invested"])
        monthly_yield_rate = interest_received / breakdown["total_invested"]
        cash_drag_value = cash_weight * monthly_yield_rate
        log.info(
            "Computed Cash drag: %.2f%% (avg idle cash %.2f EUR, cash weight %.2f%%, monthly yield %.2f%%).",
            cash_drag_value * 100, avg_idle_cash, cash_weight * 100, monthly_yield_rate * 100,
        )

        cash_drag_xirr_contribution = -(cash_drag_value * 12)
        taxes_xirr_contribution = -((amounts["withholding_tax"] / breakdown["total_invested"]) * 12)
        log.info(
            "XIRR share - cash drag: %.2f points, taxes/frais: %.2f points.",
            cash_drag_xirr_contribution * 100, taxes_xirr_contribution * 100,
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
    # Pie-chart source data (percentage points, same scale as XIRR): each
    # component's own share of the since-inception XIRR - written to new
    # sub-rows only if the user has added them to the Sheet (soft-fail
    # label lookup, same as every other key in this dict).
    if bonus_xirr_contribution is not None:
        bonus_breakdown["XIRR Bonus"] = bonus_xirr_contribution
    if cash_drag_xirr_contribution is not None:
        bonus_breakdown["XIRR Cash drag"] = cash_drag_xirr_contribution
    if taxes_xirr_contribution is not None:
        bonus_breakdown["XIRR Taxes/Frais"] = taxes_xirr_contribution
    fill_current_month_bonus_breakdown(
        platform="Swaper",
        breakdown=bonus_breakdown,
        max_rows=18,
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
