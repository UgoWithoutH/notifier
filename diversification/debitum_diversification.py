"""Debitum Investments portfolio diversification (by lending company)
fetcher.

Debitum (debitum.investments) is a Latvian business-loan crowdlending
platform. Its frontend is a React SPA calling a JSON REST API under
debitum.investments/gtw/... (an internal API gateway, "gtw" prefix) -
pure HTTP, no Playwright/browser needed (same category as Nectaro/Go &
Grow/Bricks/Iuvo).

Auth flow (reverse-engineered 2026-09-08 via a real login + Playwright
network capture, then independently replayed with a plain
`requests.Session()` - see repo memory for the full exploration):
  1. Submitting the login form (email/password) does NOT itself validate
     credentials against the server - it just navigates the SPA to
     /fr/confirm-login (the 2FA code entry screen), client-side only. The
     actual authentication happens in ONE combined call once the TOTP code
     is entered:
     `POST https://debitum.investments/gtw/oauth/token`
     `Content-Type: application/x-www-form-urlencoded`
     body: `username=<email>&password=<password>&grant_type=password&
     client_id=debitumapp&client_secret=x58GUKN8TQHB3FG&
     verification_code=<6-digit TOTP code>`
     (`client_id`/`client_secret` are hardcoded, public values baked into
     the frontend JS bundle - not a per-user secret, confirmed identical
     across runs - safe to hardcode here too, same as the app itself does)
     -> `{"access_token": <JWT>, "refresh_token": <str>, "token_type":
     "Bearer", "expires_in": 599}` (~10 min lifetime - a single run
     finishes in seconds, no refresh-token handling needed).
  2. A second call, `POST https://debitum.investments/gtw/2fa` with
     `Authorization: Bearer <access_token>` and JSON body
     `{"confirmationCode": <same TOTP code>}`, is ALSO fired by the real
     UI right after (empty response) - replicated here for parity with
     the real login flow, though it was not proven strictly required for
     subsequent API calls to succeed.
  3. Every subsequent authenticated call sends `Authorization: Bearer
     <access_token>` (confirmed live: without this header, even a
     structurally-valid request gets HTTP 401 "Access Denied" - session
     cookies set during login are NOT sufficient on their own).

Data endpoints (all under debitum.investments/gtw/..., `Authorization:
Bearer` auth):
  - `GET /gtw/loans/api/balances` -> `investedEur` (current outstanding/
    invested total) and `fiatEur` (uninvested cash wallet, "non investi").
  - `GET /gtw/loans/api/investments/charts` -> `loanOriginatorChart.
    entries: [{"originatorName", "amount"}, ...]` - the CURRENT outstanding
    amount per lending company, used directly for the "Répartition
    géographique" breakdown (no percentage-multiplication trick needed,
    unlike Swaper/Iuvo - this is already an absolute EUR figure per
    company).
  - `POST /gtw/loans/api/balances/v3/transactions-summary` body
    `{"transactionTypes": [], "periodFrame": "CUSTOM"|"ALLTIME",
    "period": {"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}|null}` -> a
    SERVER-SIDE PRE-AGGREGATED summary for the given range: `interest`
    (gross interest received), `totalTax` (withholding tax), `bonusReferral`
    (referral/loyalty bonus - AFFILIATE-type transactions), `principal`.
    An EMPTY `transactionTypes` array means "every type included" (per
    explicit user request "regarde dans le filtre tout les transactions
    type pour tous les prendre en compte") - confirmed live this is the
    SAME convention the real UI itself uses by default (its own default
    "AFFICHER LES RÉSULTATS" filter submission sends an empty array).
    `periodFrame="CUSTOM"` + a real `period.from`/`period.to` (YYYY-MM-DD)
    is used for "this month" (month start -> today); `periodFrame=
    "ALLTIME"` (period=null) is used for lifetime/since-inception totals
    (needed for the XIRR Bonus/Taxes/Intérêts counterfactual shares below)
    - MUCH simpler than Nectaro/Afranga here since these totals are
    already aggregated server-side, no per-transaction classification
    needed for them at all.
  - `POST /gtw/loans/api/balances/v3/all-transactions?page=N&size=500`
    (same body shape as transactions-summary above) -> paginated raw
    transaction list, response shape `{"content": [...], "page": {"size",
    "totalElements", "totalPages", "number"}}`. Each entry:
    `{"amount", "transactionType", "invoiceId", "originatorName",
    "createdOn", "id"}` - `id` is a real, stable, per-row unique string
    (e.g. "FIAT:TAX:<uuid>") - only used here for (a) the since-inception
    DEPOSIT/WITHDRAWAL cashflow list feeding XIRR, and (b) the day-by-day
    cash-wallet reconstruction feeding Cash drag (see
    `_cash_delta_for_transaction()`'s docstring for the verified
    SUBSCRIPTION-vs-INVESTMENT accounting quirk this relies on).
  - `GET /gtw/loans/api/interest-calculator/xirr` -> Debitum's OWN native
    XIRR time series (`{"my": [{"date", "rate"}, ...]}, "top1": [...
    a platform-wide benchmark, unused here]}`) - NOT used for the "XIRR"
    row itself (this repo computes its own since-inception XIRR from real
    DEPOSIT/WITHDRAWAL cashflows + today's total value, exactly like every
    other platform, so the "XIRR Bonus"/"XIRR Cash drag"/etc. counterfactual
    shares can be derived consistently) - kept only as a cross-check
    reference in a future session if the two ever need reconciling.

Since-inception XIRR/Cash drag block (mirrors nectaro_diversification.py's
design exactly - see that module's own docstring for the full
methodology): gated behind `is_current_month()` (LIVE-only, needs today's
real total account value) - NOT extended to support a REPORT_DATE-
backfilled past month (same documented scope limitation as Nectaro).
  - XIRR cashflows: every DEPOSIT (negative, money invested) and
    WITHDRAWAL (positive, money returned) transaction, since account
    inception, fetched incrementally via `debitum_xirr_cashflows_state.json`
    (deduped by Debitum's own transaction "id" field).
  - Terminal cashflow: today's real `investedEur + fiatEur`.
  - Cash drag / XIRR Cash drag / XIRR Bonus / XIRR Taxes/Frais / XIRR
    Intérêts: same counterfactual-XIRR technique as every other platform
    with this block (add the lifetime bonus/taxes/interest total - read
    DIRECTLY from the ALLTIME transactions-summary call, no manual summing
    needed - back to today's total value, recompute XIRR, contribution =
    xirr_real - xirr_counterfactual). Cash drag's own avg-idle-cash input
    is computed via `shared.weighted_average.compute_time_weighted_average()`
    fed by `_cash_delta_for_transaction()` - a day-by-day reconstruction of
    the "non investi" (fiatEur) wallet balance.
  - `_cash_delta_for_transaction()` - IMPORTANT verified accounting quirk:
    Debitum records BOTH a "SUBSCRIPTION" and an "INVESTMENT" transaction
    for the same loan purchase (same amount, different timestamps) - only
    ONE of them is a real cash-wallet movement. Verified live (summed every
    transaction type's own signed `amount` across the account's full
    history): excluding ONLY "INVESTMENT" (treating it as a neutral
    internal reclassification from a pending "subscriptionsAmount" bucket
    into "investedEur") reproduces the live `fiatEur` balance EXACTLY
    (0.9737... EUR, matching to sub-cent precision) - "SUBSCRIPTION" is the
    real debit (the money actually leaves the uninvested wallet when
    SUBSCRIBING, not when the pending subscription later converts into a
    booked investment). Every OTHER observed type's `amount` (TAX,
    AFFILIATE, INTEREST_REPAYMENT, INTEREST_BOOST_REPAYMENT, SUBSCRIPTION,
    DEPOSIT) is ALREADY correctly signed for its cash effect - no
    type-to-sign lookup table needed, unlike most other platforms in this
    repo. A future unrecognized type is NOT specially handled (its
    `amount` is used as-is, since every type observed so far follows this
    same signed-amount convention) - logged for visibility only.

Added 2026-09-09: switched the XIRR Bonus/Cash drag/Taxes/Intérêts shares
from isolated counterfactuals (cancel ONE factor, XIRR_real - XIRR_without
that factor) to a proper Shapley-value decomposition (see
shared/xirr_shapley.py's module docstring) - the old method left an
unexplained gap between XIRR and the sum of its "explaining" shares
because XIRR is non-linear in its cashflows (interaction effects between
factors were silently dropped). Shapley shares are additive by
construction: XIRR Bonus + XIRR Cash drag + XIRR Taxes + XIRR Frais + XIRR
Intérêts now sums back to XIRR real - XIRR with every factor neutralized
(checked at runtime, warns if off by more than 0.0001). Also split the old
single "XIRR Taxes/Frais" share into "XIRR Taxes" (withholding tax, i.e.
`totalTax`) and "XIRR Frais" - Debitum has no platform-fee concept
distinct from withholding tax (transactions-summary only ever exposes
`totalTax`, no separate fee field), so "XIRR Frais" is hardcoded to 0.0,
not computed via Shapley.

Required environment variables:
    DEBITUM_EMAIL, DEBITUM_PASSWORD -> login credentials.
    DEBITUM_TOTP_SECRET             -> base32 TOTP secret (2FA is always
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
from shared.xirr_waterfall import compute_waterfall_xirr_shares

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("debitum_diversification")

API_BASE = "https://debitum.investments/gtw"
OAUTH_TOKEN_URL = f"{API_BASE}/oauth/token"
TWO_FA_URL = f"{API_BASE}/2fa"
BALANCES_URL = f"{API_BASE}/loans/api/balances"
INVESTMENTS_CHARTS_URL = f"{API_BASE}/loans/api/investments/charts"
TRANSACTIONS_SUMMARY_URL = f"{API_BASE}/loans/api/balances/v3/transactions-summary"
ALL_TRANSACTIONS_URL = f"{API_BASE}/loans/api/balances/v3/all-transactions"

DEBITUM_EMAIL = os.environ.get("DEBITUM_EMAIL")
DEBITUM_PASSWORD = os.environ.get("DEBITUM_PASSWORD")
DEBITUM_TOTP_SECRET = os.environ.get("DEBITUM_TOTP_SECRET")

# Hardcoded, public OAuth client credentials baked into Debitum's own
# frontend JS bundle (NOT a per-user secret) - confirmed identical across
# runs via a real Playwright network capture.
OAUTH_CLIENT_ID = "debitumapp"
OAUTH_CLIENT_SECRET = "x58GUKN8TQHB3FG"

_BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}

ALL_TRANSACTIONS_PAGE_SIZE = 500
MAX_ALL_TRANSACTIONS_PAGES = 50

# XIRR is a since-inception money-weighted return - this start date is
# early enough to cover any real account's full history.
XIRR_HISTORY_START_DATE = date(2000, 1, 1)

XIRR_CASHFLOWS_STATE_FILE = Path(__file__).parent / "debitum_xirr_cashflows_state.json"
XIRR_CASHFLOWS_STATE_DEFAULT = {"transactions": [], "last_fetched_date": None}


def login(session: requests.Session) -> str:
    """Log in to Debitum using DEBITUM_EMAIL/PASSWORD + DEBITUM_TOTP_SECRET
    (2FA always enabled on the observed test account). Returns the real
    access token (a JWT) to use as `Authorization: Bearer <token>` on every
    subsequent call. See the module docstring for the full verified flow -
    unlike most other platforms in this repo, credentials + the TOTP code
    are submitted together in ONE combined password-grant call."""
    if not DEBITUM_EMAIL or not DEBITUM_PASSWORD:
        raise RuntimeError("DEBITUM_EMAIL and DEBITUM_PASSWORD environment variables are required.")
    if not DEBITUM_TOTP_SECRET:
        raise RuntimeError("DEBITUM_TOTP_SECRET environment variable is required (2FA is always enabled).")

    totp = pyotp.TOTP(DEBITUM_TOTP_SECRET)
    now = time.time()
    # Same-window retries are a no-op (pyotp.now() called twice within a
    # second returns the identical code) - try 3 DISTINCT candidate codes
    # (current/previous/next 30s window) instead, same resilience pattern
    # as every other TOTP-gated platform in this repo.
    candidates = [totp.at(now), totp.at(now - 30), totp.at(now + 30)]

    last_response = None
    for attempt, code in enumerate(candidates, start=1):
        log.info("Submitting credentials + TOTP code (attempt %d/%d)...", attempt, len(candidates))
        r = session.post(
            OAUTH_TOKEN_URL,
            data={
                "username": DEBITUM_EMAIL,
                "password": DEBITUM_PASSWORD,
                "grant_type": "password",
                "client_id": OAUTH_CLIENT_ID,
                "client_secret": OAUTH_CLIENT_SECRET,
                "verification_code": code,
            },
            headers=_BASE_HEADERS,
            timeout=20,
        )
        last_response = r
        if r.ok:
            data = r.json()
            token = data.get("access_token")
            if token:
                log.info("Logged in successfully.")
                # Diagnostic only (no secret/code values logged): compare
                # Debitum's server-reported clock to our local clock - same
                # precaution as every other TOTP-gated platform in this
                # repo, after multiple real GitHub Actions TOTP-rejection
                # incidents that turned out unrelated to clock skew but
                # were only ruled out this way.
                server_date_header = r.headers.get("Date")
                if server_date_header:
                    try:
                        server_time = parsedate_to_datetime(server_date_header)
                        skew = (datetime.now(timezone.utc) - server_time).total_seconds()
                        log.info("Clock check: local vs. Debitum server Date header skew = %.1fs", skew)
                    except Exception:
                        pass

                # Mirrors the real UI's own second call - soft-fail, not
                # proven strictly required for subsequent API calls.
                try:
                    session.post(
                        TWO_FA_URL,
                        json={"confirmationCode": code},
                        headers={**_BASE_HEADERS, "Authorization": f"Bearer {token}"},
                        timeout=20,
                    )
                except Exception:
                    log.warning("The follow-up /gtw/2fa confirmation call failed - continuing anyway (not proven required).")
                return token
        log.info("Login attempt %d/%d rejected (status=%s)...", attempt, len(candidates), r.status_code)

    raise RuntimeError(
        f"Debitum rejected the login (all {len(candidates)} TOTP candidates) - "
        f"last status={last_response.status_code if last_response is not None else 'N/A'}."
    )


def fetch_balances(session: requests.Session, headers: dict) -> dict:
    """Fetch the LIVE account balances: `cash_balance` (uninvested wallet,
    "non investi", `fiatEur`) and `invested_funds` (current outstanding/
    invested total, `investedEur`)."""
    r = session.get(BALANCES_URL, headers=headers, timeout=20)
    r.raise_for_status()
    data = r.json()
    cash_balance = data.get("fiatEur", 0.0)
    invested_funds = data.get("investedEur", 0.0)
    log.info("Balances: cash_balance=%.2f EUR, invested_funds=%.2f EUR.", cash_balance, invested_funds)
    return {"cash_balance": cash_balance, "invested_funds": invested_funds}


def fetch_portfolio_by_lending_company(session: requests.Session, headers: dict) -> list:
    """Fetch the current outstanding amount per lending company via the
    investments/charts endpoint's `loanOriginatorChart` - already an
    absolute EUR figure per company, no percentage-multiplication needed.
    Returns a list of {"name", "amount"} dicts, sorted by amount descending."""
    r = session.get(INVESTMENTS_CHARTS_URL, headers=headers, timeout=20)
    r.raise_for_status()
    entries = r.json().get("loanOriginatorChart", {}).get("entries", [])

    companies = [{"name": e["originatorName"], "amount": e.get("amount", 0.0)} for e in entries]
    companies.sort(key=lambda c: c["amount"], reverse=True)
    return companies


def fetch_transactions_summary(session: requests.Session, headers: dict, start_date: date | None, end_date: date | None) -> dict:
    """Fetch the server-side pre-aggregated transaction summary for
    [start_date, end_date] (inclusive) - or the whole account history if
    both are None (periodFrame="ALLTIME"). `transactionTypes` is
    deliberately left empty (every type included, see module docstring)."""
    if start_date is None and end_date is None:
        body = {"transactionTypes": [], "periodFrame": "ALLTIME", "period": None}
    else:
        body = {
            "transactionTypes": [],
            "periodFrame": "CUSTOM",
            "period": {"from": start_date.strftime("%Y-%m-%d"), "to": end_date.strftime("%Y-%m-%d")},
        }
    r = session.post(TRANSACTIONS_SUMMARY_URL, json=body, headers=headers, timeout=20)
    r.raise_for_status()
    return r.json()


def _fetch_all_transactions_page(session: requests.Session, headers: dict, start_date: date, end_date: date, page: int) -> dict:
    body = {
        "transactionTypes": [],
        "periodFrame": "CUSTOM",
        "period": {"from": start_date.strftime("%Y-%m-%d"), "to": end_date.strftime("%Y-%m-%d")},
    }
    r = session.post(
        ALL_TRANSACTIONS_URL,
        params={"page": page, "size": ALL_TRANSACTIONS_PAGE_SIZE},
        json=body,
        headers=headers,
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def fetch_all_transactions(session: requests.Session, headers: dict, start_date: date, end_date: date) -> list:
    """Fetch every transaction in [start_date, end_date] (inclusive),
    paginating via the response's own `page.totalPages`/`page.number`."""
    all_transactions = []
    for page in range(MAX_ALL_TRANSACTIONS_PAGES):
        data = _fetch_all_transactions_page(session, headers, start_date, end_date, page)
        rows = data.get("content", [])
        log.info("all-transactions page %d: %d row(s) fetched.", page, len(rows))
        all_transactions.extend(rows)
        page_info = data.get("page", {})
        if page + 1 >= page_info.get("totalPages", 1):
            break
    else:
        log.warning("Hit MAX_ALL_TRANSACTIONS_PAGES (%d) - history may be truncated.", MAX_ALL_TRANSACTIONS_PAGES)
    return all_transactions


def get_cached_all_transactions(session: requests.Session, headers: dict, end_date: date) -> list:
    """Incremental fetch of every transaction ever seen, cached in
    XIRR_CASHFLOWS_STATE_FILE and deduped by Debitum's own transaction
    `id` field (a real, stable, per-row unique string, confirmed live)."""
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

    log.info("Fetching transactions from %s to %s (incremental cache)...", fetch_start, end_date)
    new_rows = fetch_all_transactions(session, headers, fetch_start, end_date)
    for row in new_rows:
        cached[row["id"]] = row

    state["transactions"] = list(cached.values())
    state["last_fetched_date"] = end_date.strftime("%Y-%m-%d")
    save_state(XIRR_CASHFLOWS_STATE_FILE, state)

    log.info("%d cached transaction(s) in total.", len(cached))
    return list(cached.values())


def _parse_transaction_date(raw: str) -> date:
    """Parse Debitum's own ISO 8601 `createdOn` timestamp (e.g.
    "2026-09-01T01:33:05.161549Z") into a plain date."""
    return datetime.strptime(raw[:10], "%Y-%m-%d").date()


# Verified live (see module docstring): every transaction type's own
# `amount` is ALREADY correctly signed for its cash-wallet effect, EXCEPT
# "INVESTMENT" - which is a neutral internal reclassification from a
# pending "subscriptionsAmount" bucket (debited at SUBSCRIPTION time) into
# "investedEur", not a second real cash movement.
_CASH_NEUTRAL_TRANSACTION_TYPES = {"INVESTMENT"}


def _cash_delta_for_transaction(transaction: dict) -> float:
    """Signed delta to the uninvested cash wallet for one transaction."""
    if transaction.get("transactionType") in _CASH_NEUTRAL_TRANSACTION_TYPES:
        return 0.0
    return transaction.get("amount", 0.0)


# Signed delta to the INVESTED (outstanding) balance - only "INVESTMENT"
# is known to move money INTO investedEur (its own `amount` is negative,
# mirroring SUBSCRIPTION's real debit for the same loan purchase, see the
# module docstring's accounting-quirk paragraph) - so the invested balance
# INCREASES by -amount. No principal-repayment transaction type has been
# observed yet on this (young) account to decrease it - every other type
# is treated as neutral here, same "don't guess" convention as elsewhere
# in this repo. If a real principal-repayment type is ever observed, add
# it here (decrease = -amount) instead of leaving this at 0.0.
_INVESTED_INCREASE_TRANSACTION_TYPES = {"INVESTMENT"}


def _invested_delta_for_transaction(transaction: dict) -> float:
    if transaction.get("transactionType") in _INVESTED_INCREASE_TRANSACTION_TYPES:
        return -transaction.get("amount", 0.0)
    return 0.0


def run() -> None:
    if not DEBITUM_EMAIL or not DEBITUM_PASSWORD:
        log.error("DEBITUM_EMAIL and DEBITUM_PASSWORD environment variables are required.")
        sys.exit(1)

    # XIRR/Cash drag (like "total" elsewhere in this repo) is a LIVE-only
    # snapshot metric (needs TODAY's real total account value as its final
    # cashflow) - not yet extended to support a REPORT_DATE-backfilled past
    # month (see module docstring), so gated behind is_current_month().
    current_month = is_current_month()

    log.info("Starting Debitum diversification run (pure HTTP, no browser).")

    session = requests.Session()
    try:
        token = login(session)
        headers = {**_BASE_HEADERS, "Authorization": f"Bearer {token}"}
        balances = fetch_balances(session, headers)
        companies = fetch_portfolio_by_lending_company(session, headers)
    except Exception:
        log.exception("Failed to log in or fetch Debitum's portfolio/balances.")
        sys.exit(1)

    log.info("Fetched %d lending compan(y/ies).", len(companies))
    for c in companies:
        log.info("  %s: %.2f EUR", c["name"], c["amount"])

    today_date = get_report_date()
    month_start_date = today_date.replace(day=1)

    amounts = {
        "total": balances["invested_funds"] + balances["cash_balance"],
        "gross_interest_received": 0.0,
        "net_interest_received": 0.0,
        "withholding_tax": 0.0,
        "bonus_cashback_contest": 0.0,
    }

    try:
        month_summary = fetch_transactions_summary(session, headers, month_start_date, today_date)
        gross_interest = month_summary.get("interest", 0.0) or 0.0
        withholding_tax = month_summary.get("totalTax", 0.0) or 0.0
        bonus = month_summary.get("bonusReferral", 0.0) or 0.0
        amounts["gross_interest_received"] = gross_interest
        amounts["net_interest_received"] = gross_interest - withholding_tax
        amounts["withholding_tax"] = withholding_tax
        amounts["bonus_cashback_contest"] = bonus
        log.info(
            "This month's totals: gross_interest=%.2f EUR, withholding_tax=%.2f EUR, bonus=%.2f EUR.",
            gross_interest, withholding_tax, bonus,
        )
    except Exception:
        log.exception("Failed to fetch this month's transactions summary - defaulting interest/tax/bonus to 0.0.")

    # Since-inception XIRR/Cash drag block - see module docstring for the
    # full methodology (mirrors nectaro_diversification.py's design).
    xirr_value = None
    cash_drag_value = None
    bonus_xirr_contribution = None
    cash_drag_xirr_contribution = None
    taxes_xirr_contribution = None
    frais_xirr_contribution = None
    interest_xirr_contribution = None
    avg_invested_balance = None
    avg_non_invested_balance = None
    earliest_transaction_date = None

    all_transactions = None
    try:
        all_transactions = get_cached_all_transactions(session, headers, today_date)
    except Exception:
        log.exception("Failed to fetch the since-inception transaction history - XIRR/Cash drag will not be updated.")

    if all_transactions is not None:
        cash_events = []
        invested_events = []
        for t in all_transactions:
            try:
                t_date = _parse_transaction_date(t["createdOn"])
            except (KeyError, ValueError):
                log.warning("Skipping a transaction with an unparseable date: %r", t)
                continue
            cash_events.append((t_date, _cash_delta_for_transaction(t)))
            invested_events.append((t_date, _invested_delta_for_transaction(t)))
            if earliest_transaction_date is None or t_date < earliest_transaction_date:
                earliest_transaction_date = t_date

        # Anchored on the real LIVE balances["invested_funds"]/["cash_balance"]
        # (only meaningful for the real current month - no historical
        # equivalent exists on Debitum) instead of a pure since-inception
        # reconstruction (opening_balance=0.0) - bounds any transaction
        # misclassification drift to just this month's own events instead
        # of the account's entire history. Falls back to the plain
        # reconstruction for a backfilled month.
        if current_month:
            period_invested = [(d, v) for d, v in invested_events if month_start_date <= d <= today_date]
            avg_invested_balance = compute_time_weighted_average(
                period_invested, month_start_date, today_date,
                opening_balance=balances["invested_funds"] - sum(v for _, v in period_invested),
            )
            period_cash = [(d, v) for d, v in cash_events if month_start_date <= d <= today_date]
            avg_non_invested_balance = compute_time_weighted_average(
                period_cash, month_start_date, today_date,
                opening_balance=balances["cash_balance"] - sum(v for _, v in period_cash),
            )
        else:
            avg_invested_balance = compute_time_weighted_average(invested_events, month_start_date, today_date)
            avg_non_invested_balance = compute_time_weighted_average(cash_events, month_start_date, today_date)
        log.info(
            "Solde moyen pondéré - investi: %.2f EUR, non investi: %.2f EUR (%s to %s).",
            avg_invested_balance, avg_non_invested_balance, month_start_date, today_date,
        )

        # Cash drag now computed for a backfilled month too (not just the
        # live current month) - only needs the avg balances above (already
        # backfill-aware) and this period's own already-fetched
        # gross_interest_received.
        if avg_invested_balance is not None and avg_invested_balance > 0:
            cash_weight = avg_non_invested_balance / (avg_non_invested_balance + avg_invested_balance)
            monthly_yield_rate = amounts["gross_interest_received"] / avg_invested_balance
            cash_drag_value = cash_weight * monthly_yield_rate
            log.info(
                "Computed Cash drag: %.4f%% (avg idle cash %.2f EUR).",
                cash_drag_value * 100, avg_non_invested_balance,
            )

        if current_month:
            deposit_dates = [
                _parse_transaction_date(t["createdOn"]) for t in all_transactions if t.get("transactionType") == "DEPOSIT"
            ]
            total_invested = balances["invested_funds"]
            total_account_value = total_invested + balances["cash_balance"]

            signed_cashflows = []
            for t in all_transactions:
                ttype = t.get("transactionType")
                if ttype not in ("DEPOSIT", "WITHDRAWAL"):
                    continue
                try:
                    t_date = _parse_transaction_date(t["createdOn"])
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

                try:
                    lifetime_summary = fetch_transactions_summary(session, headers, None, None)
                except Exception:
                    log.exception("Failed to fetch lifetime transactions summary - XIRR Bonus/Taxes/Intérêts shares will not be updated.")
                    lifetime_summary = None

                if lifetime_summary is not None:
                    lifetime_bonus_total = lifetime_summary.get("bonusReferral", 0.0) or 0.0
                    lifetime_withholding_tax = lifetime_summary.get("totalTax", 0.0) or 0.0
                    lifetime_gross_interest = lifetime_summary.get("interest", 0.0) or 0.0
                    # waterfall_shares call moved further below (needs
                    # missed_earnings, computed together with Cash drag) so
                    # all steps are evaluated in the fixed waterfall order
                    # (shared/xirr_waterfall.py).

                # cash_drag_value is now computed earlier (unconditionally,
                # backfill-aware) - only the lifetime waterfall shares
                # still need this current-month-only XIRR block.
                if cash_drag_value is not None:
                    if deposit_dates and lifetime_summary is not None and total_invested > 0:
                        since_inception_date = min(deposit_dates)
                        avg_idle_cash_lifetime = compute_time_weighted_average(cash_events, since_inception_date, today_date)
                        cash_weight_lifetime = avg_idle_cash_lifetime / (avg_idle_cash_lifetime + total_invested)
                        lifetime_yield_rate = lifetime_gross_interest / total_invested
                        cash_drag_lifetime_total = cash_weight_lifetime * lifetime_yield_rate
                        missed_earnings = cash_drag_lifetime_total * (avg_idle_cash_lifetime + total_invested)

                        # Waterfall decomposition (switched from Shapley
                        # 2026-09-09, see shared/xirr_waterfall.py's
                        # module docstring for why): walks a true
                        # 0%-return baseline up to total_account_value in
                        # the fixed order Intérêts -> Cash drag -> Bonus
                        # -> Frais -> Taxes, using GROSS interest (not
                        # net) at the Intérêts step and subtracting
                        # missed_earnings right after - each euro counted
                        # exactly once, so the shares sum EXACTLY to XIRR
                        # real (checked at runtime via a warning log).
                        # Debitum has no platform-fee concept distinct
                        # from withholding tax (transactions-summary only
                        # ever exposes "totalTax", no separate fee field)
                        # - "XIRR Frais" is hardcoded to 0.0 rather than
                        # duplicating/inventing a value, and is skipped
                        # from the steps below.
                        steps = [
                            ("XIRR Intérêts", lifetime_gross_interest + missed_earnings),
                            ("XIRR Cash drag", -missed_earnings),
                            ("XIRR Bonus", lifetime_bonus_total),
                            ("XIRR Taxes", -lifetime_withholding_tax),
                        ]
                        waterfall_shares = compute_waterfall_xirr_shares(
                            signed_cashflows[:-1], today_date, total_account_value, steps,
                            log=log, log_context="Debitum",
                        )
                        bonus_xirr_contribution = waterfall_shares.get("XIRR Bonus")
                        cash_drag_xirr_contribution = waterfall_shares.get("XIRR Cash drag")
                        taxes_xirr_contribution = waterfall_shares.get("XIRR Taxes")
                        frais_xirr_contribution = 0.0
                        interest_xirr_contribution = waterfall_shares.get("XIRR Intérêts")
                        log.info(
                            "XIRR Waterfall shares (since-inception, missed earnings ~%.2f EUR): %r",
                            missed_earnings, {k: round(v * 100, 4) for k, v in waterfall_shares.items() if v is not None},
                        )

    # For a backfilled (non-current) month, only write to the Sheet if the
    # account actually existed by then (had at least one real transaction
    # on or before today_date) - otherwise every month before the account
    # was opened would get 0.00 EUR written into every cell instead of
    # staying blank, which looks like real (if empty) data was recorded
    # for a period the account didn't exist yet.
    account_existed_this_period = current_month or (
        all_transactions is None or (earliest_transaction_date is not None and today_date >= earliest_transaction_date)
    )
    if not account_existed_this_period:
        log.info(
            "Debitum account had no transactions as of %s (first known transaction %s) - "
            "skipping all Sheet writes for this backfilled month.",
            today_date, earliest_transaction_date,
        )
        return

    fill_current_month_amounts(
        platform="Debitum",
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
        platform="Debitum",
        breakdown=bonus_breakdown,
    )

    if current_month:
        fill_geographic_repartition_amounts(companies, platform="Debitum")
        fill_geographic_repartition_uninvested_amount("Debitum", balances["cash_balance"])


if __name__ == "__main__":
    run()
