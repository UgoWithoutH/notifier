"""Nectaro portfolio diversification (by lending company) fetcher.

Nectaro (nectaro.eu) is a Dyninno-group crowdlending platform, built as a
Nuxt.js (Vue) single-page app on top of a pure JSON REST API
(api.nectaro.eu, Symfony backend). Unlike most other platforms in this
repo, login/2FA/data are ALL plain HTTP JSON calls - no Playwright/browser
needed at all (same category as Go & Grow/Bricks/Iuvo).

Auth flow (reverse-engineered 2026-09-08 via a real login + Playwright
network capture, then independently verified with a plain
`requests.Session()` - see repo memory for the full exploration):
  1. POST https://api.nectaro.eu/api/login
     JSON body `{"username": <email>, "password": <password>}` (NOT
     "email" - a bare `{"email":...}` body gets rejected with a generic
     "This value should be of type string." validation error, since the
     API only recognizes the "username" field name).
     -> `{"token": <jwt>|null, "refresh_token": <str>|null,
          "temporary_token": <jwt>|null, "twoFactorAuthRequired": bool}`.
     If `token` is already set, 2FA is not required this time (not
     observed on the real test account, which always has 2FA enabled -
     handled defensively below anyway).
  2. If `twoFactorAuthRequired`, POST
     https://api.nectaro.eu/api/two-factor/verify
     JSON body `{"code": <6-digit TOTP>, "temporaryToken": <temporary_token
     from step 1>}` -> `{"token": <jwt>, "refresh_token": <str>}`.
  3. Every subsequent authenticated call sends `Authorization: Bearer
     <token>` (a JWT, ~45 min lifetime - a single run finishes in seconds,
     no refresh-token handling needed).

Data endpoints (all under api.nectaro.eu, `Authorization: Bearer` auth):
  - `GET /api/customer/me` -> `cashAccountBalance` (uninvested wallet,
    "non investi"), `portfolioAmount` (current invested/outstanding total,
    matches `investedFunds` below).
  - `GET /api/investment/dashboard` -> `investedFunds`/`currentBalance`
    (LIVE snapshot totals, same two figures as customer/me).
  - `GET /api/dashboard/investment?isHistorical=false&grouping=loan-originator`
    -> `[{"id": <product id>, "totalAmount", "totalResidueAmount"}, ...]` -
    `totalResidueAmount` is the CURRENT outstanding (residue) amount per
    product id, used for the "Répartition géographique" breakdown.
  - `GET /api/products` -> `{<product id>: {"entity": {"loanOriginator":
    {"legalName": ...}, ...}}}` - maps each product id above to its real
    lending company name (`legalName`, e.g. `SIA "Abele Finance"`) -
    confirmed to match the `lendingCompany` field on individual loans in
    the portfolio listing below exactly.
  - `POST /api/investment/portfolio` body `{"meta": [{"limit": N, "page":
    P}], "filters": []}` -> paginated list of individual loans (ID, ISIN,
    amount, interestRate, lendingCompany, borrowerCountry, status, ...) -
    NOT used for the geographic breakdown itself (the grouping endpoint
    above is simpler/already residue-based), kept available for reference
    only.
  - `POST /api/statement/list` body `{"meta": [{"limit": N, "page": P}],
    "filters": [{"name": "type", "values": [], "type": "multiselect"},
    {"name": "dateFrom", "value": "YYYY-MM-DD", "type": "datepicker"},
    {"name": "dateUntil", "value": "YYYY-MM-DD", "type": "datepicker"}]}
    -> `{"statement": {"openingBalance", "closingBalance", "incomingSum",
    "outgoingSum"}, "transactions": [{"date": "DD Mon YYYY HH:MM", "type":
    ..., "direction": "IN"|"OUT", "details", "amount", "status", "id"}]}`.
    An EMPTY `values` array on the "type" filter means "no type filter -
    every transaction type included" (confirmed live: identical result
    with vs. without a "type" filter present at all) - per explicit user
    request ("regarde dans le filtre tout les transactions type pour tous
    les prendre en compte"), the "type" filter is deliberately OMITTED
    entirely here so every real type is always included, forward-
    compatible with any new type Nectaro might add later without needing
    a code change. Real types confirmed live on the test account:
    DEPOSIT, WITHDRAWAL (not observed yet, forward-looking), INVESTMENT,
    PRINCIPAL, INTEREST, TAXATION, REWARD (bonus/cashback, not observed
    yet - genuinely 0 so far, not a placeholder).
    `direction` is a clean, generic "IN credits the cash wallet, OUT
    debits it" scheme (confirmed against every observed type) - much
    simpler than Afranga's CSS-class-based direction-in/direction-out
    convention, used directly for the day-by-day idle-cash/invested-
    balance reconstruction below instead of a type->sign lookup table.

Since-inception XIRR/Cash drag block (mirrors afranga_diversification.py's
ORIGINAL design, i.e. before its 2026-09-07 backward-reconstruction-for-
backfill enhancement - see that module's docstring/repo memory for the
full methodology this is based on): gated behind `is_current_month()`
(LIVE-only, needs today's real total account value) - NOT yet extended to
support a REPORT_DATE-backfilled past month, even though Nectaro's
statement endpoint DOES expose a real per-transaction dated ledger (same
data shape as Afranga's Details rows) that would make this feasible if
requested later.
  - XIRR cashflows: every DEPOSIT (negative, money invested) and WITHDRAWAL
    (positive, money returned) transaction, since account inception,
    fetched incrementally via `nectaro_xirr_cashflows_state.json` (deduped
    by Nectaro's own transaction "id" field - confirmed to be a real,
    stable, per-row unique integer, unlike Afranga's reused transaction_id
    that needed a composite dedup key).
  - Terminal cashflow: today's real `investedFunds + cashAccountBalance`.
  - Cash drag / XIRR Cash drag / XIRR Bonus / XIRR Taxes / XIRR Frais /
    XIRR Intérêts: XIRR Bonus/Cash drag/Taxes/Intérêts use a Shapley-value
    decomposition (see shared/xirr_shapley.py, added 2026-09-09) instead
    of isolated counterfactuals, so the shares sum back to the real XIRR
    gap exactly. Nectaro's "TAXATION" transaction type is a genuine
    withholding tax, kept under "XIRR Taxes"; no distinct fee concept
    exists on this platform, so "XIRR Frais" is hardcoded 0.0. Cash drag's
    own avg-idle-cash input computed via the existing generic
    `shared.weighted_average.compute_time_weighted_average()` (a plain
    day-weighted average of a running balance) instead of a bespoke
    per-platform reconstruction function - Nectaro's clean IN/OUT
    direction field makes this a direct drop-in.
  - "solde moyen pondéré investi"/"non investi" rows use the same utility,
    fed by INVESTMENT (direction OUT, +invested)/PRINCIPAL (direction IN,
    -invested) for the invested-balance series and every transaction's own
    IN/OUT delta for the cash-balance series.

Required environment variables:
    NECTARO_EMAIL, NECTARO_PASSWORD -> login credentials.
    NECTARO_TOTP_SECRET             -> base32 TOTP secret (2FA is always
                                       enabled on the observed test
                                       account).
    GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS -> used to write this month's
                                       totals to the Google Sheet, same
                                       convention as every other
                                       *_diversification.py.
"""

import logging
import os
import sys
import time
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import pyotp
import requests
from dotenv import load_dotenv

load_dotenv()

from shared.google_sheet import (
    fill_current_month_amounts,
    fill_current_month_bonus_breakdown,
    fill_geographic_repartition_amounts,
    fill_geographic_repartition_uninvested_amount,
)
from shared.report_date import get_report_date, is_current_month
from shared.state import load_state, save_state
from shared.weighted_average import INVESTED_BALANCE_LABEL, NON_INVESTED_BALANCE_LABEL, compute_time_weighted_average
from shared.xirr import compute_xirr
from shared.xirr_shapley import compute_shapley_xirr_shares

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("nectaro_diversification")

API_BASE = "https://api.nectaro.eu/api"
LOGIN_URL = f"{API_BASE}/login"
TWO_FA_URL = f"{API_BASE}/two-factor/verify"
CUSTOMER_ME_URL = f"{API_BASE}/customer/me"
# NOTE the path order difference between these two - easy to mix up:
# /investment/dashboard = the account-wide overview totals (currentBalance/
# investedFunds/...), /dashboard/investment = per-grouping breakdowns
# (needs isHistorical/grouping query params, see fetch_portfolio_by_lending_company()).
INVESTMENT_OVERVIEW_URL = f"{API_BASE}/investment/dashboard"
DASHBOARD_INVESTMENT_URL = f"{API_BASE}/dashboard/investment"
PRODUCTS_URL = f"{API_BASE}/products"
STATEMENT_LIST_URL = f"{API_BASE}/statement/list"

NECTARO_EMAIL = os.environ.get("NECTARO_EMAIL")
NECTARO_PASSWORD = os.environ.get("NECTARO_PASSWORD")
NECTARO_TOTP_SECRET = os.environ.get("NECTARO_TOTP_SECRET")

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Content-Type": "application/json",
}

# Cash-crediting ("IN") transaction types, per the observed real account -
# any OTHER value is treated as a debit ("OUT") by the API's own
# `direction` field (read directly, never inferred from `type`) - this
# list is informational/for reference only, not actually branched on.
STATEMENT_PAGE_SIZE = 500
MAX_STATEMENT_PAGES = 50

# XIRR is a since-inception money-weighted return - this start date is
# early enough to cover any real account's full history (Nectaro itself
# only launched in 2023 per /api/products' earliest `createdAt`).
XIRR_HISTORY_START_DATE = date(2000, 1, 1)

XIRR_CASHFLOWS_STATE_FILE = Path(__file__).parent / "nectaro_xirr_cashflows_state.json"
XIRR_CASHFLOWS_STATE_DEFAULT = {"transactions": [], "last_fetched_date": None}


def login(session: requests.Session) -> str:
    """Log in to Nectaro using NECTARO_EMAIL/PASSWORD + NECTARO_TOTP_SECRET
    (2FA is always enabled on the observed test account). Returns the real
    access token (a JWT) to use as `Authorization: Bearer <token>` on every
    subsequent call. See the module docstring for the full verified flow.
    """
    if not NECTARO_EMAIL or not NECTARO_PASSWORD:
        raise RuntimeError("NECTARO_EMAIL and NECTARO_PASSWORD environment variables are required.")

    log.info("Submitting credentials...")
    r = session.post(
        LOGIN_URL,
        json={"username": NECTARO_EMAIL, "password": NECTARO_PASSWORD},
        headers=_HEADERS,
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()

    if data.get("token"):
        log.info("Logged in successfully (no 2FA prompt).")
        return data["token"]

    temp_token = data.get("temporary_token")
    if not temp_token:
        raise RuntimeError(f"Nectaro login returned neither a token nor a temporary_token: {data}")

    if not data.get("twoFactorAuthRequired"):
        raise RuntimeError(f"Nectaro login did not return a real token and did not ask for 2FA either: {data}")

    if not NECTARO_TOTP_SECRET:
        raise RuntimeError(
            "Nectaro is asking for a 2FA code but NECTARO_TOTP_SECRET is not set. "
            "Set it to the base32 secret used to configure the authenticator app."
        )

    # Diagnostic only (no secret/code values logged): compare Nectaro's
    # server-reported clock (Date response header) to our local clock -
    # same precaution as every other TOTP-gated platform in this repo,
    # after multiple real GitHub Actions TOTP-rejection incidents that
    # turned out unrelated to clock skew but were only ruled out this way.
    server_date_header = r.headers.get("Date")
    if server_date_header:
        try:
            server_time = parsedate_to_datetime(server_date_header)
            skew = (datetime.now(timezone.utc) - server_time).total_seconds()
            log.info("Clock check: local vs. Nectaro server Date header skew = %.1fs", skew)
        except Exception:
            pass

    log.info("2FA prompt detected, generating and submitting TOTP code...")
    totp = pyotp.TOTP(NECTARO_TOTP_SECRET)
    now = time.time()
    # Same-window retries are a no-op (pyotp.now() called twice within a
    # second returns the identical code) - try 3 DISTINCT candidate codes
    # (current/previous/next 30s window) instead, same resilience pattern
    # as every other TOTP-gated platform in this repo.
    candidates = [totp.at(now), totp.at(now - 30), totp.at(now + 30)]
    for attempt, code in enumerate(candidates, start=1):
        r2 = session.post(
            TWO_FA_URL,
            json={"code": code, "temporaryToken": temp_token},
            headers=_HEADERS,
            timeout=20,
        )
        if r2.ok:
            data2 = r2.json()
            if data2.get("token"):
                log.info("Logged in successfully after 2FA.")
                return data2["token"]
        log.info("TOTP code rejected (attempt %d/%d)...", attempt, len(candidates))

    raise RuntimeError("Nectaro rejected the TOTP code (all 3 candidates).")


def fetch_overview(session: requests.Session, headers: dict) -> dict:
    """Fetch the LIVE account overview: `cash_balance` (uninvested wallet,
    "non investi") and `invested_funds` (current outstanding/invested
    total, residue-based - matches the sum of every lending company's own
    `totalResidueAmount` from fetch_portfolio_by_lending_company())."""
    r = session.get(CUSTOMER_ME_URL, headers=headers, timeout=20)
    r.raise_for_status()
    customer = r.json()

    r = session.get(INVESTMENT_OVERVIEW_URL, headers=headers, timeout=20)
    r.raise_for_status()
    dashboard = r.json()

    cash_balance = customer.get("cashAccountBalance", 0.0)
    invested_funds = dashboard.get("investedFunds", customer.get("portfolioAmount", 0.0))
    log.info("Overview: cash_balance=%.2f EUR, invested_funds=%.2f EUR.", cash_balance, invested_funds)
    return {"cash_balance": cash_balance, "invested_funds": invested_funds}


def fetch_portfolio_by_lending_company(session: requests.Session, headers: dict) -> list:
    """Fetch the current outstanding (residue) amount per lending company,
    via `grouping=loan-originator` (keyed by internal product id) + a
    products directory lookup (id -> real lending company name). Returns
    a list of {"name", "amount"} dicts, sorted by amount descending."""
    r = session.get(
        DASHBOARD_INVESTMENT_URL,
        params={"isHistorical": "false", "grouping": "loan-originator"},
        headers=headers,
        timeout=20,
    )
    r.raise_for_status()
    groups = r.json().get("data", [])

    r = session.get(PRODUCTS_URL, headers=headers, timeout=20)
    r.raise_for_status()
    products = r.json().get("data", {})

    totals = {}
    for group in groups:
        product_id = str(group.get("id"))
        product = products.get(product_id)
        if not product:
            log.warning("Product id %s (from the loan-originator grouping) not found in /api/products - skipped.", product_id)
            continue
        name = product["entity"]["loanOriginator"]["legalName"]
        amount = group.get("totalResidueAmount", 0.0)
        totals[name] = totals.get(name, 0.0) + amount

    companies = [{"name": name, "amount": amount} for name, amount in totals.items()]
    companies.sort(key=lambda c: c["amount"], reverse=True)
    return companies


def _fetch_statement_page(session: requests.Session, headers: dict, start_date: date, end_date: date, page: int) -> list:
    """Fetch one page of the statement transaction list for [start_date,
    end_date] (inclusive), deliberately WITHOUT a "type" filter (an empty/
    absent filter means every transaction type is included - see module
    docstring)."""
    body = {
        "meta": [{"limit": STATEMENT_PAGE_SIZE, "page": page}],
        "filters": [
            {"name": "dateFrom", "value": start_date.strftime("%Y-%m-%d"), "type": "datepicker"},
            {"name": "dateUntil", "value": end_date.strftime("%Y-%m-%d"), "type": "datepicker"},
        ],
    }
    r = session.post(STATEMENT_LIST_URL, json=body, headers=headers, timeout=20)
    r.raise_for_status()
    payload = r.json().get("data", {}).get("payload", {})
    return payload.get("transactions", [])


def fetch_all_statement_transactions(session: requests.Session, headers: dict, start_date: date, end_date: date) -> list:
    """Fetch every statement transaction in [start_date, end_date]
    (inclusive), paginating until a page returns fewer than
    STATEMENT_PAGE_SIZE rows."""
    all_transactions = []
    for page in range(1, MAX_STATEMENT_PAGES + 1):
        rows = _fetch_statement_page(session, headers, start_date, end_date, page)
        log.info("Statement page %d: %d row(s) fetched.", page, len(rows))
        all_transactions.extend(rows)
        if len(rows) < STATEMENT_PAGE_SIZE:
            break
    else:
        log.warning("Hit MAX_STATEMENT_PAGES (%d) without an incomplete page - history may be truncated.", MAX_STATEMENT_PAGES)
    return all_transactions


def get_cached_statement_transactions(session: requests.Session, headers: dict, end_date: date) -> list:
    """Incremental fetch of every statement transaction ever seen, cached
    in XIRR_CASHFLOWS_STATE_FILE and deduped by Nectaro's own transaction
    `id` field (a real, stable, per-row unique integer - confirmed live,
    unlike some other platforms' reused ids)."""
    state = load_state(XIRR_CASHFLOWS_STATE_FILE, XIRR_CASHFLOWS_STATE_DEFAULT)
    cached = {t["id"]: t for t in state["transactions"]}

    last_fetched_date_str = state.get("last_fetched_date")
    fetch_start = (
        datetime.strptime(last_fetched_date_str, "%Y-%m-%d").date()
        if last_fetched_date_str
        else XIRR_HISTORY_START_DATE
    )

    if fetch_start > end_date:
        # Cache already covers past end_date (e.g. a live run advanced it,
        # then a backfill run asked for an earlier REPORT_DATE) - skip the
        # fetch instead of sending an inverted start>end range to the API.
        log.info(
            "Cache already covers up to %s (requested end date %s) - skipping fetch, using cached data only.",
            fetch_start, end_date,
        )
        return list(cached.values())

    log.info("Fetching statement transactions from %s to %s (incremental cache)...", fetch_start, end_date)
    new_rows = fetch_all_statement_transactions(session, headers, fetch_start, end_date)
    for row in new_rows:
        cached[row["id"]] = row

    state["transactions"] = list(cached.values())
    state["last_fetched_date"] = end_date.strftime("%Y-%m-%d")
    save_state(XIRR_CASHFLOWS_STATE_FILE, state)

    log.info("%d cached statement transaction(s) in total.", len(cached))
    return list(cached.values())


def _parse_transaction_date(raw: str) -> date:
    """Parse Nectaro's own "DD Mon YYYY HH:MM" date format (e.g. "02 Sep
    2026 04:41") into a plain date."""
    return datetime.strptime(raw, "%d %b %Y %H:%M").date()


def _cash_delta(transaction: dict) -> float:
    """Signed delta to the uninvested cash wallet for one statement
    transaction - direction "IN" credits it, "OUT" debits it (a clean,
    generic scheme confirmed against every observed transaction type)."""
    direction = transaction.get("direction")
    amount = transaction.get("amount", 0.0)
    if direction == "IN":
        return amount
    if direction == "OUT":
        return -amount
    log.warning("Unrecognized statement transaction direction %r (type=%r) - treated as 0.", direction, transaction.get("type"))
    return 0.0


def _invested_delta(transaction: dict) -> float:
    """Signed delta to the INVESTED (outstanding) balance for one statement
    transaction - INVESTMENT (buying a loan, direction OUT on the cash
    side) increases it, PRINCIPAL (a loan repayment, direction IN on the
    cash side) decreases it. Every other type is a neutral cash-only
    reallocation (interest/taxes/deposits/withdrawals/rewards)."""
    ttype = transaction.get("type")
    amount = transaction.get("amount", 0.0)
    if ttype == "INVESTMENT":
        return amount
    if ttype == "PRINCIPAL":
        return -amount
    return 0.0


def run() -> None:
    if not NECTARO_EMAIL or not NECTARO_PASSWORD:
        log.error("NECTARO_EMAIL and NECTARO_PASSWORD environment variables are required.")
        sys.exit(1)

    # XIRR/Cash drag (like "total" elsewhere in this repo) is a LIVE-only
    # snapshot metric (needs TODAY's real total account value as its final
    # cashflow) - not yet extended to support a REPORT_DATE-backfilled past
    # month (see module docstring), so gated behind is_current_month().
    current_month = is_current_month()

    log.info("Starting Nectaro diversification run (pure HTTP, no browser).")

    session = requests.Session()
    try:
        token = login(session)
        headers = {**_HEADERS, "Authorization": f"Bearer {token}"}
        overview = fetch_overview(session, headers)
        companies = fetch_portfolio_by_lending_company(session, headers)
    except Exception:
        log.exception("Failed to log in or fetch Nectaro's portfolio/overview.")
        sys.exit(1)

    log.info("Fetched %d lending compan(y/ies).", len(companies))
    for c in companies:
        log.info("  %s: %.2f EUR", c["name"], c["amount"])

    today_date = get_report_date()

    amounts = {
        "total": overview["invested_funds"] + overview["cash_balance"],
        "gross_interest_received": 0.0,
        "net_interest_received": 0.0,
        "withholding_tax": 0.0,
        "bonus_cashback_contest": 0.0,
    }

    try:
        month_start_date = today_date.replace(day=1)
        this_month_transactions = fetch_all_statement_transactions(session, headers, month_start_date, today_date)
        gross_interest = sum(t["amount"] for t in this_month_transactions if t.get("type") == "INTEREST")
        withholding_tax = sum(t["amount"] for t in this_month_transactions if t.get("type") == "TAXATION")
        bonus = sum(t["amount"] for t in this_month_transactions if t.get("type") == "REWARD")
        amounts["gross_interest_received"] = gross_interest
        amounts["net_interest_received"] = gross_interest - withholding_tax
        amounts["withholding_tax"] = withholding_tax
        amounts["bonus_cashback_contest"] = bonus
        log.info(
            "This month's statement totals: gross_interest=%.2f EUR, withholding_tax=%.2f EUR, bonus=%.2f EUR.",
            gross_interest, withholding_tax, bonus,
        )
    except Exception:
        log.exception("Failed to fetch this month's statement totals - defaulting interest/tax/bonus to 0.0.")

    # Since-inception XIRR/Cash drag block - see module docstring for the
    # full methodology (mirrors afranga_diversification.py's original,
    # non-backfill-capable design).
    xirr_value = None
    cash_drag_value = None
    bonus_xirr_contribution = None
    cash_drag_xirr_contribution = None
    taxes_xirr_contribution = None
    frais_xirr_contribution = 0.0  # Nectaro has no distinct fee data source, see module docstring - genuinely 0, not a placeholder.
    interest_xirr_contribution = None
    avg_invested_balance = None
    avg_non_invested_balance = None

    all_transactions = None
    try:
        all_transactions = get_cached_statement_transactions(session, headers, today_date)
    except Exception:
        log.exception("Failed to fetch the since-inception statement history - XIRR/Cash drag will not be updated.")

    if all_transactions is not None:
        cash_events = []
        invested_events = []
        for t in all_transactions:
            try:
                t_date = _parse_transaction_date(t["date"])
            except (KeyError, ValueError):
                log.warning("Skipping a statement transaction with an unparseable date: %r", t)
                continue
            cash_events.append((t_date, _cash_delta(t)))
            invested_events.append((t_date, _invested_delta(t)))

        avg_invested_balance = compute_time_weighted_average(invested_events, month_start_date, today_date)
        avg_non_invested_balance = compute_time_weighted_average(cash_events, month_start_date, today_date)
        log.info(
            "Solde moyen pondéré - investi: %.2f EUR, non investi: %.2f EUR (%s to %s).",
            avg_invested_balance, avg_non_invested_balance, month_start_date, today_date,
        )

        if current_month:
            deposit_dates = [
                _parse_transaction_date(t["date"]) for t in all_transactions if t.get("type") == "DEPOSIT"
            ]
            total_invested = overview["invested_funds"]
            total_account_value = total_invested + overview["cash_balance"]

            signed_cashflows = []
            for t in all_transactions:
                ttype = t.get("type")
                if ttype not in ("DEPOSIT", "WITHDRAWAL"):
                    continue
                try:
                    t_date = _parse_transaction_date(t["date"])
                except (KeyError, ValueError):
                    continue
                amount = t.get("amount", 0.0)
                signed_cashflows.append((t_date, -amount if ttype == "DEPOSIT" else amount))
            signed_cashflows.append((today_date, total_account_value))

            xirr_value = compute_xirr(signed_cashflows)
            if xirr_value is None:
                log.warning("Could not compute XIRR from %d cashflow(s) - XIRR row will not be updated.", len(signed_cashflows))
            else:
                log.info(
                    "Computed since-inception XIRR: %.2f%% (current total value %.2f EUR).",
                    xirr_value * 100, total_account_value,
                )

                lifetime_bonus_total = sum(t["amount"] for t in all_transactions if t.get("type") == "REWARD")
                lifetime_withholding_tax = sum(t["amount"] for t in all_transactions if t.get("type") == "TAXATION")
                lifetime_gross_interest = sum(t["amount"] for t in all_transactions if t.get("type") == "INTEREST")
                lifetime_net_interest = lifetime_gross_interest - lifetime_withholding_tax

                # Shapley decomposition (added 2026-09-09, see
                # shared/xirr_shapley.py's module docstring) - Nectaro has
                # a genuine "TAXATION" transaction type, a real
                # withholding tax kept under "XIRR Taxes"; no distinct fee
                # concept exists, so "XIRR Frais" is hardcoded 0.0
                # (fixed further below, not part of the game). This
                # 3-factor call (no Cash drag) is a fallback used when
                # `total_invested`/`missed_earnings` aren't computable
                # (e.g. no invested funds) - overridden by the full
                # 4-factor joint call below whenever Cash drag CAN be
                # computed.
                factor_deltas = {
                    "XIRR Bonus": -lifetime_bonus_total,
                    "XIRR Taxes": lifetime_withholding_tax,
                    "XIRR Intérêts": -lifetime_net_interest,
                }
                shapley_shares = compute_shapley_xirr_shares(
                    signed_cashflows[:-1], today_date, total_account_value, factor_deltas,
                    log=log, log_context="Nectaro (no cash drag)",
                )
                bonus_xirr_contribution = shapley_shares.get("XIRR Bonus")
                taxes_xirr_contribution = shapley_shares.get("XIRR Taxes")
                interest_xirr_contribution = shapley_shares.get("XIRR Intérêts")

                if total_invested > 0:
                    avg_idle_cash_month = compute_time_weighted_average(cash_events, month_start_date, today_date)
                    cash_weight = avg_idle_cash_month / (avg_idle_cash_month + total_invested)
                    monthly_yield_rate = amounts["gross_interest_received"] / total_invested
                    cash_drag_value = cash_weight * monthly_yield_rate
                    log.info(
                        "Computed Cash drag: %.4f%% (avg idle cash %.2f EUR).",
                        cash_drag_value * 100, avg_idle_cash_month,
                    )

                    if deposit_dates:
                        since_inception_date = min(deposit_dates)
                        avg_idle_cash_lifetime = compute_time_weighted_average(cash_events, since_inception_date, today_date)
                        cash_weight_lifetime = avg_idle_cash_lifetime / (avg_idle_cash_lifetime + total_invested)
                        lifetime_yield_rate = lifetime_gross_interest / total_invested
                        cash_drag_lifetime_total = cash_weight_lifetime * lifetime_yield_rate
                        missed_earnings = cash_drag_lifetime_total * (avg_idle_cash_lifetime + total_invested)

                        factor_deltas = {
                            "XIRR Bonus": -lifetime_bonus_total,
                            "XIRR Cash drag": missed_earnings,
                            "XIRR Taxes": lifetime_withholding_tax,
                            "XIRR Intérêts": -lifetime_net_interest,
                        }
                        shapley_shares = compute_shapley_xirr_shares(
                            signed_cashflows[:-1], today_date, total_account_value, factor_deltas,
                            log=log, log_context="Nectaro",
                        )
                        bonus_xirr_contribution = shapley_shares.get("XIRR Bonus")
                        cash_drag_xirr_contribution = shapley_shares.get("XIRR Cash drag")
                        taxes_xirr_contribution = shapley_shares.get("XIRR Taxes")
                        interest_xirr_contribution = shapley_shares.get("XIRR Intérêts")
                        log.info(
                            "XIRR Shapley shares (since-inception, missed earnings ~%.2f EUR): %r",
                            missed_earnings, {k: round(v * 100, 4) for k, v in shapley_shares.items() if v is not None},
                        )

    fill_current_month_amounts(
        platform="Nectaro",
        amounts=amounts,
        skip_total=not current_month,
    )

    bonus_breakdown = {
        "prélèvements": amounts["withholding_tax"],
    }
    if amounts["bonus_cashback_contest"]:
        bonus_breakdown["prime"] = amounts["bonus_cashback_contest"]
    if xirr_value is not None:
        bonus_breakdown["XIRR"] = xirr_value
    if cash_drag_value is not None:
        bonus_breakdown["Cash drag"] = cash_drag_value
    if bonus_xirr_contribution is not None:
        bonus_breakdown["XIRR Bonus"] = bonus_xirr_contribution
    if cash_drag_xirr_contribution is not None:
        bonus_breakdown["XIRR Cash drag"] = cash_drag_xirr_contribution
    if taxes_xirr_contribution is not None:
        bonus_breakdown["XIRR Taxes"] = taxes_xirr_contribution
    if frais_xirr_contribution is not None:
        bonus_breakdown["XIRR Frais"] = frais_xirr_contribution
    if interest_xirr_contribution is not None:
        bonus_breakdown["XIRR Intérêts"] = interest_xirr_contribution
    if avg_invested_balance is not None:
        bonus_breakdown[INVESTED_BALANCE_LABEL] = avg_invested_balance
    if avg_non_invested_balance is not None:
        bonus_breakdown[NON_INVESTED_BALANCE_LABEL] = avg_non_invested_balance

    fill_current_month_bonus_breakdown(
        platform="Nectaro",
        breakdown=bonus_breakdown,
    )

    if current_month:
        fill_geographic_repartition_amounts(companies, platform="Nectaro")
        fill_geographic_repartition_uninvested_amount("Nectaro", overview["cash_balance"])


if __name__ == "__main__":
    run()
