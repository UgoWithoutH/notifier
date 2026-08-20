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

Also computes a since-inception XIRR (money-weighted return) plus this
month's Cash drag and the XIRR Bonus / XIRR Cash drag / XIRR Taxes/Frais /
XIRR Intérêts pie-chart shares (see run() below, and
afranga_diversification.py's own docstring for the full since-inception
XIRR methodology, shared across every *_diversification.py that computes
it).

Added 2026-08-19: XIRR Intérêts, the counterfactual XIRR share
attributable to real net interest received since inception (mirrors
afranga_diversification.py's/peerberry_diversification.py's own XIRR
Intérêts block exactly - same counterfactual-XIRR pattern as Bonus/Cash
drag/Taxes above). Like PeerBerry (and unlike Afranga, which has a real
gross/withholding-tax split to subtract), Swaper's account-entries API has
no withholding-tax data at all (taxes_xirr_contribution is hardcoded to
0.0 above for the same reason) - so `lifetime_statement_totals["earned_interest"]`
already IS the lifetime net interest figure, used directly as
`lifetime_net_interest`, no extra fetch/subtraction needed. As with
Afranga/PeerBerry, a "XIRR Intérêts" row must already exist in the Swaper
block on the sheet itself (right after "XIRR Taxes/Frais") for this new
value to land anywhere - fill_current_month_bonus_breakdown() fills an
existing row by label, it doesn't insert new labelled rows. `max_rows` is
bumped 18 -> 19 to keep the search bounded past this now-taller block.

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
from datetime import datetime, timedelta
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
# "all_entries" (every transactionType, unfiltered) is cached alongside
# "cashflows" (FUNDING/WITHDRAW*-only, for XIRR) so compute_average_idle_cash()'s
# Cash drag reconstruction reuses the SAME incremental fetch instead of
# re-fetching the whole history every run - see get_cached_account_cashflows().
XIRR_CASHFLOWS_STATE_DEFAULT = {"cashflows": [], "all_entries": [], "last_fetched_date": None}
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
    return fetch_statement_totals(page, start_date, end_date)


def fetch_statement_totals(page, start_date: str, end_date: str) -> dict:
    """Same account-entries API call as fetch_current_month_interest_received()
    (see that function's docstring for the endpoint/auth/field details),
    generalized to an arbitrary [start_date, end_date] range (both
    "YYYY-MM-DD") - used by run() to fetch SINCE-INCEPTION opening/closing
    balance + earned interest (needed for a genuine since-inception "Cash
    drag" share of XIRR), not just the current calendar month.
    """
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


def _fetch_account_entries_pages(page, start_date: str, end_date: str) -> list:
    """Fetch EVERY raw account-entries row within [start_date, end_date]
    (no type filtering at all), paginated via XIRR_PAGE_SIZE/MAX_XIRR_PAGES.
    Shared by _split_cashflows_from_entries() (which keeps only FUNDING/WITHDRAW*
    rows for the XIRR cashflow list) and compute_average_idle_cash()'s
    caller in run() (which needs EVERY row - INVESTMENT/REPAYMENT_*/
    BUYBACK_*/EXTENSION_INTEREST too - to reconstruct the day-by-day
    uninvested-cash balance for "Cash drag").
    """
    entries = []
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
        entries.extend(results)

        if total_records is None or len(results) == 0:
            break
        if page_number * XIRR_PAGE_SIZE >= total_records:
            break
        page_number += 1
    else:
        log.warning("Hit MAX_XIRR_PAGES (%d) without exhausting totalRecords=%s - entry history may be incomplete.", MAX_XIRR_PAGES, total_records)

    return entries


def _split_cashflows_from_entries(raw_entries: list) -> list:
    """Filter raw (unfiltered) account-entries rows down to just the real
    EXTERNAL cashflows (FUNDING deposits / WITHDRAW* withdrawals) needed
    for the XIRR calculation - every other transactionType (`INVESTMENT`,
    `REPAYMENT_PRINCIPAL`, `REPAYMENT_INTEREST`, `BUYBACK_PRINCIPAL`,
    `BUYBACK_INTEREST`, `EXTENSION_INTEREST`, ...) just moves money between
    "uninvested cash" and "invested in a loan" WITHIN the account, so must
    NOT be counted as a separate XIRR cashflow (the account's final total
    value already reflects their net effect). `amount` is always the
    ABSOLUTE value (the caller decides the sign based on `transactionType`).
    """
    cashflows = []
    for entry in raw_entries:
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
    return cashflows


# transactionTypes that DEBIT the uninvested-cash balance (money leaving cash
# to fund a loan) - WITHDRAW*-type rows are also a debit, matched separately
# via a substring check since Swaper's own casing/exact label isn't fixed.
_CASH_DEBIT_TRANSACTION_TYPES = {"INVESTMENT"}
# transactionTypes that CREDIT the uninvested-cash balance (money returning
# from a loan, or a deposit) - verified real types from the account's full
# history (see _split_cashflows_from_entries()'s docstring).
_CASH_CREDIT_TRANSACTION_TYPES = {
    "FUNDING", "REPAYMENT_PRINCIPAL", "REPAYMENT_INTEREST",
    "BUYBACK_PRINCIPAL", "BUYBACK_INTEREST", "EXTENSION_INTEREST",
}


def _cash_delta_for_entry(transaction_type: str, amount: float) -> float:
    """Signed change to the uninvested-cash balance a single account-entries
    row represents - an unrecognized/future transactionType is treated as
    cash-neutral (logged) rather than guessed at.
    """
    upper = transaction_type.strip().upper()
    if "WITHDRAW" in upper or upper in _CASH_DEBIT_TRANSACTION_TYPES:
        return -abs(amount)
    if upper in _CASH_CREDIT_TRANSACTION_TYPES:
        return abs(amount)
    log.warning(
        "Unrecognized account-entries transactionType %r while reconstructing the daily cash balance - treating as cash-neutral (0 impact).",
        transaction_type,
    )
    return 0.0


def compute_average_idle_cash(entries: list, opening_balance: float, closing_balance: float, start_date: str, end_date: str) -> float:
    """Reconstruct the uninvested-cash balance for EVERY day in
    [start_date, end_date] from the raw account-entries rows (every
    transaction type - INVESTMENT/REPAYMENT_*/BUYBACK_*/EXTENSION_INTEREST
    too, not just FUNDING/WITHDRAWAL) and return the day-weighted average.

    This replaces a naive `(opening_balance + closing_balance) / 2` average,
    which can badly understate "Cash drag" whenever idle cash both
    APPEARS and gets invested INSIDE the period (e.g. a deposit that sits
    uninvested for a real day mid-month before being invested - opening AND
    closing balance can both be ~0 even though cash genuinely sat idle for
    a day in between). Per explicit user request 2026-08-14.

    Each day's entries are assumed to settle by end of that day (matching
    how the API's own closing_balance for a range already reflects the
    last day's own transactions). Falls back to the simple 2-point average
    if `entries` is empty (e.g. the unfiltered fetch failed) or the dates
    can't be parsed - never raises.
    """
    if not entries:
        return (opening_balance + closing_balance) / 2

    try:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        end = datetime.strptime(end_date, "%Y-%m-%d").date()
    except ValueError:
        return (opening_balance + closing_balance) / 2

    daily_deltas: dict = {}
    for entry in entries:
        raw_date = entry.get("bookingDate")
        raw_amount = entry.get("amount")
        transaction_type = entry.get("transactionType")
        if not raw_date or raw_amount is None or not transaction_type:
            continue
        try:
            amount = float(raw_amount)
        except (TypeError, ValueError):
            continue
        daily_deltas[raw_date] = daily_deltas.get(raw_date, 0.0) + _cash_delta_for_entry(transaction_type, amount)

    running_balance = opening_balance
    total_balance = 0.0
    day_count = 0
    current = start
    while current <= end:
        running_balance += daily_deltas.get(current.strftime("%Y-%m-%d"), 0.0)
        total_balance += running_balance
        day_count += 1
        current += timedelta(days=1)

    if day_count == 0:
        return (opening_balance + closing_balance) / 2

    if abs(running_balance - closing_balance) > 0.05:
        log.warning(
            "Reconstructed closing balance (%.2f EUR) from all account-entries types doesn't match the API's own closing_balance (%.2f EUR) - "
            "an unmapped transactionType may exist; the average idle cash below may be slightly off.",
            running_balance, closing_balance,
        )

    return total_balance / day_count


def get_cached_account_cashflows(page, end_date: str) -> tuple:
    """Return `(cashflows, all_entries)` since account inception, fetching
    from the account-entries API only the entries NOT already cached
    locally (in XIRR_CASHFLOWS_STATE_FILE), instead of re-fetching the
    account's full history on every run. `cashflows` = FUNDING/WITHDRAW*-only
    (for XIRR, unchanged shape). `all_entries` = EVERY raw row regardless of
    type (INVESTMENT/REPAYMENT_*/BUYBACK_*/EXTENSION_INTEREST included),
    needed by compute_average_idle_cash() for "Cash drag" - both come from
    the SAME single incremental fetch/cache, so adding the Cash drag
    reconstruction didn't cost a second full-history API call per run.

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
    are then dropped by de-duplicating on (date, amount, transactionType)
    for cashflows, and on transactionId (falling back to a date/type/amount
    tuple if absent) for all_entries.
    """
    state = load_state(XIRR_CASHFLOWS_STATE_FILE, XIRR_CASHFLOWS_STATE_DEFAULT)
    cached_cashflows = state["cashflows"]
    cached_all_entries = state.get("all_entries", [])
    start_date = state["last_fetched_date"] or XIRR_HISTORY_START_DATE
    if not cached_all_entries and cached_cashflows and start_date != XIRR_HISTORY_START_DATE:
        # Migration from a pre-"all_entries" cache file: last_fetched_date is
        # already advanced but all_entries was never populated - force ONE
        # full-history re-fetch this run so Cash drag's day-by-day
        # reconstruction isn't missing years of INVESTMENT/REPAYMENT_*/etc.
        # entries (cashflows would just re-merge harmlessly, already deduped).
        log.info("Cached 'all_entries' is empty despite existing cashflows - backfilling the full history once.")
        start_date = XIRR_HISTORY_START_DATE

    log.info(
        "Found %d cached XIRR cashflow(s) (last fetched up to %s) - fetching only new entries from %s to %s...",
        len(cached_cashflows), state["last_fetched_date"], start_date, end_date,
    )
    new_raw_entries = _fetch_account_entries_pages(page, start_date, end_date)
    new_cashflows = _split_cashflows_from_entries(new_raw_entries)

    seen = set()
    merged_cashflows = []
    for entry in cached_cashflows + new_cashflows:
        key = (entry["date"], entry["amount"], entry["transactionType"])
        if key in seen:
            continue
        seen.add(key)
        merged_cashflows.append(entry)

    seen_entries = set()
    merged_all_entries = []
    for entry in cached_all_entries + new_raw_entries:
        key = entry.get("transactionId") or (entry.get("bookingDate"), entry.get("transactionType"), entry.get("amount"))
        if key in seen_entries:
            continue
        seen_entries.add(key)
        merged_all_entries.append(entry)

    save_state(XIRR_CASHFLOWS_STATE_FILE, {"cashflows": merged_cashflows, "all_entries": merged_all_entries, "last_fetched_date": end_date})
    log.info(
        "XIRR cashflow cache now holds %d cashflow(s)/%d total entrie(s) (was %d/%d before this run).",
        len(merged_cashflows), len(merged_all_entries), len(cached_cashflows), len(cached_all_entries),
    )
    return merged_cashflows, merged_all_entries


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
        # Since-inception opening/closing balance + earned interest (same
        # account-entries call as the monthly one above, just widened to
        # the account's real start date) - needed so "Cash drag"'s own
        # share of XIRR is computed over the SAME since-inception period as
        # XIRR itself, not just this month extrapolated.
        lifetime_statement_totals = None
        # Raw (unfiltered, every transactionType) account-entries rows for
        # the current month / since-inception, fetched here (still inside
        # the Playwright session) so compute_average_idle_cash() can
        # reconstruct a real day-by-day idle-cash balance further below,
        # instead of just interpolating opening/closing.
        all_account_entries = None
        if current_month:
            try:
                today_date = get_report_now(REPORT_TIMEZONE).strftime("%Y-%m-%d")
                log.info("Fetching the since-inception XIRR cashflows + all account entries (cached where possible)...")
                xirr_cashflow_entries, all_account_entries = get_cached_account_cashflows(page, today_date)

                funding_dates = [
                    e["date"] for e in xirr_cashflow_entries
                    if e["transactionType"].strip().upper() == "FUNDING"
                ]
                if funding_dates:
                    since_inception_date = min(funding_dates)
                    log.info("Fetching since-inception statement totals (%s to %s)...", since_inception_date, today_date)
                    lifetime_statement_totals = fetch_statement_totals(page, since_inception_date, today_date)
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
    # "total" ("en cours") written to the Sheet is invested + uninvested,
    # per user request 2026-08-14 (matching Bienprêter/Iuvo/Bricks/Lande's
    # own convention) - falls back to invested-only if the uninvested
    # balance couldn't be fetched. `breakdown["total_invested"]` itself
    # stays invested-only, since it feeds the Cash drag/XIRR math below.
    amounts = {
        "total": breakdown["total_invested"] + uninvested_balance if uninvested_balance is not None else breakdown["total_invested"],
        "gross_interest_received": interest_received,
        "net_interest_received": interest_received,
        "withholding_tax": 0.0,
        "bonus_cashback_contest": referral_bonus_earned,
        "interest_received": interest_received,
    }

    # XIRR is a since-inception money-weighted return: every real
    # deposit/withdrawal ever made (see _split_cashflows_from_entries()'s
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
    # is now a real day-weighted average (see compute_average_idle_cash()) -
    # a naive (opening+closing)/2 misses idle cash that appears AND gets
    # invested within the same month, per user request 2026-08-14.
    cash_drag_value = None
    # Cash drag/taxes' own share of XIRR, on the same since-inception,
    # annualized percentage-point scale as XIRR itself (unlike "Cash drag"
    # above, which is a monthly-only figure) - computed from
    # lifetime_statement_totals (same account-entries call, widened to the
    # account's real start date, fetched earlier in run()) instead of
    # extrapolating this month x12, so it decomposes the SAME real XIRR
    # value rather than a hypothetical "if every month looked like this
    # one". Feeds the pie-chart breakdown requested alongside
    # bonus_xirr_contribution.
    cash_drag_xirr_contribution = None
    # Swaper has no withholding-tax data at all (never charged on this
    # platform, see amounts["withholding_tax"] above) - always 0, since-
    # inception or not, so no lifetime reconstruction is needed here.
    taxes_xirr_contribution = 0.0 if current_month else None
    # XIRR Intérêts (added 2026-08-19, mirrors afranga_diversification.py's/
    # peerberry_diversification.py's own XIRR Intérêts block - see module
    # docstring for the full rationale): counterfactual XIRR share
    # attributable to real net interest received since inception. Like
    # PeerBerry (and unlike Afranga, which has to subtract a real
    # withholding tax), Swaper has no withholding-tax data at all, so
    # lifetime_statement_totals["earned_interest"] already IS the lifetime
    # net interest figure, used directly.
    interest_xirr_contribution = None
    if current_month and statement_totals is not None and breakdown["total_invested"] > 0:
        month_start_date = get_report_now(REPORT_TIMEZONE).replace(day=1).strftime("%Y-%m-%d")
        today_date_str = get_report_now(REPORT_TIMEZONE).strftime("%Y-%m-%d")
        avg_idle_cash = compute_average_idle_cash(
            all_account_entries or [], statement_totals["opening_balance"], statement_totals["closing_balance"],
            month_start_date, today_date_str,
        )
        cash_weight = avg_idle_cash / (avg_idle_cash + breakdown["total_invested"])
        monthly_yield_rate = interest_received / breakdown["total_invested"]
        cash_drag_value = cash_weight * monthly_yield_rate
        log.info(
            "Computed Cash drag: %.2f%% (avg idle cash %.2f EUR, cash weight %.2f%%, monthly yield %.2f%%).",
            cash_drag_value * 100, avg_idle_cash, cash_weight * 100, monthly_yield_rate * 100,
        )

        if lifetime_statement_totals is not None and xirr_cashflow_entries:
            funding_dates = [
                e["date"] for e in xirr_cashflow_entries
                if e["transactionType"].strip().upper() == "FUNDING"
            ]
            if funding_dates:
                since_inception_date = datetime.strptime(min(funding_dates), "%Y-%m-%d").date()
                years_elapsed = max((get_report_now(REPORT_TIMEZONE).date() - since_inception_date).days / 365.25, 1 / 365.25)
                avg_idle_cash_lifetime = compute_average_idle_cash(
                    all_account_entries or [], lifetime_statement_totals["opening_balance"], lifetime_statement_totals["closing_balance"],
                    since_inception_date.strftime("%Y-%m-%d"), get_report_now(REPORT_TIMEZONE).strftime("%Y-%m-%d"),
                )
                cash_weight_lifetime = avg_idle_cash_lifetime / (avg_idle_cash_lifetime + breakdown["total_invested"])
                lifetime_yield_rate = lifetime_statement_totals["earned_interest"] / breakdown["total_invested"]
                cash_drag_lifetime_total = cash_weight_lifetime * lifetime_yield_rate
                # Same counterfactual-XIRR technique as bonus_xirr_contribution
                # above, instead of linearly dividing cash_drag_lifetime_total by
                # years_elapsed: XIRR compounds (compute_xirr() solves a non-linear
                # equation over real dates), so a plain division mixes a cumulative
                # % with an annualized (compounding) one. Here: convert the
                # cumulative drag into the EUR amount the idle cash would have
                # earned at the SAME yield as the rest of the portfolio, add it
                # back to today's final value, recompute XIRR on that higher
                # counterfactual value, and diff vs. the real XIRR - same scale/
                # methodology as xirr_value itself.
                if xirr_value is not None:
                    missed_earnings = cash_drag_lifetime_total * (avg_idle_cash_lifetime + breakdown["total_invested"])
                    cashflows_with_cash_invested = signed_cashflows[:-1] + [
                        (today_date, total_account_value + missed_earnings)
                    ]
                    xirr_with_cash_invested = compute_xirr(cashflows_with_cash_invested)
                    if xirr_with_cash_invested is not None:
                        cash_drag_xirr_contribution = xirr_value - xirr_with_cash_invested
                        log.info(
                            "XIRR share - cash drag: %.4f points (since-inception, %.2f years, missed earnings ~%.2f EUR), taxes/frais: %.2f points.",
                            cash_drag_xirr_contribution * 100, years_elapsed, missed_earnings, taxes_xirr_contribution * 100,
                        )

                    # XIRR Intérêts: same counterfactual pattern as
                    # Bonus/Cash drag above, but for the real net interest
                    # received since inception - lifetime_statement_totals["earned_interest"]
                    # is used directly (no withholding tax to subtract, see
                    # comment above interest_xirr_contribution's
                    # declaration).
                    lifetime_net_interest = lifetime_statement_totals["earned_interest"]
                    if lifetime_net_interest:
                        cashflows_without_interest = signed_cashflows[:-1] + [
                            (today_date, total_account_value - lifetime_net_interest)
                        ]
                        xirr_without_interest = compute_xirr(cashflows_without_interest)
                        if xirr_without_interest is not None:
                            interest_xirr_contribution = xirr_value - xirr_without_interest
                            log.info(
                                "XIRR share - intérêts: %.4f points (lifetime net interest %.2f EUR).",
                                interest_xirr_contribution * 100, lifetime_net_interest,
                            )
                    else:
                        interest_xirr_contribution = 0.0

    # "total" comes from the "Currently Allocated" DOM widget plus the
    # uninvested balance (see above), a LIVE-only snapshot with no date
    # param, and account-entries (the date-ranged interest API) has no
    # balance field (2026-08-06 investigation) - skip total for a
    # backfilled month.
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
    # bound - hence the explicit max_rows=19 here, with a safety margin
    # since the user has already inserted rows in this block a few times) -
    # only included when actually computed, so a failed/skipped computation
    # leaves the existing cell untouched rather than overwriting it with a
    # wrong/zero value. "XIRR Intérêts" (added 2026-08-19) sits right after
    # "XIRR Taxes/Frais" - this pushes the block one row taller than
    # before, so `max_rows` is bumped 18 -> 19 to keep the search bounded
    # before the next platform block. IMPORTANT: a "XIRR Intérêts" row must
    # exist in the Swaper block on the sheet itself (right after "XIRR
    # Taxes/Frais") for this new value to actually land somewhere - this
    # script fills an existing row by label, it doesn't insert new
    # labelled rows into this block.
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
    if interest_xirr_contribution is not None:
        bonus_breakdown["XIRR Intérêts"] = interest_xirr_contribution
    fill_current_month_bonus_breakdown(
        platform="Swaper",
        breakdown=bonus_breakdown,
        max_rows=19,
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