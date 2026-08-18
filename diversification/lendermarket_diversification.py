"""Lendermarket portfolio diversification (by loan originator) fetcher.

Logs into Lendermarket (reusing lendermarket_monitor.login(), which already
handles email/password + TOTP 2FA over pure HTTP - not duplicated here) and
fetches every active investment via the site's own "Mes investissements"
API, then groups them by loan originator ("fournisseur de crédit") and sums
the remaining principal ("capital restant") per originator. No email is
sent - the amounts are just logged and handed to fill_current_month_amounts()
(see google_sheet.py) so they can be filled into a Google Sheet, mirroring
peerberry_diversification.py.

API verified against the real account on 2026-07-09 (and re-verified via
pure HTTP on 2026-07-18):
`GET https://api.lendermarket.com/claims/v1/investor/getInvestorInvestments?activeInvestments=1&currency=EUR&page=N`
-> `{"data": [...], "meta": {"current_page", "last_page", ...}}`, one entry
per active investment:
`{"remainingPrincipal": "42.42", "lender": {"displayName": "Creditstar Sweden", ...}, "loan": {"lender": {...}, ...}, ...}`
(the top-level `lender` and `loan.lender` are the same originator, kept as a
fallback). Paginates via `meta.last_page` defensively. Requires the same
`x-xsrf-token` + `X-INVESTOR-ID` headers as every other authenticated
Lendermarket call (see lendermarket_monitor.py's module docstring for the
full auth flow).

Also fetches this calendar month's "Intérêts reçus" + "Intérêts de retard
reçus" (summed into one "interest received" figure, per explicit user
instructions) and "Primes promotionnelles et bonus" from the Account
Statement summary API - see fetch_current_month_statement_totals() below,
same idea as loanch_diversification.fetch_current_month_statement_totals()
/ swaper_diversification.fetch_current_month_interest_received() /
afranga_diversification.fetch_current_month_statement_totals() /
bienpreter_diversification.fetch_current_month_interest_totals(). Unlike
Bienpreter, Lendermarket has a clean, ready-made JSON summary endpoint for
this - no gross/net/tax reconstruction needed here.

Also computes a since-inception XIRR (money-weighted return) plus this
month's Cash drag and the XIRR Bonus / XIRR Cash drag / XIRR Taxes/Frais
pie-chart shares, mirroring afranga_diversification.py/
swaper_diversification.py's own XIRR block (see those modules' docstrings
for the full methodology) - with ONE real difference, confirmed live
2026-08-14: getInvestorAccountStatementSummary (the ONLY statement-history
endpoint Lendermarket exposes - verified live by probing a dozen
plausible per-transaction/"ledger"/"payments" endpoint names, all 404,
and by grepping the /fr/statement SPA's own loaded JS bundles for any
`/ledger|claims|payments|wallet/v*/...` path literal, none found) only
ever returns AGGREGATE totals for an arbitrary [startDate, endDate] range
(openingBalance/closingBalance/investorDepositsAmount/
investorWithdrawalsAmount/investorReceivedInterestsAmount/
investorReceivedDelayedInterestsAmount/investorBonusesAmount/
investorFeeAmount/...) - there is NO per-transaction dated ledger like
Afranga's "Details" table or Swaper's account-entries API. So instead of
real per-transaction dates, this queries the summary endpoint ONCE PER
CALENDAR MONTH since account inception (cached incrementally, see
get_cached_monthly_summaries()/lendermarket_xirr_cashflows_state.json
below) and:
  - builds ONE XIRR cashflow per month with a nonzero net
    deposit/withdrawal, dated on the 15th of that month (a deliberate
    mid-month approximation - the best available without real per-day
    dates - capped to the month's own queried end date so a still-open
    current month never gets a "future" cashflow date);
  - reconstructs "Cash drag" using each month's own (opening+closing)/2
    balance, day-weighted by how many days that month's query actually
    covered - a MONTHLY-granularity idle-cash average, not the day-by-day
    reconstruction Afranga/Swaper's Cash drag uses (their platforms expose
    a real per-transaction ledger, Lendermarket doesn't) - documented here
    as a known, deliberate approximation, not a bug;
  - sums each month's own real `investorBonusesAmount`/`investorFeeAmount`
    for the lifetime Bonus/Taxes-Frais XIRR counterfactual shares (same
    add-back-and-recompute-XIRR technique as Afranga) - `investorFeeAmount`
    is a REAL field (confirmed 0.00 on the live test account so far, same
    "genuinely zero, not a placeholder" convention as every other verified-
    but-currently-zero figure elsewhere in this repo).
The account's inception month is found via a short yearly-then-monthly
scan (see _find_first_active_year()) rather than assuming a fixed start
date, since Lendermarket accounts are typically much younger than e.g.
Swaper's - looping monthly from an arbitrarily old fixed date would waste
many empty-month API calls on the very first run.

Added 2026-08-18: XIRR Intérêts, the counterfactual XIRR share
attributable to real net interest received (mirrors
bienpreter_diversification.py's/afranga_diversification.py's/
iuvo_diversification.py's own XIRR Intérêts blocks). Lendermarket has no
separate gross/withholding-tax split on interest either (same convention
as `net_interest_received == gross_interest_received` used for `amounts`
above) - fees ("investorFeeAmount") are already isolated on their own as
"XIRR Taxes/Frais", so "lifetime net interest" here is simply the sum of
each cached month's own `interest_received` (Intérêts reçus + Intérêts de
retard reçus), with no further adjustment. This exists because "Intérêts"
was previously only ever a RESIDUAL on the spreadsheet/dashboard side
(XIRR - XIRR Bonus - XIRR Cash drag - XIRR Taxes/Frais), which can
legitimately go negative when the bonus's counterfactual XIRR share is
disproportionately large relative to the account's real underlying
(non-bonus) performance - that's not a bug, it's the correct signal that
the account's return is propped up almost entirely by the bonus. XIRR
Intérêts instead gives a genuine, independently-measured figure (same
category of computation as Bonus/Taxes, not a derived leftover), so the
two can be compared/sanity-checked against each other on the sheet/
dashboard side.

Required env vars:
    LENDERMARKET_EMAIL, LENDERMARKET_PASSWORD  -> Lendermarket credentials
Optional:
    LENDERMARKET_TOTP_SECRET                   -> base32 secret used to set up
                                                   Google Authenticator, needed
                                                   if 2FA is enabled on the account
    GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS         -> used to write this month's
                                                   totals to the Google Sheet
                                                   via fill_current_month_amounts()
                                                   (see google_sheet.py)
"""

import calendar
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
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

from monitors.lendermarket_monitor import login, LENDERMARKET_EMAIL, LENDERMARKET_PASSWORD, _xsrf_headers, fetch_account_balance

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("lendermarket_diversification")

INVESTMENTS_API_URL = "https://api.lendermarket.com/claims/v1/investor/getInvestorInvestments"
STATEMENT_SUMMARY_API_URL = "https://api.lendermarket.com/ledger/v1/investor/getInvestorAccountStatementSummary"
# Lendermarket is a French-facing platform (app.lendermarket.com/fr/...);
# "this month" below means the current calendar month up to TODAY (1st of
# the month through today, NOT the full month) - same semantics as the
# equivalent quick filters on Swaper/Afranga/Bienpreter, matched here by
# reproducing the exact date range the page's own "Mois en cours" filter
# sends. Pinned explicitly rather than relying on the executing machine's
# local clock (e.g. UTC on a CI runner).
REPORT_TIMEZONE = ZoneInfo("Europe/Paris")

# Cache of one aggregate summary per calendar month since account
# inception (see get_cached_monthly_summaries() below) - same incremental-
# fetch idea as afranga_diversification.py's XIRR_CASHFLOWS_STATE_FILE,
# just keyed by month instead of by individual dated row (Lendermarket has
# no per-transaction ledger - see module docstring).
XIRR_CASHFLOWS_STATE_FILE = Path(__file__).parent / "lendermarket_xirr_cashflows_state.json"
XIRR_CASHFLOWS_STATE_DEFAULT = {"monthly_summaries": {}, "last_fetched_month": None}
# Conservative floor for the one-time yearly scan used to find the
# account's real inception year (see _find_first_active_year()) - well
# before Lendermarket existed, just a safety bound on the scan length.
XIRR_HISTORY_FALLBACK_START_YEAR = 2015


def fetch_investments(session: requests.Session, investor_id: str) -> list:
    """Fetch every active investment across all pages of the investments API
    (see module docstring for the verified response shape)."""
    investments = []
    page_number = 1
    while True:
        log.info("Requesting investments API page %d...", page_number)
        r = session.get(
            INVESTMENTS_API_URL,
            params={"activeInvestments": 1, "currency": "EUR", "page": page_number},
            headers=_xsrf_headers(session, investor_id),
            timeout=20,
        )
        log.info("Investments API page %d response: status=%s", page_number, r.status_code)
        if not r.ok:
            raise RuntimeError(f"Investments API returned status {r.status_code} on page {page_number}")

        body = r.json() or {}
        page_investments = body.get("data") or []
        investments.extend(page_investments)
        log.info("Page %d: %d investment(s) found (running total: %d).", page_number, len(page_investments), len(investments))

        meta = body.get("meta") or {}
        if page_number >= (meta.get("last_page") or 1):
            break
        page_number += 1

    return investments


def aggregate_by_lender(investments: list) -> list:
    """Group investments by loan originator (fournisseur de crédit) and sum
    the remaining principal (capital restant) for each - one entry per
    lender, sorted by remaining amount descending."""
    totals = {}
    for inv in investments:
        lender_name = (
            (inv.get("lender") or {}).get("displayName")
            or ((inv.get("loan") or {}).get("lender") or {}).get("displayName")
            or "Fournisseur inconnu"
        )
        try:
            remaining = float(inv.get("remainingPrincipal"))
        except (TypeError, ValueError):
            remaining = 0.0
        totals[lender_name] = totals.get(lender_name, 0.0) + remaining

    lenders = [{"lender": lender, "remaining_principal": amount} for lender, amount in totals.items()]
    lenders.sort(key=lambda l: l["remaining_principal"], reverse=True)
    return lenders


def fetch_statement_summary(session: requests.Session, investor_id: str, start_date: date, end_date: date) -> dict:
    """Fetch getInvestorAccountStatementSummary for an arbitrary
    [start_date, end_date] range - generalized 2026-08-14 (was
    fetch_current_month_statement_totals(), hardcoded to the current
    calendar month - kept below as a thin wrapper) so run() can ALSO query
    this once per calendar month since account inception, needed to build
    XIRR's monthly-approximated cashflows/Cash drag reconstruction (see
    module docstring - this endpoint has no per-transaction dated ledger).

    Verified against the real account on 2026-07-10/2026-08-14:

        GET .../ledger/v1/investor/getInvestorAccountStatementSummary?currency=EUR&startDate=...&endDate=...
        -> {"data": {"openingBalance": "0.00", "investorDepositsAmount": "6001.00",
                     "investorInvestmentAmount": "12385.57", "investorReceivedPrincipalAmount": "6200.35",
                     "investorReceivedInterestsAmount": "161.79", "investorReceivedDelayedInterestsAmount": "13.48",
                     "investorWithdrawalsAmount": "0.00", "investorBonusesAmount": "17.00",
                     "investorFeeAmount": "0.00", "closingBalance": "8.05", ...}}
    """
    start_str = start_date.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d")
    log.info("Requesting account statement summary API for %s to %s...", start_str, end_str)

    r = session.get(
        STATEMENT_SUMMARY_API_URL,
        params={"currency": "EUR", "startDate": start_str, "endDate": end_str},
        headers=_xsrf_headers(session, investor_id),
        timeout=20,
    )
    log.info("Account statement summary API response: status=%s", r.status_code)
    if not r.ok:
        raise RuntimeError(f"Account statement summary API returned status {r.status_code}")

    data = (r.json() or {}).get("data") or {}
    log.info("Raw statement summary data: %r", data)

    def _amount(key):
        try:
            return float(data.get(key) or 0.0)
        except (TypeError, ValueError):
            log.warning("Could not parse %r %r - defaulting to 0.0.", key, data.get(key))
            return 0.0

    interest_received = _amount("investorReceivedInterestsAmount") + _amount("investorReceivedDelayedInterestsAmount")
    bonuses = _amount("investorBonusesAmount")
    fees = _amount("investorFeeAmount")
    opening_balance = _amount("openingBalance")
    closing_balance = _amount("closingBalance")
    deposits = _amount("investorDepositsAmount")
    withdrawals = _amount("investorWithdrawalsAmount")

    log.info(
        "Parsed statement totals: interest_received=%.2f, bonuses=%.2f, fees=%.2f, "
        "opening_balance=%.2f, closing_balance=%.2f, deposits=%.2f, withdrawals=%.2f",
        interest_received, bonuses, fees, opening_balance, closing_balance, deposits, withdrawals,
    )
    return {
        "interest_received": interest_received,
        "bonuses": bonuses,
        "fees": fees,
        "opening_balance": opening_balance,
        "closing_balance": closing_balance,
        "deposits": deposits,
        "withdrawals": withdrawals,
    }


def fetch_current_month_statement_totals(session: requests.Session, investor_id: str) -> dict:
    """Thin wrapper around fetch_statement_summary() for the current
    calendar month (1st of the month through today) - see that function's
    docstring for the endpoint/parsing details."""
    now = get_report_now(REPORT_TIMEZONE)
    return fetch_statement_summary(session, investor_id, now.replace(day=1).date(), now.date())


def _find_first_active_year(session: requests.Session, investor_id: str, today: date) -> int:
    """Find the account's real inception year via a short yearly scan
    (Jan 1 through Dec 31, or today for the current year) starting at
    XIRR_HISTORY_FALLBACK_START_YEAR - returns the first year with a
    nonzero opening balance, deposit, or withdrawal. Falls back to
    `today.year` (a young/brand-new account) if none is found - this only
    ever runs ONCE (the very first time the cache file doesn't exist yet),
    every later run reuses the cached `last_fetched_month` instead."""
    for year in range(XIRR_HISTORY_FALLBACK_START_YEAR, today.year + 1):
        year_start = date(year, 1, 1)
        year_end = min(date(year, 12, 31), today)
        summary = fetch_statement_summary(session, investor_id, year_start, year_end)
        if summary["opening_balance"] or summary["deposits"] or summary["withdrawals"]:
            log.info("First active year found: %d.", year)
            return year
    log.info("No activity found back to %d - treating %d as the inception year.", XIRR_HISTORY_FALLBACK_START_YEAR, today.year)
    return today.year


def get_cached_monthly_summaries(session: requests.Session, investor_id: str, today: date) -> dict:
    """Return `{"YYYY-MM": {...fetch_statement_summary()'s dict..., "days_covered": N}}`
    since account inception, fetching from getInvestorAccountStatementSummary
    only the calendar months not already cached locally (in
    XIRR_CASHFLOWS_STATE_FILE) - same incremental-fetch idea as
    afranga_diversification.get_cached_account_details(), just keyed by
    month instead of by individual dated row (see module docstring for
    why: Lendermarket has no per-transaction ledger, only range
    aggregates).

    Re-fetches starting from the cached `last_fetched_month` itself (not
    the month after) so a still-partial current month gets its totals
    refreshed on every run, not just newly-added months - overwrites that
    one cache entry rather than appending a duplicate.
    """
    state = load_state(XIRR_CASHFLOWS_STATE_FILE, XIRR_CASHFLOWS_STATE_DEFAULT)
    monthly_summaries = dict(state.get("monthly_summaries") or {})
    last_fetched_month = state.get("last_fetched_month")

    if last_fetched_month:
        start_year, start_month = (int(part) for part in last_fetched_month.split("-"))
    else:
        log.info("No cached monthly summaries found - scanning for the account's inception year...")
        start_year = _find_first_active_year(session, investor_id, today)
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
        summary = fetch_statement_summary(session, investor_id, month_start, month_end)
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
    the best idle-cash reconstruction available at Lendermarket's real
    data granularity (monthly aggregates only, no per-transaction ledger -
    see module docstring). NOT a day-by-day reconstruction like Afranga/
    Swaper's own compute_average_idle_cash() - deliberately documented
    here as a coarser approximation, not a bug."""
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
    if not LENDERMARKET_EMAIL or not LENDERMARKET_PASSWORD:
        log.error("LENDERMARKET_EMAIL and LENDERMARKET_PASSWORD environment variables are required.")
        sys.exit(1)

    # XIRR (like "total" elsewhere in this repo) is a LIVE-only snapshot
    # metric (needs TODAY's real total account value as its final
    # cashflow) - only ever computed/written for the real current month,
    # same convention as Afranga/Swaper.
    current_month = is_current_month()
    today_date = get_report_now(REPORT_TIMEZONE).date()

    log.info("Starting Lendermarket diversification run (pure HTTP, no browser).")

    session = requests.Session()
    try:
        investor_id = login(session)
        investments = fetch_investments(session, investor_id)
    except Exception:
        log.exception("Failed to log in or fetch Lendermarket investments.")
        sys.exit(1)

    try:
        statement_totals = fetch_current_month_statement_totals(session, investor_id)
    except Exception:
        log.exception("Failed to fetch this month's interest received/bonuses - defaulting to 0.0.")
        statement_totals = {
            "interest_received": 0.0, "bonuses": 0.0, "fees": 0.0,
            "opening_balance": 0.0, "closing_balance": 0.0, "deposits": 0.0, "withdrawals": 0.0,
        }

    lenders = aggregate_by_lender(investments)
    log.info("Fetched %d active investment(s) across %d loan originator(s).", len(investments), len(lenders))
    for l in lenders:
        log.info("  %s: %.2f EUR", l["lender"], l["remaining_principal"])

    log.info(
        "This month's statement totals: interest_received=%.2f EUR, bonuses=%.2f EUR",
        statement_totals["interest_received"], statement_totals["bonuses"],
    )

    # Lendermarket's statement summary API has no gross/net/withholding-tax
    # breakdown (unlike Afranga/Bienpreter) - interest_received is mapped to
    # both gross_interest_received/net_interest_received since it's the
    # only real figure on hand, withholding_tax defaults to 0.0. Same
    # standardized dict shape as every other *_diversification.py, plus the
    # platform-specific interest_received/bonuses fields kept alongside it.
    # "bonuses" (investorBonusesAmount, "Primes promotionnelles et bonus")
    # was already fetched but only kept as a platform-specific extra field,
    # never surfaced under the standardized name - dissociated here too
    # via bonus_cashback_contest so it's ready to be written to its own
    # Sheet cell, separate from interest.
    total_invested = sum(l["remaining_principal"] for l in lenders)

    # Needed both for "total"/"non investi" AND as part of XIRR's final "as
    # if withdrawn today" total account value below - fetched once here,
    # ahead of all three uses.
    available_balance = fetch_account_balance(session, investor_id)
    if available_balance is None:
        log.warning("Could not fetch Lendermarket's available balance - 'total'/'non investi' and XIRR will not be updated.")

    # "total" ("en cours") written to the Sheet is invested + uninvested,
    # per user request 2026-08-14 (matching Bienprêter/Iuvo/Bricks/Lande's
    # own convention) - falls back to invested-only if the available
    # balance couldn't be fetched. `total_invested` itself stays
    # invested-only, since it feeds the Cash drag/XIRR math below.
    amounts = {
        "total": total_invested + available_balance if available_balance is not None else total_invested,
        "gross_interest_received": statement_totals["interest_received"],
        "net_interest_received": statement_totals["interest_received"],
        "withholding_tax": 0.0,
        "bonus_cashback_contest": statement_totals["bonuses"],
        "interest_received": statement_totals["interest_received"],
        "bonuses": statement_totals["bonuses"],
    }

    # Since-inception XIRR (money-weighted return) + this month's Cash
    # drag + the XIRR Bonus/Cash drag/Taxes-Frais/Intérêts pie-chart shares
    # - see module docstring for the monthly-aggregate methodology
    # (Lendermarket has no per-transaction dated ledger, unlike Afranga/
    # Swaper).
    monthly_summaries = None
    if current_month:
        try:
            log.info("Fetching the since-inception monthly statement summaries (cached where possible)...")
            monthly_summaries = get_cached_monthly_summaries(session, investor_id, today_date)
        except Exception:
            log.exception("Failed to fetch the monthly statement summary history - XIRR will not be updated.")
            monthly_summaries = None

    xirr_value = None
    signed_cashflows = None
    total_account_value = None
    bonus_xirr_contribution = None
    if current_month and monthly_summaries and available_balance is not None:
        total_account_value = total_invested + available_balance
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

            lifetime_bonus_total = sum(s["bonuses"] for s in monthly_summaries.values())
            if lifetime_bonus_total:
                cashflows_without_bonus = signed_cashflows[:-1] + [(today_date, total_account_value - lifetime_bonus_total)]
                xirr_without_bonus = compute_xirr(cashflows_without_bonus)
                if xirr_without_bonus is not None:
                    bonus_xirr_contribution = xirr_value - xirr_without_bonus
                    log.info("Bonus's own share of XIRR: %.2f points.", bonus_xirr_contribution * 100)
            else:
                bonus_xirr_contribution = 0.0

    cash_drag_value = None
    cash_drag_xirr_contribution = None
    taxes_xirr_contribution = None
    # XIRR Intérêts (added 2026-08-18, mirrors bienpreter_diversification.py's/
    # afranga_diversification.py's/iuvo_diversification.py's own XIRR
    # Intérêts blocks): counterfactual XIRR share attributable to real net
    # interest received since inception. Lendermarket has no separate
    # gross/withholding-tax split (fees are already isolated as their own
    # "Taxes/Frais" figure below), so "lifetime net interest" here is just
    # the sum of each cached month's own `interest_received` - computed
    # further down, once monthly_summaries/signed_cashflows are available.
    interest_xirr_contribution = None
    if current_month and total_invested > 0:
        avg_idle_cash_this_month = (statement_totals["opening_balance"] + statement_totals["closing_balance"]) / 2
        cash_weight = avg_idle_cash_this_month / (avg_idle_cash_this_month + total_invested)
        monthly_yield_rate = statement_totals["interest_received"] / total_invested
        cash_drag_value = cash_weight * monthly_yield_rate
        log.info(
            "Computed Cash drag: %.2f%% (avg idle cash %.2f EUR, cash weight %.2f%%, monthly yield %.2f%%).",
            cash_drag_value * 100, avg_idle_cash_this_month, cash_weight * 100, monthly_yield_rate * 100,
        )

        if xirr_value is not None and signed_cashflows is not None and monthly_summaries:
            avg_idle_cash_lifetime = compute_average_idle_cash(monthly_summaries)
            cash_weight_lifetime = avg_idle_cash_lifetime / (avg_idle_cash_lifetime + total_invested)
            lifetime_interest_total = sum(s["interest_received"] for s in monthly_summaries.values())
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

            lifetime_fees_total = sum(s["fees"] for s in monthly_summaries.values())
            if lifetime_fees_total:
                cashflows_with_fees_cancelled = signed_cashflows[:-1] + [(today_date, total_account_value + lifetime_fees_total)]
                xirr_with_fees_cancelled = compute_xirr(cashflows_with_fees_cancelled)
                if xirr_with_fees_cancelled is not None:
                    taxes_xirr_contribution = xirr_value - xirr_with_fees_cancelled
                    log.info("XIRR share - taxes/frais: %.4f points (lifetime fees %.2f EUR).", taxes_xirr_contribution * 100, lifetime_fees_total)
            else:
                taxes_xirr_contribution = 0.0

            # XIRR Intérêts (added 2026-08-18): same counterfactual pattern
            # as XIRR Bonus/XIRR Taxes-Frais above - lifetime net interest
            # here is just lifetime_interest_total (already summed above
            # for Cash drag's lifetime yield rate, reused here rather than
            # recomputed), since Lendermarket has no separate withholding
            # tax on interest.
            if lifetime_interest_total:
                cashflows_without_interest = signed_cashflows[:-1] + [(today_date, total_account_value - lifetime_interest_total)]
                xirr_without_interest = compute_xirr(cashflows_without_interest)
                if xirr_without_interest is not None:
                    interest_xirr_contribution = xirr_value - xirr_without_interest
                    log.info(
                        "XIRR share - intérêts: %.4f points (lifetime net interest %.2f EUR, no withholding tax on Lendermarket).",
                        interest_xirr_contribution * 100, lifetime_interest_total,
                    )
            else:
                interest_xirr_contribution = 0.0

    # "total" comes from a live balance call/summed active investments plus
    # the available (uninvested) balance, and
    # getInvestorAccountStatementSummary (the date-ranged statement API) has
    # no balance field (2026-08-06 investigation) - skip total for a
    # backfilled month.
    fill_current_month_amounts(
        platform="Lendermarket",
        amounts=amounts,
        skip_total=not current_month,
    )

    # Lendermarket's "investorBonusesAmount" IS literally labelled "Primes
    # promotionnelles et bonus" on the platform itself - a "prime", not a
    # cashback/concours - written to its own dedicated sub-row, never to
    # the "Bonus" row itself (a SUM formula over prime/cashback/concours).
    # "XIRR"/"Cash drag" and the XIRR Bonus/Cash drag/Taxes-Frais/Intérêts
    # pie-chart shares are appended past the default max_rows=6 bound, same
    # convention as afranga_diversification.py - only included when
    # actually computed.
    # UPDATED 2026-08-18: "XIRR Intérêts" sits right after "XIRR
    # Taxes/Frais" (mirrors Bienprêter's/Afranga's/Iuvo's own block
    # layout) - this pushes the block one row taller than it was before
    # (platform_row+9 through +13 previously), so `max_rows` is bumped
    # 14 -> 15 to keep the search bounded before the next platform block.
    # IMPORTANT: a "XIRR Intérêts" row must exist in the Lendermarket block
    # on the sheet itself (right after "XIRR Taxes/Frais") for this new
    # value to actually land somewhere - this script fills an existing row
    # by label, it doesn't insert new labelled rows into this block.
    bonus_breakdown = {"prime": statement_totals["bonuses"]}
    if xirr_value is not None:
        bonus_breakdown["XIRR"] = xirr_value
    if cash_drag_value is not None:
        bonus_breakdown["Cash drag"] = cash_drag_value
    if bonus_xirr_contribution is not None:
        bonus_breakdown["XIRR Bonus"] = bonus_xirr_contribution
    if cash_drag_xirr_contribution is not None:
        bonus_breakdown["XIRR Cash drag"] = cash_drag_xirr_contribution
    if taxes_xirr_contribution is not None:
        bonus_breakdown["XIRR Taxes/Frais"] = taxes_xirr_contribution
    if interest_xirr_contribution is not None:
        bonus_breakdown["XIRR Intérêts"] = interest_xirr_contribution
    fill_current_month_bonus_breakdown(
        platform="Lendermarket",
        breakdown=bonus_breakdown,
        max_rows=15,
    )

    loan_originators = [
        {"name": l["lender"], "amount": l["remaining_principal"]}
        for l in lenders
    ]

    if current_month:
        fill_geographic_repartition_amounts(loan_originators, platform="Lendermarket")

        # "non investi" row (added 2026-08-10): reuses
        # lendermarket_monitor.fetch_account_balance() (investorAvailableBalanceAmount),
        # already relied on elsewhere in this repo to gate the invest bot.
        if available_balance is not None:
            fill_geographic_repartition_uninvested_amount("Lendermarket", available_balance)


if __name__ == "__main__":
    run()