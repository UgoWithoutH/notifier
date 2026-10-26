"""Bienpreter dashboard balance fetcher.

Same family as afranga_diversification.py / monefit_diversification.py etc,
but simpler and different in one key way: Bienpreter is NOT broken down by
loan originator at all here - per the user's request, this just logs into
https://www.bienpreter.com, reads two figures on the dashboard
(https://www.bienpreter.com/u/tableau-de-bord):
  - "solde disponible" (available cash balance not yet invested)
  - "capital à recevoir" (capital still to be repaid on active investments)
sums them, and hands the single total to fill_current_month_amounts() (see
google_sheet.py) - no per-originator dict, just one number, no email sent
either.

ADDED 2026-07-31: unlike the "Crowdlending" section above (single aggregate
figure), the "Répartition géographique" section's Bienprêter block IS
broken down - one row per BORROWER (emprunteur, e.g. "EXCAVAN"), with a
country-column matrix (one column per country, shared across every
platform's block on that sheet). `fetch_active_loans_by_borrower()` fetches
every currently active ("en cours") loan from
Bienprêter's JSON API (/api/v2/contracts/me, paginated), sums the invested
amount per borrower (a borrower can have several concurrent loans/
contracts), and resolves each loan's country via its project's API record
(/api/v2/project/{id}, "location" field - same text as the project page's
"Localisation", e.g.
"Madrid - Espagne" -> "Espagne" - see _country_from_location() for the
French-domestic-postal-code special case). The result feeds
shared.google_sheet.fill_bienpreter_borrower_geo_amounts(), which writes
each borrower's amount into the matching country column, inserts a new row
for any borrower not already listed (right after the block's last existing
borrower - explicitly re-styled to match sibling borrower rows, since an
inserted row inherits the next platform header's bold/left-aligned style
otherwise), DELETES any row whose borrower no longer has an active loan,
and rewrites every remaining row's "total" column (one column right of the
borrower name) as a live "=SOMME(...)" formula summing that row's country
columns - it deliberately never touches the "Bienprêter" row's OWN total
column (kept manual, per explicit user request).

REWRITTEN 2026-07-17 to use plain `requests` instead of Playwright (no
browser at all), same technique as bricks_diversification.py /
goandgrow_diversification.py - much faster in GitHub Actions (no Chromium
download/launch). Verified live: Bienpreter is a plain Symfony
(server-rendered) site with NO Cloudflare/bot-protection at all on the
login form or the dashboard/operations pages - a vanilla `requests.Session()`
sails through with zero issues, no cookie-seeding/storage_state workaround
needed like Bricks briefly required.

Login mechanism (Symfony CSRF-protected form, verified 2026-07-17):
`GET https://www.bienpreter.com/connexion` returns an HTML form (name=
"user_login", method="post", NO `action` attribute - posts back to the same
`/connexion` URL) with hidden inputs `_csrf_token` and `user_login[_token]`
(both must be read fresh from that GET and POSTed back verbatim - they're
per-session/per-request Symfony CSRF tokens, not static). POST fields:
`user_login[email]`, `user_login[password]`, `user_login[remember_me]=1`,
plus the two token fields above. A successful login response is a 200
whose final URL (`requests` follows the redirect chain automatically) is
`/u/tableau-de-bord` - the dashboard IS the login response body itself (no
extra navigation needed). No 2FA/TOTP step exists
on this account (confirmed in the original 2026-07-09 Playwright build
too).

UPDATED 2026-10-21: the site also exposes the JSON API its own front-end
uses (`/api/v2/...`, JSON-LD only), authenticated by the `api-v2-token`
cookie that `GET /api-token-from-session` sets from the PHP session (see
_refresh_api_token()/_api_get_json()). Scraping the HTML silently broke
once already (/u/mes-prets markup change -> 0 loans parsed), so
`fetch_balances()` now reads "solde disponible" (`walletAmount` of
/api/v2/user/me) and "capital à recevoir" (`capitalRemaining` of
/api/v2/contracts/me) from the API instead of regexes on the dashboard
HTML (verified live to equal the dashboard's figures exactly); the
dashboard HTML is now only the persisted-session validity probe. Same for
the active loans and project locations below. The /u/operations HTML is
STILL scraped: /api/v2/operations has no running "Solde indicatif"
(needed by compute_average_idle_cash()) nor per-row capital/interest/tax
details, and the CSV export has neither the balance nor the time.

Also fetches this calendar month's interest received (like every other
*_diversification.py's equivalent) from the "Toutes mes opérations" page
(https://www.bienpreter.com/u/operations) - see
fetch_current_month_interest_totals() below for exactly how gross/net/
withholding tax are obtained (Bienpreter has no single labeled "net
interest" figure anywhere, so this is reconstructed from NET = GROSS - TAX
using two real figures read off the page, not a guessed/configured flat
tax rate). Verified this page is ALSO plain server-rendered HTML reachable
via a normal `session.get(...)` with the same date-range/page query params
used in the original Playwright build - same row markup
(`.transaction__name`/`.transaction__amount`/`.transaction__interests`),
same "one placeholder row on out-of-range pages" pagination quirk. Also
sums a real "Bonus" row type (confirmed live 2026-08-14 - a genuine
transaction label, not a placeholder) into `bonus_total`, replacing the
old hardcoded 0.0 placeholder that predated this discovery.

Added 2026-08-14: since-inception XIRR (money-weighted return) plus this
month's Cash drag and the XIRR Bonus / XIRR Cash drag / XIRR Taxes/Frais
pie-chart shares, mirroring swaper_diversification.py's/
afranga_diversification.py's own XIRR blocks (see those modules'
docstrings for the full methodology) - see fetch_all_operations()/
get_cached_operations()/compute_average_idle_cash() below. IMPORTANT
DIFFERENCE from Swaper/Afranga: Bienpreter's own operations table already
carries a real per-row "Solde indicatif" running balance right after
every transaction (`.transaction__balance`, verified live to match the
dashboard's own "Solde disponible" exactly for the most recent row) - so
Cash drag's day-by-day idle-cash reconstruction here just REPLAYS those
real balance snapshots instead of reconstructing a running total from
signed per-type deltas the way Swaper/Afranga have to (neither of those
platforms expose a real per-transaction balance). Every operation type's
amount here is ALSO already signed correctly by the site itself (deposits
positive, investments/withdrawals/withholding tax negative, etc. -
verified live across all 10 distinct transaction types this account has
ever had) - no direction-class/type-to-sign mapping needed either, unlike
Afranga's Details table. "Dépôt de fonds"/"Retrait de fonds" are the only
real EXTERNAL cashflows (XIRR); "Vente de prêt" (loan resold on the
secondary market) and "Rétractation de l'intention de prêt" (a loan
commitment reversed) are internal reallocations, same treatment as
Swaper's INVESTMENT/REPAYMENT/BUYBACK types - never treated as XIRR
cashflows, but DO count for the day-by-day balance replay (their real
"Solde indicatif" is used as-is).

Added 2026-08-18: XIRR Intérêts, the counterfactual XIRR share
attributable to real net interest received (mirrors XIRR Bonus's own
counterfactual pattern exactly, just with the lifetime net interest total
- gross interest received minus withholding tax, both already reconstructed
from real /u/operations rows - subtracted from today's total account value
instead of the lifetime bonus total). This exists because "Intérêts" was
previously only ever a RESIDUAL on the spreadsheet/dashboard side (XIRR -
XIRR Bonus - XIRR Cash drag - XIRR Taxes/Frais), which can legitimately go
negative when the bonus's counterfactual XIRR share is disproportionately
large relative to the account's real underlying (non-bonus) performance -
that's not a bug, it's the correct signal that the account's return is
propped up almost entirely by the bonus. XIRR Intérêts instead gives a
genuine, independently-measured figure (same category of computation as
Bonus/Taxes, not a derived leftover), so the two can be compared/sanity-
checked against each other on the sheet/dashboard side.

UPDATE 2026-10-06: the backward reconstruction below is NO LONGER used by
run() - it propagated every later loss/drift (e.g. loans sold at a
discount) into past months and gave absurd XIRRs (330769% for 2025-04).
A backfilled month's total_account_value is now rebuilt FORWARD from
inception via _total_account_value_delta_for_row() (same as
compute_average_balances()). The lifetime XIRR waterfall shares also no
longer depend on the previous month having a balance (first-month accounts).

UPDATE 2026-09-07 (implemented via backward reconstruction, resolving the
limitation noted below): forward-reconstructing "capital à recevoir" for
an arbitrary past date is still impossible (see the unchanged paragraph
below for why), but total_account_value at a past `today_date` can
instead be derived BACKWARD from TODAY's known live total (solde
disponible + capital à recevoir, always fetched live regardless of
REPORT_DATE): every transaction dated after `today_date` is either a pure
cash<->invested-capital reallocation (Investissement, Remboursement
mensuel's bundled capital portion, Vente de prêt, Rétractation de
l'intention de prêt - net zero effect on the SUM of the two components)
or a real external cashflow/earnings event (Dépôt de fonds, Retrait de
fonds, Intérêts, Bonus, Prélèvements fiscaux) whose effect on that sum IS
known precisely - so subtracting only the latter's net effect (see
`_net_value_change()`) from today's live total gives today_date's real
total_account_value, with no need to ever itemize a matured loan's
principal at all. "Capital à recevoir" at today_date then falls out as a
remainder: total_account_value minus the real replayed "Solde indicatif"
cash balance as of that date (`_balance_as_of()`, same snapshot-replay
technique as compute_average_idle_cash()). This is what now lets XIRR/
Cash drag/the pie-chart shares be computed for a backfilled month too
(previously current-month-only, gated behind `is_current_month()`) - see
run()'s `real_today`/`today_date` split below. "total" itself (the
Crowdlending section's raw solde disponible + capital à recevoir figures)
still isn't written for a backfilled month (still skip_total, unchanged) -
only the derived XIRR/Cash drag block benefits from this reconstruction.

ORIGINAL 2026-09-07 FINDING (still true, explains why forward
reconstruction specifically doesn't work): unlike Afranga (whose Details
rows have an explicit "Investments in loans"/"Principal ..." label per
row, letting `reconstruct_outstanding()` replay the invested-principal
balance for any past date), Bienpreter's "capital à recevoir" CANNOT be
reliably reconstructed FORWARD from zero using /u/operations rows for an
arbitrary past date: "Remboursement mensuel" rows only itemize the
INTEREST portion (`.transaction__interests`) - any bundled CAPITAL
repayment (when a loan matures within the queried window) is silently
folded into that same row's `.transaction__amount` with no separate
principal figure anywhere (see this module's own "In Fine loans"
docstring paragraph above).

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
"Prélèvements fiscaux") and "XIRR Frais" - Bienprêter has NO platform-fee
concept distinct from withholding tax (no such operation type was ever
observed on this account), so "XIRR Frais" is hardcoded to 0.0, not
computed via Shapley. Note "XIRR Cash drag" here is the LIFETIME (since-
inception) share, distinct from the plain monthly "Cash drag" row above,
which is unchanged.

Required env vars:
    BIENPRETER_EMAIL, BIENPRETER_PASSWORD -> Bienpreter account credentials
Optional:
    GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS    -> used to write this month's totals
                                              to the Google Sheet via
                                              fill_current_month_amounts() (see
                                              google_sheet.py)
"""

import re
import os
import sys
import html
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

from shared.google_sheet import (
    fill_current_month_amounts,
    fill_current_month_bonus_breakdown,
    fill_bienpreter_borrower_geo_amounts,
    fill_geographic_repartition_uninvested_amount,
    fill_remaining_duration_amounts,
)
from shared.remaining_duration import overdue_months_and_days, remaining_months_and_days
from shared.report_date import get_report_now, is_current_month
from shared.session_cache import get_or_refresh_session
from shared.notifier import send_bienpreter_geo_issues_email
from shared.state import load_state, save_state
from shared.weighted_average import INVESTED_BALANCE_LABEL, NON_INVESTED_BALANCE_LABEL, compute_time_weighted_average
from shared.monthly_yield_waterfall import compute_monthly_yield_shares
from shared.xirr import compute_xirr
from shared.xirr_waterfall import compute_waterfall_xirr_shares

load_dotenv()

from zoneinfo import ZoneInfo

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bienpreter_diversification")

LOGIN_URL = "https://www.bienpreter.com/connexion"
DASHBOARD_URL = "https://www.bienpreter.com/u/tableau-de-bord"
OPERATIONS_URL = "https://www.bienpreter.com/u/operations"
MAX_OPERATIONS_PAGES = 300  # safety cap against an infinite loop if pagination ever misbehaves
# Bienpreter is a French platform; "this month" below means the current
# calendar month up to TODAY (1st of the month through today, NOT the full
# month) - same semantics verified for Swaper's "This Month" / Afranga's
# "Current Month" quick filters. Pinned explicitly rather than relying on
# the executing machine's local clock (e.g. UTC on a CI runner).
REPORT_TIMEZONE = ZoneInfo("Europe/Paris")

# Cache of every /u/operations row ever fetched (see get_cached_operations()
# below) - same incremental-fetch idea as afranga_diversification.py's
# XIRR_CASHFLOWS_STATE_FILE, avoids re-fetching the account's entire
# history (77+ pages) on every run.
SESSION_STATE_FILE = Path(__file__).parent / "bienpreter_diversification_session_state.json"
XIRR_CASHFLOWS_STATE_FILE = Path(__file__).parent / "bienpreter_xirr_cashflows_state.json"
# Bump when the cached row shape changes (v2: interest/embedded tax parsed from each row's detail panel;
# v3: v2 caches still held pre-panel-parsing rows with no interest for early 2025, forcing a full refetch).
XIRR_CACHE_SCHEMA_VERSION = 3
XIRR_CASHFLOWS_STATE_DEFAULT = {"rows": [], "last_fetched_date": None, "schema_version": XIRR_CACHE_SCHEMA_VERSION}
# Rows can be posted days after their own date, so re-fetch this many days before the cache frontier.
XIRR_CACHE_OVERLAP_DAYS = 30
# XIRR is a since-inception money-weighted return (not per-month) - this
# start date is early enough to cover any real account's full history.
XIRR_HISTORY_START_DATE = date(2000, 1, 1)

BIENPRETER_EMAIL = os.environ.get("BIENPRETER_EMAIL")
BIENPRETER_PASSWORD = os.environ.get("BIENPRETER_PASSWORD")

_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def login(session: requests.Session) -> str:
    """Log in to Bienpreter using BIENPRETER_EMAIL/BIENPRETER_PASSWORD via
    a plain HTTP POST (no browser). Returns the dashboard HTML (the login
    response's body IS the dashboard page - see module docstring).

    Raises RuntimeError if the login form's CSRF tokens can't be found, or
    if the post-login response doesn't land on /u/tableau-de-bord (wrong
    credentials show the same /connexion form again with an error banner).
    """
    log.info("GET %s (fetching login form + CSRF tokens)...", LOGIN_URL)
    r = session.get(LOGIN_URL, timeout=30)
    log.info("GET login page: status=%s", r.status_code)
    r.raise_for_status()

    csrf_match = re.search(r'name="_csrf_token" value="([^"]*)"', r.text)
    token_match = re.search(r'name="user_login\[_token\]" value="([^"]*)"', r.text)
    if not csrf_match or not token_match:
        raise RuntimeError("Could not find Bienpreter login CSRF tokens on the /connexion page.")

    payload = {
        "user_login[email]": BIENPRETER_EMAIL,
        "user_login[password]": BIENPRETER_PASSWORD,
        "user_login[remember_me]": "1",
        "_csrf_token": csrf_match.group(1),
        "user_login[_token]": token_match.group(1),
    }
    log.info("POST %s (submitting credentials)...", LOGIN_URL)
    r2 = session.post(LOGIN_URL, data=payload, timeout=30)
    log.info("POST login: status=%s, final_url=%s", r2.status_code, r2.url)
    r2.raise_for_status()

    if "/u/tableau-de-bord" not in r2.url:
        raise RuntimeError(f"Login did not reach the dashboard (still on {r2.url}) - check credentials.")
    log.info("Logged in successfully.")
    return r2.text


def _fetch_dashboard_html(session: requests.Session) -> str:
    """Fetch the dashboard page directly, reusing an already-authenticated
    session's cookies (no credentials submitted) - used to reuse a
    persisted session instead of logging in again. Raises if the session
    has expired (redirected back to the login form)."""
    r = session.get(DASHBOARD_URL, timeout=30)
    r.raise_for_status()
    if "/u/tableau-de-bord" not in r.url:
        raise RuntimeError(f"Session no longer authenticated (redirected to {r.url}).")
    return r.text


def _parse_amount(text: str):
    """Parse a currency-formatted amount (e.g. "955,25 €", "1 220 €",
    "€1,234.56") into a float, without assuming a fixed locale - whichever
    of ',' or '.' appears last is treated as the decimal separator, the
    other (or repeats of it) as thousands separators."""
    if not text:
        return None
    cleaned = text.replace("\xa0", " ").replace("&nbsp;", " ").strip()
    cleaned = re.sub(r"[^\d.,\s-]", "", cleaned).replace(" ", "")
    if not cleaned:
        return None

    has_comma, has_dot = "," in cleaned, "." in cleaned
    if has_comma and has_dot:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif has_comma:
        last_part = cleaned.rsplit(",", 1)[-1]
        if len(last_part) == 2:
            cleaned = cleaned.replace(",", "", cleaned.count(",") - 1).replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")

    try:
        return float(cleaned)
    except ValueError:
        return None


def _strip_tags(html_fragment: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", "", html_fragment or "")).strip()


def fetch_balances(session: requests.Session) -> dict:
    """"solde disponible" and "capital à recevoir" as floats, from the JSON
    API (verified live 2026-10-21 to equal the dashboard's own figures
    exactly): available balance = `walletAmount` of /api/v2/user/me;
    capital to receive = `capitalRemaining` of /api/v2/contracts/me (an
    account-wide figure repeated on every contract row, so a single row
    is enough; 0.0 when the account has no contract at all)."""
    me = _api_get_json(session, USER_ME_API_URL)
    available_balance = me.get("walletAmount")
    if available_balance is None:
        raise RuntimeError("Could not find 'walletAmount' in the Bienpreter /api/v2/user/me response.")

    results = _get_contracts_page(session, {"page": 1, "limit": 1}).get("results") or []
    capital_to_receive = results[0].get("capitalRemaining") if results else 0.0
    if capital_to_receive is None:
        raise RuntimeError("Could not find 'capitalRemaining' in the Bienpreter /api/v2/contracts/me response.")
    log.info("Balances from the API: solde=%s, capital_a_recevoir=%s", available_balance, capital_to_receive)

    return {"available_balance": float(available_balance), "capital_to_receive": float(capital_to_receive)}


# The /u/mes-prets page is a server-rendered HTML table whose markup changed
# without notice (2026-10-21: amount wrapped in a tooltip <span> -> 0 loans
# parsed). The site's own front-end reads the same data from a JSON API
# (`/api/v2/contracts/me`, JSON-LD only), authenticated by the `api-v2-token`
# cookie that `GET /api-token-from-session` (204) sets from the PHP session -
# verified live 2026-10-21 to return exactly the same 55 active loans,
# amounts, borrowers, project ids and next-due dates as the HTML table.
API_TOKEN_URL = "https://www.bienpreter.com/api-token-from-session"
ACTIVE_LOANS_API_URL = "https://www.bienpreter.com/api/v2/contracts/me"
ACTIVE_LOANS_PAGE_SIZE = 100
LOAN_STATUS_ACTIVE = "3"  # criteria[status][] = "en cours"
MAX_ACTIVE_LOANS_PAGES = 20  # safety net against an infinite loop
PROJECT_API_URL = "https://www.bienpreter.com/api/v2/project/{project_id}"
USER_ME_API_URL = "https://www.bienpreter.com/api/v2/user/me"
# `criteria[litigation]` filter (same values as the old HTML page's
# "Retards / Défauts" <select>): 11 = "Prêts en retard (retard < 6 mois)",
# 12 = "Prêts en défaut (retard > 6 mois)" (1 = either of them).
LITIGATION_LATE = "11"
LITIGATION_DEFAULT = "12"


def _parse_active_loans_results(results: list) -> list:
    """Converts the `items.results` of /api/v2/contracts/me into a list of
    {"project_id", "borrower", "amount", "contract_id", "next_payment_date"}
    dicts. Verified API fields: `id`, `amount`, `project.id`,
    `project.userSocietyInfo.name` (borrower), `nextDue.dueDate` (ISO
    datetime, `nextDue` is null when nothing is due -> None). Rows missing
    a borrower/amount are skipped (surfaced by the total-count check in
    fetch_active_loans())."""
    rows = []
    for result in results:
        project = result.get("project") or {}
        borrower = ((project.get("userSocietyInfo") or {}).get("name") or "").strip()
        amount = result.get("amount")
        if not borrower or amount is None or project.get("id") is None:
            continue

        next_due = result.get("nextDue") or {}
        due_date = next_due.get("dueDate")
        rows.append(
            {
                "project_id": str(project["id"]),
                "borrower": borrower,
                "amount": float(amount),
                "contract_id": str(result["id"]) if result.get("id") is not None else None,
                "next_payment_date": datetime.fromisoformat(due_date).date() if due_date else None,
            }
        )
    return rows


def _refresh_api_token(session: requests.Session) -> None:
    """Sets the `api-v2-token` cookie the JSON API authenticates with, from
    the PHP session cookie (what the site's own front-end does on a 401)."""
    r = session.get(API_TOKEN_URL, timeout=30)
    log.info("GET api-token-from-session: status=%s", r.status_code)
    r.raise_for_status()


def _api_get_json(session: requests.Session, url: str, params: dict = None) -> dict:
    """GET on the JSON API -> parsed body. Re-fetches the API token once on
    a 401 (expired/missing token) before retrying. Only JSON-LD is served,
    hence the explicit Accept header."""
    headers = {"Accept": "application/ld+json"}
    r = session.get(url, params=params, headers=headers, timeout=30)
    if r.status_code == 401:
        _refresh_api_token(session)
        r = session.get(url, params=params, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()


def _get_contracts_page(session: requests.Session, params: dict) -> dict:
    """One GET of /api/v2/contracts/me -> its `items` object ({"results",
    "total", "limit", "page"})."""
    return _api_get_json(session, ACTIVE_LOANS_API_URL, params)["items"]


def fetch_active_loans(session: requests.Session, litigation: str = ""):
    """Fetches every currently active ("en cours") Bienprêter loan from the
    JSON API (/api/v2/contracts/me, see ACTIVE_LOANS_API_URL), paginating
    via `page`/`limit` until the API's own `total` is reached (bounded by
    MAX_ACTIVE_LOANS_PAGES as a safety net). `litigation` optionally
    restricts it to late (LITIGATION_LATE) or defaulted (LITIGATION_DEFAULT)
    loans. Returns (loans, issues):
    - loans : flat list of {"project_id", "borrower", "amount",
      "contract_id", "next_payment_date"} dicts, one per loan/contract (a
      single borrower can appear multiple times, once per contract).
    - issues : list of short strings describing a pagination problem (hit
      the page safety net, or collected a different number of loans than
      the API's own `total` - both signal the result may be incomplete) -
      feeds shared.notifier.send_bienpreter_geo_issues_email()."""
    loans = []
    issues = []
    seen_contract_ids = set()
    expected_total = None
    _refresh_api_token(session)
    for page_number in range(1, MAX_ACTIVE_LOANS_PAGES + 1):
        params = {
            "page": page_number,
            "limit": ACTIVE_LOANS_PAGE_SIZE,
            "criteria[status][]": LOAN_STATUS_ACTIVE,
        }
        if litigation:
            params["criteria[litigation]"] = litigation
        log.info("GET active loans page %d...", page_number)
        items = _get_contracts_page(session, params)

        if expected_total is None:
            expected_total = items.get("total")
            log.info("Active loans: %s résultat(s) au total (from page 1).", expected_total)

        results = items.get("results") or []
        rows = [
            row
            for row in _parse_active_loans_results(results)
            if row["contract_id"] not in seen_contract_ids
        ]
        seen_contract_ids.update(row["contract_id"] for row in rows)
        log.info("Active loans page %d: %d loan(s) found.", page_number, len(rows))
        if not results:
            break
        loans.extend(rows)
        if expected_total is not None and page_number * ACTIVE_LOANS_PAGE_SIZE >= expected_total:
            break
    else:
        message = (
            f"Pagination des prêts actifs interrompue après {MAX_ACTIVE_LOANS_PAGES} pages sans "
            "atteindre le total attendu - les résultats sont peut-être incomplets."
        )
        log.warning(message)
        issues.append(message)

    if expected_total is not None and len(loans) != expected_total:
        message = (
            f"Prêts actifs : {expected_total} résultat(s) au total attendu(s) mais {len(loans)} "
            "collecté(s) - des contrats ont peut-être été ignorés (emprunteur/montant manquant "
            "dans la réponse de l'API) ou dupliqués."
        )
        log.warning(message)
        issues.append(message)

    log.info("Total active loans found: %d", len(loans))
    return loans, issues


def _country_from_location(location: str):
    """Parses the project page's 'Localisation' text into just the
    country name. Formats observed on real projects: foreign ones are
    'City - Country' (e.g. 'Madrid - Espagne', 'Bucarest - Roumanie',
    even 'Montpellier - France'), domestic (French) ones instead start
    with a postal code and have NO country at all - either just
    '13007 Marseille'/'92800 PUTEAUX', OR (confusingly) STILL a dash
    before a city name, e.g. '83330 - Le Castellet' (NOT a 'city -
    country' pair despite the dash). So: if it starts with a postal code,
    it's always France regardless of any dash; otherwise take the text
    after the last ' - ' if present.

    BUG FIXED 2026-09-08: a THIRD domestic format also exists - some French
    project pages render ONLY the bare city name, with NEITHER a postal
    code NOR a dash at all (confirmed live: 'Saint-Philbert-de-Grand-Lieu',
    'Rivière-Salée' - both real French communes, project ids 5472/5473,
    borrower 'BEMA'). The OLD code returned this bare string as-is (treated
    as if it were itself a "country" name), which never matches any real
    country column header - the amount was then silently dropped from the
    Répartition géographique write ("pays introuvable" issue), causing a
    real, reproduced 250 EUR gap between the live 'capital à recevoir'
    total (5320 EUR) and the sum of the Bienprêter geo block's borrower
    rows (5070 EUR). Every FOREIGN project observed so far always includes
    an explicit ' - <Country>' suffix, so a bare city name with no dash and
    no leading digits is assumed to be French domestic too, not a country
    name on its own - logged as a warning (not silently guessed) since this
    is a real assumption, not a proven rule, in case a future foreign
    project ever omits its country suffix too.
    """
    location = location.strip()
    if re.match(r"^\d{4,5}\b", location):
        return "France"
    if " - " in location:
        return location.rsplit(" - ", 1)[-1].strip()
    if location:
        log.warning(
            "Localisation %r n'a ni code postal ni ' - Pays' - traitée comme la France par défaut "
            "(vérifier si un jour un projet étranger utilise ce format).",
            location,
        )
        return "France"
    return None


def fetch_project_country(session: requests.Session, project_id: str):
    """Fetches https://www.bienpreter.com/api/v2/project/{project_id} and
    reads its `location` field (same "City - Country" text the project
    page's 'Localisation' shows, verified identical live 2026-10-21),
    returning just the country part via _country_from_location(). NOT the
    API's `borrowerCountry`/`userSocietyInfo.country` (the borrower
    company's home country, e.g. 'RO' for ROMRADIATOARE whose project is in
    Rotterdam/Pays Bas) - the Répartition géographique follows the
    project's location. Returns None if the location can't be found."""
    url = PROJECT_API_URL.format(project_id=project_id)
    log.info("GET project %s (for country)...", project_id)
    location = (_api_get_json(session, url).get("location") or "").strip()
    if not location:
        log.warning("Could not find 'location' for project %s.", project_id)
        return None

    country = _country_from_location(location)
    log.info("Project %s location=%r -> country=%r", project_id, location, country)
    return country


def fetch_active_loans_by_borrower(session: requests.Session):
    """Fetches every active loan (fetch_active_loans()) and its project's
    country (fetch_project_country(), cached per project_id since several
    contracts can point at the same project), then groups/sums by
    (borrower name, country) - a borrower with active loans in SEVERAL
    countries gets one entry per country instead of only the first one
    found (FIXED 2026-09-08: the old "keep only the first country, warn
    and drop the rest" behavior silently discarded real invested amounts
    from the Répartition géographique total whenever a borrower - e.g.
    real case "ROMRADIATOARE", Cluj-Napoca/Roumanie + Rotterdam/Pays Bas -
    genuinely has loans in more than one country). Returns (borrowers, issues):
    - borrowers : {borrower_name: {country_name: summed_amount}} - feeds
      fill_bienpreter_borrower_geo_amounts() in shared/google_sheet.py,
      which now splits each borrower's row across every matching country
      column instead of a single amount/country pair. A country that
      couldn't be resolved (fetch_project_country() returned None) is kept
      under the "" key so its amount isn't silently dropped from the
      dict, even though fill_bienpreter_borrower_geo_amounts() still can't
      write it to any column (reported via `issues` instead).
    - issues : list of short strings, one per country that couldn't be
      found/fetched - feeds shared.notifier.send_bienpreter_geo_issues_email()
      (per explicit user request: any missing country or error here should
      be emailed, not just logged).
    """
    loans, issues = fetch_active_loans(session)

    project_country_cache = {}
    borrowers = {}

    for loan in loans:
        project_id = loan["project_id"]
        name = loan["borrower"]
        if project_id not in project_country_cache:
            try:
                country = fetch_project_country(session, project_id)
                if country is None:
                    issues.append(
                        f"Pays introuvable sur la page du projet {project_id} (emprunteur '{name}')."
                    )
                project_country_cache[project_id] = country
            except Exception as exc:
                log.exception("Failed to fetch country for project %s - leaving it unknown.", project_id)
                issues.append(
                    f"Erreur en récupérant le pays du projet {project_id} (emprunteur '{name}') : {exc}"
                )
                project_country_cache[project_id] = None

        country = project_country_cache[project_id] or ""
        entry = borrowers.setdefault(name, {})
        entry[country] = entry.get(country, 0.0) + loan["amount"]

    log.info("Active loans grouped by borrower: %d borrower(s) found.", len(borrowers))
    return borrowers, issues


def fetch_contract_dates(session: requests.Session, contract_id: str) -> tuple:
    """(maturity, next_due) dates of a loan contract from the JSON API
    behind the "Gérer mon prêt" page, /api/v1/contracts/{id} (verified live
    2026-10-16): maturity = "refundDateEstimation" (YYYY-MM-DD; 1st due +
    duesLeft - 1 months), next_due = "nextDue.dueDate" (the next instalment
    to be received). Either is None if absent."""
    r = session.get(
        f"https://www.bienpreter.com/api/v1/contracts/{contract_id}",
        headers={"Accept": "application/ld+json"},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    estimate = data.get("refundDateEstimation")
    next_due = (data.get("nextDue") or {}).get("dueDate")
    return (
        date.fromisoformat(estimate[:10]) if estimate else None,
        date.fromisoformat(next_due[:10]) if next_due else None,
    )


def fetch_litigation_loans(session: requests.Session) -> dict:
    """Late / defaulted active loans: {contract_id: {"default": bool,
    "next_payment_date": date | None}}, from the /u/mes-prets "Retards /
    Défauts" filter - "late" = retard < 6 mois, "default" = retard > 6 mois
    (the platform's own definition; verified live 2026-10-16 that the
    filter exists and that the account then had no such loan, so the row
    content of a late loan is NOT verified)."""
    result = {}
    for litigation, is_default in ((LITIGATION_LATE, False), (LITIGATION_DEFAULT, True)):
        loans, _ = fetch_active_loans(session, litigation=litigation)
        for loan in loans:
            if loan.get("contract_id"):
                result[loan["contract_id"]] = {"default": is_default, "next_payment_date": loan["next_payment_date"]}
    return result


def aggregate_remaining_durations(session: requests.Session, loans: list, today: date) -> dict:
    """Group the active loans (fetch_active_loans()) by borrower - same key
    as the geographic breakdown - then by remaining term as of `today`
    (months, days): {borrower: {(months, days): amount}}. Loans whose
    maturity can't be determined are skipped (with a warning).

    Late / defaulted loans (fetch_litigation_loans(): the platform's own
    "retard < 6 mois" / "retard > 6 mois" filters) are instead grouped by how
    long ago their missed instalment was due, via overdue_months_and_days():
    the earliest past date among the contract's `nextDue.dueDate` and the
    loan row's next payment date (INFERRED, not verified: no late loan existed
    to observe whether these still point at the missed instalment). If none
    is in the past, they keep their remaining term."""
    try:
        litigation_loans = fetch_litigation_loans(session)
    except Exception:
        log.exception("Failed to fetch the late/defaulted loans - using remaining terms only.")
        litigation_loans = {}

    result = {}
    for loan in loans:
        name = loan["borrower"]
        contract_id = loan.get("contract_id")
        try:
            maturity, next_due = fetch_contract_dates(session, contract_id) if contract_id else (None, None)
        except Exception:
            log.exception("Failed to fetch the dates of contract %s.", contract_id)
            maturity, next_due = None, None

        term = None
        litigation = litigation_loans.get(contract_id)
        if litigation:
            past_dues = [d for d in (next_due, litigation["next_payment_date"]) if d and d < today]
            if past_dues:
                term = overdue_months_and_days(today, min(past_dues), default=litigation["default"])
            else:
                log.warning("Late loan of %s (%.2f EUR) has no past due date - using its remaining term.", name, loan["amount"])
        if term is None:
            if maturity is None:
                log.warning("No maturity date found for a loan of %s (%.2f EUR) - skipped.", name, loan["amount"])
                continue
            term = remaining_months_and_days(today, maturity)
        durations = result.setdefault(name, {})
        durations[term] = durations.get(term, 0.0) + loan["amount"]
    return result


def _fetch_operations_page(session: requests.Session, start_date: str, end_date: str, page_number: int) -> list:
    """Fetch one page of https://www.bienpreter.com/u/operations (plain
    server-rendered HTML, no JSON API) filtered to the given date range,
    and extract each transaction row. See module docstring for the
    verified row markup (`.transaction__name`/`.transaction__amount`/
    `.transaction__interests`).

    Also parses (added 2026-08-14, needed for XIRR/Cash drag below):
    - "date": the row's own transaction date ("YYYY-MM-DD"), read from the
      `.transaction__date` cell's `title` attribute (e.g.
      "14/08/26 13:32", `%d/%m/%y %H:%M`) rather than its visible
      "14/08/26" text - same value, just avoids a second regex+strip.
    - "balance": the real "Solde indicatif" running balance right AFTER
      this transaction (`.transaction__balance` cell) - verified live to
      match the dashboard's own "Solde disponible" exactly for the most
      recent row, so this can be replayed directly instead of
      reconstructing a balance from summed per-type deltas.
    """
    url = f"{OPERATIONS_URL}?selected-tab=1&startDate={start_date}&endDate={end_date}&page={page_number}"
    log.info("GET operations page %d for %s to %s...", page_number, start_date, end_date)
    r = session.get(url, timeout=30)
    log.info("GET operations page %d: status=%s", page_number, r.status_code)
    r.raise_for_status()

    rows_html = re.findall(r"<tr[^>]*>.*?</tr>", r.text, re.DOTALL)
    rows = []
    for row_html in rows_html:
        name_match = re.search(r'transaction__name">(.*?)</p>', row_html, re.DOTALL)
        amount_match = re.search(r'transaction__amount[^"]*">(.*?)</', row_html, re.DOTALL)
        interest_matches = re.findall(r'transaction__interests[^"]*">(.*?)</', row_html, re.DOTALL)
        date_match = re.search(r'transaction__date"\s+title="([^"]+)"', row_html, re.DOTALL)
        balance_match = re.search(r'transaction__balance">(.*?)</', row_html, re.DOTALL)

        row_date = None
        if date_match:
            try:
                row_date = datetime.strptime(date_match.group(1).strip(), "%d/%m/%y %H:%M").strftime("%Y-%m-%d")
            except ValueError:
                row_date = None

        label = _strip_tags(name_match.group(1)) if name_match else None
        amount_text = _strip_tags(amount_match.group(1)) if amount_match else None
        interest_texts = [_strip_tags(t) for t in interest_matches]
        embedded_tax = 0.0

        # Repayment rows carry a "Capital remboursé / Intérêts remboursés / Prélèvements fiscaux et sociaux"
        # panel. Older rows (and early repayments) have no `.transaction__interests` cell at all, and their
        # amount is NET of the tax (no separate "Prélèvements fiscaux" row) - so the panel is the only
        # complete source for gross interest and that embedded tax.
        details_match = re.search(r'transaction__project__details[^>]*>(.*?)</div>\s*</td>', row_html, re.DOTALL)
        if details_match:
            details_text = html.unescape(_strip_tags(details_match.group(1)))
            panel_interests = re.findall(r"Intérêts remboursés\s*:\s*([\d.,\s]+?)\s*€", details_text)
            if panel_interests:
                interest_texts = panel_interests
            if label and label.startswith("Remboursement"):
                panel_capital = sum(_parse_amount(t) or 0.0 for t in re.findall(r"Capital remboursé\s*:\s*([\d.,\s]+?)\s*€", details_text))
                panel_tax = sum(_parse_amount(t) or 0.0 for t in re.findall(r"Prélèvements fiscaux et sociaux\s*:\s*([\d.,\s]+?)\s*€", details_text))
                panel_interest_total = sum(_parse_amount(t) or 0.0 for t in interest_texts)
                row_amount = abs(_parse_amount(amount_text) or 0.0)
                if panel_tax > 0 and abs(row_amount - (panel_capital + panel_interest_total - panel_tax)) <= 0.011:
                    embedded_tax = panel_tax

        rows.append(
            {
                "label": label,
                "amountText": amount_text,
                "interestTexts": interest_texts,
                "embeddedTax": embedded_tax,
                "date": row_date,
                "balance": _parse_amount(_strip_tags(balance_match.group(1))) if balance_match else None,
            }
        )
    return rows


def fetch_all_operations(session: requests.Session, start_date: str, end_date: str) -> list:
    """Fetch EVERY /u/operations row within [start_date, end_date]
    ("YYYY-MM-DD" strings), paginating via the `page` query param until a
    page returns no real rows (bounded by MAX_OPERATIONS_PAGES as a safety
    net) - the shared pagination loop behind both
    fetch_current_month_interest_totals() (this month only) and
    get_cached_operations() (full/incremental history, for XIRR/Cash drag).
    """
    all_rows = []
    for page_number in range(1, MAX_OPERATIONS_PAGES + 1):
        rows = _fetch_operations_page(session, start_date, end_date, page_number)
        rows = [r for r in rows if r.get("label")]
        log.info("Operations page %d (%s to %s): %d real row(s) found.", page_number, start_date, end_date, len(rows))
        if not rows:
            break
        all_rows.extend(rows)
    else:
        log.warning(
            "Hit MAX_OPERATIONS_PAGES (%d) without an empty page (%s to %s) - results may be truncated.",
            MAX_OPERATIONS_PAGES, start_date, end_date,
        )
    return all_rows


def get_cached_operations(session: requests.Session, end_date: date) -> list:
    """Return every /u/operations row since account inception through
    `end_date`, fetching from the site only the rows NOT already cached
    locally (in XIRR_CASHFLOWS_STATE_FILE) - same incremental-fetch idea
    as afranga_diversification.get_cached_account_details()/
    swaper_diversification.get_cached_account_cashflows() (see those
    docstrings for the full rationale). Re-fetches starting from the
    cached `last_fetched_date` itself (not the day after) so a row booked
    on that same day, added after the previous run already fetched it,
    isn't missed - duplicates are dropped by deduplicating on
    (date, label, amountText, balance): the real "balance" (a precise
    running total) makes an accidental collision between two genuinely
    different transactions extremely unlikely.
    """
    state = load_state(XIRR_CASHFLOWS_STATE_FILE, XIRR_CASHFLOWS_STATE_DEFAULT)
    if state.get("schema_version") != XIRR_CACHE_SCHEMA_VERSION:
        log.info("Operations cache has an outdated row shape - discarding it and re-fetching the full history.")
        state = dict(XIRR_CASHFLOWS_STATE_DEFAULT)
    cached_rows = state.get("rows", [])
    last_fetched_date = (
        datetime.strptime(state["last_fetched_date"], "%Y-%m-%d").date() if state.get("last_fetched_date") else None
    )
    start_date = (
        max(XIRR_HISTORY_START_DATE, last_fetched_date - timedelta(days=XIRR_CACHE_OVERLAP_DAYS))
        if last_fetched_date else XIRR_HISTORY_START_DATE
    )

    if start_date > end_date:
        # Cache already covers past end_date (e.g. a live run advanced it,
        # then a backfill run asked for an earlier REPORT_DATE) - skip the
        # fetch instead of sending an inverted start>end range to the API.
        log.info(
            "Cache already covers up to %s (requested end date %s) - skipping fetch, using cached data only.",
            start_date, end_date,
        )
        return cached_rows

    log.info(
        "Found %d cached operation row(s) (last fetched up to %s) - fetching only %s to %s...",
        len(cached_rows), state.get("last_fetched_date"), start_date, end_date,
    )
    new_rows = fetch_all_operations(session, start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d"))

    seen = set()
    merged = []
    for row in cached_rows + new_rows:
        key = (row.get("date"), row.get("label"), row.get("amountText"), row.get("balance"))
        if key in seen:
            continue
        seen.add(key)
        merged.append(row)

    save_state(XIRR_CASHFLOWS_STATE_FILE, {
        "rows": merged,
        "last_fetched_date": max(end_date, last_fetched_date or end_date).strftime("%Y-%m-%d"),
        "schema_version": XIRR_CACHE_SCHEMA_VERSION,
    })
    log.info("Operations cache now holds %d row(s) (was %d before this run).", len(merged), len(cached_rows))
    return merged


def _day_end_balances(rows: list) -> list:
    """[(day, closing 'Solde indicatif')] ascending. Same-day rows have no reliable order in the cache,
    so each day's sequence is rebuilt by chaining balance == previous balance + amount."""
    by_day = {}
    for r in rows:
        if r.get("date") and r.get("balance") is not None:
            by_day.setdefault(r["date"], []).append(r)
    result = []
    current = 0.0
    for day in sorted(by_day):
        remaining = list(by_day[day])
        while remaining:
            nxt = next(
                (r for r in remaining if abs(current + (_parse_amount(r.get("amountText")) or 0.0) - r["balance"]) <= 0.011),
                None,
            )
            if nxt is None:
                nxt = remaining[-1]
            remaining.remove(nxt)
            current = nxt["balance"]
        result.append((day, current))
    return result


def compute_average_idle_cash(rows: list, start_date: str, end_date: str) -> float:
    """Day-weighted average uninvested-cash balance across [start_date,
    end_date] ("YYYY-MM-DD" strings). Unlike
    swaper_diversification.compute_average_idle_cash()/
    afranga_diversification.compute_average_idle_cash() (which both
    reconstruct a running balance from summed signed per-type deltas,
    since neither platform exposes a real per-transaction balance),
    Bienpreter's own operations rows already carry the REAL "Solde
    indicatif" balance right after every transaction (see
    _fetch_operations_page()'s "balance" field) - so this just replays
    those real snapshots day-by-day instead of reconstructing anything.
    `rows` should be the FULL history (or at least back to before
    `start_date`) so the balance carried INTO `start_date` is accurate -
    passing only rows already restricted to [start_date, end_date] would
    wrongly start the average from 0.0. Returns 0.0 if no dated/balanced
    row is available at all (e.g. before the account's very first
    transaction).
    """
    dated_balances = _day_end_balances(rows)
    if not dated_balances:
        return 0.0

    try:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        end = datetime.strptime(end_date, "%Y-%m-%d").date()
    except ValueError:
        return 0.0

    balance_by_day = {}
    for day_str, balance in dated_balances:
        balance_by_day[day_str] = balance  # ascending order -> last write = latest same-day transaction

    running_balance = 0.0  # before the account's very first transaction, balance is genuinely 0
    start_str = start.strftime("%Y-%m-%d")
    for day_str, balance in dated_balances:
        if day_str >= start_str:
            break
        running_balance = balance

    total_balance = 0.0
    day_count = 0
    current = start
    while current <= end:
        key = current.strftime("%Y-%m-%d")
        if key in balance_by_day:
            running_balance = balance_by_day[key]
        total_balance += running_balance
        day_count += 1
        current += timedelta(days=1)

    return total_balance / day_count if day_count else 0.0


def _balance_as_of(rows: list, as_of_date: date) -> float:
    """Real 'Solde indicatif' available-cash balance as of `as_of_date`
    (the most recent recorded balance on or before that date, 0.0 if
    before the account's very first transaction) - same snapshot-replay
    technique as compute_average_idle_cash(), used to reconstruct a
    backfilled month's "solde disponible" (see module docstring's
    2026-09-07 backward-reconstruction addition)."""
    as_of_str = as_of_date.strftime("%Y-%m-%d")
    dated_balances = _day_end_balances(rows)
    balance = 0.0
    for day_str, bal in dated_balances:
        if day_str > as_of_str:
            break
        balance = bal
    return balance


def _total_account_value_delta_for_row(row: dict) -> float:
    """Signed change in TOTAL account value (solde disponible + capital à
    recevoir) caused by a single /u/operations row - real external
    cashflows (Dépôt de fonds/Retrait de fonds) and earnings (Intérêts/
    Bonus/Prélèvements fiscaux) change the total; every other row type
    (Investissement, Remboursement mensuel, Vente de prêt, Rétractation de
    l'intention de prêt) is a pure cash<->invested-capital reallocation,
    net zero on the total - see _net_value_change()'s docstring, which
    reuses this per-row classification summed over a date range."""
    label = row.get("label") or ""
    amount = abs(_parse_amount(row.get("amountText")) or 0.0)
    delta = 0.0
    if label == "Dépôt de fonds":
        delta += amount
    elif label == "Retrait de fonds":
        delta -= amount
    elif label == "Bonus":
        delta += amount
    elif label == "Prélèvements fiscaux":
        delta -= amount
    delta -= row.get("embeddedTax") or 0.0
    for interest_text in row.get("interestTexts") or []:
        delta += _parse_amount(interest_text) or 0.0
    return delta


def _net_value_change(rows: list, start_date: date, end_date: date) -> float:
    """Net change in TOTAL account value (solde disponible + capital à
    recevoir) caused by every transaction dated in (start_date, end_date] -
    see _total_account_value_delta_for_row() for the per-row
    classification. Used to reconstruct a past total_account_value from
    today's live total by subtracting off everything that happened AFTER
    `start_date` (see module docstring's 2026-09-07 backward-
    reconstruction addition)."""
    start_str = start_date.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d")
    delta = 0.0
    for row in rows:
        row_date = row.get("date")
        if not row_date or not (start_str < row_date <= end_str):
            continue
        delta += _total_account_value_delta_for_row(row)
    return delta


def compute_average_balances(rows: list, start_date: date, end_date: date) -> tuple:
    """Day-weighted average INVESTED ("capital à recevoir")/NON-INVESTED
    ("solde disponible") balances over [start_date, end_date] - REAL,
    day-by-day, not the point-in-time approximation this used before
    2026-09-08. "non investi" replays the real "Solde indicatif" snapshots
    (compute_average_idle_cash(), unchanged). "investi" is derived as
    avg_total_account_value - avg_non_invested: TOTAL account value can be
    built FORWARD from 0.0 at account inception using
    compute_time_weighted_average() fed by
    _total_account_value_delta_for_row()'s per-row classification (only
    real external cashflows/earnings move it; Investissement/
    Remboursement/Vente de prêt/Rétractation are pure cash<->invested
    reallocations, net zero on the total - so no invested-side
    reconstruction is actually needed at all). Since total(d) =
    invested(d) + non_invested(d) holds for every single day, averaging
    that identity over the period gives avg_invested = avg_total -
    avg_non_invested exactly, with no loss of precision.
    `rows` should cover the account's FULL history (see
    get_cached_operations())."""
    total_value_events = [
        (datetime.strptime(row["date"], "%Y-%m-%d").date(), _total_account_value_delta_for_row(row))
        for row in rows if row.get("date")
    ]
    avg_total_account_value = compute_time_weighted_average(total_value_events, start_date, end_date)
    avg_non_invested = compute_average_idle_cash(rows, start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d"))
    avg_invested = avg_total_account_value - avg_non_invested
    return avg_invested, avg_non_invested


def fetch_current_month_interest_totals(session: requests.Session) -> dict:
    """Fetch this calendar month's interest received, split into net/gross/
    withholding tax (plus this month's real "Bonus" total, see below), from
    the "Toutes mes op\u00e9rations" page. See module docstring / historical
    Playwright-version docstring (kept in repo memory) for the full
    reasoning behind reconstructing gross/net from `.transaction__interests`
    + "Pr\u00e9l\u00e8vements fiscaux" rows instead of `.transaction__amount` directly
    (Bienpreter loans are "In Fine" - capital bundled into a repayment
    row's total would otherwise contaminate the interest figure).

    Uses REPORT_TIMEZONE (Europe/Paris) to decide "this month" (1st of the
    current month through TODAY, not the full month).
    """
    now = get_report_now(REPORT_TIMEZONE)
    start_date = now.replace(day=1).strftime("%Y-%m-%d")
    end_date = now.strftime("%Y-%m-%d")

    rows = fetch_all_operations(session, start_date, end_date)

    gross_interest_received = 0.0
    withholding_tax = 0.0
    bonus_total = 0.0
    for row in rows:
        for interest_text in row.get("interestTexts") or []:
            gross_interest_received += _parse_amount(interest_text) or 0.0

        label = row.get("label") or ""
        withholding_tax += row.get("embeddedTax") or 0.0
        if label == "Pr\u00e9l\u00e8vements fiscaux":
            withholding_tax += abs(_parse_amount(row.get("amountText")) or 0.0)
        elif label == "Bonus":
            bonus_total += abs(_parse_amount(row.get("amountText")) or 0.0)

    net_interest_received = gross_interest_received - withholding_tax
    log.info(
        "Parsed interest totals: gross_interest_received=%.2f, withholding_tax=%.2f, "
        "net_interest_received=%.2f, bonus_total=%.2f",
        gross_interest_received, withholding_tax, net_interest_received, bonus_total,
    )
    return {
        "net_interest_received": net_interest_received,
        "withholding_tax": withholding_tax,
        "gross_interest_received": gross_interest_received,
        "bonus_total": bonus_total,
    }


def run() -> None:
    if not BIENPRETER_EMAIL or not BIENPRETER_PASSWORD:
        log.error("BIENPRETER_EMAIL and BIENPRETER_PASSWORD environment variables are required.")
        sys.exit(1)

    log.info("Starting Bienpreter diversification run (pure HTTP, no browser).")

    session = requests.Session()
    session.headers.update(_HEADERS)

    try:
        # The dashboard HTML is only an "is the persisted session still
        # authenticated" probe - the balances themselves come from the API.
        get_or_refresh_session(
            session, SESSION_STATE_FILE,
            fetch_fn=lambda extra: _fetch_dashboard_html(session),
            login_fn=lambda: (login(session), {}),
            platform_name="Bienpreter",
        )
        balances = fetch_balances(session)
    except Exception:
        log.exception("Failed to log in or fetch Bienpreter balances.")
        sys.exit(1)

    try:
        log.info("Fetching this month's interest totals from the operations page...")
        interest_totals = fetch_current_month_interest_totals(session)
    except Exception:
        log.exception("Failed to fetch this month's interest totals - defaulting all figures to 0.0.")
        interest_totals = {
            "net_interest_received": 0.0, "withholding_tax": 0.0,
            "gross_interest_received": 0.0, "bonus_total": 0.0,
        }

    total = balances["available_balance"] + balances["capital_to_receive"]
    interest_totals["total"] = total
    # UPDATED 2026-08-14: the 2026-07-17 "no bonus/cashback/contest
    # TRANSACTION shows up in /u/operations" finding turned out to be
    # stale - a real "Bonus" transaction type DOES exist on this account
    # now (confirmed live 2026-08-14, 3 lifetime rows of 500/100/10 EUR) -
    # bonus_total (this month's real sum, from fetch_current_month_interest_totals())
    # replaces the old hardcoded 0.0 placeholder.
    interest_totals["bonus_cashback_contest"] = interest_totals.get("bonus_total", 0.0)
    log.info(
        "Bienpreter: solde disponible=%.2f EUR + capital à recevoir=%.2f EUR = %.2f EUR",
        balances["available_balance"], balances["capital_to_receive"], total,
    )
    log.info(
        "This month's interest totals: gross_interest_received=%.2f EUR, net_interest_received=%.2f EUR, "
        "withholding_tax=%.2f EUR, bonus_total=%.2f EUR",
        interest_totals["gross_interest_received"], interest_totals["net_interest_received"],
        interest_totals["withholding_tax"], interest_totals["bonus_total"],
    )

    # "total"/"balances" fetched above are always a LIVE snapshot (as of
    # real_today, no date param exists on the dashboard) - XIRR/Cash drag
    # can still be computed "as of" a backfilled today_date though, by
    # reconstructing that date's total account value BACKWARD from today's
    # live total (see module docstring's 2026-09-07 addition).
    current_month = is_current_month()

    # Since-inception XIRR (money-weighted return) + this month's/lifetime
    # Cash drag + the XIRR Bonus/Cash drag/Taxes-Frais/Intérêts pie-chart
    # shares - mirrors swaper_diversification.py's/afranga_diversification.py's
    # own XIRR blocks (see those modules' docstrings for the full
    # methodology; see THIS module's docstring for the Bienpreter-specific
    # differences - a real per-transaction "Solde indicatif" balance is
    # already on every /u/operations row, so no delta-reconstruction is
    # needed here).
    today_date = get_report_now(REPORT_TIMEZONE).date()  # the date this block is computed "as of" (REPORT_DATE if set, else real today)
    real_today = date.today()  # always the actual current date - operations must be fetched through here so a backfilled today_date can subtract every intervening cashflow/earnings event from today's live total
    xirr_value = None
    bonus_xirr_contribution = None
    cash_drag_brut_value = None
    cash_drag_net_value = None
    cash_drag_xirr_contribution = None
    taxes_xirr_contribution = None
    frais_xirr_contribution = None
    interest_xirr_contribution = None
    rendement_brut_value = None
    monthly_yield_shares: dict = {}

    all_operations = None
    try:
        log.info("Fetching the since-inception operations history (cached where possible) for XIRR/Cash drag...")
        all_operations = get_cached_operations(session, real_today)
    except Exception:
        log.exception("Failed to fetch the operations history - XIRR/Cash drag will not be updated.")
        all_operations = None

    total_invested = balances["capital_to_receive"]
    avg_invested_balance = None
    avg_non_invested_balance = None
    if all_operations:
        today_date_str = today_date.strftime("%Y-%m-%d")
        # Rows after today_date belong to a backfilled month's future (real
        # now, not today_date) - excluded from every "as of today_date"
        # figure below via operations_as_of; `all_operations` (unfiltered)
        # is still needed for the backward reconstruction just below, which
        # specifically looks PAST today_date.
        operations_as_of = [r for r in all_operations if r.get("date") and r["date"] <= today_date_str]

        # Day-weighted average invested/non-invested balances (new Sheet
        # rows "solde moyen pondéré investi"/"non investi", added
        # 2026-09-08) - computed here (moved 2026-09-11, ahead of Cash
        # drag below) so Cash drag can be derived FROM these same two
        # averages instead of a live total_invested snapshot.
        month_start_date = today_date.replace(day=1)
        avg_invested_balance, avg_non_invested_balance = compute_average_balances(
            operations_as_of, month_start_date, today_date
        )
        log.info(
            "Solde moyen pondéré - investi: %.2f EUR, non investi: %.2f EUR (%s to %s).",
            avg_invested_balance, avg_non_invested_balance, month_start_date, today_date,
        )

        if current_month:
            total_account_value = total  # solde disponible + capital à recevoir, same "as if withdrawn today" value used elsewhere in this repo
        else:
            # Backfilled month: rebuild today_date's total account value
            # FORWARD from inception (same per-row deltas as
            # compute_average_balances()). The old backward reconstruction
            # from today's live total carried every later loss/drift into
            # past months and produced absurd XIRRs (e.g. 330769% for 2025-04).
            total_account_value = sum(_total_account_value_delta_for_row(r) for r in operations_as_of)
            available_balance_as_of = _balance_as_of(operations_as_of, today_date)
            total_invested = total_account_value - available_balance_as_of
            log.info(
                "Backfilled month (%s): forward-reconstructed total_account_value=%.2f EUR, "
                "available_balance_as_of=%.2f EUR, total_invested=%.2f EUR.",
                today_date, total_account_value, available_balance_as_of, total_invested,
            )

        signed_cashflows = []
        for row in operations_as_of:
            if not row.get("date"):
                continue
            row_date = datetime.strptime(row["date"], "%Y-%m-%d").date()
            amount = abs(_parse_amount(row.get("amountText")) or 0.0)
            if row["label"] == "Dépôt de fonds":
                signed_cashflows.append((row_date, -amount))
            elif row["label"] == "Retrait de fonds":
                signed_cashflows.append((row_date, amount))
        signed_cashflows.sort(key=lambda t: t[0])
        signed_cashflows.append((today_date, total_account_value))

        xirr_value = compute_xirr(signed_cashflows)
        if xirr_value is not None and abs(xirr_value) < 1e-9:
            xirr_value = 0.0  # solver noise (e.g. -3.7e-15) when there is genuinely no return yet
        if xirr_value is None:
            log.warning("Could not compute XIRR from %d cashflow(s) - XIRR row will not be updated.", len(signed_cashflows) - 1)
        else:
            log.info(
                "Computed since-inception XIRR: %.2f%% (%d deposit/withdrawal cashflow(s), current total value %.2f EUR).",
                xirr_value * 100, len(signed_cashflows) - 1, total_account_value,
            )

            # Lifetime gross interest received (real "Intérêts" amount
            # credited to the account since inception, from every
            # /u/operations row's `.transaction__interests` cell) - used
            # below both for XIRR Intérêts (right away) and for Cash
            # drag's lifetime yield rate (further down, reused instead of
            # recomputed).
            lifetime_gross_interest = sum(
                _parse_amount(t) or 0.0 for r in operations_as_of for t in (r.get("interestTexts") or [])
            )

            lifetime_bonus_total = sum(
                abs(_parse_amount(r.get("amountText")) or 0.0) for r in operations_as_of if r["label"] == "Bonus"
            )
            bonus_rows = [r for r in operations_as_of if r["label"] == "Bonus"]
            log.info(
                "Bonus: %d transaction(s) trouvée(s), total lifetime = %.2f EUR (dates: %s).",
                len(bonus_rows), lifetime_bonus_total,
                [r.get("date") for r in bonus_rows],
            )

            lifetime_withholding_tax = sum(
                abs(_parse_amount(r.get("amountText")) or 0.0) for r in operations_as_of if r["label"] == "Prélèvements fiscaux"
            ) + sum(r.get("embeddedTax") or 0.0 for r in operations_as_of)
            lifetime_net_interest = lifetime_gross_interest - lifetime_withholding_tax

            # cash_weight/monthly_yield_rate use the already-computed
            # avg_invested_balance/avg_non_invested_balance (same figures
            # as the "solde moyen pondéré" Sheet rows above) instead of
            # the live total_invested snapshot (fixed 2026-09-11), so this
            # % is exactly reconstructible from those two Sheet rows. The
            # lifetime share below still uses total_invested (no
            # lifetime-average equivalent exists).
            today_str = today_date.strftime("%Y-%m-%d")
            # Cash drag/Rendements % brut's DENOMINATOR uses the PREVIOUS
            # calendar month's average balances, not this month's - added
            # 2026-09-15. Bienprêter pays interest with a one-month lag (a
            # given month's accrued interest is only credited/visible the
            # FOLLOWING month), so the interest actually received THIS
            # month was earned by whatever capital was invested during the
            # PRIOR month, not this one. The NUMERATOR
            # (interest_totals[...] below) deliberately stays on THIS
            # month. Deliberately a SEPARATE pair of averages from
            # avg_invested_balance/avg_non_invested_balance above (which
            # stays THIS month - it feeds the standalone "solde moyen
            # pondéré" Sheet rows, unrelated to this fix).
            prev_month_end_date = month_start_date - timedelta(days=1)
            prev_month_start_date = prev_month_end_date.replace(day=1)
            avg_invested_prev_month, avg_non_invested_prev_month = compute_average_balances(
                operations_as_of, prev_month_start_date, prev_month_end_date
            )
            if avg_invested_prev_month is not None and avg_invested_prev_month > 0:
                cash_weight = avg_non_invested_prev_month / (avg_non_invested_prev_month + avg_invested_prev_month)
                monthly_yield_rate_brut = interest_totals["gross_interest_received"] / avg_invested_prev_month
                monthly_yield_rate_net = interest_totals["net_interest_received"] / avg_invested_prev_month
                cash_drag_brut_value = cash_weight * monthly_yield_rate_brut
                cash_drag_net_value = cash_weight * monthly_yield_rate_net
                log.info(
                    "Computed Cash drag: brut=%.2f%% net=%.2f%% (avg idle cash %.2f EUR, cash weight %.2f%%).",
                    cash_drag_brut_value * 100, cash_drag_net_value * 100, avg_non_invested_prev_month, cash_weight * 100,
                )

                # Monthly gross-yield waterfall ("Rendements % brut" block,
                # added 2026-09-14) - the non-annualized, this-month-only
                # sibling of the since-inception XIRR waterfall below. See
                # shared/monthly_yield_waterfall.py's module docstring for
                # why this is a plain division (no IRR-solving needed).
                # Bienprêter has no platform-fee concept distinct from
                # withholding tax (same reasoning as "XIRR Frais" below).
                avg_total_balance_month = avg_invested_prev_month + avg_non_invested_prev_month
                missed_earnings_month = cash_drag_brut_value * avg_total_balance_month
                monthly_yield_steps = [
                    ("Intérêts brut %", interest_totals["gross_interest_received"] + missed_earnings_month),
                    ("Cash drag brut %", -missed_earnings_month),
                    ("Bonus brut %", interest_totals["bonus_cashback_contest"]),
                    ("Frais brut %", 0.0),
                    ("Taxes brut %", -interest_totals["withholding_tax"]),
                ]
                monthly_yield_shares = compute_monthly_yield_shares(
                    avg_total_balance_month, monthly_yield_steps, log=log, log_context="Bienprêter",
                )
                rendement_brut_value = sum(v for v in monthly_yield_shares.values() if v is not None)
                log.info(
                    "Monthly gross-yield waterfall shares: Rendements %% brut=%.2f%% %r",
                    rendement_brut_value * 100, {k: round(v * 100, 4) for k, v in monthly_yield_shares.items() if v is not None},
                )

            # Lifetime waterfall does not depend on the previous month's balances,
            # so it must also run for an account's first month (no prior capital).
            deposit_dates = [r["date"] for r in operations_as_of if r.get("date") and r["label"] == "Dépôt de fonds"]
            if deposit_dates and total_invested > 0:
                since_inception_date = datetime.strptime(min(deposit_dates), "%Y-%m-%d").date()
                years_elapsed = max((today_date - since_inception_date).days / 365.25, 1 / 365.25)
                # lifetime_gross_interest already computed above (used
                # by XIRR Intérêts too) - reused here, not recomputed.
                avg_idle_cash_lifetime = compute_average_idle_cash(
                    operations_as_of, since_inception_date.strftime("%Y-%m-%d"), today_str
                )
                # Time-weighted like the idle cash: end-of-period total_invested understates the yield on a fast-growing account.
                avg_invested_lifetime = compute_average_balances(operations_as_of, since_inception_date, today_date)[0]
                if avg_invested_lifetime is None or avg_invested_lifetime <= 0:
                    avg_invested_lifetime = total_invested
                cash_weight_lifetime = avg_idle_cash_lifetime / (avg_idle_cash_lifetime + avg_invested_lifetime)
                lifetime_yield_rate = lifetime_gross_interest / avg_invested_lifetime
                cash_drag_lifetime_total = cash_weight_lifetime * lifetime_yield_rate
                missed_earnings = cash_drag_lifetime_total * (avg_idle_cash_lifetime + avg_invested_lifetime)

                # Waterfall decomposition (switched from Shapley
                # 2026-09-09, see shared/xirr_waterfall.py's module
                # docstring for why): walks a true 0%-return baseline
                # up to total_account_value in the fixed order
                # Intérêts -> Cash drag -> Bonus -> Frais -> Taxes,
                # using GROSS interest (not net) at the Intérêts step
                # and subtracting missed_earnings right after - each
                # euro counted exactly once, so the shares sum
                # EXACTLY to XIRR real (checked at runtime via a
                # warning log). Bienprêter has no platform-fee
                # concept distinct from withholding tax
                # ("Prélèvements fiscaux" is the only fiscal/fee-like
                # operation type ever seen on this account) - "XIRR
                # Frais" is hardcoded to 0.0 rather than
                # duplicating/inventing a value, and is skipped from
                # the steps below.
                steps = [
                    ("XIRR Intérêts", lifetime_gross_interest + missed_earnings),
                    ("XIRR Cash drag", -missed_earnings),
                    ("XIRR Bonus", lifetime_bonus_total),
                    ("XIRR Taxes", -lifetime_withholding_tax),
                ]
                waterfall_shares = compute_waterfall_xirr_shares(
                    signed_cashflows[:-1], today_date, total_account_value, steps,
                    log=log, log_context="Bienprêter",
                )
                bonus_xirr_contribution = waterfall_shares.get("XIRR Bonus")
                cash_drag_xirr_contribution = waterfall_shares.get("XIRR Cash drag")
                taxes_xirr_contribution = waterfall_shares.get("XIRR Taxes")
                frais_xirr_contribution = 0.0
                interest_xirr_contribution = waterfall_shares.get("XIRR Intérêts")
                log.info(
                    "XIRR Waterfall shares (since-inception, %.2f years, missed earnings ~%.2f EUR): %r",
                    years_elapsed, missed_earnings, {k: round(v * 100, 4) for k, v in waterfall_shares.items() if v is not None},
                )

    # "total" = solde disponible + capital à recevoir, both scraped from
    # LIVE-only dashboard widgets with no date param and no historical/
    # closing-balance equivalent found anywhere on the site (2026-08-06
    # investigation) - skip it for a backfilled month.
    fill_current_month_amounts(
        platform="Bienprêter",
        amounts=interest_totals,
        skip_total=not current_month,
    )

    # "Bonus" now gets the real "Bonus" transaction total (see above -
    # replaces the old placeholder). "prélèvements" (withholding tax on
    # interest, real figure - see fetch_current_month_interest_totals())
    # is a separate sub-row in the same block, right before "Rendements %"
    # - verified live 2026-08-05. "Cash drag"/"XIRR"/"XIRR Bonus"/
    # "XIRR Cash drag"/"XIRR Taxes/Frais" (added 2026-08-14, only included
    # when actually computed) sit right after "concours" - verified live
    # at platform_row+9 through +13.
    # UPDATED 2026-08-18: "XIRR Intérêts" (also only included when
    # actually computed) sits right after "XIRR Taxes/Frais" - this pushes
    # the block one row taller than it was verified at 2026-08-14, so
    # `max_rows` is bumped 14 -> 15 to keep the search bounded before the
    # next platform block ("Hive5", now platform_row+17 instead of +16).
    # IMPORTANT: a "XIRR Intérêts" row must exist in the Bienprêter block
    # on the sheet itself (right after "XIRR Taxes/Frais") for this new
    # value to actually land somewhere - this script fills an existing
    # row by label, it doesn't insert new labelled rows into this block.
    bonus_breakdown = {
        "Bonus": interest_totals["bonus_cashback_contest"],
        "prélèvements": interest_totals["withholding_tax"],
    }
    if rendement_brut_value is not None:
        bonus_breakdown["Rendements % brut"] = rendement_brut_value
    for step_name in ("Intérêts brut %", "Cash drag brut %", "Bonus brut %", "Frais brut %", "Taxes brut %"):
        step_value = monthly_yield_shares.get(step_name)
        if step_value is not None:
            bonus_breakdown[step_name] = step_value
    if xirr_value is not None:
        bonus_breakdown["XIRR"] = xirr_value
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
        platform="Bienprêter",
        breakdown=bonus_breakdown,
    )

    # "Répartition géographique" per-borrower breakdown (added 2026-07-31,
    # per explicit user request): does NOT touch the "Bienprêter" row's own
    # total (kept manual) - only the borrower sub-rows below it, one per
    # active loan's company, amount placed under its loan's country column.
    # Any missing/ambiguous country or unexpected error is emailed (per
    # explicit user request), not just logged - never blocks the rest of
    # the run (Crowdlending section writes already happened above).
    geo_issues = []
    geo_error = None
    if current_month:
        try:
            log.info("Fetching active loans grouped by borrower (for the geographic breakdown)...")
            borrowers, fetch_issues = fetch_active_loans_by_borrower(session)
            geo_issues.extend(fetch_issues)
            geo_issues.extend(fill_bienpreter_borrower_geo_amounts(borrowers))
        except Exception as exc:
            log.exception("Failed to update the Bienprêter geographic breakdown by borrower.")
            geo_error = str(exc)

        # "non investi" row (added 2026-08-10): the same "solde disponible"
        # already scraped above for the Crowdlending section's "total".
        try:
            fill_geographic_repartition_uninvested_amount("Bienprêter", balances["available_balance"])
        except Exception as exc:
            log.exception("Failed to update Bienprêter's 'non investi' row.")
            geo_error = geo_error or str(exc)

        # Own try/except: the "durée" tab is a secondary view, its failure
        # must not fail the whole run.
        try:
            active_loans, _ = fetch_active_loans(session)
            fill_remaining_duration_amounts(
                "Bienprêter", aggregate_remaining_durations(session, active_loans, today_date), platform_level=True, projects=True
            )
        except Exception:
            log.exception("Failed to update the 'durée' sheet for Bienprêter.")

    if geo_issues or geo_error:
        send_bienpreter_geo_issues_email(geo_issues, error=geo_error)


if __name__ == "__main__":
    run()