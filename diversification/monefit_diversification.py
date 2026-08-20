"""Monefit (SmartSaver) account balance fetcher.

Same idea as afranga_diversification.py / peerberry_diversification.py /
lendermarket_diversification.py / loanch_diversification.py, but simpler:
Monefit SmartSaver is a single savings product (not a marketplace split
across many loan originators), so there's nothing to group/aggregate - this
just logs into https://smartsaver.monefit.com, reads the account's "Total
Wealth" balance, and hands it to fill_current_month_amounts() (see
google_sheet.py) as a single-entry list using "monefit" as the loan
originator label, mirroring the {"originator", "outstanding"} shape used by
afranga_diversification.py. No email is sent - same as the other
diversification scripts.

REWRITTEN 2026-07-18 to use plain `requests` instead of Playwright (no
browser at all), same technique as bricks_diversification.py /
goandgrow_diversification.py / bienpreter_diversification.py - much faster
in GitHub Actions. Verified live: Monefit's backend (a separate API host,
`api-smartsaver.monefit.com`) has NO Cloudflare/bot-protection at all and
returns the JWT directly in the login response body (no DOM-interception
trick needed like the old Playwright version had to use for the account
summary call).

Login mechanism (verified 2026-07-18): `POST
https://api-smartsaver.monefit.com/v1/auth/login` with JSON body
`{"identificator": <email>, "password": <password>}` (NOT `"email"` - that
field name returns a 401 "Incorrect e-mail or password" even with correct
credentials, `"identificator"` is the field the SPA actually uses) returns
`{"data": {"result": {"mfa": false, "token": "<JWT>", ...}}}` - the JWT is
used as a `Authorization: Bearer <token>` header on every subsequent API
call. `mfa` is `false` on this account (confirmed no 2FA/TOTP step exists,
consistent with the original 2026-07-09 Playwright build's observation).

Balance: `GET https://api-smartsaver.monefit.com/v1/vaults` returns
`{"data": {"total": "...", "totalWealth": "5194.02522602", "mainAccount":
"...", "result": [...per-vault details...]}}` - `totalWealth` matches the
summary endpoint's `closingBalance` (see below) and is the same figure
shown as "Total Wealth" on the summary page.

This month's stats: `GET
https://api-smartsaver.monefit.com/v1/account/summary?dateFrom=<1st of
month>&dateTo=<today>` returns `{"data": {"result": {"openingBalance":
..., "closingBalance": ..., "interestIncome": "0.00000408", "bonus":
"0.00000000", "maturedVaults": "0", ...}}}` - same fields/semantics as the
old Playwright version's `page.expect_response()` capture of this same
endpoint (the SPA calls it itself); "Current month" default window
(1st of the current month through TODAY).

Also computes a since-inception XIRR (money-weighted return) plus this
month's Cash drag and the XIRR Bonus / XIRR Cash drag / XIRR Taxes/Frais /
XIRR Intérêts pie-chart shares (see run() below) - same monthly-aggregate
methodology as lendermarket_diversification.py/iuvo_diversification.py
(Monefit's account/summary endpoint only returns aggregate totals for a
queried range, no per-transaction dated ledger, unlike
afranga/peerberry/swaper).

Added 2026-08-19: XIRR Intérêts, the counterfactual XIRR share
attributable to real net interest received since inception (mirrors
afranga_diversification.py's/peerberry_diversification.py's/
swaper_diversification.py's own XIRR Intérêts block exactly - same
counterfactual-XIRR pattern as Bonus/Cash drag/Taxes above, computed in
the same block as cash_drag_xirr_contribution, reusing the already-summed
`lifetime_interest_total` - no extra fetch needed). Like Swaper/PeerBerry
(and unlike Afranga, which has a real gross/withholding-tax split to
subtract), Monefit's account/summary API has no withholding-tax data at
all (withholding_tax defaults to 0.0 above) - so `lifetime_interest_total`
(the sum of each cached month's "daily_returns"/interestIncome) already IS
the lifetime net interest figure, used directly as `lifetime_net_interest`.
As with the other platforms, a "XIRR Intérêts" row must already exist in
the Monefit block on the sheet itself (right after "XIRR Taxes/Frais") for
this new value to land anywhere - fill_current_month_bonus_breakdown()
fills an existing row by label, it doesn't insert new labelled rows.
`max_rows` is bumped 14 -> 15 to keep the search bounded past this now-
taller block.

Required env vars:
    MONEFIT_EMAIL, MONEFIT_PASSWORD    -> Monefit SmartSaver account credentials
Optional:
    GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS  -> used to write this month's totals
                                            to the Google Sheet via
                                            fill_current_month_amounts() (see
                                            google_sheet.py)
"""

import os
import sys
import calendar
import logging
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

from shared.google_sheet import fill_current_month_amounts, fill_current_month_bonus_breakdown, fill_geographic_repartition_amounts
from shared.report_date import get_report_now, is_current_month
from shared.state import load_state, save_state
from shared.xirr import compute_xirr

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("monefit_diversification")

API_BASE = "https://api-smartsaver.monefit.com"
LOGIN_URL = f"{API_BASE}/v1/auth/login"
VAULTS_URL = f"{API_BASE}/v1/vaults"
SUMMARY_URL = f"{API_BASE}/v1/account/summary"
LOAN_ORIGINATOR_LABEL = "monefit"
# Pin the timezone explicitly (rather than the executing machine's local
# clock, e.g. UTC on a CI runner) so "today"/"this month" - and, for a
# month-range backfill run, the REPORT_DATE-driven end-of-month date used
# below to look up a past month's real closing balance - are computed in
# the account's own local time, same pattern as every other
# *_diversification.py.
REPORT_TIMEZONE = ZoneInfo("Europe/Paris")

MONEFIT_EMAIL = os.environ.get("MONEFIT_EMAIL")
MONEFIT_PASSWORD = os.environ.get("MONEFIT_PASSWORD")

# Cache of one aggregate account/summary per calendar month since account
# inception (see get_cached_monthly_summaries() below) - Monefit's own
# account/summary endpoint only returns AGGREGATE totals for an arbitrary
# date range (openingBalance/closingBalance/depositSum/withdrawalSum/
# interestIncome/bonus/fees/...), no per-transaction dated ledger - same
# monthly-aggregate approximation already used for Lendermarket's/Iuvo's
# XIRR block (see lendermarket_diversification.py's module docstring for
# the full methodology this mirrors). IMPORTANT DIFFERENCE from those two:
# this endpoint's openingBalance/closingBalance is the WHOLE ACCOUNT value
# (verified live 2026-08-14: closingBalance over the account's full
# history matches "totalWealth" exactly), NOT an uninvested-wallet-only
# balance - so it is NOT used for Cash drag's avg-idle-cash reconstruction
# (see compute_average_idle_cash() below, which uses the live `mainAccount`
# snapshot instead).
XIRR_CASHFLOWS_STATE_FILE = Path(__file__).parent / "monefit_xirr_cashflows_state.json"
XIRR_CASHFLOWS_STATE_DEFAULT = {"monthly_summaries": {}, "last_fetched_month": None}
# Conservative floor for the one-time yearly scan used to find the
# account's real inception year (see _find_first_active_year()) - well
# before Monefit SmartSaver existed, just a safety bound on the scan length.
XIRR_HISTORY_FALLBACK_START_YEAR = 2015

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://smartsaver.monefit.com",
    "Referer": "https://smartsaver.monefit.com/en/login",
}


def login(session: requests.Session) -> None:
    """Log in to Monefit SmartSaver using MONEFIT_EMAIL/MONEFIT_PASSWORD via
    a plain HTTP POST (no browser), and set the returned JWT as the
    session's Authorization header for subsequent calls.

    Raises RuntimeError if the account has 2FA enabled (not handled here -
    no 2FA was ever observed on this account, see module docstring) or if
    login otherwise fails.
    """
    log.info("POST %s (submitting credentials)...", LOGIN_URL)
    r = session.post(LOGIN_URL, json={"identificator": MONEFIT_EMAIL, "password": MONEFIT_PASSWORD}, timeout=30)
    log.info("POST login: status=%s", r.status_code)
    r.raise_for_status()

    result = (r.json() or {}).get("data", {}).get("result") or {}
    if result.get("mfa"):
        raise RuntimeError("Monefit is asking for a 2FA/MFA step, which this pure-HTTP script does not handle.")

    token = result.get("token")
    if not token:
        raise RuntimeError(f"Login response did not contain a token: {result!r}")

    session.headers.update({"Authorization": f"Bearer {token}"})
    log.info("Logged in successfully.")


def fetch_balance(session: requests.Session) -> float:
    """Fetch the account's "Total Wealth" balance via the vaults endpoint.
    See module docstring for the verified `totalWealth` field."""
    return fetch_vaults_breakdown(session)["total_wealth"]


def fetch_vaults_breakdown(session: requests.Session) -> dict:
    """Fetch the vaults endpoint's full balance breakdown: `total_wealth`
    (the whole account, same as fetch_balance()'s return value),
    `invested` (`total` field - sum of all vault balances, i.e. the money
    actually earning a vault's own rate), and `main_account` (cash sitting
    OUTSIDE any vault, not yet earning - verified live 2026-08-14:
    `total_wealth - main_account == invested` exactly). `main_account` is
    the real "idle cash" figure used by Cash drag below - typically tiny
    on this account (SmartSaver auto-allocates deposits into vaults almost
    immediately)."""
    log.info("GET %s (fetching balance breakdown)...", VAULTS_URL)
    r = session.get(VAULTS_URL, timeout=30)
    log.info("GET vaults: status=%s", r.status_code)
    r.raise_for_status()

    result = (r.json() or {}).get("data") or {}
    total_wealth = result.get("totalWealth")
    if total_wealth is None:
        raise RuntimeError(f"Could not find 'totalWealth' in the vaults response: {result!r}")
    try:
        total_wealth = float(total_wealth)
        invested = float(result.get("total") if result.get("total") is not None else total_wealth)
        main_account = float(result.get("mainAccount") or 0.0)
    except (TypeError, ValueError):
        raise RuntimeError(f"Could not parse vaults response: {result!r}")

    return {"total_wealth": total_wealth, "invested": invested, "main_account": main_account}


def fetch_statement_summary(session: requests.Session, start_date: date, end_date: date) -> dict:
    """Fetch account/summary for an arbitrary [start_date, end_date] range -
    generalized 2026-08-14 (was fetch_current_month_statement_totals(),
    hardcoded to the current calendar month - kept below as a thin
    wrapper) so run() can ALSO query this once per calendar month since
    account inception, needed to build XIRR's monthly-approximated
    cashflows (see module docstring). Also parses `depositSum`/
    `withdrawalSum`/`fees`/`openingBalance` - real fields, verified live
    2026-08-14 over a wide multi-year range (`depositSum=5000.00,
    withdrawalSum=38.42, fees=0`).
    """
    params = {"dateFrom": start_date.isoformat(), "dateTo": end_date.isoformat()}
    log.info("GET %s (fetching statement totals for %s to %s)...", SUMMARY_URL, start_date, end_date)
    r = session.get(SUMMARY_URL, params=params, timeout=30)
    log.info("GET account/summary: status=%s", r.status_code)
    r.raise_for_status()

    result = (r.json() or {}).get("data", {}).get("result") or {}
    log.info("Raw account/summary result: %r", result)

    def _amount(key):
        try:
            return round(float(result.get(key) or 0.0), 2)
        except (TypeError, ValueError):
            log.warning("Could not parse %r %r - defaulting to 0.0.", key, result.get(key))
            return 0.0

    daily_returns = _amount("interestIncome")
    rewards_bonuses = _amount("bonus")
    matured_vaults = _amount("maturedVaults")
    deposits = _amount("depositSum")
    withdrawals = _amount("withdrawalSum")
    fees = _amount("fees")
    opening_balance = _amount("openingBalance")
    try:
        closing_balance = round(float(result["closingBalance"]), 2) if result.get("closingBalance") is not None else None
    except (TypeError, ValueError):
        log.warning("Could not parse 'closingBalance' %r.", result.get("closingBalance"))
        closing_balance = None

    log.info(
        "Parsed statement totals: daily_returns=%.2f, rewards_bonuses=%.2f, matured_vaults=%.2f, "
        "deposits=%.2f, withdrawals=%.2f, fees=%.2f, opening_balance=%.2f, closing_balance=%s",
        daily_returns, rewards_bonuses, matured_vaults, deposits, withdrawals, fees, opening_balance, closing_balance,
    )
    return {
        "daily_returns": daily_returns,
        "rewards_bonuses": rewards_bonuses,
        "matured_vaults": matured_vaults,
        "deposits": deposits,
        "withdrawals": withdrawals,
        "fees": fees,
        "opening_balance": opening_balance,
        "closing_balance": closing_balance,
    }


def fetch_current_month_statement_totals(session: requests.Session) -> dict:
    """Thin wrapper around fetch_statement_summary() for the current
    calendar month (1st of the month through today)."""
    end_date = get_report_now(REPORT_TIMEZONE).date()
    return fetch_statement_summary(session, end_date.replace(day=1), end_date)


def _find_first_active_year(session: requests.Session, today: date) -> int:
    """Find the account's real inception year via a short yearly scan (Jan
    1 through Dec 31, or today for the current year) starting at
    XIRR_HISTORY_FALLBACK_START_YEAR - returns the first year with a
    nonzero opening balance or deposit. Falls back to `today.year` (a
    young/brand-new account) if none is found - only runs ONCE (the very
    first time the cache file doesn't exist yet)."""
    for year in range(XIRR_HISTORY_FALLBACK_START_YEAR, today.year + 1):
        year_start = date(year, 1, 1)
        year_end = min(date(year, 12, 31), today)
        summary = fetch_statement_summary(session, year_start, year_end)
        if summary["opening_balance"] or summary["deposits"] or summary["withdrawals"]:
            log.info("First active year found: %d.", year)
            return year
    log.info("No activity found back to %d - treating %d as the inception year.", XIRR_HISTORY_FALLBACK_START_YEAR, today.year)
    return today.year


def get_cached_monthly_summaries(session: requests.Session, today: date) -> dict:
    """Return `{"YYYY-MM": {...fetch_statement_summary()'s dict..., "days_covered": N}}`
    since account inception, fetching from account/summary only the
    calendar months not already cached locally - same incremental idea as
    lendermarket_diversification.get_cached_monthly_summaries()/
    iuvo_diversification.get_cached_monthly_summaries()."""
    state = load_state(XIRR_CASHFLOWS_STATE_FILE, XIRR_CASHFLOWS_STATE_DEFAULT)
    monthly_summaries = dict(state.get("monthly_summaries") or {})
    last_fetched_month = state.get("last_fetched_month")

    if last_fetched_month:
        start_year, start_month = (int(part) for part in last_fetched_month.split("-"))
    else:
        log.info("No cached monthly summaries found - scanning for the account's inception year...")
        start_year = _find_first_active_year(session, today)
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
        summary = fetch_statement_summary(session, month_start, month_end)
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


def run() -> None:
    if not MONEFIT_EMAIL or not MONEFIT_PASSWORD:
        log.error("MONEFIT_EMAIL and MONEFIT_PASSWORD environment variables are required.")
        sys.exit(1)

    log.info("Starting Monefit diversification run (pure HTTP, no browser).")

    session = requests.Session()
    session.headers.update(_HEADERS)

    try:
        login(session)
        vaults = fetch_vaults_breakdown(session)
        balance = vaults["total_wealth"]
    except Exception:
        log.exception("Failed to log in or fetch the Monefit balance.")
        sys.exit(1)

    try:
        log.info("Fetching this month's statement totals...")
        statement_totals = fetch_current_month_statement_totals(session)
    except Exception:
        log.exception(
            "Failed to fetch this month's From daily returns/Rewards & bonuses/Matured Vaults - "
            "defaulting all three to 0.0."
        )
        statement_totals = {"daily_returns": 0.0, "rewards_bonuses": 0.0, "matured_vaults": 0.0, "deposits": 0.0, "withdrawals": 0.0, "fees": 0.0, "opening_balance": 0.0, "closing_balance": None}

    originators = [{"originator": LOAN_ORIGINATOR_LABEL, "outstanding": balance}]
    log.info("Monefit balance: %.2f EUR", balance)
    log.info(
        "This month's From daily returns: %.2f EUR, Rewards & bonuses: %.2f EUR, Matured Vaults: %.2f EUR",
        statement_totals["daily_returns"], statement_totals["rewards_bonuses"], statement_totals["matured_vaults"],
    )

    # Monefit's account/summary API has no gross/net/withholding-tax
    # breakdown (unlike Afranga/Bienpreter) - "From daily returns" is
    # mapped to both gross_interest_received/net_interest_received since
    # it's the closest equivalent figure on hand, withholding_tax defaults
    # to 0.0. Same standardized dict shape as every other
    # *_diversification.py, plus the platform-specific daily_returns/
    # rewards_bonuses/matured_vaults fields kept alongside it.
    # "rewards_bonuses" (the "Rewards & bonuses" figure) was already
    # fetched but only kept as a platform-specific extra field, never
    # surfaced under the standardized name - dissociated here too via
    # bonus_cashback_contest so it's ready to be written to its own Sheet
    # cell, separate from interest.
    current_month = is_current_month()
    today_date = get_report_now(REPORT_TIMEZONE).date()
    end_date = today_date
    closing_balance = statement_totals.get("closing_balance")
    # For the real current month, always use the live "Total Wealth"
    # balance (fetch_balance()). For a backfilled past month (a month-range
    # run), the account/summary endpoint's own "closingBalance" for that
    # month IS the real historical total (verified to match "Total Wealth"
    # - see module docstring) - use it instead of skipping the total
    # entirely, falling back to skip_total only if that field couldn't be
    # fetched/parsed this run.
    total = balance if current_month else (closing_balance if closing_balance is not None else balance)
    skip_total = not current_month and closing_balance is None

    amounts = {
        "total": total,
        "gross_interest_received": statement_totals["daily_returns"],
        "net_interest_received": statement_totals["daily_returns"],
        "withholding_tax": 0.0,
        "bonus_cashback_contest": statement_totals["rewards_bonuses"],
        "daily_returns": statement_totals["daily_returns"],
        "rewards_bonuses": statement_totals["rewards_bonuses"],
        "matured_vaults": statement_totals["matured_vaults"],
    }

    fill_current_month_amounts(
        platform="Monefit",
        amounts=amounts,
        section="Crowdlending savings",
        skip_total=skip_total,
    )

    # Since-inception XIRR (money-weighted return) + this month's Cash
    # drag + the XIRR Bonus/Cash drag/Taxes-Frais/Intérêts pie-chart
    # shares - same monthly-aggregate methodology as
    # lendermarket_diversification.py/iuvo_diversification.py (Monefit's
    # account/summary endpoint only returns aggregate totals for a queried
    # range, no per-transaction dated ledger). Cash drag's avg-idle-cash
    # uses the LIVE `mainAccount` snapshot (see fetch_vaults_breakdown()'s
    # docstring for why the monthly opening/closing balance can't be
    # reused here - it's the WHOLE account, not an idle-cash-only figure)
    # - a real, but current-snapshot-only (not a true historical average),
    # figure.
    xirr_value = None
    signed_cashflows = None
    total_account_value = None
    bonus_xirr_contribution = None
    cash_drag_value = None
    cash_drag_xirr_contribution = None
    taxes_xirr_contribution = None
    # XIRR Intérêts (added 2026-08-19, mirrors afranga_diversification.py's/
    # peerberry_diversification.py's/swaper_diversification.py's own XIRR
    # Intérêts block - see module docstring for the full rationale):
    # counterfactual XIRR share attributable to real net interest received
    # since inception. Like Swaper/PeerBerry (and unlike Afranga, which has
    # to subtract a real withholding tax), Monefit has no withholding-tax
    # data at all, so the lifetime interest total already summed below for
    # Cash drag's lifetime_yield_rate (`lifetime_interest_total`) IS the
    # lifetime net interest figure, reused directly here.
    interest_xirr_contribution = None
    monthly_summaries = None
    total_invested = vaults["invested"]
    avg_idle_cash = vaults["main_account"]

    if current_month:
        try:
            log.info("Fetching the since-inception monthly statement summaries (cached where possible)...")
            monthly_summaries = get_cached_monthly_summaries(session, end_date)
        except Exception:
            log.exception("Failed to fetch the monthly statement summary history - XIRR will not be updated.")
            monthly_summaries = None

    if current_month and monthly_summaries:
        total_account_value = balance
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

        signed_cashflows.append((end_date, total_account_value))

        xirr_value = compute_xirr(signed_cashflows)
        if xirr_value is None:
            log.warning("Could not compute XIRR from %d monthly cashflow(s) - XIRR row will not be updated.", len(signed_cashflows) - 1)
        else:
            log.info(
                "Computed since-inception XIRR: %.2f%% (%d monthly cashflow(s), current total value %.2f EUR).",
                xirr_value * 100, len(signed_cashflows) - 1, total_account_value,
            )

            lifetime_bonus_total = sum(s["rewards_bonuses"] for s in monthly_summaries.values())
            if lifetime_bonus_total:
                cashflows_without_bonus = signed_cashflows[:-1] + [(end_date, total_account_value - lifetime_bonus_total)]
                xirr_without_bonus = compute_xirr(cashflows_without_bonus)
                if xirr_without_bonus is not None:
                    bonus_xirr_contribution = xirr_value - xirr_without_bonus
                    log.info("Bonus's own share of XIRR: %.2f points.", bonus_xirr_contribution * 100)
            else:
                bonus_xirr_contribution = 0.0

            lifetime_fees_total = sum(s["fees"] for s in monthly_summaries.values())
            if lifetime_fees_total:
                cashflows_with_fees_cancelled = signed_cashflows[:-1] + [(end_date, total_account_value + lifetime_fees_total)]
                xirr_with_fees_cancelled = compute_xirr(cashflows_with_fees_cancelled)
                if xirr_with_fees_cancelled is not None:
                    taxes_xirr_contribution = xirr_value - xirr_with_fees_cancelled
                    log.info("XIRR share - taxes/frais: %.4f points (lifetime fees %.2f EUR).", taxes_xirr_contribution * 100, lifetime_fees_total)
            else:
                taxes_xirr_contribution = 0.0

    if current_month and total_invested > 0:
        cash_weight = avg_idle_cash / (avg_idle_cash + total_invested)
        monthly_yield_rate = statement_totals["daily_returns"] / total_invested
        cash_drag_value = cash_weight * monthly_yield_rate
        log.info(
            "Computed Cash drag: %.2f%% (avg idle cash %.2f EUR, cash weight %.2f%%, monthly yield %.2f%%).",
            cash_drag_value * 100, avg_idle_cash, cash_weight * 100, monthly_yield_rate * 100,
        )

        if xirr_value is not None and signed_cashflows is not None and monthly_summaries:
            cash_weight_lifetime = cash_weight  # no real historical idle-cash time series - reuse the live snapshot (see comment above).
            lifetime_interest_total = sum(s["daily_returns"] for s in monthly_summaries.values())
            lifetime_yield_rate = lifetime_interest_total / total_invested
            cash_drag_lifetime_total = cash_weight_lifetime * lifetime_yield_rate
            missed_earnings = cash_drag_lifetime_total * (avg_idle_cash + total_invested)
            cashflows_with_cash_invested = signed_cashflows[:-1] + [(end_date, total_account_value + missed_earnings)]
            xirr_with_cash_invested = compute_xirr(cashflows_with_cash_invested)
            if xirr_with_cash_invested is not None:
                cash_drag_xirr_contribution = xirr_value - xirr_with_cash_invested
                log.info(
                    "XIRR share - cash drag: %.4f points (since-inception, avg idle cash %.2f EUR, missed earnings ~%.2f EUR).",
                    cash_drag_xirr_contribution * 100, avg_idle_cash, missed_earnings,
                )

            # XIRR Intérêts: same counterfactual pattern as Bonus/Cash drag
            # above, but for the real net interest received since
            # inception - lifetime_interest_total (already summed just
            # above for cash_drag_lifetime_total's yield rate) is used
            # directly (no withholding tax to subtract, see comment above
            # interest_xirr_contribution's declaration).
            if lifetime_interest_total:
                cashflows_without_interest = signed_cashflows[:-1] + [(end_date, total_account_value - lifetime_interest_total)]
                xirr_without_interest = compute_xirr(cashflows_without_interest)
                if xirr_without_interest is not None:
                    interest_xirr_contribution = xirr_value - xirr_without_interest
                    log.info(
                        "XIRR share - intérêts: %.4f points (lifetime net interest %.2f EUR).",
                        interest_xirr_contribution * 100, lifetime_interest_total,
                    )
            else:
                interest_xirr_contribution = 0.0

    # Monefit's "bonus" field ("Rewards & bonuses") maps to "prime" (a
    # referral-style reward) - written to its own dedicated sub-row, never
    # to the "Bonus" row itself (a SUM formula over prime/cashback/
    # concours). Note: Monefit also runs a separate weekly investment-draw
    # ("concours"-style lottery, confirmed via live page text: "5 winners
    # ... picked at random") but the account/summary API has no distinct
    # field for draw winnings - only "prime" is written here, "concours"
    # is left untouched pending a dedicated data source. "XIRR"/"Cash
    # drag" and the XIRR Bonus/Cash drag/Taxes-Frais/Intérêts pie-chart
    # shares (rows already added by the user) are only included when
    # actually computed. "XIRR Intérêts" (added 2026-08-19) sits right
    # after "XIRR Taxes/Frais" - this pushes the block one row taller than
    # before, so `max_rows` is bumped 14 -> 15 to keep the search bounded
    # before the next platform block. IMPORTANT: a "XIRR Intérêts" row
    # must exist in the Monefit block on the sheet itself (right after
    # "XIRR Taxes/Frais") for this new value to actually land somewhere -
    # this script fills an existing row by label, it doesn't insert new
    # labelled rows into this block.
    bonus_breakdown = {"prime": statement_totals["rewards_bonuses"]}
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
        platform="Monefit",
        breakdown=bonus_breakdown,
        section="Crowdlending savings",
        max_rows=15,
    )

    loan_originators = [{"name": LOAN_ORIGINATOR_LABEL, "amount": balance}]

    if current_month:
        fill_geographic_repartition_amounts(loan_originators)


if __name__ == "__main__":
    run()