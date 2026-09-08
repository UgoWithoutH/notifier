"""Bricks (app.bricks.co) portfolio balance fetcher.

Same family as bienpreter_diversification.py / monefit_diversification.py:
Bricks is a French real-estate crowdfunding platform (immobilier), not
broken down by loan originator here - per the user's request this just
logs in and reads two figures:
  - "Investissements en cours" (currently invested capital)
  - "Solde total" (cash wallet balance = "Solde principal" + "Solde
    cadeau")
and hands total = investissements_en_cours + solde_total to
fill_current_month_amounts() - same pattern as
bienpreter_diversification.py (which similarly sums two dashboard
figures into one total), mirroring every other *_diversification.py.

REWRITTEN 2026-07-17 to use plain `requests` instead of Playwright (no
browser at all), the same technique used for
goandgrow_diversification.py. Historical context (kept for anyone reading
this later): earlier testing (2026-07-16) found that a plain non-browser
HTTP client (urllib) hitting api.bricks.co got blocked by Cloudflare (HTTP
403, "error code: 1010", Bot Fight Mode) on an OPTIONS CORS-preflight
request specifically - this led to a whole workaround (BRICKS_STORAGE_
STATE_B64: seed a Playwright storage_state exported from a real manual
login, since Playwright itself worked fine from the user's home network
but NOT from GitHub Actions' sign-in attempt, which also got "Failed to
fetch" errors matching that same Cloudflare block). Re-tested 2026-07-17
with a real Python `requests` session (not urllib, not an OPTIONS
preflight - a direct POST, which is what actually matters since browsers
only send an OPTIONS preflight for genuinely cross-origin requests, not
what a same-machine non-browser script needs to send) and it went through
cleanly: `POST api.bricks.co/api/auth/sign-in/email` returned a normal 200
with a real session cookie (`__Secure-better-auth.session_token`, 7-day
`Max-Age`), and every authenticated endpoint tested worked immediately
after. Whatever was blocking the OPTIONS preflight either doesn't apply to
a direct POST, or Cloudflare's rule has changed/relaxed since - either way,
this is now CONFIRMED working end-to-end against the real account, so the
Playwright + BRICKS_STORAGE_STATE_B64 workaround is no longer needed and
has been removed (see .github/workflows/diversification.yml). If this
platform ever gets blocked again for the plain HTTP approach too, revisit
this docstring before assuming a Playwright-based fix is the only option -
try a plain `requests` session again first, it may just work.

Auth mechanism: `POST https://api.bricks.co/api/auth/sign-in/email` with
JSON body `{"email": ..., "password": ...}` sets a
`__Secure-better-auth.session_token` cookie (a "better-auth" library
session, HttpOnly/Secure/SameSite=None) - `requests.Session()`'s cookie
jar carries it automatically on every subsequent request to
api.bricks.co, no bearer-token/header wiring needed. No session
persistence across runs is implemented (same design choice as
goandgrow_diversification.py) - logging in fresh every run is cheap and
avoids ever needing to think about cookie/session expiry.

Data sources (both real JSON APIs, found 2026-07-17 by downloading the
Expo/React-Native-Web SPA's JS bundles - `https://app.bricks.co/` links to
`/_expo/static/js/web/index-<hash>.js`, filename may change on future
redeploys, re-fetch the homepage HTML to find the current one if this
404s later - and grepping for `investor/` API path string literals):
  - `GET https://api.bricks.co/investor/portfolio/wealth/home-metrics` ->
    `{"portfolioCurrentValue": <cents>, "balanceAvailable": <cents>,
    "giftBalance": <cents>}` - this is exactly the "Patrimoine" widget's
    data (verified 2026-07-17 against the real account: portfolioCurrentValue
    = 308753 cents = 3087.53 EUR "Investissements en cours",
    balanceAvailable = 39973 cents = 399.73 EUR "Solde principal",
    giftBalance = 27 cents = 0.27 EUR "Solde cadeau" - total =
    3087.53 + 399.73 + 0.27 = 3487.53 EUR, matching the exact total
    verified via DOM-scraping in an earlier session). `solde_total` =
    balanceAvailable + giftBalance (mirrors the DOM widget's own "Solde
    total" = "Solde principal" + "Solde cadeau" breakdown).
  - `GET https://api.bricks.co/investor/portfolio/revenue?startDate=
    <yyyy-mm>&endDate=<yyyy-mm>` (MONTH granularity, unlike every other
    platform's day-level "1st of month to today" - using the current
    month for both start/end gives month-to-date) - same endpoint/shape
    already verified in earlier sessions (see fetch_current_month_revenue_totals()
    below), now just called directly via `requests` instead of through
    `page.evaluate(fetch(...))`.

Login is a real 401 (`{"message": "Invalid email or password", "code":
"INVALID_EMAIL_OR_PASSWORD"}`) on wrong credentials, not a Cloudflare
block page - handled as a normal auth failure.

IMPORTANT (2026-07-15, still applies): fill_current_month_amounts() IS
called, despite a known Sheet layout issue the user was made aware of and
chose to accept: the "Bricks" row (under the "Crowdfunding immobilier"
section) has NO blank spacer row below it like every other platform - the
very next row is "Bourse" (a different, unrelated section).
fill_current_month_amounts() always writes the platform's own row (total)
AND the row directly below it (gross_interest_received) at the
current-month column - here that second write lands on Bourse's row
instead of a spacer. The user was asked twice whether to insert a blank
row first and explicitly said not to worry about it and to just call the
Sheet function anyway - don't "fix" this by skipping the Sheet call again
without being asked.

Added 2026-09-07: Cash drag/XIRR/XIRR Bonus/XIRR Cash drag/XIRR Taxes/Frais/
XIRR Interets, mirroring the same block already built for Afranga/Swaper/
Lendermarket/PeerBerry/Loanch/Mintos/Lande - per explicit user request,
same corrections as afranga_diversification.py's 2026-09-07 backfill-month
fix (see that module's docstring), applied here from scratch. Unlike
Afranga (separate Details/Summary endpoints) or Loanch (a transaction-type
enum ledger), Bricks exposes a SINGLE unified per-transaction wallet
ledger, found via a real browser network capture (Playwright, logged in
with BRICKS_EMAIL/PASSWORD) while opening the "Mon solde" ("wallet") page:

    GET https://api.bricks.co/wallet-transactions?cursor=<offset>&take=50
    -> {"data": [{"id", "kind", "createdAt": "<ISO8601>", "status":
    confirmed/canceled/declined, "value": <cents, SIGNED for its real
    wallet-cash impact>, "giftBalanceChange": <cents>, "propertyId"/
    "propertyName": <only for property-linked kinds>, ...}], "cursor":
    <next page's cursor>, "size": 0}. `take` silently caps at 50
    server-side (2000 tested, still returned 50) - pagination is by
    `cursor` (a simple forward offset) only, newest-first, no date-range
    filter (same limitation as Loanch's transaction API) - the incremental
    cache below stops as soon as an already-cached id is seen.

    Every `kind` observed by paginating the ENTIRE account history (332
    transactions, 2025-04-16 to today) and reconciling against the live
    home-metrics API (see fetch_balances() above) - reconstructing BOTH
    the invested principal ("Investissements en cours") AND the wallet
    cash balance ("Solde total") from ONLY the confirmed rows' `value`
    field matched the live API EXACTLY (3060.34 EUR / 441.96 EUR),
    confirming the classification below is complete and correct:
      - "primary_purchase_with_refund" (confirmed only - status
        "canceled" means the purchase was refunded and net-zero, no
        separate refund transaction ever appears in the ledger) -
        INVESTMENT: value negative (wallet -> project), outstanding
        increases by -value.
      - "obligation_principal_repayment_partial"/"_final" - REPAYMENT:
        value positive (project -> wallet), outstanding decreases by
        -value (same formula as investment, just the opposite sign of
        value).
      - "topup_wire"/"topup_card" (confirmed only - "declined" card
        top-ups never reached the wallet) - EXTERNAL deposit cashflow
        for XIRR (value positive).
      - "withdrawal" - EXTERNAL withdrawal cashflow for XIRR (value
        negative).
      - "recurring_revenue" - gross interest received (gross, i.e.
        BEFORE tax - matches the existing revenue endpoint's
        obligationCoupons.untaxedTotal).
      - "withholding_tax" - tax withheld on interest (value negative,
        stored here as a positive amount, same sign convention as
        Afranga's own withholding_tax).
      - "boosted_balance_gain"/"refer_referee" - bonus/referral income
        (matches the existing revenue endpoint's boostedBalanceGain/
        referrals totals exactly).
      - Every kind above only ever affects the WALLET cash balance via
        its own `value` (no separate direction lookup needed, unlike
        Afranga's CSS-class-based direction-in/out) -
        _wallet_balance_as_of()/compute_average_idle_cash() below simply
        sum `value` for every CONFIRMED row regardless of kind.

    Because the ledger is fetched back to account inception (like
    Loanch), reconstruct_outstanding()/_wallet_balance_as_of() start
    accumulating from a true 0, and - UNLIKE every other platform in this
    repo - the reconstructed total (outstanding + wallet balance) can
    ALSO stand in for a BACKFILLED month's "total" (see run() below):
    home-metrics is a live-only endpoint with no historical equivalent,
    but since reconstructing from the ledger matches it exactly for
    today, it's used as a genuine historical total instead of Bricks'
    previous skip_total=True lock for any non-current month.

Required env vars:
    BRICKS_EMAIL, BRICKS_PASSWORD       -> Bricks account credentials
Optional:
    GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS  -> used to write this month's
                                            totals to the Google Sheet via
                                            fill_current_month_amounts()/
                                            fill_current_month_bonus_breakdown()
                                            (see google_sheet.py)
"""

import os
import sys
import time
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

import requests

try:
    from shared.google_sheet import (
        fill_current_month_amounts,
        fill_current_month_bonus_breakdown,
        fill_geographic_repartition_amounts,
        fill_geographic_repartition_uninvested_amount,
    )
    from shared.report_date import get_report_now, is_current_month
except ModuleNotFoundError:
    # Support direct execution (python diversification/bricks_diversification.py)
    # where the project root may not be on sys.path.
    project_root = Path(__file__).resolve().parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    from shared.google_sheet import (
        fill_current_month_amounts,
        fill_current_month_bonus_breakdown,
        fill_geographic_repartition_amounts,
        fill_geographic_repartition_uninvested_amount,
    )
    from shared.report_date import get_report_now, is_current_month

from shared.state import load_state, save_state
from shared.weighted_average import compute_time_weighted_average
from shared.xirr import compute_xirr

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bricks_diversification")

SIGNIN_URL = "https://api.bricks.co/api/auth/sign-in/email"
HOME_METRICS_URL = "https://api.bricks.co/investor/portfolio/wealth/home-metrics"
REVENUE_API_URL = "https://api.bricks.co/investor/portfolio/revenue"
WALLET_TRANSACTIONS_URL = "https://api.bricks.co/wallet-transactions"
# Server-side cap, confirmed live (requesting take=2000 still only returned 50).
WALLET_TRANSACTIONS_PAGE_SIZE = 50
MAX_WALLET_TRANSACTIONS_PAGES = 200
# Incremental cache of every wallet-transactions row ever fetched (since
# account inception) - same idea as loanch_diversification.XIRR_CASHFLOWS_STATE_FILE.
XIRR_CASHFLOWS_STATE_FILE = Path(__file__).parent / "bricks_xirr_cashflows_state.json"
XIRR_CASHFLOWS_STATE_DEFAULT = {"all_entries": []}

# wallet-transactions "kind" classification - see module docstring for how
# each was verified (live reconciliation against home-metrics' own
# portfolioCurrentValue/balanceAvailable+giftBalance).
_DEPOSIT_KINDS = {"topup_wire", "topup_card"}
_WITHDRAWAL_KIND = "withdrawal"
_OUTSTANDING_KINDS = {
    "primary_purchase_with_refund", "obligation_principal_repayment_partial", "obligation_principal_repayment_final",
}
_INTEREST_KIND = "recurring_revenue"
_TAX_KIND = "withholding_tax"
_BONUS_KINDS = {"boosted_balance_gain", "refer_referee"}
# Sentinel "since the dawn of time" start date for lifetime/since-inception
# range sums below - the ledger itself never has anything before account
# inception, so this is just a convenient lower bound.
_LEDGER_EPOCH = date(2000, 1, 1)
# Bricks' revenue endpoint is aggregated by MONTH (not day like every other
# platform's equivalent) - using the current month for both startDate/endDate
# gives month-to-date totals. Pinned explicitly rather than relying on the
# executing machine's local clock (e.g. UTC on a CI runner), same pattern as
# every other *_diversification.py's REPORT_TIMEZONE.
REPORT_TIMEZONE = ZoneInfo("Europe/Paris")

BRICKS_EMAIL = os.environ.get("BRICKS_EMAIL")
BRICKS_PASSWORD = os.environ.get("BRICKS_PASSWORD")


def login(session: requests.Session) -> None:
    """Log in to Bricks via a direct POST to its sign-in API (no browser -
    see module docstring). Sets the `__Secure-better-auth.session_token`
    cookie on `session` for all subsequent authenticated requests."""
    log.info("Signing in to Bricks as %s...", BRICKS_EMAIL)
    resp = session.post(SIGNIN_URL, json={"email": BRICKS_EMAIL, "password": BRICKS_PASSWORD}, timeout=15)
    log.info("Sign-in response: status=%s", resp.status_code)
    if resp.status_code != 200:
        raise RuntimeError(f"Bricks login failed: HTTP {resp.status_code} - {resp.text[:300]}")
    if "__Secure-better-auth.session_token" not in session.cookies.get_dict():
        raise RuntimeError("Bricks login did not return a session cookie - response: " + resp.text[:300])
    log.info("Logged in successfully.")


def fetch_balances(session: requests.Session) -> dict:
    """Fetch "Investissements en cours" / "Solde total" (and its "Solde
    principal"/"Solde cadeau" breakdown) via the portfolio wealth
    home-metrics API - see module docstring for the verified field
    mapping."""
    log.info("Requesting Bricks portfolio wealth home-metrics...")
    resp = session.get(HOME_METRICS_URL, timeout=15)
    log.info("Home-metrics response: status=%s", resp.status_code)
    if resp.status_code == 401:
        # Seen on at least one other account: a valid, just-established session
        # can still 401 here once - log details and retry once after a short
        # pause in case it's a server-side session-propagation race.
        log.warning(
            "Home-metrics returned 401 right after a successful login - headers=%r body=%s. "
            "Retrying once after a short pause.",
            {k: v for k, v in resp.headers.items() if k.lower() in ("cf-ray", "server", "content-type")},
            resp.text[:500],
        )
        time.sleep(3)
        resp = session.get(HOME_METRICS_URL, timeout=15)
        log.info("Home-metrics retry response: status=%s", resp.status_code)
    if resp.status_code != 200:
        raise RuntimeError(
            f"Bricks home-metrics endpoint returned status {resp.status_code} - body: {resp.text[:500]}"
        )

    data = resp.json()
    log.info("Raw home-metrics payload: %r", data)

    try:
        investments_en_cours = round(float(data.get("portfolioCurrentValue") or 0) / 100, 2)
    except (TypeError, ValueError):
        raise RuntimeError(f"Could not parse 'portfolioCurrentValue' out of {data.get('portfolioCurrentValue')!r}.")
    try:
        solde_principal = round(float(data.get("balanceAvailable") or 0) / 100, 2)
    except (TypeError, ValueError):
        raise RuntimeError(f"Could not parse 'balanceAvailable' out of {data.get('balanceAvailable')!r}.")
    try:
        solde_cadeau = round(float(data.get("giftBalance") or 0) / 100, 2)
    except (TypeError, ValueError):
        log.warning("Could not parse 'giftBalance' %r - defaulting to 0.0.", data.get("giftBalance"))
        solde_cadeau = 0.0

    solde_total = round(solde_principal + solde_cadeau, 2)

    return {
        "investments_en_cours": investments_en_cours,
        "solde_total": solde_total,
        "solde_principal": solde_principal,
        "solde_cadeau": solde_cadeau,
    }


def fetch_current_month_revenue_totals(session: requests.Session) -> dict:
    """Fetch this calendar month's gross/net interest received and
    withholding tax from the same JSON API the "Suivi" > "Revenus" page
    (https://app.bricks.co/portfolio/revenues) itself calls - that page's
    whole UI is rendered on a react-native-skia canvas (no accessible DOM
    text to scrape at all, unlike the Accueil page), so this replays the
    request directly instead.

    Verified against the real account on 2026-07-15 (see module docstring
    for the full reconciliation, incl. cross-checking the full-history
    range against the Accueil page's "Revenus ... perçus depuis le début"
    = 527.61 EUR, exact match): `GET .../investor/portfolio/revenue?
    startDate=<yyyy-mm>&endDate=<yyyy-mm>` (MONTH granularity - the current
    month for both gives month-to-date) returns `{"revenuesTotal":
    {"untaxedTotal": <cents>, "taxedTotal": <cents>, "revenues": {
    "referrals": {"total": <cents>}, "boostedBalanceGain": {"total":
    <cents>}, "obligationCoupons": {"untaxedTotal": <cents>, "taxedTotal":
    <cents>, ...}, ...}}}`.

    Per explicit user request (2026-07-17), interest is now dissociated
    from bonus/cashback/referral income instead of folding everything into
    one figure: `revenuesTotal.revenues.obligationCoupons.untaxedTotal`/
    `.taxedTotal` are the REAL rent-coupon-only interest (excludes
    referrals/boosted-balance gains), while `bonus_cashback_contest` =
    `referrals.total + boostedBalanceGain.total` (both pass through
    untaxed, verified 2026-07-17: this month's referrals=0,
    boostedBalanceGain=0, obligationCoupons.untaxedTotal=2248==
    revenuesTotal.untaxedTotal=2248, confirming the identity
    untaxedTotal = obligationCoupons.untaxedTotal + referrals.total +
    boostedBalanceGain.total holds and obligationCoupons alone can safely
    be used as the "real" gross/net interest going forward).
    `withholding_tax` = obligationCoupons gross - net (interest-only tax,
    referrals/boosted-balance gains being untaxed have none to subtract).
    """
    month_str = get_report_now(REPORT_TIMEZONE).strftime("%Y-%m")
    log.info("Requesting Bricks revenue endpoint for %s...", month_str)

    resp = session.get(
        REVENUE_API_URL,
        params={"startDate": month_str, "endDate": month_str},
        timeout=15,
    )
    log.info("Revenue endpoint response: status=%s", resp.status_code)
    if resp.status_code != 200:
        raise RuntimeError(f"Bricks revenue endpoint returned status {resp.status_code}")

    revenues_total = (resp.json() or {}).get("revenuesTotal") or {}
    revenues_detail = revenues_total.get("revenues") or {}
    obligation_coupons = revenues_detail.get("obligationCoupons") or {}
    log.info("Raw revenuesTotal for %s: %r", month_str, revenues_total)

    try:
        gross_interest_received = round(float(obligation_coupons.get("untaxedTotal") or 0) / 100, 2)
    except (TypeError, ValueError):
        log.warning("Could not parse obligationCoupons 'untaxedTotal' %r - defaulting to 0.0.", obligation_coupons.get("untaxedTotal"))
        gross_interest_received = 0.0
    try:
        net_interest_received = round(float(obligation_coupons.get("taxedTotal") or 0) / 100, 2)
    except (TypeError, ValueError):
        log.warning("Could not parse obligationCoupons 'taxedTotal' %r - defaulting to 0.0.", obligation_coupons.get("taxedTotal"))
        net_interest_received = 0.0

    withholding_tax = round(gross_interest_received - net_interest_received, 2)

    try:
        referrals_total = float((revenues_detail.get("referrals") or {}).get("total") or 0) / 100
    except (TypeError, ValueError):
        log.warning("Could not parse 'referrals.total' %r - defaulting to 0.0.", (revenues_detail.get("referrals") or {}).get("total"))
        referrals_total = 0.0
    try:
        boosted_balance_gain_total = float((revenues_detail.get("boostedBalanceGain") or {}).get("total") or 0) / 100
    except (TypeError, ValueError):
        log.warning("Could not parse 'boostedBalanceGain.total' %r - defaulting to 0.0.", (revenues_detail.get("boostedBalanceGain") or {}).get("total"))
        boosted_balance_gain_total = 0.0
    bonus_cashback_contest = round(referrals_total + boosted_balance_gain_total, 2)

    log.info(
        "Parsed revenue totals for %s: gross_interest_received=%.2f, net_interest_received=%.2f, "
        "withholding_tax=%.2f, bonus_cashback_contest=%.2f (referrals=%.2f, boosted_balance_gain=%.2f)",
        month_str, gross_interest_received, net_interest_received, withholding_tax,
        bonus_cashback_contest, referrals_total, boosted_balance_gain_total,
    )
    return {
        "gross_interest_received": gross_interest_received,
        "net_interest_received": net_interest_received,
        "withholding_tax": withholding_tax,
        "bonus_cashback_contest": bonus_cashback_contest,
        "referrals": referrals_total,
        "boosted_balance_gain": boosted_balance_gain_total,
    }


def fetch_wallet_transactions_page(session: requests.Session, cursor: int) -> dict:
    """Fetch one page of the account's own wallet-transactions ledger API
    (see module docstring for the verified request/response shape)."""
    resp = session.get(
        WALLET_TRANSACTIONS_URL,
        params={"cursor": cursor, "take": WALLET_TRANSACTIONS_PAGE_SIZE},
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Bricks wallet-transactions endpoint returned status {resp.status_code} (cursor={cursor})")
    return resp.json() or {}


def get_cached_wallet_transactions(session: requests.Session) -> list:
    """Return every wallet-transactions row since account inception,
    fetching only the newest page(s) not already cached locally (in
    XIRR_CASHFLOWS_STATE_FILE) - same "stop at an already-cached id"
    incremental-cache idea as loanch_diversification.get_cached_transactions()
    (this endpoint has no date-range filter either, only a forward `cursor`
    offset, newest first)."""
    state = load_state(XIRR_CASHFLOWS_STATE_FILE, XIRR_CASHFLOWS_STATE_DEFAULT)
    cached_entries = state.get("all_entries") or []
    cached_ids = {entry.get("id") for entry in cached_entries}

    log.info("Found %d cached wallet-transaction(s) - fetching newest page(s) until an already-cached one is seen...", len(cached_entries))
    new_entries = []
    cursor = 0
    reached_cached_entry = False
    for page_number in range(1, MAX_WALLET_TRANSACTIONS_PAGES + 1):
        body = fetch_wallet_transactions_page(session, cursor)
        page_entries = body.get("data") or []
        log.info("Page %d (cursor=%d): %d entrie(s) found.", page_number, cursor, len(page_entries))
        for entry in page_entries:
            if entry.get("id") in cached_ids:
                reached_cached_entry = True
                break
            new_entries.append(entry)
        if reached_cached_entry or not page_entries:
            break
        next_cursor = body.get("cursor")
        if next_cursor is None or next_cursor == cursor:
            break
        cursor = next_cursor
    else:
        log.warning("Hit MAX_WALLET_TRANSACTIONS_PAGES (%d) without reaching a cached entry or the end of the ledger - it may be incomplete.", MAX_WALLET_TRANSACTIONS_PAGES)

    seen = set()
    merged = []
    for entry in new_entries + cached_entries:
        key = entry.get("id")
        if key in seen:
            continue
        seen.add(key)
        merged.append(entry)

    save_state(XIRR_CASHFLOWS_STATE_FILE, {"all_entries": merged})
    log.info("Wallet-transactions cache now holds %d entrie(s) (was %d before this run, %d new).", len(merged), len(cached_entries), len(new_entries))
    return merged


def _entry_date(entry: dict):
    raw = entry.get("createdAt")
    if not raw:
        return None
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _entry_value(entry: dict) -> float:
    try:
        return float(entry.get("value") or 0) / 100
    except (TypeError, ValueError):
        return 0.0


def _is_confirmed(entry: dict) -> bool:
    return entry.get("status") == "confirmed"


def reconstruct_outstanding(all_entries: list, end_date) -> float:
    """Reconstruct the INVESTED principal ("Investissements en cours") as
    of an arbitrary past `end_date`, by replaying every confirmed
    purchase/repayment row dated on or before that date - see module
    docstring for why `-value` is the right delta for BOTH investment
    (value negative, outstanding increases) and repayment (value positive,
    outstanding decreases) kinds."""
    outstanding = 0.0
    for entry in all_entries:
        entry_date = _entry_date(entry)
        if entry_date is None or entry_date > end_date or not _is_confirmed(entry):
            continue
        if entry.get("kind") in _OUTSTANDING_KINDS:
            outstanding += -_entry_value(entry)
    return outstanding


def _wallet_balance_as_of(all_entries: list, end_date) -> float:
    """Reconstructed wallet cash balance ("Solde total") as of `end_date` -
    every CONFIRMED row's own `value` already represents its real
    wallet-cash impact regardless of kind (see module docstring), so this
    is just their sum, starting from a true 0 at account inception (the
    ledger is fetched back that far - see get_cached_wallet_transactions())."""
    balance = 0.0
    for entry in all_entries:
        entry_date = _entry_date(entry)
        if entry_date is None or entry_date > end_date or not _is_confirmed(entry):
            continue
        balance += _entry_value(entry)
    return balance


def compute_average_idle_cash(all_entries: list, start_date: str, end_date: str) -> float:
    """Reconstruct the wallet's uninvested-cash balance for every day up to
    end_date from the raw ledger rows and return the day-weighted average
    over [start_date, end_date] - same technique as
    loanch_diversification.compute_average_idle_cash()."""
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        end = datetime.strptime(end_date, "%Y-%m-%d").date()
    except ValueError:
        return 0.0

    daily_deltas: dict = {}
    for entry in all_entries:
        entry_date = _entry_date(entry)
        if entry_date is None or entry_date > end or not _is_confirmed(entry):
            continue
        daily_deltas[entry_date] = daily_deltas.get(entry_date, 0.0) + _entry_value(entry)

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


def compute_average_balances(all_entries: list, start_date, end_date) -> tuple:
    """Day-weighted average INVESTED ("outstanding") and NON-INVESTED
    (wallet cash) balances over [start_date, end_date] (`date` objects) -
    for the "solde moyen pondéré investi"/"solde moyen pondéré non
    investi" Sheet rows (added 2026-09-08). Reuses the SAME per-entry
    classifiers as reconstruct_outstanding()/compute_average_idle_cash()
    above (_OUTSTANDING_KINDS/_entry_value), just fed into the generic
    shared day-weighted-average helper (opening_balance=0.0 at account
    inception) instead of a point-in-time replay - `all_entries` is
    expected to cover the account's full history (see
    get_cached_wallet_transactions()), so the running balance carried into
    `start_date` from summed prior deltas is accurate."""
    invested_events = []
    non_invested_events = []
    for entry in all_entries:
        entry_date = _entry_date(entry)
        if entry_date is None or not _is_confirmed(entry):
            continue
        value = _entry_value(entry)
        non_invested_events.append((entry_date, value))
        if entry.get("kind") in _OUTSTANDING_KINDS:
            invested_events.append((entry_date, -value))

    avg_invested = compute_time_weighted_average(invested_events, start_date, end_date)
    avg_non_invested = compute_time_weighted_average(non_invested_events, start_date, end_date)
    return avg_invested, avg_non_invested


def _sum_in_range(all_entries: list, kinds: set, start_date, end_date) -> float:
    """Sum of every confirmed entry's own `value` whose `kind` is in
    `kinds`, dated within [start_date, end_date] (inclusive)."""
    total = 0.0
    for entry in all_entries:
        entry_date = _entry_date(entry)
        if entry_date is None or entry_date < start_date or entry_date > end_date or not _is_confirmed(entry):
            continue
        if entry.get("kind") in kinds:
            total += _entry_value(entry)
    return total


def _lifetime_sum_as_of(all_entries: list, kinds: set, end_date) -> float:
    """Sum of every confirmed entry's own `value` whose `kind` is in
    `kinds`, dated on or before `end_date` (since account inception)."""
    return _sum_in_range(all_entries, kinds, _LEDGER_EPOCH, end_date)


def _build_since_inception_cashflows_as_of(all_entries: list, end_date) -> list:
    """Real deposit ("topup_wire"/"topup_card")/withdrawal cashflows,
    signed and dated, filtered to date<=end_date - `value` is already
    signed for its wallet-cash impact (deposit positive, withdrawal
    negative), so negating it uniformly gives the right XIRR convention
    for both (deposit -> negative cashflow, withdrawal -> positive one)."""
    signed_cashflows = []
    for entry in all_entries:
        entry_date = _entry_date(entry)
        if entry_date is None or entry_date > end_date or not _is_confirmed(entry):
            continue
        kind = entry.get("kind")
        if kind in _DEPOSIT_KINDS or kind == _WITHDRAWAL_KIND:
            signed_cashflows.append((entry_date, -_entry_value(entry)))
    return signed_cashflows


def compute_xirr_block_as_of(all_entries: list, end_date) -> dict:
    """Compute the FULL XIRR pie-chart block (XIRR, Cash drag, XIRR Bonus,
    XIRR Cash drag, XIRR Taxes/Frais, XIRR Intérêts) as of an arbitrary
    `end_date` - works identically for the current month or a BACKFILLED
    (past) one, since Bricks' single wallet-transactions ledger can
    reconstruct the terminal account value (outstanding + wallet balance)
    for ANY past date, unlike every other platform in this repo that needs
    a live-only endpoint for "today" - see module docstring. Mirrors
    afranga_diversification.compute_xirr_block_as_of() /
    loanch_diversification.compute_xirr_block_as_of()'s methodology
    exactly: Cash drag is a MONTHLY figure (this month alone), everything
    else is a since-inception counterfactual XIRR share through end_date.

    Returns a dict with any subset of {"XIRR", "Cash drag", "XIRR Bonus",
    "XIRR Cash drag", "XIRR Taxes/Frais", "XIRR Intérêts"} that could
    actually be computed - a missing key means "couldn't be computed", same
    soft-fail convention as everywhere else in this module.
    """
    result: dict = {}

    outstanding_as_of = reconstruct_outstanding(all_entries, end_date)
    wallet_balance_as_of = _wallet_balance_as_of(all_entries, end_date)
    total_value_as_of = outstanding_as_of + wallet_balance_as_of

    base_cashflows = _build_since_inception_cashflows_as_of(all_entries, end_date)
    xirr_value = compute_xirr(base_cashflows + [(end_date, total_value_as_of)])
    if xirr_value is None:
        log.warning("Could not compute XIRR as of %s from the reconstructed cashflows.", end_date)
        return result
    result["XIRR"] = xirr_value
    log.info("Computed XIRR as of %s: %.2f%% (total value=%.2f EUR).", end_date, xirr_value * 100, total_value_as_of)

    if outstanding_as_of <= 0:
        # Nothing invested at end_date - Cash drag/the pie shares below all
        # divide by the invested amount.
        return result

    month_start_str = end_date.replace(day=1).strftime("%Y-%m-%d")
    end_date_str = end_date.strftime("%Y-%m-%d")
    avg_idle_cash = compute_average_idle_cash(all_entries, month_start_str, end_date_str)
    monthly_interest = _sum_in_range(all_entries, {_INTEREST_KIND}, end_date.replace(day=1), end_date)
    cash_weight = avg_idle_cash / (avg_idle_cash + outstanding_as_of)
    monthly_yield_rate = monthly_interest / outstanding_as_of
    result["Cash drag"] = cash_weight * monthly_yield_rate
    log.info(
        "Computed Cash drag as of %s: %.2f%% (avg idle cash %.2f EUR, cash weight %.2f%%, monthly yield %.2f%%).",
        end_date, result["Cash drag"] * 100, avg_idle_cash, cash_weight * 100, monthly_yield_rate * 100,
    )

    deposit_dates = [
        d for d in (_entry_date(e) for e in all_entries if e.get("kind") in _DEPOSIT_KINDS and _is_confirmed(e))
        if d is not None and d <= end_date
    ]
    if not deposit_dates:
        return result
    since_inception_date = min(deposit_dates)
    since_inception_str = since_inception_date.strftime("%Y-%m-%d")

    lifetime_bonus = _lifetime_sum_as_of(all_entries, _BONUS_KINDS, end_date)
    if lifetime_bonus:
        xirr_without_bonus = compute_xirr(base_cashflows + [(end_date, total_value_as_of - lifetime_bonus)])
        if xirr_without_bonus is not None:
            result["XIRR Bonus"] = xirr_value - xirr_without_bonus
            log.info("XIRR share - bonus as of %s: %.2f points.", end_date, result["XIRR Bonus"] * 100)
    else:
        result["XIRR Bonus"] = 0.0

    avg_idle_cash_lifetime = compute_average_idle_cash(all_entries, since_inception_str, end_date_str)
    cash_weight_lifetime = avg_idle_cash_lifetime / (avg_idle_cash_lifetime + outstanding_as_of)
    lifetime_gross_interest = _lifetime_sum_as_of(all_entries, {_INTEREST_KIND}, end_date)
    lifetime_yield_rate = lifetime_gross_interest / outstanding_as_of
    cash_drag_lifetime_total = cash_weight_lifetime * lifetime_yield_rate
    missed_earnings = cash_drag_lifetime_total * (avg_idle_cash_lifetime + outstanding_as_of)
    xirr_with_cash_invested = compute_xirr(base_cashflows + [(end_date, total_value_as_of + missed_earnings)])
    if xirr_with_cash_invested is not None:
        result["XIRR Cash drag"] = xirr_value - xirr_with_cash_invested
        log.info(
            "XIRR share - cash drag as of %s: %.4f points (since-inception, missed earnings ~%.2f EUR).",
            end_date, result["XIRR Cash drag"] * 100, missed_earnings,
        )

    # withholding_tax's own `value` is already negative (see module
    # docstring) - flip it here to a positive "amount withheld" figure,
    # same sign convention as Afranga's withholding_tax.
    lifetime_withholding_tax = -_lifetime_sum_as_of(all_entries, {_TAX_KIND}, end_date)
    if lifetime_withholding_tax:
        xirr_with_taxes_cancelled = compute_xirr(base_cashflows + [(end_date, total_value_as_of + lifetime_withholding_tax)])
        if xirr_with_taxes_cancelled is not None:
            result["XIRR Taxes/Frais"] = xirr_value - xirr_with_taxes_cancelled
            log.info("XIRR share - taxes/frais as of %s: %.4f points (lifetime withholding tax %.2f EUR).", end_date, result["XIRR Taxes/Frais"] * 100, lifetime_withholding_tax)
    else:
        result["XIRR Taxes/Frais"] = 0.0

    lifetime_net_interest = lifetime_gross_interest - lifetime_withholding_tax
    if lifetime_net_interest:
        xirr_without_interest = compute_xirr(base_cashflows + [(end_date, total_value_as_of - lifetime_net_interest)])
        if xirr_without_interest is not None:
            result["XIRR Intérêts"] = xirr_value - xirr_without_interest
            log.info(
                "XIRR share - intérêts as of %s: %.4f points (lifetime net interest %.2f EUR = %.2f gross - %.2f taxes).",
                end_date, result["XIRR Intérêts"] * 100, lifetime_net_interest, lifetime_gross_interest, lifetime_withholding_tax,
            )
    else:
        result["XIRR Intérêts"] = 0.0

    return result


def run() -> None:
    if not BRICKS_EMAIL or not BRICKS_PASSWORD:
        log.error("BRICKS_EMAIL and BRICKS_PASSWORD environment variables are required.")
        sys.exit(1)

    log.info("Starting Bricks diversification run (pure-HTTP, no browser).")

    session = requests.Session()
    # Sent on EVERY request (not just sign-in) - some accounts have been seen
    # to get a 401 on the very next authenticated call otherwise.
    session.headers.update({"Origin": "https://app.bricks.co", "Referer": "https://app.bricks.co/"})

    try:
        login(session)
        balances = fetch_balances(session)
    except Exception:
        log.exception("Failed to log in or fetch Bricks balances.")
        sys.exit(1)

    try:
        log.info("Fetching this month's revenue totals...")
        revenue_totals = fetch_current_month_revenue_totals(session)
    except Exception:
        log.exception(
            "Failed to fetch this month's gross/net interest received/withholding tax/bonus - "
            "defaulting all four to 0.0."
        )
        revenue_totals = {
            "gross_interest_received": 0.0, "net_interest_received": 0.0,
            "withholding_tax": 0.0, "bonus_cashback_contest": 0.0,
            "referrals": 0.0, "boosted_balance_gain": 0.0,
        }

    total = round(balances["investments_en_cours"] + balances["solde_total"], 2)
    log.info(
        "Bricks balances: investments_en_cours=%.2f EUR, solde_total=%.2f EUR "
        "(solde_principal=%.2f EUR, solde_cadeau=%.2f EUR), total=%.2f EUR",
        balances["investments_en_cours"], balances["solde_total"],
        balances["solde_principal"], balances["solde_cadeau"], total,
    )

    # Since-inception XIRR (money-weighted return) + this month's Cash drag
    # + the XIRR Bonus/Cash drag/Taxes-Frais/Intérêts pie-chart shares - see
    # module docstring for the wallet-transactions ledger this is built
    # from. Fetched/cached unconditionally (not just for current_month) -
    # this endpoint has no date-range params to invert (paginates by
    # `cursor` only, newest first, stopping at an already-cached id), so a
    # backfilled month's run can safely extend/persist this cache too, same
    # reasoning as loanch_diversification.py.
    all_entries = None
    try:
        log.info("Fetching the since-inception wallet-transactions ledger (cached where possible)...")
        all_entries = get_cached_wallet_transactions(session)
    except Exception:
        log.exception("Failed to fetch the wallet-transactions ledger - XIRR/Cash drag will not be updated.")
        all_entries = None

    current_month = is_current_month()
    today_date = get_report_now(REPORT_TIMEZONE).date()

    xirr_block = {}
    if all_entries is not None:
        try:
            xirr_block = compute_xirr_block_as_of(all_entries, today_date)
        except Exception:
            log.exception("Failed to compute the XIRR block as of %s.", today_date)
            xirr_block = {}

    # Unlike every other platform in this repo, Bricks' single ledger can
    # ALSO reconstruct a genuine historical total (outstanding + wallet
    # balance) for a backfilled month, reconciled exactly against the live
    # home-metrics balances above (see module docstring) - so a backfilled
    # month is no longer forced to skip "total" the way it used to.
    skip_total = False
    if not current_month:
        if all_entries is not None:
            reconstructed_total = round(
                reconstruct_outstanding(all_entries, today_date) + _wallet_balance_as_of(all_entries, today_date), 2
            )
            log.info("Backfilled month (%s): using reconstructed total %.2f EUR instead of skipping it.", today_date, reconstructed_total)
            total = reconstructed_total
        else:
            skip_total = True

    amounts = {
        "total": total,
        "gross_interest_received": revenue_totals["gross_interest_received"],
        "net_interest_received": revenue_totals["net_interest_received"],
        "withholding_tax": revenue_totals["withholding_tax"],
        "bonus_cashback_contest": revenue_totals["bonus_cashback_contest"],
        "investments_en_cours": balances["investments_en_cours"],
        "solde_total": balances["solde_total"],
        "solde_principal": balances["solde_principal"],
        "solde_cadeau": balances["solde_cadeau"],
    }
    log.info(
        "This month's revenue totals: gross_interest_received=%.2f EUR, net_interest_received=%.2f EUR, "
        "withholding_tax=%.2f EUR",
        amounts["gross_interest_received"], amounts["net_interest_received"], amounts["withholding_tax"],
    )

    # Per the user's explicit decision (2026-07-15, after being warned
    # twice about the Sheet layout issue described in the module
    # docstring), fill_current_month_amounts() IS called even though the
    # "Bricks" row has no blank spacer row below it - the
    # gross_interest_received write below will land on "Bourse"'s row
    # instead. Don't revert this to a log-only skeleton without being
    # asked again.

    # "total" used to be unconditionally skipped for a backfilled month
    # (home-metrics is LIVE-only, no historical equivalent) - now it's
    # reconstructed from the ledger instead (see above) and only skipped
    # if that ledger fetch itself failed, matching every other platform's
    # soft-fail convention instead of an unconditional lock.
    fill_current_month_amounts(
        platform="Bricks",
        amounts=amounts,
        section="Crowdfunding immobilier",
        skip_total=skip_total,
    )

    # Day-weighted average invested/non-invested balances (new Sheet rows
    # "solde moyen pondéré investi"/"non investi", added 2026-09-08) -
    # computed whenever all_entries is available, independent of
    # current_month, so this also works for a REPORT_DATE-backfilled past
    # month. Uses the REAL number of days in the period, never a
    # hardcoded 30.
    avg_invested_balance = None
    avg_non_invested_balance = None
    if all_entries is not None:
        month_start_date = today_date.replace(day=1)
        avg_invested_balance, avg_non_invested_balance = compute_average_balances(
            all_entries, month_start_date, today_date
        )
        log.info(
            "Solde moyen pondéré - investi: %.2f EUR, non investi: %.2f EUR (%s to %s).",
            avg_invested_balance, avg_non_invested_balance, month_start_date, today_date,
        )

    # Bricks' block uses its own distinct sub-row labels ("parrainages" /
    # "soldes boostés"), not the generic prime/cashback/concours trio used
    # elsewhere - map referrals/boostedBalanceGain to them directly, never
    # touching the "Bonus" row itself (a SUM formula over those sub-rows).
    # "prélèvements" gets the real withholding tax on the obligationCoupons
    # interest, same convention as Afranga/Bienprêter/Mintos's equivalent row
    # - previously missing here, silently leaving that row blank for Bricks.
    # "Cash drag"/"XIRR"/"XIRR Intérêts"/"XIRR Bonus"/"XIRR Cash drag"/
    # "XIRR Taxes/Frais" rows already exist in the live Sheet (rows already
    # pre-added below Bricks' own row) - appended past the default
    # max_rows=6 bound, only included when actually computed (soft-fail,
    # same convention as everywhere else).
    breakdown = {
        "parrainages": revenue_totals["referrals"],
        "soldes boost\u00e9s": revenue_totals["boosted_balance_gain"],
        "pr\u00e9l\u00e8vements": revenue_totals["withholding_tax"],
    }
    breakdown.update(xirr_block)
    if avg_invested_balance is not None:
        breakdown["solde moyen pondéré investi"] = avg_invested_balance
    if avg_non_invested_balance is not None:
        breakdown["solde moyen pondéré non investi"] = avg_non_invested_balance
    fill_current_month_bonus_breakdown(
        platform="Bricks",
        breakdown=breakdown,
        section="Crowdfunding immobilier",
        max_rows=14,
    )

    # "Répartition géographique" has a single "Bricks" aggregate row (no
    # per-project/per-country breakdown, unlike Mintos/Swaper) - same value
    # as the Crowdfunding immobilier section's total, mirroring Lande's
    # single-row pattern. Also has its own "non investi" row (solde_total
    # = balanceAvailable + giftBalance, i.e. cash not yet invested).
    if current_month:
        try:
            fill_geographic_repartition_amounts([{"name": "Bricks", "amount": balances["investments_en_cours"]}])
            fill_geographic_repartition_uninvested_amount("Bricks", balances["solde_total"])
        except Exception:
            log.exception("Failed to update Bricks' 'Répartition géographique' rows.")


if __name__ == "__main__":
    run()
