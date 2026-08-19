"""Loanch portfolio diversification (by loan originator) fetcher.

Logs into loanch.com (email/password + a 6-box Google Authenticator TOTP
code, same idea as Swaper/Lendermarket/PeerBerry) and fetches every active
investment via the site's own "Mes investissements"
(https://loanch.com/fr/dashboard/investments) API, then groups them by loan
originator and sums the currently remaining (not yet repaid) principal of
every active investment per originator. It also fetches this calendar
month's "Total des interets payes" / "Total des recompenses" from the
"Releve de compte" (https://loanch.com/fr/dashboard/statement) API - see
fetch_current_month_statement_totals() below. No email is sent - the
amounts are just logged and handed to fill_current_month_amounts() (see
google_sheet.py) so they can be filled into a Google Sheet,
mirroring afranga_diversification.py / peerberry_diversification.py /
lendermarket_diversification.py.

login() logs in via loanch.com's django-allauth "headless" browser API
(https://docs.allauth.org/en/latest/headless/), confirmed with a real
network capture on 2026-07-27 (Playwright script with
page.on("request")/page.on("response") listeners around the login form
submission, then double-checked with a plain `requests.Session()` replay -
see git history for the throwaway capture script if this ever needs to be
redone). The flow is:

1. `GET https://api.loanch.com/_allauth/browser/v1/auth/session` - always
   returns 401 for a logged-out session (expected, not an error), but its
   response sets the `csrftoken` cookie needed for step 2.
2. `POST https://api.loanch.com/_allauth/browser/v1/auth/login` with JSON
   body `{"email": ..., "password": ...}` and header
   `X-CSRFToken: <csrftoken cookie value>`. On success without 2FA, returns
   `200` with `meta.is_authenticated = true`. If 2FA is enabled, returns
   `401` with `data.flows` containing `{"id": "mfa_authenticate", "is_pending": true}`.
3. If 2FA is pending: `POST https://api.loanch.com/_allauth/browser/v1/auth/2fa/authenticate`
   with JSON body `{"code": "<TOTP code>"}` (same CSRF header). Returns
   `200` with `meta.is_authenticated = true` on success.

The data-fetching endpoints below (fetch_investments/
fetch_current_month_statement_totals) are a separate, already-verified
DRF API (`/api/v1/...`) that relies on the Django session cookie set by
the allauth login above - verified against the real account on
2026-07-09/07-10 (see their own docstrings).

API verified against the real account on 2026-07-09, in two steps:

1. `GET https://api.loanch.com/api/v1/investments?closed=false&ordering=-invested_date&page=N&page_size=100`
   (same list endpoint/filter the dashboard's own React app uses, fetched
   here via `credentials: 'include'` - no CORS/bearer-token issues, unlike
   PeerBerry) -> `{"count", "total_pages", "next", "previous", "results": [...]}`,
   one entry per still-active investment:
   `{"id": "...", "originator_name": "Tambadana", "amount": "146.46", "closed": false, ...}`.
   Paginates via `next`/`total_pages` defensively, even though
   `page_size=100` fit all of them in one page at the time of writing.

2. Importantly, this list's `amount` field is the ORIGINALLY invested
   amount for that investment - installment loans partially repay
   principal over time without the investment being marked `closed` until
   the very last installment, so summing `amount` over-counts the actually
   still-invested capital (verified: 656.79 EUR that way vs. 529.32 EUR
   shown as `total_invested` on `GET https://api.loanch.com/api/v1/dashboard`,
   the account's ground truth). The correct currently-outstanding amount is
   the `principal_left` field, only exposed on the per-investment detail
   endpoint `GET https://api.loanch.com/api/v1/investments/<id>` (no
   trailing slash - one extra request per active investment, confirmed to
   reproduce the 529.32 EUR total exactly when summed).

Since the account's session cookie (set at login) is shared across the
loanch.com/api.loanch.com subdomains (confirmed by the 2026-07-27 capture -
the `csrftoken` cookie set by api.loanch.com is readable via
`document.cookie` on loanch.com), a plain `requests.Session()` carries it
the same way the browser's own `fetch(..., {credentials: 'include'})`
calls did, as long as login() succeeds in setting it.

Also computes a since-inception XIRR (money-weighted return) plus this
month's Cash drag and the XIRR Bonus / XIRR Cash drag / XIRR Taxes/Frais
pie-chart shares, mirroring afranga_diversification.py/
swaper_diversification.py/peerberry_diversification.py's own XIRR block -
added 2026-08-14, per explicit user request. Unlike Lendermarket (no
per-transaction ledger at all), Loanch DOES expose a genuine per-transaction
dated ledger - the same one shown under "Transactions" on
https://loanch.com/fr/dashboard/statement - found via a real browser network
capture (login automated with LOANCH_EMAIL/PASSWORD/TOTP_SECRET from .env,
see the throwaway probe technique in git history if this ever needs to be
redone for another platform):

    GET https://api.loanch.com/api/v1/transaction?page=<N>&page_size=<N>&transaction_type=0&transaction_type=1&...&transaction_type=10
    -> {"count", "total_pages", "next", "previous", "results": [...]}, one
    entry per transaction, newest first: {"id", "transaction_type_display":
    "Placed Investment"|"Paid interest"|"Paid principal"|"Deposit"|
    "Withdrawal"|"Bonus", "transaction_type": <int>, "amount": "<already
    SIGNED for its cash-balance impact, no per-type sign lookup needed>",
    "date": "YYYY-MM-DD", "deposit"/"withdrawal"/"investment"/"bonus"/
    "exchange": <linked object id or null>}. transaction_type is a small
    int enum - probed live 2026-08-14 by requesting each value 0-15
    individually: 0=Deposit, 1=Withdrawal, 2=Placed Investment, 3=Paid
    principal, 6=Paid interest, 10=Bonus are the ones actually observed on
    this account (summing to the API's own `count` exactly); 4/5/7/8/9 are
    valid choices (200, not 400) but had zero occurrences on this account -
    their real label is unconfirmed, so if any of them ever appear they are
    conservatively bucketed as an unclassified Taxes/Frais-style cost/
    adjustment (same add-back-and-recompute-XIRR technique as every other
    platform's Taxes/Frais share - sign-agnostic, safe even though untested
    at 0.00, same reasoning as PeerBerry's untested INVESTMENT_SALE_FEE/
    REFERRAL_FEE). page_size is capped server-side at 200 regardless of the
    value requested (verified live: page_size=500 still only returned 200
    rows/page). No date-range filter exists on this endpoint (unlike
    PeerBerry's transactions API) - pagination is by `page` only, newest
    first, so the incremental cache below stops fetching as soon as it
    reaches an already-cached transaction id instead of always re-fetching
    the entire history.

    Since Loanch's transaction ledger is fetched back to the very first
    transaction (account inception), the daily uninvested-cash balance used
    for Cash drag is reconstructed by accumulating every entry's own signed
    `amount` starting from a true zero balance at inception - no separate
    "opening balance" API call is needed (unlike PeerBerry, which seeds
    from its account-summary API's openingBalance). Loanch's own
    statement-report opening_account/closing_account fields were
    considered as that anchor but ruled out (see the loanch-login-api repo
    memory note, 2026-08-07 investigation): they match /api/v1/dashboard's
    balance_sum, which itself equals total_invested on this platform - i.e.
    they track invested capital, not idle cash despite the generic name -
    so they're not used here.

Added 2026-08-18: XIRR Intérêts, the counterfactual XIRR share
attributable to real net interest received (mirrors
bienpreter_diversification.py's/afranga_diversification.py's/
iuvo_diversification.py's/lendermarket_diversification.py's own XIRR
Intérêts blocks). Loanch has no separate gross/withholding-tax split on
interest either (interest_paid is mapped to both
gross_interest_received/net_interest_received - see below), and its "Paid
interest" transaction type (INTEREST_TYPE) is a real, already-signed
per-transaction ledger figure (unlike the "unclassified"
Taxes/Frais-style bucket, which is untested/probably-zero) - so "lifetime
net interest" here is simply the sum of every INTEREST_TYPE transaction's
own `amount` since inception (`lifetime_interest_paid`, already computed
in the Cash drag lifetime block below - reused, not recomputed). This
exists because "Intérêts" was previously only ever a RESIDUAL on the
spreadsheet/dashboard side (XIRR - XIRR Bonus - XIRR Cash drag - XIRR
Taxes/Frais), which can legitimately go negative when the bonus's
counterfactual XIRR share is disproportionately large relative to the
account's real underlying (non-bonus) performance - that's not a bug,
it's the correct signal that the account's return is propped up almost
entirely by the bonus. XIRR Intérêts instead gives a genuine,
independently-measured figure (same category of computation as
Bonus/Taxes, not a derived leftover), so the two can be compared/sanity-
checked against each other on the sheet/dashboard side.

Required env vars:
    LOANCH_EMAIL, LOANCH_PASSWORD      -> Loanch account credentials
Optional:
    LOANCH_TOTP_SECRET                 -> base32 secret used to set up
                                           Google Authenticator, needed if
                                           2FA is enabled on the account
    GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS -> used to write this month's totals to
                                           the Google Sheet via
                                           fill_current_month_amounts() (see
                                           google_sheet.py)
"""

import calendar
import os
import sys
import time
import logging
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

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
from shared.report_date import get_report_now, is_current_month
from shared.state import load_state, save_state
from shared.xirr import compute_xirr

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("loanch_diversification")

LOGIN_PAGE_URL = "https://loanch.com/fr/login"
# Confirmed via a real network capture on 2026-07-27 - see module docstring.
API_SESSION_URL = "https://api.loanch.com/_allauth/browser/v1/auth/session"
API_LOGIN_URL = "https://api.loanch.com/_allauth/browser/v1/auth/login"
API_2FA_URL = "https://api.loanch.com/_allauth/browser/v1/auth/2fa/authenticate"
INVESTMENTS_API_URL = "https://api.loanch.com/api/v1/investments"
STATEMENT_API_URL = "https://api.loanch.com/api/v1/statement-report"
DASHBOARD_API_URL = "https://api.loanch.com/api/v1/dashboard"
TRANSACTIONS_API_URL = "https://api.loanch.com/api/v1/transaction"
PAGE_SIZE = 100
# Every valid `transaction_type` choice as of 2026-08-14 (probed live by
# requesting each value individually - 11+ all returned 400
# "invalid_choice") - see module docstring for what each observed value means.
TRANSACTION_TYPES = list(range(0, 11))
DEPOSIT_TYPE = 0
WITHDRAWAL_TYPE = 1
INTEREST_TYPE = 6
BONUS_TYPE = 10
# Server caps page_size at 200 regardless of the value requested (verified
# live 2026-08-14: page_size=500 still returned only 200 rows/page).
TRANSACTIONS_PAGE_SIZE = 200
MAX_TRANSACTIONS_PAGES = 100
# Cache of every transaction row ever fetched (see get_cached_transactions()
# below) - avoids re-fetching the account's entire history every run, same
# idea as peerberry_diversification.XIRR_CASHFLOWS_STATE_FILE.
XIRR_CASHFLOWS_STATE_FILE = Path(__file__).parent / "loanch_xirr_cashflows_state.json"
XIRR_CASHFLOWS_STATE_DEFAULT = {"all_entries": []}
# Loanch is a French platform and its "Ce mois-ci" filter means the current
# calendar month in French local time - pin the timezone explicitly instead
# of relying on the executing machine's local clock (e.g. UTC on a CI
# runner), which would compute the wrong month boundary around midnight.
REPORT_TIMEZONE = ZoneInfo("Europe/Paris")

LOANCH_EMAIL = os.environ.get("LOANCH_EMAIL")
LOANCH_PASSWORD = os.environ.get("LOANCH_PASSWORD")
LOANCH_TOTP_SECRET = os.environ.get("LOANCH_TOTP_SECRET")

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Referer": LOGIN_PAGE_URL,
    "Origin": "https://loanch.com",
}


def _get_csrf_token(session: requests.Session) -> str:
    """GET the allauth headless auth/session endpoint purely to have Django
    set the `csrftoken` cookie (its own response body/401 status is
    expected and irrelevant here - see module docstring). That cookie's
    value must be sent as the `X-CSRFToken` header on every subsequent
    unsafe (POST) request to the allauth headless API."""
    session.get(API_SESSION_URL, headers=_HEADERS, timeout=20)
    csrf_token = session.cookies.get("csrftoken")
    if not csrf_token:
        raise RuntimeError("Loanch did not set a csrftoken cookie on the auth/session request.")
    return csrf_token


def login(session: requests.Session) -> None:
    """Log in to Loanch using LOANCH_EMAIL/PASSWORD (and LOANCH_TOTP_SECRET
    if 2FA is enabled), via the confirmed django-allauth headless API flow
    (see module docstring). Raises a RuntimeError with the full response
    status/body on any unexpected shape."""
    if not LOANCH_EMAIL or not LOANCH_PASSWORD:
        raise RuntimeError("LOANCH_EMAIL/LOANCH_PASSWORD environment variables are required.")

    csrf_token = _get_csrf_token(session)
    r = session.post(
        API_LOGIN_URL,
        json={"email": LOANCH_EMAIL, "password": LOANCH_PASSWORD},
        headers={**_HEADERS, "X-CSRFToken": csrf_token},
        timeout=20,
    )
    data = r.json() if r.content else {}
    log.info("Login response body (truncated): %r", str(data)[:300])

    if r.status_code == 200 and (data.get("meta") or {}).get("is_authenticated"):
        log.info("Logged in successfully (no 2FA prompt).")
        return

    flows = (data.get("data") or {}).get("flows") or []
    mfa_pending = any(f.get("id") == "mfa_authenticate" and f.get("is_pending") for f in flows)

    if r.status_code != 401 or not mfa_pending:
        raise RuntimeError(f"Loanch login failed (status={r.status_code}): {r.text[:500]}")

    if not LOANCH_TOTP_SECRET:
        raise RuntimeError(
            "Loanch is asking for a 2FA code but LOANCH_TOTP_SECRET is not set. "
            "Set it to the base32 secret used to configure Google Authenticator."
        )

    log.info("2FA prompt detected, generating and submitting TOTP code...")
    totp = pyotp.TOTP(LOANCH_TOTP_SECRET)

    # Diagnostic only (no secret/code values logged): compare Loanch's
    # server-reported clock (Date response header) to our local clock -
    # helps distinguish a genuine clock-skew issue from a wrong
    # LOANCH_TOTP_SECRET if this still fails below.
    server_date_header = r.headers.get("Date")
    if server_date_header:
        try:
            server_time = parsedate_to_datetime(server_date_header)
            skew = (datetime.now(timezone.utc) - server_time).total_seconds()
            log.info("Clock check: local vs. Loanch server Date header skew = %.1fs", skew)
        except Exception:
            pass

    # Guard against submitting a code right as its 30s window is about to
    # roll over.
    remaining = 30 - (int(time.time()) % 30)
    if remaining < 5:
        time.sleep(remaining + 1)

    # A single totp.now() call rejected once is NOT resilient to a
    # boundary-rollover/mild clock-skew - this exact anti-pattern was
    # already found and fixed for Afranga (2026-07-18) and Lendermarket
    # (2026-07-27), but was never applied here. Try 3 distinct candidate
    # codes (current/previous/next 30s window) instead of one.
    now = time.time()
    candidates = [totp.at(now), totp.at(now - 30), totp.at(now + 30)]
    csrf_token = session.cookies.get("csrftoken") or csrf_token
    r = None
    data = {}
    for attempt, code in enumerate(candidates, start=1):
        r = session.post(
            API_2FA_URL,
            json={"code": code},
            headers={**_HEADERS, "X-CSRFToken": csrf_token},
            timeout=20,
        )
        data = r.json() if r.content else {}
        if r.status_code == 200 and (data.get("meta") or {}).get("is_authenticated"):
            break
        log.info("TOTP code rejected (attempt %d/%d)...", attempt, len(candidates))
    if r.status_code != 200 or not (data.get("meta") or {}).get("is_authenticated"):
        raise RuntimeError(f"Loanch rejected the TOTP code (status={r.status_code}): {r.text[:500]}")
    log.info("Logged in successfully (with 2FA).")


def fetch_investments(session: requests.Session) -> list:
    """Fetch every still-active (closed=false) investment across all pages
    of the investments API, then fetch each one's detail endpoint to get
    its `principal_left` (see module docstring for why the list endpoint's
    `amount` field alone isn't enough - it's the original invested amount,
    not what's still outstanding on partially-repaid installment loans)."""
    investments = []
    page_number = 1
    while True:
        log.info("Requesting investments API page %d...", page_number)
        r = session.get(
            INVESTMENTS_API_URL,
            params={
                "closed": "false",
                "ordering": "-invested_date",
                "page": page_number,
                "page_size": PAGE_SIZE,
            },
            headers=_HEADERS,
            timeout=20,
        )
        log.info("Investments API page %d response: status=%s", page_number, r.status_code)
        if not r.ok:
            raise RuntimeError(f"Investments API returned status {r.status_code} on page {page_number}")

        body = r.json() or {}
        page_investments = body.get("results") or []
        investments.extend(page_investments)
        log.info("Page %d: %d investment(s) found (running total: %d).", page_number, len(page_investments), len(investments))

        if not body.get("next") or page_number >= (body.get("total_pages") or 1):
            break
        page_number += 1

    log.info("Fetching principal_left detail for %d active investment(s)...", len(investments))
    for i, inv in enumerate(investments, start=1):
        r = session.get(f"{INVESTMENTS_API_URL}/{inv['id']}", headers=_HEADERS, timeout=20)
        if not r.ok:
            raise RuntimeError(f"Investment detail API returned status {r.status_code} for {inv['id']}")
        inv["principal_left"] = (r.json() or {}).get("principal_left")
        if i % 20 == 0 or i == len(investments):
            log.info("Fetched detail for %d/%d investment(s) so far.", i, len(investments))

    return investments


def aggregate_by_originator(investments: list) -> list:
    """Group active investments by loan originator and sum each one's
    remaining principal (principal_left) - one entry per originator, sorted
    by amount descending."""
    totals = {}
    for inv in investments:
        originator = inv.get("originator_name") or "Unknown"
        try:
            amount = float(inv.get("principal_left"))
        except (TypeError, ValueError):
            amount = 0.0
        totals[originator] = totals.get(originator, 0.0) + amount

    originators = [{"originator": name, "amount": amount} for name, amount in totals.items()]
    originators.sort(key=lambda o: o["amount"], reverse=True)
    return originators


def fetch_current_month_statement_totals(session: requests.Session) -> dict:
    """Fetch this calendar month's "Total des interets payes" and "Total
    des recompenses", as shown on
    https://loanch.com/fr/dashboard/statement, via the same
    `statement-report` API the page's own "Ce mois-ci" quick filter uses.

    Verified against the real account on 2026-07-10, in two ways:

    1. Network capture while clicking "Ce mois-ci" on the statement page
       showed it requests
       `GET .../statement-report?start_date=2026-07-01&end_date=2026-07-31`
       - i.e. the entire calendar month (first day to last day), not
       "start of month to today" as one might assume - so this reproduces
       that exact range instead.
    2. The response's `total_interest` (1.59) and `total_bonus` (0) fields
       matched the "Total des interets payes" / "Total des recompenses"
       figures shown on the page exactly for July 2026.

    The full response also has `opening_account`/`closing_account`,
    `opening_portfolio`/`closing_portfolio`, `total_profit_start/end`,
    `total_deposited`, `total_withdrawn`, `total_invested` (new money
    invested during the period, not a running total) and `total_principal`
    (principal repaid during the period) - checked live 2026-08-07 to see
    if any of them could fill in a backfilled past month's "total"
    (currently skip_total, unlike Monefit/Go & Grow/PeerBerry - see
    shared/report_date.py): none of them is the currently-outstanding
    invested principal per loan originator (what run()'s "total" needs).
    `closing_portfolio` looked like the best candidate but does NOT match
    even for the CURRENT still-open month (4667.86 vs the live
    sum(principal_left)/`/api/v1/dashboard`'s `total_invested` of
    2029.93) - it tracks cumulative portfolio activity, not the
    currently-outstanding balance. `closing_account` instead matches
    `/api/v1/dashboard`'s `balance_sum` (cash-side figure, not the
    invested-principal one). Loanch has no endpoint exposing "outstanding
    invested principal as of an arbitrary past date" - only the live-only
    `/api/v1/dashboard` has it for today - so "total" keeps being skipped
    for a backfilled month.

    Uses REPORT_TIMEZONE (Europe/Paris) rather than the executing machine's
    local clock to decide what "this month" means, so this stays correct
    regardless of where/when (e.g. a UTC CI runner around midnight) this
    script actually runs.
    """
    now = get_report_now(REPORT_TIMEZONE)
    start_date = now.replace(day=1).strftime("%Y-%m-%d")
    last_day_of_month = calendar.monthrange(now.year, now.month)[1]
    end_date = now.replace(day=last_day_of_month).strftime("%Y-%m-%d")
    log.info("Requesting statement-report API for %s to %s...", start_date, end_date)

    r = session.get(
        STATEMENT_API_URL,
        params={"start_date": start_date, "end_date": end_date},
        headers=_HEADERS,
        timeout=20,
    )
    log.info("Statement-report API response: status=%s", r.status_code)
    if not r.ok:
        raise RuntimeError(f"Statement report API returned status {r.status_code}")

    body = r.json() or {}
    log.info("Raw statement-report body: %r", body)
    try:
        interest_paid = float(body.get("total_interest") or 0.0)
    except (TypeError, ValueError):
        log.warning("Could not parse 'total_interest' %r - defaulting to 0.0.", body.get("total_interest"))
        interest_paid = 0.0
    try:
        rewards = float(body.get("total_bonus") or 0.0)
    except (TypeError, ValueError):
        log.warning("Could not parse 'total_bonus' %r - defaulting to 0.0.", body.get("total_bonus"))
        rewards = 0.0

    log.info("Parsed statement totals: interest_paid=%.2f, rewards=%.2f", interest_paid, rewards)
    return {
        "interest_paid": interest_paid,
        "rewards": rewards,
    }


def fetch_dashboard_uninvested_balance(session: requests.Session) -> float:
    """Fetch the account's uninvested cash balance ("non investi") from
    `GET /api/v1/dashboard`'s `total_balance` field.

    Verified live 2026-08-10: the dashboard response is
    `{"total_deposit", "total_withdrawal", "inprocess_withdrawal",
    "total_balance", "total_invested", "paid_interest", "balance_sum",
    "total_bonus"}` - `total_balance` (0 on the real account tested, fully
    invested at the time) is used here rather than `balance_sum` (which
    matched `total_invested` exactly on that same call - i.e. it tracks
    invested capital, not idle cash, despite its generic-sounding name).
    """
    r = session.get(DASHBOARD_API_URL, headers=_HEADERS, timeout=20)
    if not r.ok:
        raise RuntimeError(f"Dashboard API returned status {r.status_code}")
    body = r.json() or {}
    try:
        return float(body.get("total_balance") or 0.0)
    except (TypeError, ValueError):
        raise RuntimeError(f"Could not parse 'total_balance' out of {body!r}.")


def fetch_transactions_page(session: requests.Session, page_number: int) -> dict:
    """Fetch one page of the account's own transaction-ledger API (see
    module docstring for the verified request/response shape)."""
    params = [("page", page_number), ("page_size", TRANSACTIONS_PAGE_SIZE)]
    params += [("transaction_type", t) for t in TRANSACTION_TYPES]
    r = session.get(TRANSACTIONS_API_URL, params=params, headers=_HEADERS, timeout=20)
    if not r.ok:
        raise RuntimeError(f"Transaction API returned status {r.status_code} on page {page_number}")
    return r.json() or {}


def get_cached_transactions(session: requests.Session) -> list:
    """Return every transaction row since account inception, fetching from
    the transaction API only the newest pages not already cached locally
    (in XIRR_CASHFLOWS_STATE_FILE) - unlike PeerBerry's equivalent, this
    endpoint has no date-range filter, only `page` (newest first), so this
    stops paginating as soon as it encounters an id already present in the
    cache instead of always walking the entire history.
    """
    state = load_state(XIRR_CASHFLOWS_STATE_FILE, XIRR_CASHFLOWS_STATE_DEFAULT)
    cached_entries = state.get("all_entries") or []
    cached_ids = {entry.get("id") for entry in cached_entries}

    log.info("Found %d cached transaction(s) - fetching newest page(s) until an already-cached one is seen...", len(cached_entries))
    new_entries = []
    page_number = 1
    reached_cached_entry = False
    while page_number <= MAX_TRANSACTIONS_PAGES:
        body = fetch_transactions_page(session, page_number)
        page_entries = body.get("results") or []
        log.info("Page %d: %d entrie(s) found.", page_number, len(page_entries))
        for entry in page_entries:
            if entry.get("id") in cached_ids:
                reached_cached_entry = True
                break
            new_entries.append(entry)
        if reached_cached_entry or not body.get("next"):
            break
        page_number += 1
    else:
        log.warning("Hit MAX_TRANSACTIONS_PAGES (%d) without reaching a cached entry or the end of the ledger - it may be incomplete.", MAX_TRANSACTIONS_PAGES)

    seen = set()
    merged = []
    for entry in new_entries + cached_entries:
        key = entry.get("id")
        if key in seen:
            continue
        seen.add(key)
        merged.append(entry)

    save_state(XIRR_CASHFLOWS_STATE_FILE, {"all_entries": merged})
    log.info("Transaction ledger cache now holds %d entrie(s) (was %d before this run, %d new).", len(merged), len(cached_entries), len(new_entries))
    return merged


def compute_average_idle_cash(entries: list, start_date: str, end_date: str) -> float:
    """Reconstruct the uninvested-cash/wallet balance for every day up to
    end_date from the raw transaction rows (every entry's own `amount` is
    already signed for its real cash-balance impact - see module
    docstring, no per-type sign lookup needed) and return the day-weighted
    average over [start_date, end_date].

    Unlike PeerBerry's equivalent, this doesn't need an `opening_balance`
    seed fetched from another API - the ledger is fetched back to account
    inception (see get_cached_transactions()), so the running balance
    simply starts at 0 on the day of the very first transaction.
    """
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        end = datetime.strptime(end_date, "%Y-%m-%d").date()
    except ValueError:
        return 0.0

    daily_deltas: dict = {}
    for entry in entries:
        raw_date = entry.get("date")
        raw_amount = entry.get("amount")
        if not raw_date or raw_amount is None:
            continue
        try:
            entry_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except ValueError:
            continue
        if entry_date > end:
            continue
        try:
            amount = float(raw_amount)
        except (TypeError, ValueError):
            continue
        daily_deltas[entry_date] = daily_deltas.get(entry_date, 0.0) + amount

    if not daily_deltas:
        return 0.0

    running_balance = 0.0
    total_balance = 0.0
    day_count = 0
    current = min(daily_deltas)
    while current <= end:
        running_balance += daily_deltas.get(current, 0.0)
        if current >= start:
            total_balance += running_balance
            day_count += 1
        current += timedelta(days=1)

    if day_count == 0:
        return running_balance
    return total_balance / day_count


def run() -> None:
    if not LOANCH_EMAIL or not LOANCH_PASSWORD:
        log.error("LOANCH_EMAIL and LOANCH_PASSWORD environment variables are required.")
        sys.exit(1)

    # XIRR (like "total" elsewhere in this repo) is a LIVE-only snapshot
    # metric (needs TODAY's real total account value as its final
    # cashflow) - only ever computed/written for the real current month,
    # same convention as Afranga/Swaper/Lendermarket/PeerBerry.
    current_month = is_current_month()
    today_date = get_report_now(REPORT_TIMEZONE).date()

    log.info("Starting Loanch diversification run (pure HTTP, no browser - see module docstring for the login() flow).")

    session = requests.Session()
    try:
        login(session)
        investments = fetch_investments(session)
    except Exception:
        log.exception("Failed to log in or fetch Loanch investments.")
        sys.exit(1)

    try:
        log.info("Fetching this month's statement totals...")
        statement_totals = fetch_current_month_statement_totals(session)
    except Exception:
        log.exception("Failed to fetch this month's interest paid/rewards - defaulting both to 0.0.")
        statement_totals = {"interest_paid": 0.0, "rewards": 0.0}

    originators = aggregate_by_originator(investments)
    log.info("Fetched %d active investment(s) across %d loan originator(s).", len(investments), len(originators))
    for o in originators:
        log.info("  %s: %.2f EUR", o["originator"], o["amount"])

    log.info(
        "This month's statement totals: interest_paid=%.2f EUR, rewards=%.2f EUR",
        statement_totals["interest_paid"], statement_totals["rewards"],
    )

    try:
        uninvested_balance = fetch_dashboard_uninvested_balance(session)
    except Exception:
        log.exception("Failed to fetch Loanch's uninvested balance - 'total' will be invested-only, 'non investi' will not be updated.")
        uninvested_balance = None

    total_invested = sum(o["amount"] for o in originators)

    # Since-inception XIRR (money-weighted return) + this month's Cash drag
    # + the XIRR Bonus/Cash drag/Taxes-Frais/Intérêts pie-chart shares - see
    # module docstring for the real per-transaction ledger this is built
    # from.
    all_entries = None
    if current_month:
        try:
            log.info("Fetching the since-inception transaction ledger (cached where possible)...")
            all_entries = get_cached_transactions(session)
        except Exception:
            log.exception("Failed to fetch the transaction ledger - XIRR/Cash drag will not be updated.")
            all_entries = None

    def _entry_date(entry: dict):
        raw = entry.get("date")
        if not raw:
            return None
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
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
    since_inception_date = None
    lifetime_bonus = 0.0
    if current_month and all_entries and uninvested_balance is not None:
        total_account_value = total_invested + uninvested_balance
        signed_cashflows = []
        deposit_dates = []
        for entry in all_entries:
            entry_date = _entry_date(entry)
            ttype = entry.get("transaction_type")
            if entry_date is None or ttype not in (DEPOSIT_TYPE, WITHDRAWAL_TYPE):
                continue
            # `amount` is already signed for its cash-balance impact
            # (Deposit positive, Withdrawal negative) - negate it for
            # XIRR's own convention (money going INTO the platform is a
            # negative cashflow, money coming back OUT is positive).
            signed_cashflows.append((entry_date, -_entry_amount(entry)))
            if ttype == DEPOSIT_TYPE:
                deposit_dates.append(entry_date)

        since_inception_date = min(deposit_dates) if deposit_dates else None
        lifetime_bonus = sum(_entry_amount(e) for e in all_entries if e.get("transaction_type") == BONUS_TYPE)

        signed_cashflows.append((today_date, total_account_value))

        xirr_value = compute_xirr(signed_cashflows)
        if xirr_value is None:
            log.warning("Could not compute XIRR from %d cashflow(s) - XIRR row will not be updated.", len(signed_cashflows) - 1)
        else:
            log.info(
                "Computed since-inception XIRR: %.2f%% (%d deposit/withdrawal cashflow(s), current total value %.2f EUR).",
                xirr_value * 100, len(signed_cashflows) - 1, total_account_value,
            )

            if lifetime_bonus:
                cashflows_without_bonus = signed_cashflows[:-1] + [(today_date, total_account_value - lifetime_bonus)]
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
    # afranga_diversification.py's/iuvo_diversification.py's/
    # lendermarket_diversification.py's own XIRR Intérêts blocks):
    # counterfactual XIRR share attributable to real net interest received
    # since inception. Loanch has no separate withholding tax on interest
    # (interest_paid maps to both gross/net above), so "lifetime net
    # interest" here is just `lifetime_interest_paid`, computed below in
    # the Cash drag lifetime block (reused, not recomputed).
    interest_xirr_contribution = None
    if current_month and total_invested > 0 and all_entries is not None:
        month_start_str = today_date.replace(day=1).strftime("%Y-%m-%d")
        today_str = today_date.strftime("%Y-%m-%d")
        avg_idle_cash_this_month = compute_average_idle_cash(all_entries, month_start_str, today_str)
        cash_weight = avg_idle_cash_this_month / (avg_idle_cash_this_month + total_invested)
        monthly_yield_rate = statement_totals["interest_paid"] / total_invested
        cash_drag_value = cash_weight * monthly_yield_rate
        log.info(
            "Computed Cash drag: %.2f%% (avg idle cash %.2f EUR, cash weight %.2f%%, monthly yield %.2f%%).",
            cash_drag_value * 100, avg_idle_cash_this_month, cash_weight * 100, monthly_yield_rate * 100,
        )

        if xirr_value is not None and signed_cashflows is not None and since_inception_date is not None:
            avg_idle_cash_lifetime = compute_average_idle_cash(all_entries, since_inception_date.strftime("%Y-%m-%d"), today_str)
            cash_weight_lifetime = avg_idle_cash_lifetime / (avg_idle_cash_lifetime + total_invested)
            lifetime_interest_paid = sum(_entry_amount(e) for e in all_entries if e.get("transaction_type") == INTEREST_TYPE)
            lifetime_yield_rate = lifetime_interest_paid / total_invested
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

            # transaction_type not in the confirmed set (see module
            # docstring - 4/5/7/8/9 were never observed on this account) is
            # conservatively bucketed here as an unclassified Taxes/Frais-
            # style cost/adjustment.
            known_types = (DEPOSIT_TYPE, WITHDRAWAL_TYPE, 2, 3, INTEREST_TYPE, BONUS_TYPE)
            lifetime_unclassified = sum(_entry_amount(e) for e in all_entries if e.get("transaction_type") not in known_types)
            if lifetime_unclassified:
                cashflows_with_fees_cancelled = signed_cashflows[:-1] + [(today_date, total_account_value - lifetime_unclassified)]
                xirr_with_fees_cancelled = compute_xirr(cashflows_with_fees_cancelled)
                if xirr_with_fees_cancelled is not None:
                    taxes_xirr_contribution = xirr_value - xirr_with_fees_cancelled
                    log.info("XIRR share - taxes/frais: %.4f points (lifetime unclassified amount %.2f EUR).", taxes_xirr_contribution * 100, lifetime_unclassified)
            else:
                taxes_xirr_contribution = 0.0

            # XIRR Intérêts (added 2026-08-18): same counterfactual pattern
            # as XIRR Bonus/XIRR Taxes-Frais above - lifetime_interest_paid
            # (real "Paid interest" transactions since inception, already
            # summed above for Cash drag's lifetime yield rate) is reused
            # here rather than recomputed.
            if lifetime_interest_paid:
                cashflows_without_interest = signed_cashflows[:-1] + [(today_date, total_account_value - lifetime_interest_paid)]
                xirr_without_interest = compute_xirr(cashflows_without_interest)
                if xirr_without_interest is not None:
                    interest_xirr_contribution = xirr_value - xirr_without_interest
                    log.info(
                        "XIRR share - intérêts: %.4f points (lifetime net interest %.2f EUR, no withholding tax on Loanch).",
                        interest_xirr_contribution * 100, lifetime_interest_paid,
                    )
            else:
                interest_xirr_contribution = 0.0

    # Loanch's statement-report API has no gross/net/withholding-tax
    # breakdown (unlike Afranga/Bienpreter) - interest_paid is mapped to
    # both gross_interest_received/net_interest_received since it's the
    # only real figure on hand, withholding_tax defaults to 0.0. Same
    # standardized dict shape as every other *_diversification.py, plus the
    # platform-specific interest_paid/rewards fields kept alongside it.
    # "rewards" (total_bonus, "Total des recompenses") was already
    # fetched but only kept as a platform-specific extra field, never
    # surfaced under the standardized name - dissociated here too via
    # bonus_cashback_contest so it's ready to be written to its own Sheet
    # cell, separate from interest.
    # "total" ("en cours") written to the Sheet is invested + uninvested,
    # per user request 2026-08-14 (matching Bienprêter/Iuvo/Bricks/Lande's
    # own convention) - falls back to invested-only if the uninvested
    # balance couldn't be fetched.
    amounts = {
        "total": total_invested + uninvested_balance if uninvested_balance is not None else total_invested,
        "gross_interest_received": statement_totals["interest_paid"],
        "net_interest_received": statement_totals["interest_paid"],
        "withholding_tax": 0.0,
        "bonus_cashback_contest": statement_totals["rewards"],
        "interest_paid": statement_totals["interest_paid"],
        "rewards": statement_totals["rewards"],
    }

    fill_current_month_amounts(
        platform="Loanch",
        amounts=amounts,
        skip_total=not current_month,
    )

    # Loanch's "total_bonus" ("rewards") is a promotional/referral reward -
    # a "prime", not a cashback/concours - written to its own dedicated
    # sub-row, never to the "Bonus" row itself (a SUM formula over
    # prime/cashback/concours). "XIRR"/"Cash drag" and the XIRR
    # Bonus/Cash drag/Taxes-Frais/Intérêts pie-chart shares (rows already
    # added by the user, mirroring Afranga/Swaper/Lendermarket/PeerBerry's
    # own blocks) are appended past the default max_rows=6 bound - only
    # included when actually computed.
    # UPDATED 2026-08-18: "XIRR Intérêts" sits right after "XIRR
    # Taxes/Frais" (mirrors Bienprêter's/Afranga's/Iuvo's/Lendermarket's
    # own block layout) - this pushes the block one row taller than it was
    # before, so `max_rows` is bumped 14 -> 15 to keep the search bounded
    # before the next platform block. IMPORTANT: a "XIRR Intérêts" row must
    # exist in the Loanch block on the sheet itself (right after "XIRR
    # Taxes/Frais") for this new value to actually land somewhere - this
    # script fills an existing row by label, it doesn't insert new
    # labelled rows into this block.
    bonus_breakdown = {"prime": statement_totals["rewards"]}
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
        platform="Loanch",
        breakdown=bonus_breakdown,
        max_rows=15,
    )

    loan_originators = [
        {"name": o["originator"], "amount": o["amount"]}
        for o in originators
    ]

    if current_month:
        fill_geographic_repartition_amounts(loan_originators, platform="Loanch")

        if uninvested_balance is not None:
            fill_geographic_repartition_uninvested_amount("Loanch", uninvested_balance)


if __name__ == "__main__":
    run()