"""Income Marketplace (getincome.com) portfolio diversification fetcher.

Income Marketplace is a Vue SPA (frontend at getincome.com) on top of a
pure JSON REST API (api.getincome.com), using Auth0 for authentication.
Reverse-engineered live 2026-09-08 via a real login + Playwright network
capture (see repo memory for the full exploration).

Auth flow:
    1. POST https://api.getincome.com/api/auth0-login
       JSON body {"username": <email>, "password": <password>}
       -> {"success": false, "mfa_required": true, "mfa_token": <opaque
           Iron-sealed string>} (2FA is always enabled on the observed
       test account).
    2. POST https://api.getincome.com/api/auth0-login-mfa
       JSON body {"mfa_token": <from step 1>, "otp": <6-digit TOTP code>}
       -> the real access/refresh tokens (delivered to the SPA, which
       stores them in `localStorage["accessToken"]`/`["refreshToken"]` -
       the accessToken is a ~24h JWT, sent as `Authorization: Bearer
       <accessToken>` on every subsequent call).
    A DIRECT plain `requests` replay of step 1 (no browser at all) was
    tried and got an HTTP 503 - but the response body was Michelin's own
    "Security threat detected" corporate-proxy block page, NOT a real
    getincome.com response (confirmed by the page title/HTML) - i.e. this
    is the same LOCAL-ONLY corporate-proxy gotcha already documented
    elsewhere in this repo for POST-to-a-login-path requests, not proof
    that the real server blocks pure HTTP. Since this couldn't be
    conclusively re-tested from a non-Michelin network, this module stays
    Playwright-based for BOTH login and every data call (via
    `page.evaluate(fetch(...))`, reusing the same `Authorization: Bearer`
    header read from localStorage) rather than risk an unverified plain
    `requests.Session()` architecture - same safety-first precedent as
    Swaper's permanently-Playwright-based design elsewhere in this repo.

Data endpoints (all under api.getincome.com, `Authorization: Bearer` auth):
    - GET /api/investor-details -> `funds` (available=uninvested cash,
      invested=current outstanding total, total=available+invested),
      `earnings` (`interest`=lifetime gross interest), `marketplace_earned`
      (`all_time`/`ytd`/`mtd`=month-to-date, matches the account-statement
      "Interest collected" turnover for the current month exactly),
      `charts.division.by_loan_originator` = [{"loan_originator": <name>,
      "total_sum": <current outstanding EUR>}] - the exact per-lending-
      company breakdown needed for "Répartition géographique" (same
      "group by loan originator" convention as Swaper/Nectaro/Iuvo in this
      repo, despite the Sheet section being named "géographique").
    - GET /api/account-statement/{start:YYYY-MM-DD}/{end:YYYY-MM-DD}/{page}
      -> `{"opening_balance", "closing_balance", "invested_start_balance",
      "invested_balance", "statement": [{"account_type": <numeric id str>,
      "account_type_name": <label>, "opening_balance", "closing_balance",
      "turnover2": <sum for the period>, "turnover": [{"Date"
      (YYYY-MM-DD), "Amount", "transaction_id", "lo_name", ...}]}]}` - this
      ALWAYS returns every category (no "type" filter to omit - there
      isn't one on this endpoint at all), directly satisfying the user's
      "prends en compte tous les types de transactions" request with no
      extra code needed. Verified live (full lifetime history, brand-new
      test account created 2026-08-25) - only 7 categories exist:
      2199021 "Deposits made" (cash IN, not invested), 2199022 "Principal
      collected" (cash IN, invested DECREASE), 2199023 "Interest collected"
      (cash IN, not invested - the gross interest figure), 2199024 "Bonus
      collected" (cash IN, not invested - the bonus/cashback figure),
      2199025 "Investments made" (cash OUT - `Amount` is ALREADY negative
      in the raw data, invested INCREASE), 2199026 "Withdrawals" (cash OUT
      - never observed live yet, ASSUMED negative like "Investments made"
      to match every other observed category's sign convention - if a
      real withdrawal ever shows a POSITIVE `Amount` instead, the
      reconstructed closing-balance cross-check below will flag a
      mismatch), 2199027 "Principal collected by enforced buyback
      obligation" (never observed live yet, treated identically to
      "Principal collected" by its name). NO withholding-tax category
      exists at all - `withholding_tax` is hardcoded 0.0 (a genuinely
      verified absence, not a placeholder - re-check with a wider date
      dump if a future run's diagnostics ever show tax being withheld).
      PAGINATION GOTCHA (found live 2026-09-08): the trailing `{page}`
      path segment does NOT paginate safely past page 0 - requesting page
      1 returns a PDF blob (`%PDF-1.4...`) instead of a JSON "empty next
      page", crashing a naive fetch/JSON-parse. Since this account is
      small (a handful of dozens of rows per category over its whole
      lifetime, confirmed live), this module only ever requests page 0 -
      see `fetch_all_statement_transactions()`'s own docstring for the
      (best-effort, not foolproof) large-row-count warning tripwire.
      IMPORTANT GOTCHA (same class of bug as Afranga's/Debitum's own
      documented lesson): the same `transaction_id` is reused across
      MULTIPLE distinct rows for one real underlying event (e.g. a single
      loan repayment splits into a "Principal collected" row AND an
      "Interest collected" row sharing one `transaction_id`) - the
      incremental cache below dedupes on (account_type, transaction_id,
      date, amount), never `transaction_id` alone.

Since-inception XIRR/Cash drag block (mirrors nectaro_diversification.py's
design almost exactly, including using the SAME shared
`shared.weighted_average.compute_time_weighted_average()` helper for
"solde moyen pondéré investi"/"non investi"): gated behind
`is_current_month()` (LIVE-only, needs today's real total account value) -
NOT yet extended to support a REPORT_DATE-backfilled past month.

Required environment variables:
    INCOME_MARKETPLACE_EMAIL, INCOME_MARKETPLACE_PASSWORD -> login
                                       credentials.
    INCOME_MARKETPLACE_TOTP_SECRET   -> base32 TOTP secret (2FA is always
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
from dotenv import load_dotenv
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("income_marketplace_diversification")

LOGIN_PAGE_URL = "https://getincome.com/login"
API_BASE = "https://api.getincome.com/api"
INVESTOR_DETAILS_URL = f"{API_BASE}/investor-details"
ACCOUNT_STATEMENT_URL_TEMPLATE = API_BASE + "/account-statement/{start}/{end}/{page}"

INCOME_MARKETPLACE_EMAIL = os.environ.get("INCOME_MARKETPLACE_EMAIL")
INCOME_MARKETPLACE_PASSWORD = os.environ.get("INCOME_MARKETPLACE_PASSWORD")
INCOME_MARKETPLACE_TOTP_SECRET = os.environ.get("INCOME_MARKETPLACE_TOTP_SECRET")

# Account-type classification (numeric ids, stable/currency-scoped -
# verified live, see module docstring). Never silently guess an unknown
# id - _cash_delta_for_type()/_invested_delta_for_type() log a warning and
# treat it as neutral (0) instead.
CASH_IN_TYPES = {"2199021", "2199022", "2199023", "2199024", "2199027"}  # Deposits/Principal/Interest/Bonus/enforced-buyback-principal
CASH_OUT_TYPES = {"2199025", "2199026"}  # Investments made/Withdrawals (Amount already negative)
INVESTED_INCREASE_TYPES = {"2199025"}  # Investments made
INVESTED_DECREASE_TYPES = {"2199022", "2199027"}  # Principal collected (incl. enforced buyback)
DEPOSIT_TYPE = "2199021"
WITHDRAWAL_TYPE = "2199026"
INTEREST_TYPE = "2199023"
BONUS_TYPE = "2199024"

XIRR_HISTORY_START_DATE = date(2000, 1, 1)
XIRR_CASHFLOWS_STATE_FILE = Path(__file__).parent / "income_marketplace_xirr_cashflows_state.json"
XIRR_CASHFLOWS_STATE_DEFAULT = {"transactions": [], "last_fetched_date": None}


def login(page) -> str:
    """Log in to Income Marketplace using INCOME_MARKETPLACE_EMAIL/PASSWORD
    + INCOME_MARKETPLACE_TOTP_SECRET (2FA is always enabled on the
    observed test account). Returns the real access token (a JWT) from
    localStorage, to use as `Authorization: Bearer <token>` on every
    subsequent `page.evaluate(fetch(...))` call."""
    if not INCOME_MARKETPLACE_EMAIL or not INCOME_MARKETPLACE_PASSWORD:
        raise RuntimeError("INCOME_MARKETPLACE_EMAIL and INCOME_MARKETPLACE_PASSWORD environment variables are required.")

    log.info("Submitting credentials...")
    page.goto(LOGIN_PAGE_URL, wait_until="networkidle", timeout=30000)
    page.fill("#input-username", INCOME_MARKETPLACE_EMAIL)
    page.fill("#input-password", INCOME_MARKETPLACE_PASSWORD)

    with page.expect_response(lambda r: r.url.endswith("/api/auth0-login")) as resp_info:
        page.click("button[type='submit']")
    login_data = resp_info.value.json()

    if not login_data.get("mfa_required"):
        # Not observed live (2FA always enabled on the test account), kept
        # as a defensive fallback in case it's ever disabled.
        token = page.evaluate("() => localStorage.getItem('accessToken')")
        if token:
            log.info("Logged in successfully (no 2FA prompt).")
            return token
        raise RuntimeError(f"Income Marketplace login did not ask for 2FA but no accessToken was found: {login_data}")

    mfa_token = login_data.get("mfa_token")
    if not mfa_token:
        raise RuntimeError(f"Income Marketplace login said 2FA is required but returned no mfa_token: {login_data}")

    if not INCOME_MARKETPLACE_TOTP_SECRET:
        raise RuntimeError(
            "Income Marketplace is asking for a 2FA code but INCOME_MARKETPLACE_TOTP_SECRET is not set. "
            "Set it to the base32 secret used to configure the authenticator app."
        )

    # Diagnostic only (no secret/code values logged): compare the server's
    # Date response header to our local clock - same precaution as every
    # other TOTP-gated platform in this repo.
    server_date_header = resp_info.value.headers.get("date")
    if server_date_header:
        try:
            server_time = parsedate_to_datetime(server_date_header)
            skew = (datetime.now(timezone.utc) - server_time).total_seconds()
            log.info("Clock check: local vs. Income Marketplace server Date header skew = %.1fs", skew)
        except Exception:
            pass

    page.wait_for_selector("input[placeholder='123456']", timeout=15000)

    totp = pyotp.TOTP(INCOME_MARKETPLACE_TOTP_SECRET)
    now = time.time()
    # Same-window retries are a no-op (calling totp.now() twice within a
    # second returns the identical code) - try 3 DISTINCT candidate codes
    # (current/previous/next 30s window) instead, same resilience pattern
    # as every other TOTP-gated platform in this repo.
    candidates = [totp.at(now), totp.at(now - 30), totp.at(now + 30)]
    for attempt, code in enumerate(candidates, start=1):
        log.info("2FA prompt detected, submitting TOTP candidate %d/%d...", attempt, len(candidates))
        page.fill("input[placeholder='123456']", "")
        page.fill("input[placeholder='123456']", code)
        try:
            with page.expect_response(lambda r: r.url.endswith("/api/auth0-login-mfa"), timeout=15000):
                page.click("button:has-text('Verify')")
        except PlaywrightTimeoutError:
            log.warning("No auth0-login-mfa response observed for candidate %d - retrying.", attempt)
            continue
        time.sleep(1.5)
        token = page.evaluate("() => localStorage.getItem('accessToken')")
        if token:
            log.info("Logged in successfully after 2FA.")
            return token

    raise RuntimeError("Income Marketplace rejected the TOTP code (all 3 candidates).")


def _fetch_json(page, url: str, token: str) -> dict:
    """Fetch `url` from inside the already-authenticated page's own JS
    context (never a plain `requests` call - see module docstring for why
    this stays Playwright-based) with the given Bearer token."""
    result = page.evaluate(
        """
        async ({url, token}) => {
            const r = await fetch(url, {
                headers: {'Accept': 'application/json', 'Authorization': 'Bearer ' + token}
            });
            const body = await r.json();
            return {status: r.status, body: body};
        }
        """,
        {"url": url, "token": token},
    )
    if result["status"] != 200:
        raise RuntimeError(f"GET {url} returned HTTP {result['status']}: {result['body']}")
    return result["body"]


def fetch_investor_details(page, token: str) -> dict:
    """Fetch the LIVE account overview: `cash_balance` (uninvested wallet,
    "non investi"), `invested_total` (current outstanding, "total" on the
    platform's own Crowdlending row), and `companies` (per-lending-company
    current outstanding amounts, for "Répartition géographique")."""
    data = _fetch_json(page, INVESTOR_DETAILS_URL, token)
    funds = data.get("funds", {})
    cash_balance = funds.get("available", 0.0)
    invested_total = funds.get("invested", 0.0)

    division = data.get("charts", {}).get("division", {})
    companies = [
        {"name": c["loan_originator"], "amount": c.get("total_sum", 0.0)}
        for c in division.get("by_loan_originator", [])
    ]
    companies.sort(key=lambda c: c["amount"], reverse=True)

    log.info("Investor details: cash_balance=%.2f EUR, invested_total=%.2f EUR, %d compan(y/ies).", cash_balance, invested_total, len(companies))
    return {"cash_balance": cash_balance, "invested_total": invested_total, "companies": companies}


def fetch_account_statement(page, token: str, start_date: date, end_date: date, page_num: int = 0) -> dict:
    url = ACCOUNT_STATEMENT_URL_TEMPLATE.format(
        start=start_date.strftime("%Y-%m-%d"), end=end_date.strftime("%Y-%m-%d"), page=page_num
    )
    return _fetch_json(page, url, token)


def fetch_all_statement_transactions(page, token: str, start_date: date, end_date: date) -> list:
    """Fetch every statement transaction in [start_date, end_date]
    (inclusive), flattened across every account_type category (there's no
    "type" filter to worry about on this endpoint - it always returns
    every category, see module docstring).

    IMPORTANT: unlike every other paginated *_diversification.py endpoint
    in this repo, this one does NOT paginate safely - requesting page 1 (or
    higher) returns a PDF blob instead of JSON (verified live 2026-09-08,
    `Page.evaluate: SyntaxError: ... is not valid JSON` - the response body
    starts with the PDF magic bytes "%PDF-1.4", not an empty/next JSON
    page). So this ONLY ever requests page 0 - safe for this small/new
    account (confirmed live: a full lifetime fetch returns everything in
    one page), but if a future run's row count for any category looks
    suspiciously close to a true page-size limit, that would silently
    truncate history with no error raised - the warning below is a
    best-effort tripwire for that, not a real fix (no known way to safely
    request "the next page" was found)."""
    STATEMENT_ROW_COUNT_WARNING_THRESHOLD = 200
    body = fetch_account_statement(page, token, start_date, end_date, page_num=0)
    all_transactions = []
    for group in body.get("statement", []):
        account_type = group.get("account_type")
        account_type_name = group.get("account_type_name")
        rows = group.get("turnover", [])
        if len(rows) >= STATEMENT_ROW_COUNT_WARNING_THRESHOLD:
            log.warning(
                "Category %s (%s) returned %d rows, close to/at a possible page-size limit - "
                "history may be silently truncated (page 1+ is known to return a PDF, not more JSON).",
                account_type, account_type_name, len(rows),
            )
        for row in rows:
            all_transactions.append({
                "account_type": account_type,
                "account_type_name": account_type_name,
                "date": row.get("Date"),
                "amount": row.get("Amount", 0.0),
                "transaction_id": row.get("transaction_id"),
            })
    log.info("Account statement (%s to %s): %d row(s) fetched.", start_date, end_date, len(all_transactions))
    return all_transactions


def get_cached_statement_transactions(page, token: str, end_date: date) -> list:
    """Incremental fetch of every statement transaction ever seen, cached
    in XIRR_CASHFLOWS_STATE_FILE. Deduped on (account_type, transaction_id,
    date, amount) - NOT transaction_id alone, since the same transaction_id
    is reused across distinct rows in different categories for one real
    underlying event (see module docstring)."""
    state = load_state(XIRR_CASHFLOWS_STATE_FILE, XIRR_CASHFLOWS_STATE_DEFAULT)
    cached = {
        f"{t['account_type']}|{t['transaction_id']}|{t['date']}|{t['amount']}": t
        for t in state["transactions"]
    }

    last_fetched_date_str = state.get("last_fetched_date")
    fetch_start = (
        datetime.strptime(last_fetched_date_str, "%Y-%m-%d").date()
        if last_fetched_date_str
        else XIRR_HISTORY_START_DATE
    )

    if fetch_start > end_date:
        # Cache already covers past end_date (e.g. a live run advanced it,
        # then a backfill run asked for an earlier REPORT_DATE) - the API
        # rejects a start>end range with a non-JSON "End date must be..."
        # error body, which crashes page.evaluate's JSON parse, so skip
        # the fetch entirely and just filter the existing cache instead.
        log.info(
            "Cache already covers up to %s (requested end date %s) - skipping fetch, using cached data only.",
            fetch_start, end_date,
        )
        return list(cached.values())

    log.info("Fetching statement transactions from %s to %s (incremental cache)...", fetch_start, end_date)
    new_rows = fetch_all_statement_transactions(page, token, fetch_start, end_date)
    for row in new_rows:
        key = f"{row['account_type']}|{row['transaction_id']}|{row['date']}|{row['amount']}"
        cached[key] = row

    state["transactions"] = list(cached.values())
    state["last_fetched_date"] = end_date.strftime("%Y-%m-%d")
    save_state(XIRR_CASHFLOWS_STATE_FILE, state)

    log.info("%d cached statement transaction(s) in total.", len(cached))
    return list(cached.values())


def _parse_transaction_date(raw: str) -> date:
    return datetime.strptime(raw, "%Y-%m-%d").date()


def _cash_delta_for_type(account_type: str, amount: float) -> float:
    """Signed delta to the uninvested cash wallet - every observed
    category's raw `Amount` is ALREADY correctly signed for this purpose
    (Deposits/Principal/Interest/Bonus positive, Investments made
    negative) - see module docstring for the "Withdrawals never observed,
    assumed negative" caveat."""
    if account_type in CASH_IN_TYPES or account_type in CASH_OUT_TYPES:
        return amount
    log.warning("Unrecognized statement account_type %r - treated as a neutral (0) cash delta.", account_type)
    return 0.0


def _invested_delta_for_type(account_type: str, amount: float) -> float:
    """Signed delta to the INVESTED (outstanding) balance - "Investments
    made" (amount already negative) increases it, "Principal collected"
    (incl. enforced buyback) decreases it. Every other category is a
    neutral cash-only reallocation (interest/bonus/deposits/withdrawals)."""
    if account_type in INVESTED_INCREASE_TYPES:
        return -amount
    if account_type in INVESTED_DECREASE_TYPES:
        return -amount
    return 0.0


def run() -> None:
    if not INCOME_MARKETPLACE_EMAIL or not INCOME_MARKETPLACE_PASSWORD:
        log.error("INCOME_MARKETPLACE_EMAIL and INCOME_MARKETPLACE_PASSWORD environment variables are required.")
        sys.exit(1)

    # XIRR/Cash drag (like "total" elsewhere in this repo) is a LIVE-only
    # snapshot metric (needs TODAY's real total account value as its final
    # cashflow) - not yet extended to support a REPORT_DATE-backfilled past
    # month.
    current_month = is_current_month()

    log.info("Starting Income Marketplace diversification run.")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()
            token = login(page)
            overview = fetch_investor_details(page, token)

            today_date = get_report_date()
            amounts = {
                "total": overview["invested_total"] + overview["cash_balance"],
                "gross_interest_received": 0.0,
                "net_interest_received": 0.0,
                "withholding_tax": 0.0,
                "bonus_cashback_contest": 0.0,
            }

            try:
                month_start_date = today_date.replace(day=1)
                this_month_transactions = fetch_all_statement_transactions(page, token, month_start_date, today_date)
                gross_interest = sum(t["amount"] for t in this_month_transactions if t["account_type"] == INTEREST_TYPE)
                bonus = sum(t["amount"] for t in this_month_transactions if t["account_type"] == BONUS_TYPE)
                amounts["gross_interest_received"] = gross_interest
                amounts["net_interest_received"] = gross_interest
                amounts["bonus_cashback_contest"] = bonus
                log.info(
                    "This month's statement totals: gross_interest=%.2f EUR, bonus=%.2f EUR.",
                    gross_interest, bonus,
                )
            except Exception:
                log.exception("Failed to fetch this month's statement totals - defaulting interest/bonus to 0.0.")

            # Since-inception XIRR/Cash drag block - see module docstring.
            xirr_value = None
            cash_drag_value = None
            bonus_xirr_contribution = None
            cash_drag_xirr_contribution = None
            taxes_xirr_contribution = 0.0  # no withholding-tax category exists at all, see docstring
            interest_xirr_contribution = None
            avg_invested_balance = None
            avg_non_invested_balance = None

            all_transactions = None
            try:
                all_transactions = get_cached_statement_transactions(page, token, today_date)
            except Exception:
                log.exception("Failed to fetch the since-inception statement history - XIRR/Cash drag will not be updated.")

            if all_transactions is not None:
                cash_events = []
                invested_events = []
                for t in all_transactions:
                    try:
                        t_date = _parse_transaction_date(t["date"])
                    except (KeyError, ValueError, TypeError):
                        log.warning("Skipping a statement transaction with an unparseable date: %r", t)
                        continue
                    cash_events.append((t_date, _cash_delta_for_type(t["account_type"], t["amount"])))
                    invested_events.append((t_date, _invested_delta_for_type(t["account_type"], t["amount"])))

                avg_invested_balance = compute_time_weighted_average(invested_events, month_start_date, today_date)
                avg_non_invested_balance = compute_time_weighted_average(cash_events, month_start_date, today_date)
                log.info(
                    "Solde moyen pondéré - investi: %.2f EUR, non investi: %.2f EUR (%s to %s).",
                    avg_invested_balance, avg_non_invested_balance, month_start_date, today_date,
                )

                if current_month:
                    deposit_dates = [
                        _parse_transaction_date(t["date"]) for t in all_transactions if t["account_type"] == DEPOSIT_TYPE
                    ]
                    total_invested = overview["invested_total"]
                    total_account_value = total_invested + overview["cash_balance"]

                    signed_cashflows = []
                    for t in all_transactions:
                        if t["account_type"] not in (DEPOSIT_TYPE, WITHDRAWAL_TYPE):
                            continue
                        try:
                            t_date = _parse_transaction_date(t["date"])
                        except (KeyError, ValueError, TypeError):
                            continue
                        # A deposit credits the wallet (+amount) but is a
                        # NEGATIVE cashflow for the investor (money paid in);
                        # a withdrawal debits the wallet (assumed -amount)
                        # but is a POSITIVE cashflow (money paid out) - both
                        # are simply the negation of the wallet-side delta.
                        signed_cashflows.append((t_date, -_cash_delta_for_type(t["account_type"], t["amount"])))
                    signed_cashflows.append((today_date, total_account_value))

                    xirr_value = compute_xirr(signed_cashflows)
                    if xirr_value is None:
                        log.warning("Could not compute XIRR from %d cashflow(s) - XIRR row will not be updated.", len(signed_cashflows))
                    else:
                        log.info(
                            "Computed since-inception XIRR: %.2f%% (current total value %.2f EUR).",
                            xirr_value * 100, total_account_value,
                        )

                        lifetime_bonus_total = sum(t["amount"] for t in all_transactions if t["account_type"] == BONUS_TYPE)
                        lifetime_gross_interest = sum(t["amount"] for t in all_transactions if t["account_type"] == INTEREST_TYPE)
                        lifetime_net_interest = lifetime_gross_interest  # no withholding tax, see docstring

                        if lifetime_bonus_total:
                            cashflows_without_bonus = signed_cashflows[:-1] + [(today_date, total_account_value - lifetime_bonus_total)]
                            xirr_without_bonus = compute_xirr(cashflows_without_bonus)
                            if xirr_without_bonus is not None:
                                bonus_xirr_contribution = xirr_value - xirr_without_bonus
                        else:
                            bonus_xirr_contribution = 0.0

                        if lifetime_net_interest:
                            cashflows_without_interest = signed_cashflows[:-1] + [(today_date, total_account_value - lifetime_net_interest)]
                            xirr_without_interest = compute_xirr(cashflows_without_interest)
                            if xirr_without_interest is not None:
                                interest_xirr_contribution = xirr_value - xirr_without_interest
                        else:
                            interest_xirr_contribution = 0.0

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
                                cashflows_with_cash_invested = signed_cashflows[:-1] + [(today_date, total_account_value + missed_earnings)]
                                xirr_with_cash_invested = compute_xirr(cashflows_with_cash_invested)
                                if xirr_with_cash_invested is not None:
                                    cash_drag_xirr_contribution = xirr_value - xirr_with_cash_invested
                                    log.info("XIRR share - cash drag: %.4f points.", cash_drag_xirr_contribution * 100)

            browser.close()
    except Exception:
        log.exception("Failed to log in or fetch Income Marketplace's portfolio/overview.")
        sys.exit(1)

    fill_current_month_amounts(
        platform="Income Marketplace",
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
        bonus_breakdown["XIRR Taxes/Frais"] = taxes_xirr_contribution
    if interest_xirr_contribution is not None:
        bonus_breakdown["XIRR Intérêts"] = interest_xirr_contribution
    if avg_invested_balance is not None:
        bonus_breakdown[INVESTED_BALANCE_LABEL] = avg_invested_balance
    if avg_non_invested_balance is not None:
        bonus_breakdown[NON_INVESTED_BALANCE_LABEL] = avg_non_invested_balance

    fill_current_month_bonus_breakdown(
        platform="Income Marketplace",
        breakdown=bonus_breakdown,
        max_rows=19,
    )

    if current_month:
        fill_geographic_repartition_amounts(overview["companies"], platform="Income Marketplace")
        fill_geographic_repartition_uninvested_amount("Income Marketplace", overview["cash_balance"])


if __name__ == "__main__":
    run()
