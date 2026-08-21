"""PeerBerry portfolio "distribution by loan originators" fetcher.

Logs into peerberry.com via pure HTTP by reusing
monitors.peerberry_monitor.login() (not duplicated here - same dependency
direction as swaper/lendermarket: login() lives in the monitor module,
which has no google_sheet dependency, and this module imports from it, not
the other way around) and fetches the per-loan-originator investment
breakdown that's shown on the Overview page under Investments > "Loan
originators" (amount invested + % of the portfolio, one row per
originator). No email is sent - the amounts are just logged and handed to
fill_current_month_amounts() (see google_sheet.py) so they can be filled
into a Google Sheet. The "total" ("en cours") written for the live current
month is invested + uninvested (available_money) - added 2026-08-14, per
explicit user request - unlike most other platforms here whose "total" is
invested-only (uninvested cash tracked separately via
fill_geographic_repartition_uninvested_amount).

`GET https://api.peerberry.com/v1/investor/overview/originators` (using the
`access_token` returned by monitors.peerberry_monitor.login() as an
`Authorization: Bearer` header, same as every other authenticated call).
Verified against the real account on 2026-07-09 (and re-verified via pure
HTTP on 2026-07-18): response is a JSON array of
`{"originator": "Lendplus ZA", "originatorId": 56, "company": "Aventus Group",
"companyId": 1, "iso2": "ZA", "amount": "1091.02", "part": "10.90"}`.

Also fetches this calendar month's "Interest income" from the Account
Summary API - see fetch_statement_summary() below, same idea as
swaper_diversification.fetch_current_month_interest_received().

Also computes a since-inception XIRR (money-weighted return) plus this
month's Cash drag and the XIRR Bonus / XIRR Cash drag / XIRR Taxes/Frais
pie-chart shares, mirroring afranga_diversification.py/
swaper_diversification.py's own XIRR block (see those modules' docstrings
for the full methodology) - added 2026-08-14, per explicit user request.
Unlike Lendermarket (no per-transaction ledger at all - only date-range
aggregates), PeerBerry DOES expose a genuine per-transaction dated ledger,
found via a real browser network capture of the Transactions section on
https://peerberry.com/en/client/statement/account-summary:

    GET https://api.peerberry.com/v2/investor/transactions?period=&startDate=<d1>&endDate=<d2>&loanId=&offset=<N>&pageSize=<N>
    -> a raw JSON array (no pagination metadata/total-count - the caller
    just keeps paginating via offset until a page comes back shorter than
    pageSize), one entry per transaction:
    `{"id": 336378843, "postDate": "2026-08-14 06:37:59", "details": "INVESTMENT",
      "loanId": 27686563, "investorId": 128112, "currencyIso": "EUR",
      "type": "INVESTMENT", "amount": "-100.00"}`.
`details` is one of the 7 categories PeerBerry's own `/v1/globals` response
enumerates under `transactionTypes` (DEPOSIT/WITHDRAWAL/REPAYMENT_PRINCIPAL/
REPAYMENT_INTEREST/INVESTMENT/INVESTMENT_SALE_FEE/REFERRAL_FEE); `type` is a
more granular technical flavor of the same thing (e.g. "BUYBACK_INTEREST"/
"BUYBACK_PRINCIPAL" when a repayment came via the buyback guarantee instead
of a normal scheduled one) - not needed here, `details` alone is enough to
classify every row. IMPORTANT: `amount` is already SIGNED to match its real
impact on the account's uninvested cash/wallet balance (DEPOSIT/REPAYMENT_*
positive, INVESTMENT negative) - confirmed by reconciling a real
account-summary response: `openingBalance + sum(operations.values()) ==
closingBalance` down to the cent (297.70 - 4390.36 + 5579.16 + 52.81 ==
1539.31) - so, unlike Swaper's account-entries rows (which need a
per-transactionType sign lookup table), this endpoint's own `amount` can be
summed directly, no sign-guessing needed.

Verified 2026-08-14 (dumping this account's entire history, 595 rows, via
pure HTTP with pageSize=20000): this account has only ever seen
DEPOSIT/INVESTMENT/BUYBACK_INTEREST/BUYBACK_PRINCIPAL rows so far - no
WITHDRAWAL/INVESTMENT_SALE_FEE/REFERRAL_FEE row exists yet to confirm their
real sign against a live example; REFERRAL_FEE is treated as a bonus/prime
credit (mirroring Swaper's referral bonus) and INVESTMENT_SALE_FEE as a
Taxes/Frais-bucket cost, both via the same add-back-and-recompute-XIRR
counterfactual technique as every other platform's Bonus/Taxes-Frais share
(sign-agnostic: it just cancels out whatever the lifetime sum's REAL sign
turns out to be) - so this is safe even though currently untested at 0.00.

IMPORTANT correction (found 2026-08-14 while adding this): a PREVIOUS
version of this module's fetch_current_month_statement_totals() docstring
claimed the account-summary API's `closingBalance` "IS the account's real
total invested+cash balance" and used it as a fallback `total` for a
backfilled month - that was WRONG. The reconciliation above proves
opening/closingBalance track the UNINVESTED CASH/WALLET balance only (it
drops when INVESTMENT happens, which doesn't change total portfolio value)
- confirmed live: `closingBalance` (1539.31) exactly equalled
`/v1/investor/overview`'s own `availableMoney` (1539.31) at the same
instant, not its `totalBalance` (10105.70). Backfilled-month `total` now
correctly uses skip_total (no historical total data source exists for this
platform, same as Lendermarket) instead of silently writing the wallet
balance into the "total invested" cell.

Added 2026-08-19: XIRR Intérêts, the counterfactual XIRR share
attributable to real net interest received since inception (mirrors
afranga_diversification.py's own XIRR Intérêts block exactly - same
counterfactual-XIRR pattern as Bonus/Cash drag/Taxes above). Unlike
Afranga (which has a real gross/withholding-tax split and must subtract
the two to get a net figure), PeerBerry's account-summary API has no such
split - `interest_income` (from fetch_statement_summary(), queried here
over the since-inception range already fetched for Cash drag/Taxes-Frais,
no extra fetch needed) already IS the lifetime net interest figure, so it
is used directly as `lifetime_net_interest`. As with Afranga, a "XIRR
Intérêts" row must already exist in the PeerBerry block on the sheet
itself (right after "XIRR Taxes/Frais") for this new value to land
anywhere - fill_current_month_bonus_breakdown() fills an existing row by
label, it doesn't insert new labelled rows. `max_rows` is bumped 14 -> 15
to keep the search bounded past this now-taller block.

BUGFIX 2026-08-21: a backfill run (scripts/run_diversification_for_month_
range.sh, which simulates get_report_now() as an arbitrary day within a
target month) for the CURRENT calendar month was able to trigger the
XIRR/transactions block below, because is_current_month() only compares
the MONTH, not the exact simulated day. When the simulated day is in the
future relative to the real wall-clock day (e.g. a backfill run using
today_date=2026-08-31 while the real date is 2026-08-20), that future date
got passed into get_cached_transactions() and persisted as
last_fetched_date in XIRR_CASHFLOWS_STATE_FILE. The NEXT real run then
called the transactions API with startDate (2026-08-31) AFTER endDate
(2026-08-20) - an inverted range - which PeerBerry's API rejects with a
422, breaking XIRR/Cash drag/Taxes-Frais/Intérêts for that run. Fixed by
gating the transactions/XIRR block on a new `is_real_today` check (today_
date must equal the REAL wall-clock day in REPORT_TIMEZONE, not just be in
the current real month) in addition to `current_month`, and by making
fetch_all_transactions() refuse to call the API at all with an inverted
date range (defense in depth, in case a stale/poisoned cache slips through
some other way). No change to any XIRR/Cash drag/Taxes-Frais/Intérêts
calculation itself - only to when the fetch that feeds them is allowed to
run and persist its cache.

Required env vars:
    PEERBERRY_EMAIL, PEERBERRY_PASSWORD    -> PeerBerry account credentials
Optional:
    PEERBERRY_TOTP_SECRET                  -> base32 secret used to set up
                                               Google Authenticator, needed
                                               if 2FA is enabled on the account
    GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS     -> used to write this month's
                                               totals to the Google Sheet via
                                               fill_current_month_amounts()
                                               (see google_sheet.py)
"""

import sys
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()

from shared.google_sheet import (
    fill_current_month_amounts,
    fill_current_month_bonus_breakdown,
    fill_geographic_repartition_amounts,
    fill_geographic_repartition_uninvested_amount,
)
from shared.report_date import get_report_now, is_current_month
from shared.state import load_state, save_state
from shared.xirr import compute_xirr
from monitors.peerberry_monitor import login, PEERBERRY_EMAIL, PEERBERRY_PASSWORD, _HEADERS, fetch_available_money

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("peerberry_diversification")

ORIGINATORS_API_URL = "https://api.peerberry.com/v1/investor/overview/originators"
ACCOUNT_SUMMARY_API_URL = "https://api.peerberry.com/v2/investor/account-summary"
TRANSACTIONS_API_URL = "https://api.peerberry.com/v2/investor/transactions"
# The Account Summary page's default "This month" period (verified 2026-07-10
# by capturing its own request) = 1st of the current month through TODAY, not
# the full calendar month - same semantics as Swaper/Afranga/Lendermarket's
# equivalents. Pin the timezone explicitly rather than relying on the
# executing machine's local clock (e.g. UTC on a CI runner).
REPORT_TIMEZONE = ZoneInfo("Europe/Paris")
# Cache of every transaction row ever fetched (see get_cached_transactions()
# below) - same incremental-fetch idea as swaper_diversification's own
# XIRR_CASHFLOWS_STATE_FILE, avoids re-fetching the account's ENTIRE history
# on every monthly run. XIRR itself is still recomputed from scratch every
# run over the full merged list (a root of a non-linear equation over every
# historical cashflow - can't be derived from last month's XIRR value).
XIRR_CASHFLOWS_STATE_FILE = Path(__file__).parent / "peerberry_xirr_cashflows_state.json"
XIRR_CASHFLOWS_STATE_DEFAULT = {"all_entries": [], "last_fetched_date": None}
# XIRR is a since-inception money-weighted return (not per-month) - this
# start date is early enough to cover any real account's full history
# (PeerBerry itself only launched in 2017).
XIRR_HISTORY_START_DATE = "2000-01-01"
# Verified live 2026-08-14: pageSize=20000 returned this account's entire
# 595-row history in one page with no error - kept as a generous page size/
# safety net (with real offset-based pagination below regardless) for
# accounts with more history than this one.
TRANSACTIONS_PAGE_SIZE = 1000
MAX_TRANSACTIONS_PAGES = 50


def fetch_originator_distribution(session: requests.Session) -> list:
    """Fetch the per-loan-originator investment breakdown via PeerBerry's own
    API (see module docstring)."""
    log.info("Requesting originators API...")
    r = session.get(ORIGINATORS_API_URL, headers=_HEADERS, timeout=20)
    log.info("Originators API response: status=%s", r.status_code)
    if not r.ok:
        raise RuntimeError(f"Originators API request failed (status={r.status_code})")

    body = r.json() or []
    log.info("Originators API returned %d raw entry(ies).", len(body))
    return body


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


def fetch_statement_summary(session: requests.Session, start_date: str, end_date: str) -> dict:
    """Fetch getInvestorAccountStatementSummary-equivalent totals for an
    arbitrary [start_date, end_date] range (both "YYYY-MM-DD") - generalized
    2026-08-14 (was fetch_current_month_statement_totals(), hardcoded to the
    current calendar month - kept below as a thin wrapper) so run() can ALSO
    query this once for the account's full since-inception range, needed by
    the XIRR/Cash drag block (see module docstring).

    Verified against the real account on 2026-07-10 (and re-verified via
    pure HTTP on 2026-07-18/2026-08-14):
    `GET https://api.peerberry.com/v2/investor/account-summary?period=&startDate=<d1>&endDate=<d2>`
    -> `{"openingBalance": "297.70", "closingBalance": "1539.31",
    "operations": {"DEPOSIT": "5000.00", "INVESTMENT": "-6578.71",
    "INTEREST": "9.88", "PRINCIPAL": "1563.61"}}` - `operations.INTEREST`
    matched the page's displayed "Interest income +€9.88" exactly.

    IMPORTANT (corrected 2026-08-14, see module docstring for the full
    reconciliation proof): `openingBalance`/`closingBalance` are the
    account's UNINVESTED CASH/WALLET balance at the range's boundaries, NOT
    the total invested+cash portfolio value (a previous version of this
    docstring claimed the latter and was wrong) - so they're used here only
    for Cash drag's day-by-day idle-cash reconstruction, never as a
    substitute for "total".
    """
    log.info("Requesting account-summary API for %s to %s...", start_date, end_date)

    r = session.get(
        ACCOUNT_SUMMARY_API_URL,
        params={"period": "", "startDate": start_date, "endDate": end_date},
        headers=_HEADERS,
        timeout=20,
    )
    log.info("Account summary API response: status=%s", r.status_code)
    if not r.ok:
        raise RuntimeError(f"Account summary API request failed (status={r.status_code})")

    body = r.json() or {}
    operations = body.get("operations") or {}
    log.info("Raw account-summary API body: %r", body)
    try:
        interest_income = float(operations.get("INTEREST") or 0.0)
    except (TypeError, ValueError):
        log.warning("Could not parse 'INTEREST' value %r as a float - defaulting to 0.0.", operations.get("INTEREST"))
        interest_income = 0.0
    try:
        opening_balance = float(body.get("openingBalance") or 0.0)
        closing_balance = float(body.get("closingBalance") or 0.0)
    except (TypeError, ValueError):
        log.warning("Could not parse openingBalance/closingBalance %r/%r - defaulting to 0.0.", body.get("openingBalance"), body.get("closingBalance"))
        opening_balance = closing_balance = 0.0

    log.info(
        "Parsed statement totals: interest_income=%.2f EUR, opening_balance=%.2f EUR, closing_balance=%.2f EUR",
        interest_income, opening_balance, closing_balance,
    )
    return {"interest_income": interest_income, "opening_balance": opening_balance, "closing_balance": closing_balance}


def fetch_current_month_statement_totals(session: requests.Session) -> dict:
    """Thin wrapper around fetch_statement_summary() for the current
    calendar month (1st of the month through today) - see that function's
    docstring for the endpoint/parsing details."""
    now = get_report_now(REPORT_TIMEZONE)
    return fetch_statement_summary(session, now.replace(day=1).strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d"))


def fetch_transactions_page(session: requests.Session, start_date: str, end_date: str, offset: int, page_size: int) -> list:
    """Fetch one page of the Transactions section's own API (see module
    docstring for the verified request/response shape) - a raw JSON array,
    one entry per transaction, no pagination metadata."""
    r = session.get(
        TRANSACTIONS_API_URL,
        params={"period": "", "startDate": start_date, "endDate": end_date, "loanId": "", "offset": offset, "pageSize": page_size},
        headers=_HEADERS,
        timeout=20,
    )
    if not r.ok:
        raise RuntimeError(f"Transactions API returned status {r.status_code} (offset={offset})")
    return r.json() or []


def fetch_all_transactions(session: requests.Session, start_date: str, end_date: str) -> list:
    """Fetch EVERY transaction row within [start_date, end_date], paginated
    via TRANSACTIONS_PAGE_SIZE/MAX_TRANSACTIONS_PAGES - the endpoint has no
    total-count field, so pagination stops as soon as a page comes back
    shorter than the requested pageSize (or empty).

    BUGFIX 2026-08-21: refuse to call the API at all when start_date is
    AFTER end_date - PeerBerry's API returns a 422 for an inverted range
    (see module docstring for how a poisoned/future last_fetched_date could
    end up here). Defense in depth on top of the is_real_today guard in
    run()/get_cached_transactions(): treated as "no new entries" rather
    than raising, so a stale cache can't hard-crash a real run.
    """
    if start_date > end_date:
        log.warning(
            "start_date %s is after end_date %s - skipping the transactions fetch "
            "(returning no new entries) instead of calling the API with an inverted range.",
            start_date, end_date,
        )
        return []

    entries = []
    offset = 0
    for page_number in range(1, MAX_TRANSACTIONS_PAGES + 1):
        log.info("Requesting transactions API at offset %d...", offset)
        page_entries = fetch_transactions_page(session, start_date, end_date, offset, TRANSACTIONS_PAGE_SIZE)
        log.info("Page %d: %d entrie(s) found.", page_number, len(page_entries))
        entries.extend(page_entries)
        if len(page_entries) < TRANSACTIONS_PAGE_SIZE:
            break
        offset += TRANSACTIONS_PAGE_SIZE
    else:
        log.warning("Hit MAX_TRANSACTIONS_PAGES (%d) without exhausting the transaction history - it may be incomplete.", MAX_TRANSACTIONS_PAGES)
    return entries


def get_cached_transactions(session: requests.Session, end_date: str) -> list:
    """Return every transaction row since account inception, fetching from
    the transactions API only the range NOT already cached locally (in
    XIRR_CASHFLOWS_STATE_FILE) - same incremental-fetch idea as
    swaper_diversification.get_cached_account_cashflows(), just simpler
    (one flat entry list, no separate cashflows-vs-all_entries split
    needed - see module docstring: `amount` is already signed for cash
    balance impact, and `details` alone is enough to pick out the
    DEPOSIT/WITHDRAWAL rows for XIRR when needed).

    Re-fetches starting from the cached `last_fetched_date` itself (not the
    day after) so a same-day transaction added after the previous run
    already fetched it isn't missed - duplicates are then dropped by
    de-duplicating on the row's own `id`.

    Callers must only invoke this with the REAL wall-clock day as
    `end_date` (see run()'s `is_real_today` guard) - `end_date` is
    persisted as the new `last_fetched_date` below, and a simulated/backfill
    date here would poison the cache for future real runs (see module
    docstring BUGFIX 2026-08-21).
    """
    state = load_state(XIRR_CASHFLOWS_STATE_FILE, XIRR_CASHFLOWS_STATE_DEFAULT)
    cached_entries = state.get("all_entries") or []
    start_date = state.get("last_fetched_date") or XIRR_HISTORY_START_DATE

    log.info(
        "Found %d cached transaction(s) (last fetched up to %s) - fetching only new entries from %s to %s...",
        len(cached_entries), state.get("last_fetched_date"), start_date, end_date,
    )
    new_entries = fetch_all_transactions(session, start_date, end_date)

    seen = set()
    merged = []
    for entry in cached_entries + new_entries:
        key = entry.get("id")
        if key in seen:
            continue
        seen.add(key)
        merged.append(entry)

    save_state(XIRR_CASHFLOWS_STATE_FILE, {"all_entries": merged, "last_fetched_date": end_date})
    log.info("Transactions cache now holds %d entrie(s) (was %d before this run).", len(merged), len(cached_entries))
    return merged


def compute_average_idle_cash(entries: list, opening_balance: float, start_date: str, end_date: str) -> float:
    """Reconstruct the uninvested-cash/wallet balance for EVERY day in
    [start_date, end_date] from the raw transaction rows and return the
    day-weighted average - same day-by-day idea as
    swaper_diversification.compute_average_idle_cash(), simplified since
    every row's own `amount` is already signed for its real cash-balance
    impact (see module docstring - no per-`details` sign lookup needed
    here, unlike Swaper's `transactionType`-keyed table).

    Falls back to just `opening_balance` if `entries`/dates are missing or
    unparseable - never raises.
    """
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        end = datetime.strptime(end_date, "%Y-%m-%d").date()
    except ValueError:
        return opening_balance

    daily_deltas: dict = {}
    for entry in entries:
        raw_date = entry.get("postDate")
        raw_amount = entry.get("amount")
        if not raw_date or raw_amount is None:
            continue
        try:
            entry_date = datetime.strptime(raw_date[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if not (start <= entry_date <= end):
            continue
        try:
            amount = float(raw_amount)
        except (TypeError, ValueError):
            continue
        daily_deltas[entry_date] = daily_deltas.get(entry_date, 0.0) + amount

    running_balance = opening_balance
    total_balance = 0.0
    day_count = 0
    current = start
    while current <= end:
        running_balance += daily_deltas.get(current, 0.0)
        total_balance += running_balance
        day_count += 1
        current += timedelta(days=1)

    if day_count == 0:
        return opening_balance
    return total_balance / day_count


def run() -> None:
    if not PEERBERRY_EMAIL or not PEERBERRY_PASSWORD:
        log.error("PEERBERRY_EMAIL and PEERBERRY_PASSWORD environment variables are required.")
        sys.exit(1)

    # XIRR (like "total" elsewhere in this repo) is a LIVE-only snapshot
    # metric (needs TODAY's real total account value as its final
    # cashflow) - only ever computed/written for the real current month,
    # same convention as Afranga/Swaper/Lendermarket.
    current_month = is_current_month()
    today_date = get_report_now(REPORT_TIMEZONE).date()
    # BUGFIX 2026-08-21: is_current_month() only compares the MONTH, not the
    # exact day - a backfill run (scripts/run_diversification_for_month_
    # range.sh) that simulates "now" as some other day within the current
    # real calendar month (e.g. the month's last day, in the future
    # relative to the real wall-clock day) would still satisfy
    # current_month=True. The transactions/XIRR block below must only run
    # against the REAL wall-clock day - get_cached_transactions() persists
    # today_date as last_fetched_date, and a simulated/future date there
    # poisons the cache for the next real run (inverted date range ->
    # transactions API 422). See module docstring for the full incident.
    is_real_today = today_date == datetime.now(REPORT_TIMEZONE).date()

    log.info("Starting PeerBerry diversification run (pure HTTP, no browser).")

    session = requests.Session()
    try:
        login(session)
        payload = fetch_originator_distribution(session)
    except Exception:
        log.exception("Failed to log in or fetch the loan originator distribution.")
        sys.exit(1)

    try:
        statement_totals = fetch_current_month_statement_totals(session)
    except Exception:
        log.exception("Failed to fetch this month's Interest income - defaulting to 0.0.")
        statement_totals = {"interest_income": 0.0, "opening_balance": 0.0, "closing_balance": 0.0}
    interest_income = statement_totals["interest_income"]

    originators = normalize_originators(payload)
    log.info("Fetched distribution for %d loan originator(s).", len(originators))
    for o in originators:
        log.info("  %s (%s, %s): %.2f EUR (%.2f%%)", o["originator"], o["company"], o["iso2"], o["amount"], o["part"])

    log.info("This month's Interest income: %.2f EUR", interest_income)

    total_invested = sum(o["amount"] for o in originators)

    # Needed both for "non investi" (unchanged, existing feature) AND as
    # part of XIRR's final "as if withdrawn today" total account value
    # below - fetched once here, ahead of both uses.
    try:
        available_money = fetch_available_money(session)
    except Exception:
        log.exception("Failed to fetch PeerBerry's available-for-investment balance - 'non investi' and XIRR will not be updated.")
        available_money = None

    # Since-inception XIRR (money-weighted return) + this month's Cash drag
    # + the XIRR Bonus/Cash drag/Taxes-Frais/Intérêts pie-chart shares - see
    # module docstring for the real per-transaction ledger this is built
    # from (unlike Lendermarket, which has no such ledger and must
    # approximate monthly).
    all_entries = None
    if current_month and is_real_today:
        try:
            log.info("Fetching the since-inception transaction history (cached where possible)...")
            all_entries = get_cached_transactions(session, today_date.strftime("%Y-%m-%d"))
        except Exception:
            log.exception("Failed to fetch the transaction history - XIRR will not be updated.")
            all_entries = None

    def _entry_date(entry: dict):
        raw = entry.get("postDate")
        if not raw:
            return None
        try:
            return datetime.strptime(raw[:10], "%Y-%m-%d").date()
        except ValueError:
            return None

    def _entry_amount(entry: dict) -> float:
        try:
            return float(entry.get("amount") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    xirr_value = None
    signed_cashflows = None
    total_account_value = None
    bonus_xirr_contribution = None
    monthly_referral_bonus = 0.0
    lifetime_referral_bonus = 0.0
    since_inception_date = None
    if current_month and all_entries and available_money is not None:
        total_account_value = total_invested + available_money
        signed_cashflows = []
        deposit_dates = []
        for entry in all_entries:
            entry_date = _entry_date(entry)
            details = entry.get("details")
            if entry_date is None or details not in ("DEPOSIT", "WITHDRAWAL"):
                continue
            # `amount` is already signed for its cash-balance impact
            # (DEPOSIT positive, WITHDRAWAL negative) - negate it for XIRR's
            # own convention (money going INTO the platform is a negative
            # cashflow, money coming back OUT is positive) - see module
            # docstring.
            signed_cashflows.append((entry_date, -_entry_amount(entry)))
            if details == "DEPOSIT":
                deposit_dates.append(entry_date)

        since_inception_date = min(deposit_dates) if deposit_dates else None
        monthly_referral_bonus = sum(
            _entry_amount(e) for e in all_entries
            if e.get("details") == "REFERRAL_FEE" and (_entry_date(e) or date(1970, 1, 1)) >= today_date.replace(day=1)
        )
        lifetime_referral_bonus = sum(_entry_amount(e) for e in all_entries if e.get("details") == "REFERRAL_FEE")

        signed_cashflows.append((today_date, total_account_value))

        xirr_value = compute_xirr(signed_cashflows)
        if xirr_value is None:
            log.warning("Could not compute XIRR from %d cashflow(s) - XIRR row will not be updated.", len(signed_cashflows) - 1)
        else:
            log.info(
                "Computed since-inception XIRR: %.2f%% (%d deposit/withdrawal cashflow(s), current total value %.2f EUR).",
                xirr_value * 100, len(signed_cashflows) - 1, total_account_value,
            )

            if lifetime_referral_bonus:
                cashflows_without_bonus = signed_cashflows[:-1] + [(today_date, total_account_value - lifetime_referral_bonus)]
                xirr_without_bonus = compute_xirr(cashflows_without_bonus)
                if xirr_without_bonus is not None:
                    bonus_xirr_contribution = xirr_value - xirr_without_bonus
                    log.info("Bonus's own share of XIRR: %.2f points.", bonus_xirr_contribution * 100)
            else:
                bonus_xirr_contribution = 0.0

    cash_drag_value = None
    cash_drag_xirr_contribution = None
    taxes_xirr_contribution = None
    # XIRR Intérêts (added 2026-08-19, mirrors afranga_diversification.py's
    # own XIRR Intérêts block - see module docstring for the full
    # rationale): counterfactual XIRR share attributable to real net
    # interest received since inception.
    interest_xirr_contribution = None
    if current_month and total_invested > 0 and all_entries is not None:
        month_start_str = today_date.replace(day=1).strftime("%Y-%m-%d")
        today_str = today_date.strftime("%Y-%m-%d")
        avg_idle_cash_this_month = compute_average_idle_cash(all_entries, statement_totals["opening_balance"], month_start_str, today_str)
        cash_weight = avg_idle_cash_this_month / (avg_idle_cash_this_month + total_invested)
        monthly_yield_rate = interest_income / total_invested
        cash_drag_value = cash_weight * monthly_yield_rate
        log.info(
            "Computed Cash drag: %.2f%% (avg idle cash %.2f EUR, cash weight %.2f%%, monthly yield %.2f%%).",
            cash_drag_value * 100, avg_idle_cash_this_month, cash_weight * 100, monthly_yield_rate * 100,
        )

        if xirr_value is not None and signed_cashflows is not None and since_inception_date is not None:
            try:
                lifetime_statement = fetch_statement_summary(session, since_inception_date.strftime("%Y-%m-%d"), today_str)
            except Exception:
                log.exception("Failed to fetch since-inception statement totals - Cash drag/Taxes-Frais/Intérêts XIRR shares will not be updated.")
                lifetime_statement = None

            if lifetime_statement is not None:
                avg_idle_cash_lifetime = compute_average_idle_cash(
                    all_entries, lifetime_statement["opening_balance"], since_inception_date.strftime("%Y-%m-%d"), today_str,
                )
                cash_weight_lifetime = avg_idle_cash_lifetime / (avg_idle_cash_lifetime + total_invested)
                lifetime_yield_rate = lifetime_statement["interest_income"] / total_invested
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

                lifetime_sale_fees = sum(_entry_amount(e) for e in all_entries if e.get("details") == "INVESTMENT_SALE_FEE")
                if lifetime_sale_fees:
                    cashflows_with_fees_cancelled = signed_cashflows[:-1] + [(today_date, total_account_value - lifetime_sale_fees)]
                    xirr_with_fees_cancelled = compute_xirr(cashflows_with_fees_cancelled)
                    if xirr_with_fees_cancelled is not None:
                        taxes_xirr_contribution = xirr_value - xirr_with_fees_cancelled
                        log.info("XIRR share - taxes/frais: %.4f points (lifetime fees %.2f EUR).", taxes_xirr_contribution * 100, lifetime_sale_fees)
                else:
                    taxes_xirr_contribution = 0.0

                # XIRR Intérêts: same counterfactual pattern as Bonus/Cash
                # drag/Taxes above, but for the real net interest received
                # since inception. Unlike Afranga (gross minus withholding
                # tax), PeerBerry's account-summary API has no such split
                # (see module docstring) - lifetime_statement["interest_income"]
                # already IS the lifetime net interest figure, used directly.
                lifetime_net_interest = lifetime_statement["interest_income"]
                if lifetime_net_interest:
                    cashflows_without_interest = signed_cashflows[:-1] + [(today_date, total_account_value - lifetime_net_interest)]
                    xirr_without_interest = compute_xirr(cashflows_without_interest)
                    if xirr_without_interest is not None:
                        interest_xirr_contribution = xirr_value - xirr_without_interest
                        log.info(
                            "XIRR share - intérêts: %.4f points (lifetime net interest %.2f EUR).",
                            interest_xirr_contribution * 100, lifetime_net_interest,
                        )
                else:
                    interest_xirr_contribution = 0.0

    # PeerBerry's account-summary API has no gross/net/withholding-tax
    # breakdown (unlike Afranga/Bienpreter) - interest_income is mapped to
    # both gross_interest_received/net_interest_received since it's the
    # only real figure on hand, withholding_tax defaults to 0.0. Same
    # standardized dict shape as every other *_diversification.py, plus the
    # platform-specific interest_income field kept alongside it.
    # bonus_cashback_contest is now genuinely fetched (this month's
    # REFERRAL_FEE rows from the transactions ledger) instead of hardcoded
    # to 0.0 - see module docstring for why it's currently 0.00 on this
    # account (no REFERRAL_FEE row has ever occurred yet).
    # "total" ("en cours" in the Crowdlending table) is invested + uninvested
    # (available_money), per explicit user request 2026-08-14 - unlike most
    # other platforms here, whose "total" is invested-only (uninvested cash
    # is tracked separately via fill_geographic_repartition_uninvested_amount).
    # Falls back to invested-only if available_money couldn't be fetched.
    amounts = {
        "total": total_invested + available_money if available_money is not None else total_invested,
        "gross_interest_received": interest_income,
        "net_interest_received": interest_income,
        "withholding_tax": 0.0,
        "bonus_cashback_contest": monthly_referral_bonus,
        "interest_income": interest_income,
    }

    # "total" comes from the live originator-distribution total plus the
    # live available-money balance, and the account-summary API's
    # opening/closingBalance is the uninvested CASH balance, not a
    # historical total invested figure (see module docstring for the
    # 2026-08-14 correction) - always skip_total for a backfilled month,
    # same convention as Lendermarket.
    fill_current_month_amounts(
        platform="PeerBerry",
        amounts=amounts,
        skip_total=not current_month,
    )

    # PeerBerry's REFERRAL_FEE is treated as a "prime" (referral reward),
    # same convention as Swaper's referral bonus - written to its own
    # dedicated sub-row, never to the "Bonus" row itself (a SUM formula
    # over prime/cashback/concours). "XIRR"/"Cash drag" and the XIRR
    # Bonus/Cash drag/Taxes-Frais/Intérêts pie-chart shares (rows already
    # added by the user, mirroring Afranga/Swaper/Lendermarket's own
    # blocks) are appended past the default max_rows=6 bound - only
    # included when actually computed. "XIRR Intérêts" (added 2026-08-19)
    # sits right after "XIRR Taxes/Frais" - this pushes the block one row
    # taller than before, so `max_rows` is bumped 14 -> 15 to keep the
    # search bounded before the next platform block. IMPORTANT: a "XIRR
    # Intérêts" row must exist in the PeerBerry block on the sheet itself
    # (right after "XIRR Taxes/Frais") for this new value to actually land
    # somewhere - this script fills an existing row by label, it doesn't
    # insert new labelled rows into this block.
    bonus_breakdown = {"prime": monthly_referral_bonus}
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
        platform="PeerBerry",
        breakdown=bonus_breakdown,
        max_rows=15,
    )

    loan_originators = [
        {"name": o["originator"], "amount": o["amount"]}
        for o in originators
    ]

    if current_month:
        fill_geographic_repartition_amounts(loan_originators, platform="Peerberry")

        if available_money is not None:
            fill_geographic_repartition_uninvested_amount("Peerberry", available_money)


if __name__ == "__main__":
    run()