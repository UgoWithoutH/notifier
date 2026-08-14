"""Iuvo (iuvo-group.com) portfolio balance + loan-originator diversification
fetcher.

REWRITTEN 2026-07-27 to use plain `requests` instead of Playwright (no
browser at all) - same technique as bricks_diversification.py/
monefit_diversification.py/goandgrow_diversification.py. The original
Playwright-based version (verified working end-to-end the same day, prior
to this rewrite) was replaced after network-capturing a real login: despite
Cloudflare fronting tbp2p.iuvo-group.com (`cf-ray`/`server: cloudflare`
response headers, and a passive `cdn-cgi/challenge-platform` JS beacon
observed during normal page loads), a direct non-browser `requests` POST to
the login endpoint is NOT blocked - confirmed 2026-07-27 end-to-end against
the real account (login + balance/originator breakdown + date-filtered
account statement all succeeded with a plain `requests.Session()`). No
storage_state/cookie persistence across runs is implemented either, same
as bricks/monefit/goandgrow - logging in fresh every run is cheap.

Underlying architecture: legacy "TeleBid/TB p2p" white-label engine (the
same backend engine also runs Swaper - see swaper_diversification.py -
but that's a separate deployment; don't assume this pure-HTTP technique
transfers there without re-testing).

Auth mechanism: `POST https://tbp2p.iuvo-group.com/p2p-ui/?p0=login;=en_US;
randn=<random 0..1>` with form-urlencoded body `login=<email>&password=
<password>` returns `{"result": {"session_token": "<32-char hex>", ...},
"status": {"status": "ok"}}` - this `session_token` (NOT a cookie) must be
passed as the `p2=` query-string param on every subsequent
tbp2p.iuvo-group.com API call. `token_expiration_seconds` is only 300 (5
minutes), but a full run of this script takes a few seconds, well within
that window - no refresh logic implemented. A `PHPSESSID` cookie is also
set by the main iuvo-group.com WordPress site (harmless/unused by the API
calls themselves; `requests.Session()` carries it automatically anyway).

Data sources (both real endpoints, found 2026-07-27 by capturing a
Playwright-driven browser's network traffic during exploration):
  - `GET https://tbp2p.iuvo-group.com/p2p-ui/v2/app?p0=overview_page;
    p2=<token>;lang=en_US&screen_width=1280&screen_height=720` -> a
    server-rendered HTML page (NOT a JSON API) that embeds a
    `var investors = [{...}];` JavaScript literal directly in a `<script>`
    tag - this is FULL-PRECISION JSON (unlike the "Chart by loans" widget
    rendered from the same data, which visually rounds to the nearest
    whole EUR). `investors[0].accountBalance` has: `availableFunds` =
    "Available Funds", `investedFunds` = "Receivables in P2P",
    `productInvestedFunds` = "Receivables in iuvoSAVE", `totalAmount` =
    "Total" (verified 2026-07-27: 0.00 + 1000.00 + 0.00 = 1000.00 EUR,
    same real-account figures as the original Playwright version).
    `investmentsByLoanOriginator` is a list of
    `{"aggregator": <name>, "value": <exact float>, "percentage": <pct>}`
    - `value` is used DIRECTLY as each originator's amount (no more
    percentage-of-total computation trick needed like Swaper/Bricks, since
    this `value` is already full precision - confirmed 2026-07-27:
    VivaCredit value=1000.0, exact match with investedFunds).
  - `GET https://tbp2p.iuvo-group.com/p2p-ui/v2/app?p0=
    account_statement_grouped_page;p2=<token>;lang=en_US&screen_width=
    1280&screen_height=720` (no extra params) - called ONCE first just to
    scrape `investor_account_id` (a stable per-currency-account id, e.g.
    29101 for this EUR account) out of the pre-selected
    `<option value="29101" selected="selected">EUR (€)</option>` in the
    page's own "Accounts" filter `<select>` - then the SAME endpoint is
    called AGAIN with `investor_account_id=<id>&trans_category=all&
    date_from=<1st of month>&date_to=<today>&p2=<token>&lang=en_US&
    screen_width=...&screen_height=...` added, which returns the actual
    date-filtered `<table class="table table-bordered p2p-table">` (this
    table's rows are technically malformed HTML - missing `</tr>` closing
    tags - so it's parsed with a regex matching each row's
    `<a class="btn p2p-trans" value="TYPE">...</a></td><td class="
    (positive|negative)-turnover">AMOUNT</td>` pair directly, rather than
    per-`<tr>` DOM/BeautifulSoup parsing, verified 2026-07-27 to correctly
    extract both rows of a real 2-row statement). Same `trans_type`
    classification convention as the original Playwright version:
    `payment_interest`/`payment_interest_buyback`/`payment_interest_early`
    summed into gross interest, `bonus` summed into
    bonus_cashback_contest. Iuvo has no separate withholding-tax
    transaction type, so net_interest_received == gross_interest_received
    and withholding_tax defaults to 0.0 (same convention as
    Swaper/Loanch/etc.).

Required env vars:
    IUVO_EMAIL, IUVO_PASSWORD            -> Iuvo account credentials
Optional:
    GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS   -> used to write this month's
                                             totals/breakdown to the Google
                                             Sheet via
                                             fill_current_month_amounts()/
                                             fill_geographic_repartition_amounts()
                                             (see google_sheet.py)
"""

import calendar
import json
import logging
import os
import random
import re
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

import requests

try:
    from shared.google_sheet import fill_current_month_amounts, fill_current_month_bonus_breakdown, fill_geographic_repartition_amounts, fill_geographic_repartition_uninvested_amount
    from shared.report_date import get_report_now, is_current_month
    from shared.state import load_state, save_state
    from shared.xirr import compute_xirr
except ModuleNotFoundError:
    # Support direct execution (python diversification/iuvo_diversification.py)
    # where the project root may not be on sys.path.
    project_root = Path(__file__).resolve().parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    from shared.google_sheet import fill_current_month_amounts, fill_current_month_bonus_breakdown, fill_geographic_repartition_amounts, fill_geographic_repartition_uninvested_amount
    from shared.report_date import get_report_now, is_current_month
    from shared.state import load_state, save_state
    from shared.xirr import compute_xirr

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("iuvo_diversification")

LOGIN_PAGE_URL = "https://iuvo-group.com/en/login/"
API_BASE = "https://tbp2p.iuvo-group.com"
# Pin the timezone explicitly (rather than relying on the executing
# machine's local clock, e.g. UTC on a CI runner) so "today"/"this month"
# are computed in the account's own local time, same pattern as every
# other *_diversification.py.
REPORT_TIMEZONE = ZoneInfo("Europe/Paris")

# Verified 2026-07-27 against the real `trans_type` filter dropdown's
# option values and the account statement transaction rows' `value`
# attributes (see module docstring).
INTEREST_TRANS_TYPES = {"payment_interest", "payment_interest_buyback", "payment_interest_early"}
BONUS_TRANS_TYPES = {"bonus"}
# Verified live 2026-08-14 (full-history dump): a deposit shows up as
# `value="deposit"` with a positive-turnover amount. No withdrawal has ever
# happened on this test account - matched via a case-insensitive "withdraw"
# substring as a forward-looking safety net, same convention used elsewhere
# in this repo (e.g. Swaper's WITHDRAW*-type matching).
DEPOSIT_TRANS_TYPES = {"deposit"}

IUVO_EMAIL = os.environ.get("IUVO_EMAIL")
IUVO_PASSWORD = os.environ.get("IUVO_PASSWORD")

# Cache of one aggregate statement summary per calendar month since account
# inception (see get_cached_monthly_summaries() below) - Iuvo's
# account_statement_grouped_page endpoint only returns TYPE-GROUPED totals
# for a queried date range (no per-transaction dated ledger, confirmed live
# 2026-08-14: each row is one aggregated trans_type, not one dated event) -
# same monthly-aggregate approximation already used for Lendermarket's XIRR
# block (see lendermarket_diversification.py's module docstring for the
# full methodology this mirrors).
XIRR_CASHFLOWS_STATE_FILE = Path(__file__).parent / "iuvo_xirr_cashflows_state.json"
XIRR_CASHFLOWS_STATE_DEFAULT = {"monthly_summaries": {}, "last_fetched_month": None}
# Conservative floor for the one-time yearly scan used to find the
# account's real inception year (see _find_first_active_year()) - well
# before Iuvo existed, just a safety bound on the scan length.
XIRR_HISTORY_FALLBACK_START_YEAR = 2015


def _parse_amount(text):
    """Parse a currency-formatted amount (e.g. "1000.00", "-1000.00")
    into a float. Iuvo's own figures use '.' as the decimal separator and
    ',' only as a thousands separator (never both meaning decimals)."""
    if text is None:
        return None
    cleaned = text.replace("\xa0", " ").replace("EUR", "").replace("€", "").strip()
    cleaned = cleaned.replace(" ", "").replace(",", "")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def login(session: requests.Session) -> str:
    """Log in to Iuvo via a direct POST to its legacy TeleBid backend (no
    browser - see module docstring). Returns the `session_token` ("p2")
    that must be passed on every subsequent API call."""
    log.info("Logging in to Iuvo as %s...", IUVO_EMAIL)
    # Cheap warm-up GET of the login page - not strictly required (the
    # login POST works fine without it too), but mirrors a real browser's
    # navigation and picks up the WordPress PHPSESSID cookie.
    session.get(LOGIN_PAGE_URL, timeout=30)

    resp = session.post(
        f"{API_BASE}/p2p-ui/?p0=login;=en_US;randn={random.random()}",
        data={"login": IUVO_EMAIL, "password": IUVO_PASSWORD},
        headers={"Referer": LOGIN_PAGE_URL, "Origin": "https://iuvo-group.com"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Iuvo login failed: HTTP {resp.status_code} - {resp.text[:300]}")

    data = resp.json()
    if (data.get("status") or {}).get("status") != "ok":
        raise RuntimeError(f"Iuvo login failed: {data!r}")

    session_token = (data.get("result") or {}).get("session_token")
    if not session_token:
        raise RuntimeError(f"Iuvo login did not return a session_token: {data!r}")

    log.info("Logged in successfully.")
    return session_token


def fetch_balance_and_originators(session: requests.Session, session_token: str) -> dict:
    """Fetch the "Account Balance" widget's exact figures + per-loan-
    originator breakdown from the overview_page's embedded
    `var investors = [...]` JS literal. See module docstring."""
    log.info("Fetching Iuvo overview page (balance + loan-originator breakdown)...")
    resp = session.get(
        f"{API_BASE}/p2p-ui/v2/app",
        params={
            "p0": "overview_page", "p2": session_token, "lang": "en_US",
            "screen_width": 1280, "screen_height": 720,
        },
        headers={"Referer": "https://iuvo-group.com/en/dashboard/"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Iuvo overview_page returned HTTP {resp.status_code}")

    match = re.search(r"var investors = (\[.*?\]);", resp.text)
    if not match:
        raise RuntimeError("Could not find 'var investors = [...]' in the overview_page response.")
    investors = json.loads(match.group(1))
    if not investors:
        raise RuntimeError("The 'investors' array in the overview_page response is empty.")

    balance = investors[0].get("accountBalance") or {}
    log.info("Raw accountBalance payload: %r", balance)
    total = _parse_amount(str(balance.get("totalAmount")))
    invested_funds = _parse_amount(str(balance.get("investedFunds")))
    available_funds = _parse_amount(str(balance.get("availableFunds")))
    if total is None or invested_funds is None:
        raise RuntimeError(f"Could not parse 'totalAmount'/'investedFunds' out of {balance!r}")

    originators = []
    for entry in balance.get("investmentsByLoanOriginator") or []:
        name = entry.get("aggregator")
        value = entry.get("value")
        if name is None or value is None:
            continue
        originators.append({"name": str(name).strip(), "amount": round(float(value), 2)})

    return {
        "total": total,
        "receivables_p2p": invested_funds,
        "available_funds": available_funds if available_funds is not None else 0.0,
        "originators": originators,
    }


def _find_investor_account_id(html: str) -> str:
    match = re.search(
        r'id="investor_account_id".*?<option value="(\d+)"\s*(?:\n\s*)?selected="selected"',
        html, re.S,
    )
    if not match:
        raise RuntimeError("Could not find the selected 'investor_account_id' option in the account-statement page.")
    return match.group(1)


def fetch_statement_summary(session: requests.Session, session_token: str, investor_account_id: str, start_date: date, end_date: date) -> dict:
    """Fetch account_statement_grouped_page for an arbitrary [start_date,
    end_date] range - generalized 2026-08-14 (was fetch_current_month_interest(),
    hardcoded to the current calendar month - kept below as a thin wrapper)
    so run() can ALSO query this once per calendar month since account
    inception, needed to build XIRR's monthly-approximated cashflows/Cash
    drag reconstruction (see module docstring - this endpoint has no
    per-transaction dated ledger, only type-grouped totals for the queried
    range).

    Also parses the page's own real "Opening Balance"/"Closing Balance"
    row (verified live 2026-08-14: this is the account's UNINVESTED wallet
    balance - i.e. the same concept as `available_funds` in
    fetch_balance_and_originators(), not the whole account value - since a
    real 1000 EUR deposit + 1000 EUR auto-invest + 0.61 interest + 6.99
    principal repayment nets out to exactly the real closing "Available
    Funds" figure) - this makes it a direct drop-in for Cash drag's
    avg-idle-cash reconstruction, same idea as Lendermarket's own
    openingBalance/closingBalance.
    """
    date_from = start_date.isoformat()
    date_to = end_date.isoformat()
    log.info(
        "Fetching Iuvo account-statement transactions (investor_account_id=%s, date range %s to %s)...",
        investor_account_id, date_from, date_to,
    )
    resp = session.get(
        f"{API_BASE}/p2p-ui/v2/app",
        params={
            "p0": "account_statement_grouped_page",
            "investor_account_id": investor_account_id,
            "trans_category": "all",
            "date_from": date_from,
            "date_to": date_to,
            "p2": session_token,
            "lang": "en_US",
            "screen_width": 1280,
            "screen_height": 720,
        },
        headers={"Referer": "https://iuvo-group.com/en/account-statement/"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Iuvo filtered account_statement_grouped_page returned HTTP {resp.status_code}")
    html = resp.text

    rows = re.findall(
        r'<a class="btn p2p-trans" value="([a-zA-Z_]*)"[^>]*>.*?</a>\s*</td>\s*'
        r'<td class="(?:positive|negative)-turnover">(-?[\d.,]+)</td>',
        html, re.S,
    )
    log.info("Raw account-statement transaction rows for %s to %s: %r", date_from, date_to, rows)

    opening_match = re.search(r'opening-balance-row">.*?<strong>[^<]*</strong>\s*</td>\s*<td class="head-cell"><strong>(-?[\d.,]+)</strong>', html, re.S)
    closing_match = re.search(r'closing-balance-row">.*?<strong>[^<]*</strong>\s*</td>\s*<td class="head-cell"><strong>(-?[\d.,]+)</strong>', html, re.S)
    opening_balance = _parse_amount(opening_match.group(1)) if opening_match else 0.0
    closing_balance = _parse_amount(closing_match.group(1)) if closing_match else 0.0

    gross_interest_received = 0.0
    bonus_cashback_contest = 0.0
    deposits = 0.0
    withdrawals = 0.0
    for trans_type, amount_text in rows:
        amount = _parse_amount(amount_text)
        if amount is None:
            continue
        if trans_type in INTEREST_TRANS_TYPES:
            gross_interest_received += amount
        elif trans_type in BONUS_TRANS_TYPES:
            bonus_cashback_contest += amount
        elif trans_type in DEPOSIT_TRANS_TYPES:
            deposits += amount
        elif "withdraw" in trans_type.lower():
            withdrawals += abs(amount)

    result = {
        "gross_interest_received": round(gross_interest_received, 2),
        "net_interest_received": round(gross_interest_received, 2),
        "withholding_tax": 0.0,
        "bonus_cashback_contest": round(bonus_cashback_contest, 2),
        "deposits": round(deposits, 2),
        "withdrawals": round(withdrawals, 2),
        "opening_balance": round(opening_balance or 0.0, 2),
        "closing_balance": round(closing_balance or 0.0, 2),
    }
    log.info("Parsed statement totals for %s to %s: %r", date_from, date_to, result)
    return result


def _fetch_investor_account_id(session: requests.Session, session_token: str) -> str:
    """Fetch the unfiltered account_statement_grouped_page just to scrape
    `investor_account_id` out of it (see module docstring)."""
    log.info("Fetching Iuvo account-statement page (to find investor_account_id)...")
    resp = session.get(
        f"{API_BASE}/p2p-ui/v2/app",
        params={
            "p0": "account_statement_grouped_page", "p2": session_token, "lang": "en_US",
            "screen_width": 1280, "screen_height": 720,
        },
        headers={"Referer": "https://iuvo-group.com/en/account-statement/"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Iuvo account_statement_grouped_page returned HTTP {resp.status_code}")
    return _find_investor_account_id(resp.text)


def fetch_current_month_interest(session: requests.Session, session_token: str) -> dict:
    """Thin wrapper around fetch_statement_summary() for the current
    calendar month (1st of the month through today)."""
    now = get_report_now(REPORT_TIMEZONE)
    investor_account_id = _fetch_investor_account_id(session, session_token)
    return fetch_statement_summary(session, session_token, investor_account_id, now.replace(day=1).date(), now.date())


def _find_first_active_year(session: requests.Session, session_token: str, investor_account_id: str, today: date) -> int:
    """Find the account's real inception year via a short yearly scan (Jan
    1 through Dec 31, or today for the current year) starting at
    XIRR_HISTORY_FALLBACK_START_YEAR - returns the first year with a
    nonzero opening balance or deposit. Falls back to `today.year` (a
    young/brand-new account) if none is found - only runs ONCE (the very
    first time the cache file doesn't exist yet)."""
    for year in range(XIRR_HISTORY_FALLBACK_START_YEAR, today.year + 1):
        year_start = date(year, 1, 1)
        year_end = min(date(year, 12, 31), today)
        summary = fetch_statement_summary(session, session_token, investor_account_id, year_start, year_end)
        if summary["opening_balance"] or summary["deposits"] or summary["withdrawals"]:
            log.info("First active year found: %d.", year)
            return year
    log.info("No activity found back to %d - treating %d as the inception year.", XIRR_HISTORY_FALLBACK_START_YEAR, today.year)
    return today.year


def get_cached_monthly_summaries(session: requests.Session, session_token: str, investor_account_id: str, today: date) -> dict:
    """Return `{"YYYY-MM": {...fetch_statement_summary()'s dict..., "days_covered": N}}`
    since account inception, fetching from account_statement_grouped_page
    only the calendar months not already cached locally - same incremental
    idea as lendermarket_diversification.get_cached_monthly_summaries()."""
    state = load_state(XIRR_CASHFLOWS_STATE_FILE, XIRR_CASHFLOWS_STATE_DEFAULT)
    monthly_summaries = dict(state.get("monthly_summaries") or {})
    last_fetched_month = state.get("last_fetched_month")

    if last_fetched_month:
        start_year, start_month = (int(part) for part in last_fetched_month.split("-"))
    else:
        log.info("No cached monthly summaries found - scanning for the account's inception year...")
        start_year = _find_first_active_year(session, session_token, investor_account_id, today)
        start_month = 1

    log.info(
        "Found %d cached monthly summary(ies) (last fetched: %s) - fetching from %04d-%02d through %04d-%02d...",
        len(monthly_summaries), last_fetched_month, start_year, start_month, today.year, today.month,
    )

    year, month = start_year, start_month
    while (year, month) <= (today.year, today.month):
        month_start = date(year, month, 1)
        last_day_of_month = calendar.monthrange(year, month)[1]
        month_end = min(date(year, month, last_day_of_month), today)
        summary = fetch_statement_summary(session, session_token, investor_account_id, month_start, month_end)
        summary["days_covered"] = (month_end - month_start).days + 1
        summary["end_date"] = month_end.strftime("%Y-%m-%d")
        monthly_summaries[f"{year:04d}-{month:02d}"] = summary

        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1

    save_state(XIRR_CASHFLOWS_STATE_FILE, {
        "monthly_summaries": monthly_summaries,
        "last_fetched_month": f"{today.year:04d}-{today.month:02d}",
    })
    log.info("Monthly summaries cache now holds %d month(s).", len(monthly_summaries))
    return monthly_summaries


def compute_average_idle_cash(monthly_summaries: dict) -> float:
    """Day-weighted average of each cached month's own (opening+closing)/2
    balance, weighted by how many days that month's own query covered -
    same monthly-granularity approximation as
    lendermarket_diversification.compute_average_idle_cash() (Iuvo has no
    per-transaction dated ledger - see module docstring)."""
    total_weighted = 0.0
    total_days = 0
    for summary in monthly_summaries.values():
        days = summary.get("days_covered") or 0
        if days <= 0:
            continue
        midpoint = (summary["opening_balance"] + summary["closing_balance"]) / 2
        total_weighted += midpoint * days
        total_days += days
    if total_days == 0:
        return 0.0
    return total_weighted / total_days


def run() -> None:
    if not IUVO_EMAIL or not IUVO_PASSWORD:
        log.error("IUVO_EMAIL and IUVO_PASSWORD environment variables are required.")
        sys.exit(1)

    log.info("Starting Iuvo diversification run (pure-HTTP, no browser).")

    session = requests.Session()

    try:
        session_token = login(session)
        balance_data = fetch_balance_and_originators(session, session_token)
    except Exception:
        log.exception("Failed to log in or fetch Iuvo's balance/loan-originator breakdown.")
        sys.exit(1)

    try:
        log.info("Fetching this month's interest received from the account statement...")
        interest_totals = fetch_current_month_interest(session, session_token)
    except Exception:
        log.exception("Failed to fetch this month's interest received - defaulting to 0.0.")
        interest_totals = {
            "gross_interest_received": 0.0, "net_interest_received": 0.0,
            "withholding_tax": 0.0, "bonus_cashback_contest": 0.0,
        }

    log.info(
        "Iuvo balance: total=%.2f EUR, receivables_in_p2p=%.2f EUR across %d loan originator(s):",
        balance_data["total"], balance_data["receivables_p2p"], len(balance_data["originators"]),
    )
    for o in balance_data["originators"]:
        log.info("  %s: %.2f EUR", o["name"], o["amount"])
    log.info("This month's interest received: %.2f EUR", interest_totals["gross_interest_received"])

    amounts = {
        "total": balance_data["total"],
        "gross_interest_received": interest_totals["gross_interest_received"],
        "net_interest_received": interest_totals["net_interest_received"],
        "withholding_tax": interest_totals["withholding_tax"],
        "bonus_cashback_contest": interest_totals["bonus_cashback_contest"],
        "receivables_p2p": balance_data["receivables_p2p"],
    }

    current_month = is_current_month()
    today_date = get_report_now(REPORT_TIMEZONE).date()

    # Since-inception XIRR (money-weighted return) + this month's Cash
    # drag + the XIRR Bonus/Cash drag/Taxes-Frais pie-chart shares - same
    # monthly-aggregate methodology as lendermarket_diversification.py
    # (Iuvo's statement endpoint only returns type-grouped totals for a
    # queried range, no per-transaction dated ledger - see module
    # docstring). total_invested here = everything NOT sitting idle in the
    # uninvested wallet (receivables in P2P + iuvoSAVE), i.e. total minus
    # available_funds.
    total_invested = balance_data["total"] - balance_data["available_funds"]
    xirr_value = None
    signed_cashflows = None
    total_account_value = None
    bonus_xirr_contribution = None
    cash_drag_value = None
    cash_drag_xirr_contribution = None
    taxes_xirr_contribution = 0.0  # Iuvo has no separate withholding-tax transaction type (see module docstring) - genuinely 0, not a placeholder.
    monthly_summaries = None
    if current_month:
        try:
            investor_account_id = _fetch_investor_account_id(session, session_token)
            log.info("Fetching the since-inception monthly statement summaries (cached where possible)...")
            monthly_summaries = get_cached_monthly_summaries(session, session_token, investor_account_id, today_date)
        except Exception:
            log.exception("Failed to fetch the monthly statement summary history - XIRR will not be updated.")
            monthly_summaries = None

    if current_month and monthly_summaries:
        total_account_value = balance_data["total"]
        signed_cashflows = []
        for month_key in sorted(monthly_summaries):
            summary = monthly_summaries[month_key]
            net_deposit = summary["deposits"] - summary["withdrawals"]
            if abs(net_deposit) < 0.005:
                continue
            year, month = (int(part) for part in month_key.split("-"))
            try:
                month_end = datetime.strptime(summary["end_date"], "%Y-%m-%d").date()
            except (KeyError, ValueError):
                month_end = date(year, month, calendar.monthrange(year, month)[1])
            cashflow_day = min(15, month_end.day)
            signed_cashflows.append((date(year, month, cashflow_day), -net_deposit))

        signed_cashflows.append((today_date, total_account_value))

        xirr_value = compute_xirr(signed_cashflows)
        if xirr_value is None:
            log.warning("Could not compute XIRR from %d monthly cashflow(s) - XIRR row will not be updated.", len(signed_cashflows) - 1)
        else:
            log.info(
                "Computed since-inception XIRR: %.2f%% (%d monthly cashflow(s), current total value %.2f EUR).",
                xirr_value * 100, len(signed_cashflows) - 1, total_account_value,
            )

            lifetime_bonus_total = sum(s["bonus_cashback_contest"] for s in monthly_summaries.values())
            if lifetime_bonus_total:
                cashflows_without_bonus = signed_cashflows[:-1] + [(today_date, total_account_value - lifetime_bonus_total)]
                xirr_without_bonus = compute_xirr(cashflows_without_bonus)
                if xirr_without_bonus is not None:
                    bonus_xirr_contribution = xirr_value - xirr_without_bonus
                    log.info("Bonus's own share of XIRR: %.2f points.", bonus_xirr_contribution * 100)
            else:
                bonus_xirr_contribution = 0.0

    if current_month and total_invested > 0 and monthly_summaries:
        current_month_summary = monthly_summaries.get(f"{today_date.year:04d}-{today_date.month:02d}") or {}
        avg_idle_cash_this_month = (current_month_summary.get("opening_balance", 0.0) + current_month_summary.get("closing_balance", 0.0)) / 2
        cash_weight = avg_idle_cash_this_month / (avg_idle_cash_this_month + total_invested)
        monthly_yield_rate = current_month_summary.get("gross_interest_received", 0.0) / total_invested
        cash_drag_value = cash_weight * monthly_yield_rate
        log.info(
            "Computed Cash drag: %.2f%% (avg idle cash %.2f EUR, cash weight %.2f%%, monthly yield %.2f%%).",
            cash_drag_value * 100, avg_idle_cash_this_month, cash_weight * 100, monthly_yield_rate * 100,
        )

        if xirr_value is not None and signed_cashflows is not None:
            avg_idle_cash_lifetime = compute_average_idle_cash(monthly_summaries)
            cash_weight_lifetime = avg_idle_cash_lifetime / (avg_idle_cash_lifetime + total_invested)
            lifetime_interest_total = sum(s["gross_interest_received"] for s in monthly_summaries.values())
            lifetime_yield_rate = lifetime_interest_total / total_invested
            cash_drag_lifetime_total = cash_weight_lifetime * lifetime_yield_rate
            missed_earnings = cash_drag_lifetime_total * (avg_idle_cash_lifetime + total_invested)
            cashflows_with_cash_invested = signed_cashflows[:-1] + [(today_date, total_account_value + missed_earnings)]
            xirr_with_cash_invested = compute_xirr(cashflows_with_cash_invested)
            if xirr_with_cash_invested is not None:
                cash_drag_xirr_contribution = xirr_value - xirr_with_cash_invested
                log.info(
                    "XIRR share - cash drag: %.4f points (since-inception, avg idle cash %.2f EUR, missed earnings ~%.2f EUR).",
                    cash_drag_xirr_contribution * 100, avg_idle_cash_lifetime, missed_earnings,
                )

    # "total" comes from the overview_page's embedded `investors` JS
    # literal, a LIVE-only snapshot; the date-filtered account-statement
    # endpoint has no balance field either (2026-08-06 investigation) -
    # skip total for a backfilled month.
    fill_current_month_amounts(platform="Iuvo", amounts=amounts, skip_total=not current_month)

    # "XIRR"/"Cash drag" and the XIRR Bonus/Cash drag/Taxes-Frais
    # pie-chart shares (rows already added by the user, platform_row+10
    # through +14) - only included when actually computed.
    bonus_breakdown = {}
    if xirr_value is not None:
        bonus_breakdown["XIRR"] = xirr_value
    if cash_drag_value is not None:
        bonus_breakdown["Cash drag"] = cash_drag_value
    if bonus_xirr_contribution is not None:
        bonus_breakdown["XIRR Bonus"] = bonus_xirr_contribution
    if cash_drag_xirr_contribution is not None:
        bonus_breakdown["XIRR Cash drag"] = cash_drag_xirr_contribution
    if xirr_value is not None:
        bonus_breakdown["XIRR Taxes/Frais"] = taxes_xirr_contribution
    if bonus_breakdown:
        fill_current_month_bonus_breakdown(platform="Iuvo", breakdown=bonus_breakdown, max_rows=15)

    if current_month:
        fill_geographic_repartition_amounts(balance_data["originators"], platform="Iuvo")
        fill_geographic_repartition_uninvested_amount("Iuvo", balance_data["available_funds"])


if __name__ == "__main__":
    run()
