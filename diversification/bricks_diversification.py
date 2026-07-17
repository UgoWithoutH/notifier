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
import logging
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

import requests

try:
    from shared.google_sheet import fill_current_month_amounts, fill_current_month_bonus_breakdown
    from shared.report_date import get_report_now
except ModuleNotFoundError:
    # Support direct execution (python diversification/bricks_diversification.py)
    # where the project root may not be on sys.path.
    project_root = Path(__file__).resolve().parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    from shared.google_sheet import fill_current_month_amounts, fill_current_month_bonus_breakdown
    from shared.report_date import get_report_now

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bricks_diversification")

SIGNIN_URL = "https://api.bricks.co/api/auth/sign-in/email"
HOME_METRICS_URL = "https://api.bricks.co/investor/portfolio/wealth/home-metrics"
REVENUE_API_URL = "https://api.bricks.co/investor/portfolio/revenue"
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
    resp = session.post(
        SIGNIN_URL,
        json={"email": BRICKS_EMAIL, "password": BRICKS_PASSWORD},
        headers={"Origin": "https://app.bricks.co", "Referer": "https://app.bricks.co/"},
        timeout=15,
    )
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
    if resp.status_code != 200:
        raise RuntimeError(f"Bricks home-metrics endpoint returned status {resp.status_code}")

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


def run() -> None:
    if not BRICKS_EMAIL or not BRICKS_PASSWORD:
        log.error("BRICKS_EMAIL and BRICKS_PASSWORD environment variables are required.")
        sys.exit(1)

    log.info("Starting Bricks diversification run (pure-HTTP, no browser).")

    session = requests.Session()

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
    fill_current_month_amounts(
        platform="Bricks",
        amounts=amounts,
        section="Crowdfunding immobilier",
    )

    # Bricks' block uses its own distinct sub-row labels ("parrainages" /
    # "soldes boostés"), not the generic prime/cashback/concours trio used
    # elsewhere - map referrals/boostedBalanceGain to them directly, never
    # touching the "Bonus" row itself (a SUM formula over those sub-rows).
    fill_current_month_bonus_breakdown(
        platform="Bricks",
        breakdown={
            "parrainages": revenue_totals["referrals"],
            "soldes boost\u00e9s": revenue_totals["boosted_balance_gain"],
        },
        section="Crowdfunding immobilier",
    )


if __name__ == "__main__":
    run()
