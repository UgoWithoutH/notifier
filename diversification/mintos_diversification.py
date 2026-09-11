"""Mintos loan portfolio fetcher (this month's interest + invested total).

Same family as bricks_diversification.py / monefit_diversification.py: pure
`requests` (no Playwright at runtime), reads the account's invested total,
its loans-vs-obligations OUTSTANDING split, and this calendar month's
interest received (also split loans-vs-obligations) / withholding tax, then
hands them to fill_current_month_amounts_with_labels() /
fill_geographic_repartition_amounts() (see google_sheet.py) so they can be
filled into the Google Sheet. No email is sent - same as the other
diversification scripts.

Sheet layout note (added 2026-07-29): Mintos' block in the "Crowdlending"
section was split by hand from a single merged "intérêts brut" row (right
below the "Mintos" row) into 4 individually-labeled sub-rows: "en cours
prêts", "en cours obligations", "intérêts brut prêts", "intérêts brut
obligations" (verified live at rows 47-51 of the "dashboard 2026" sheet).
"encours total" (the aggregate `invested` figure) is unaffected and still
written directly onto the "Mintos" row itself, same as before - only the
row(s) below it changed shape, hence the switch from
fill_current_month_amounts() (which hardcodes "the row right below the
platform is a single merged interest row") to
fill_current_month_amounts_with_labels() (label-matched, like
fill_current_month_bonus_breakdown()).

Why pure `requests` here (unlike an earlier Playwright-based version of
this file): Mintos's DATA endpoints (see below) are NOT bot-protected at
all - a plain authenticated HTTP request works with zero extra headers
beyond a CSRF token that's readable straight out of the server-rendered
HTML (no JS execution needed - confirmed live on 2026-07-24 with a plain
`requests.get()`, no browser). The LOGIN step, however, is gated by Google
reCAPTCHA Enterprise (site key `6Ldx1tcpAAAAAHgB7BUqc2A4h1Jn8ECfq416N2wT`)
which sometimes shows an explicit interactive puzzle (confirmed live via
real test logins - it's adaptive/risk-based, not deterministic, and more
likely to trigger from a datacenter/CI IP than a residential one). Since
CAPTCHAs are never programmatically solved/bypassed (repo security policy),
login is handled OUT OF BAND by mintos_get_session.py - a separate,
LOCAL-only helper you run by hand (from your own machine/network) whenever
the session below has gone stale; see that file's docstring. This script
only ever does the (unprotected) DATA-fetching part, authenticating with the
two session cookies that helper prints.

Session cookies (MINTOS_PHPSESSID / MINTOS_MW_SESSION_ID env vars): Mintos
renews these on every authenticated request (a fresh `Set-Cookie` with a
later `Expires` was observed on every API call tested on 2026-07-24), so as
long as this script runs at least once within roughly 15 minutes of the
last use, the session should keep sliding forward indefinitely without
needing mintos_get_session.py to be re-run. If the cookies are missing/
stale, run() fails fast with a clear message instead of silently doing
nothing.

Data endpoints (all verified live on 2026-07-24 against a real authenticated
account, currency EUR = Mintos's numeric ISO 4217 id 978):

- `GET /webapp/api/marketplace-api/v1/accounts/978` -> NESTED fields, e.g.
  `{"invested": {"currency": "EUR", "amount": "836.40..."}, "available":
  {...}, ...}` - "invested" is this account's total loans portfolio value
  (matches the "Prêts" figure on the Portfolio Dashboard). Still used as-is
  for the "encours total" figure (unchanged by the 2026-07-29 split below).
- `GET /webapp/api/marketplace-api/v1/user/overview/currency/978` ->
  `{"total": {"value": ...}, "loans": {"value": ...}, "bonds": {"value":
  ...}, "realEstate": {...}, "etf": {...}, "smartCash": {...},
  "fundraising": {...}, "cryptoEtp": {...}, "singleStock": {...},
  "singleEtf": {...}}` (verified live 2026-07-29; cross-checked identity
  `total.value == loans.value + bonds.value + realEstate.value + ... +
  available_cash`, exact match on a real account). Used here for the
  "en cours prêts" / "en cours obligations" outstanding split
  (`fetch_portfolio_split()`); the other categories were 0 on the account
  tested and are not currently folded into either bucket.
- `GET /webapp/api/marketplace-api/v1/accounts/978/portfolio-distributions`
  -> `{"loanOriginators": {"distribution": [{"name": "ID Finance", "total":
  {"currency": "EUR", "amount": "169.34..."}, "count": 51}, ...]},
  "countries": {"distribution": [...]}, ...}` - per-LOAN-originator invested
  amounts (`fetch_loan_originator_breakdown()`); names have no date suffix.
- `GET /webapp/api/bonds-api/v3/investments/current` -> `{"items": [
  {"bondName": "ID Finance Sep 2028", "investedAmount": {"amount": "214.0"},
  ...}, ...]}` (verified live 2026-07-29) - per-BOND-issuer invested amounts
  (`fetch_bond_issuer_breakdown()`); `bondName` has a trailing maturity
  month+year stripped off (`_BOND_NAME_DATE_SUFFIX`) to get the plain
  issuer name, matching the loan originators' naming.
  The same issuer can appear in both lists (e.g. "ID Finance" held as both
  a loan and a bond) - run() merges them by name (summed) before writing to
  the Sheet, so each issuer gets a single combined row. Both are written to
  the "Répartition géographique" section, one row per merged issuer, right
  below the "Mintos" aggregate row and above the next platform's own row
  (verified live 2026-07-29 against the "ID Finance"/"Evergreen Finance"
  rows already present there) - same per-originator pattern as Swaper.
- This month's interest/withholding tax: `GET /fr/account-statement/` (the
  plain HTML page, no JS needed) embeds a per-session `csrfToken` directly
  in its server-rendered markup (regex-extracted below); `POST
  /webapp/api/fr/account-statement/summary/` (note the locale segment in the
  path) as `application/x-www-form-urlencoded` with body
  `account_statement_filter[currency]=978&account_statement_filter
  [fromDate]=<DD.MM.YYYY>&account_statement_filter[toDate]=<DD.MM.YYYY>`,
  requiring headers `X-Requested-With: XMLHttpRequest` and `Anti-Csrf-Token:
  <token>`. Response body: `{"data": {"summary": {"statementEntryGroups":
  {"<type_id>": "<net amount for that type over the period>", ...},
  "types": {"<type_id>": "<French label>", ...}, ...}}}` - e.g. type 17 =
  "Intérêt perçu" (interest received), type 46 = "Intérêts perçus sur le
  rachat de prêt" (interest received on loan buyback - also real interest,
  added to the gross total), type 137 = "Prélèvement à la source"
  (withholding tax, reported as a NEGATIVE amount). Matched here by the
  French label text (case-insensitive substring), not the numeric type id
  directly, in case the id mapping ever shifts - mirrors
  afranga_diversification.py's own label-based matching convention. The
  account's withholding tax rate is 5% (confirmed via the
  `individualWithholdingTax` field on the user profile API, and matches the
  Sheet's own "Mintos (Impôts à la source 5%)" row label).

  Loans vs. obligations interest split (added 2026-07-29, for type ids 17,
  46, 130, 163, 151, 143, 172, 174, 179 among others): any interest-matching
  label that ALSO contains "obligata"/"obligation" (e.g. type 151 "Revenus
  des intérêts sur les obligations", type 143 "Intérêts obligataires sur le
  rapprochement du transit") is bucketed as "intérêts brut obligations";
  everything else interest-matching (loans, loan buyback, pending-payment
  interest, real estate, Smart Cash, etc.) is bucketed as "intérêts brut
  prêts" - a deliberate catch-all so the two Sheet columns always sum to
  the FULL gross interest for the period, instead of silently dropping any
  product type this repo doesn't explicitly know about.

Also computes a since-inception XIRR (money-weighted return) plus this
month's Cash drag and the XIRR Bonus / XIRR Cash drag / XIRR Taxes/Frais
pie-chart shares - added 2026-08-14 per explicit user request, mirroring
Afranga/Swaper/Lendermarket/PeerBerry/Loanch's own XIRR blocks. Unlike the
aggregated 'summary' endpoint above, the Account Statement page
(https://www.mintos.com/fr/account-statement/) also backs a real
per-transaction dated ledger, found via a real browser network capture
(login automated with MINTOS_EMAIL/PASSWORD/TOTP_SECRET from .env):

    POST https://www.mintos.com/webapp/api/fr/v2/account-statement/page/
    (same csrfToken/Anti-Csrf-Token mechanism as the summary endpoint),
    body `account_statement_filter[currency]=978&...[fromDate]=DD.MM.YYYY&
    ...[toDate]=DD.MM.YYYY&...[maxResults]=100(&...[cursor]=<opaque>)`
    -> `{"data": {"nextCursor": "<opaque base64, absent on the final
    page>", "accountStatements": [{"transactionId", "date": "DD.MM.YYYY",
    "details": "ID de transaction : ... -  Dépôts" | "... (Prêt <a ...>
    ...</a>) Investissement" | "... Intérêt perçu" | "... Tax withholding"
    | ... (HTML-wrapped loan link plus a trailing French/English action
    label - see _extract_action_label()), "turnover": "<already SIGNED for
    its cash-balance impact>", "balance": "<the account's REAL uninvested
    cash/wallet balance right after this transaction>", "currency",
    "loanIdentifier", "isin"}, ...]}, "errors": [], "error_count": 0}`.

    Verified live 2026-08-14: `maxResults` is silently capped at 100 (110+
    falls back to a 20-row default, no error) and `fromDate` is rejected
    below MIN_STATEMENT_DATE (2025-01-01 for this account - the
    account-statement backend's own earliest available date, not
    necessarily the account's real inception). Entries are returned
    OLDEST-first (unlike PeerBerry/Loanch's newest-first APIs) with a
    forward-only opaque `cursor` - present on every full page, ABSENT on
    the final (possibly partial) page. get_cached_transactions() therefore
    resumes pagination from the last cursor seen on a PREVIOUS run (saved
    in XIRR_CASHFLOWS_STATE_FILE) and de-dupes by `transactionId`, instead
    of Loanch's "stop at an already-cached id" trick (which only works
    newest-first).

    The very last entry's `balance` was confirmed live to exactly match
    accounts/978's `available.amount` at the same instant - i.e. `balance`
    IS the account's real uninvested-cash figure after each transaction, so
    compute_average_idle_cash() can build the day-weighted average directly
    from it (forward-filling on days without a transaction) instead of
    reconstructing a running balance from signed deltas off a separate
    opening-balance anchor (unlike PeerBerry/Loanch/Swaper).

    Only "Dépôts" (deposit) / "Retrait" (withdrawal) rows are true EXTERNAL
    cashflows for XIRR - every other observed label (Investissement,
    Principal perçu, Intérêt perçu and their many Agreement
    Amendment/Rebuy Purpose Buyback/Agreement Prolongation/"Autre"/"The
    loan was repurchased..." prefixed variants, Tax withholding, bond
    interest/capital-increase rows) is an INTERNAL movement between cash
    and the invested portfolio - ignored for the cashflow list, but still
    reflected via `balance` for Cash drag. No "Bonus"-style transaction has
    ever occurred on this account (verified live against the account's full
    2025-01-01-to-date history) - _is_bonus() is a defensive keyword-only
    catch-all (same conservative-but-untested pattern as PeerBerry's
    REFERRAL_FEE/INVESTMENT_SALE_FEE), and the XIRR Taxes/Frais share
    reuses the already-verified gross/net/withholding-tax split from
    fetch_statement_totals() (generalized 2026-08-14 from
    fetch_current_month_statement_totals(), same pattern as
    peerberry_diversification.fetch_statement_summary()) over the
    since-inception range, instead of re-deriving it from the ledger a
    second time.

Added 2026-08-19: XIRR Intérêts, the counterfactual XIRR share attributable
to real net interest received (mirrors loanch_diversification.py's/
bienpreter_diversification.py's/afranga_diversification.py's/
iuvo_diversification.py's/lendermarket_diversification.py's own XIRR
Intérêts blocks). Unlike Loanch (no gross/withholding-tax split at all, so
"net interest" there is just the raw "Paid interest" ledger sum), Mintos
DOES have a genuine gross/withholding-tax split
(fetch_statement_totals()'s "gross_interest_received"/"withholding_tax",
5% rate) - so "lifetime net interest" here is
`lifetime_statement_totals["gross_interest_received"] -
lifetime_statement_totals["withholding_tax"]`, reusing the same
`lifetime_statement_totals` dict already fetched (once, since-inception)
for the Cash drag/XIRR Taxes-Frais block just above it, not recomputed a
second time. Same reasoning as the other platforms: "Intérêts" was
previously only ever a RESIDUAL (XIRR - XIRR Bonus - XIRR Cash drag - XIRR
Taxes/Frais) on the spreadsheet/dashboard side, which can legitimately go
negative or misleading depending on the other shares' relative size - XIRR
Intérêts instead gives a genuine, independently-measured figure (same
category of computation as Bonus/Cash drag/Taxes, not a derived leftover),
so the two can be compared/sanity-checked against each other on the
sheet/dashboard side.

Added 2026-09-07: the whole XIRR pie-chart block (Cash drag, XIRR, XIRR
Intérêts, XIRR Bonus, XIRR Cash drag, XIRR Taxes/Frais) can now ALSO be
computed for a BACKFILLED (past) month, not just the live current one -
mirroring afranga_diversification.py's own backfill port. New
`reconstruct_outstanding()`/`_outstanding_delta_for_entry()` replay the
ledger's own `turnover`/`details` fields to reconstruct the loans+bonds
outstanding balance as of an arbitrary past `end_date` (deposits/
withdrawals are external, neutral; a negative-turnover row is money
invested, a positive-turnover row is either interest/tax/bonus income
(neutral) or a principal/capital return, decided by the label text - see
`_outstanding_delta_for_entry()`'s own docstring for the full reasoning
and its "not fully verified live" caveat), `_wallet_balance_as_of()` reads
the wallet balance directly off the last ledger row at/before that date
(same trick `compute_average_idle_cash()` already uses), and
`compute_xirr_block_as_of()` ties it all together (mirrors
afranga_diversification.compute_xirr_block_as_of() closely). The
account-statement ledger fetch (`get_cached_transactions()`) now runs
unconditionally (was gated behind `if current_month:`) so a backfill run
can reconstruct up to that past date too. "en cours prêts"/"en cours
obligations"/"total" stay current-month-only as before (genuinely no
historical-outstanding endpoint exists, unrelated to this XIRR change).

FIXED 2026-09-08 (same day, twice): a version shipped earlier the same day
excluded bonds entirely from this whole XIRR block (terminal valuation +
gross interest/withholding tax), keeping the FULL deposit/withdrawal
cashflows unchanged - since deposits fund both loans AND bonds, this
silently understated the account's terminal value by the whole bonds
position while still counting the cash that bought it, making XIRR
persistently/artificially negative for any month on an account holding
bonds (confirmed against a real account: reported XIRR -11.7% vs. the
correct +9.8% once bonds were added back). Reverted the same day: bonds
are included again everywhere in this block (reconstruct_outstanding(),
compute_average_balances(), run(), compute_xirr_block_as_of()) - the
bond-classification heuristic (`"obligat" in details`) that motivated the
original exclusion was itself already verified accurate against the real
ledger, so there was no remaining reason to exclude bonds' real value.

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
single "XIRR Taxes/Frais" share into "XIRR Taxes" (the real, genuine 5%
withholding tax) and "XIRR Frais" - Mintos has no distinct platform-fee
concept beyond that withholding tax, so "XIRR Frais" is hardcoded to 0.0,
not computed via Shapley.


run() accepts an optional pre-built `requests.Session` (see
mintos_get_session.py) for a one-shot "log in by hand, then let this take
over" flow - the env vars below are only required when calling run() with
no session (the normal scheduled/headless case).

Required env vars:
    MINTOS_PHPSESSID, MINTOS_MW_SESSION_ID -> session cookies, see
                                               mintos_get_session.py
    GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS     -> used to write this month's
                                               totals to the Google Sheet via
                                               fill_current_month_amounts_with_labels()
                                               (see google_sheet.py)
"""

import os
import re
import sys
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

from shared.google_sheet import (
    fill_current_month_amounts_with_labels,
    fill_current_month_bonus_breakdown,
    fill_geographic_repartition_amounts,
    fill_geographic_repartition_uninvested_amount,
)
from shared.report_date import get_report_date, is_current_month
from shared.state import load_state, save_state
from shared.weighted_average import INVESTED_BALANCE_LABEL, NON_INVESTED_BALANCE_LABEL, compute_time_weighted_average
from shared.xirr import compute_xirr
from shared.xirr_waterfall import compute_waterfall_xirr_shares

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("mintos_diversification")

ACCOUNT_STATEMENT_PAGE_URL = "https://www.mintos.com/fr/account-statement/"
ACCOUNT_STATEMENT_SUMMARY_URL = "https://www.mintos.com/webapp/api/fr/account-statement/summary/"
# Per-transaction dated ledger backing the Account Statement page's own
# table (verified live 2026-08-14, see module docstring's XIRR/Cash drag
# section) - NOT the aggregated 'summary' endpoint above.
ACCOUNT_STATEMENT_ITEMS_URL = "https://www.mintos.com/webapp/api/fr/v2/account-statement/page/"
CURRENCY_ID = 978  # ISO 4217 numeric code for EUR
ACCOUNT_URL = f"https://www.mintos.com/webapp/api/marketplace-api/v1/accounts/{CURRENCY_ID}"
PORTFOLIO_DISTRIBUTIONS_URL = f"https://www.mintos.com/webapp/api/marketplace-api/v1/accounts/{CURRENCY_ID}/portfolio-distributions"
ACCOUNT_OVERVIEW_URL = f"https://www.mintos.com/webapp/api/marketplace-api/v1/user/overview/currency/{CURRENCY_ID}"
BONDS_INVESTMENTS_URL = "https://www.mintos.com/webapp/api/bonds-api/v3/investments/current"
PLATFORM_LABEL = "Mintos"

# Bond names include a trailing maturity month+year, e.g. "ID Finance Sep
# 2028" - stripped to get the plain issuer name ("ID Finance") so it can be
# merged with the same issuer's loan originator entry.
_BOND_NAME_DATE_SUFFIX = re.compile(r"\s+[A-Za-z\u00c0-\u00ff]{3,5}\.?\s+\d{4}$")

# Verified live 2026-08-14: Mintos rejects any 'fromDate' earlier than this
# for this account ("Cette valeur doit être supérieure ou égale à 1 janv.
# 2025") - the account-statement backend's own earliest available date.
MIN_STATEMENT_DATE = date(2025, 1, 1)
# Server silently caps at 100 rows/page no matter what's requested
# (verified live: 110+ silently fell back to a 20-row default, no error).
TRANSACTIONS_PAGE_SIZE = 100
MAX_TRANSACTIONS_PAGES = 500
# Cache of every account-statement row ever fetched, plus the cursor to
# resume pagination from next run (see get_cached_transactions() below).
XIRR_CASHFLOWS_STATE_FILE = Path(__file__).parent / "mintos_xirr_cashflows_state.json"
XIRR_CASHFLOWS_STATE_DEFAULT = {"all_entries": [], "resume_cursor": None}

_ACTION_LABEL_HTML_TAG = re.compile(r"<[^>]+>")

MINTOS_PHPSESSID = os.environ.get("MINTOS_PHPSESSID")
MINTOS_MW_SESSION_ID = os.environ.get("MINTOS_MW_SESSION_ID")

SESSION_EXPIRED_MESSAGE = (
    "Mintos session looks expired or invalid (got redirected to /login or an "
    "auth error). Run `python -m diversification.mintos_get_session` locally "
    "(from your own machine, not CI) to log in again, solve the CAPTCHA if "
    "one appears, then update MINTOS_PHPSESSID/MINTOS_MW_SESSION_ID in your "
    "local .env and in the GitHub repository secrets."
)


def build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0"})
    s.cookies.set("PHPSESSID", MINTOS_PHPSESSID, domain="www.mintos.com", path="/")
    s.cookies.set("MW_SESSION_ID", MINTOS_MW_SESSION_ID, domain="www.mintos.com", path="/")
    return s


def _check_authenticated(r: requests.Response) -> None:
    if r.status_code in (401, 403) or "/login" in r.url:
        raise RuntimeError(SESSION_EXPIRED_MESSAGE)


def fetch_account_summary(session: requests.Session) -> dict:
    """Fetch this account's total invested (loans) amount and its
    uninvested "available" cash balance ("non investi"). See module
    docstring for the verified (nested) 'invested'/'available' fields."""
    log.info("GET %s (fetching account summary)...", ACCOUNT_URL)
    r = session.get(ACCOUNT_URL, timeout=20)
    log.info("GET accounts/%s: status=%s", CURRENCY_ID, r.status_code)
    _check_authenticated(r)
    if not r.ok:
        raise RuntimeError(f"Accounts endpoint returned status {r.status_code}")

    data = r.json() or {}
    invested = (data.get("invested") or {}).get("amount")
    if invested is None:
        raise RuntimeError(f"Could not find 'invested.amount' in the accounts response: {data!r}")
    available = (data.get("available") or {}).get("amount")

    try:
        invested = float(invested)
        available = float(available) if available is not None else 0.0
    except (TypeError, ValueError):
        raise RuntimeError(f"Could not parse 'invested.amount'/'available.amount' out of {data!r}.")

    return {"invested": invested, "available": available}


def fetch_portfolio_split(session: requests.Session) -> dict:
    """Fetch the loans-vs-bonds (obligations) OUTSTANDING split from the
    Overview page's own summary endpoint (verified live 2026-07-29):
    `GET /webapp/api/marketplace-api/v1/user/overview/currency/978` ->
    `{"total": {"value": <account total incl. uninvested cash>}, "loans":
    {"value": ...}, "bonds": {"value": ...}, "realEstate": {...}, "etf":
    {...}, "smartCash": {...}, "fundraising": {...}, "cryptoEtp": {...},
    "singleStock": {...}, "singleEtf": {...}}` - only "loans"/"bonds" are
    used here (both 0 for the other categories on this account as of
    writing - if this account ever invests in real estate/ETF/smart cash/
    etc., those amounts would currently not be reflected in either the
    'en cours prêts' or 'en cours obligations' Sheet rows, since the user
    only asked for a prêts/obligations split)."""
    log.info("GET %s (fetching loans/bonds outstanding split)...", ACCOUNT_OVERVIEW_URL)
    r = session.get(ACCOUNT_OVERVIEW_URL, timeout=20)
    log.info("GET user/overview: status=%s", r.status_code)
    _check_authenticated(r)
    if not r.ok:
        raise RuntimeError(f"Overview endpoint returned status {r.status_code}")

    data = r.json() or {}
    try:
        loans = float((data.get("loans") or {}).get("value", 0))
        bonds = float((data.get("bonds") or {}).get("value", 0))
    except (TypeError, ValueError):
        raise RuntimeError(f"Could not parse loans/bonds 'value' out of {data!r}.")

    log.info("Portfolio split: loans=%.2f EUR, bonds=%.2f EUR", loans, bonds)
    return {"loans": loans, "bonds": bonds}


def fetch_loan_originator_breakdown(session: requests.Session) -> list:
    """Fetch the per-loan-originator invested EUR breakdown (loans only -
    see fetch_bond_issuer_breakdown() for bonds), written per-originator to
    the Sheet's 'Répartition géographique' section under the 'Mintos' row
    (see module docstring)."""
    log.info("GET %s (fetching loan originator breakdown)...", PORTFOLIO_DISTRIBUTIONS_URL)
    r = session.get(PORTFOLIO_DISTRIBUTIONS_URL, timeout=20)
    log.info("GET portfolio-distributions: status=%s", r.status_code)
    if not r.ok:
        log.warning("Portfolio-distributions endpoint returned status %s - skipping.", r.status_code)
        return []

    data = r.json() or {}
    distribution = (data.get("loanOriginators") or {}).get("distribution") or []
    originators = []
    for item in distribution:
        name = item.get("name")
        try:
            amount = round(float((item.get("total") or {}).get("amount", 0)), 2)
        except (TypeError, ValueError):
            continue
        originators.append({"originator": name, "outstanding": amount})
    return originators


def fetch_bond_issuer_breakdown(session: requests.Session) -> list:
    """Fetch the per-bond-issuer invested EUR breakdown from the Bonds
    page's own current-investments endpoint (verified live 2026-07-29):
    `GET /webapp/api/bonds-api/v3/investments/current` -> `{"items": [
    {"bondName": "ID Finance Sep 2028", "investedAmount": {"amount": ...}},
    ...]}`. `bondName`'s trailing maturity month+year is stripped
    (_BOND_NAME_DATE_SUFFIX) to match the loan originators' plain naming,
    e.g. "ID Finance" - so the same issuer appearing in both loans and
    bonds can be merged (summed) into one Sheet row (see run())."""
    params = {"sorting[field]": "maturityDate", "sorting[order]": "asc", "pagination[limit]": "100"}
    log.info("GET %s (fetching bond issuer breakdown)...", BONDS_INVESTMENTS_URL)
    r = session.get(BONDS_INVESTMENTS_URL, params=params, timeout=20)
    log.info("GET bonds-api/investments/current: status=%s", r.status_code)
    if not r.ok:
        log.warning("Bonds investments endpoint returned status %s - skipping.", r.status_code)
        return []

    data = r.json() or {}
    issuers = []
    for item in data.get("items") or []:
        name = _BOND_NAME_DATE_SUFFIX.sub("", item.get("bondName") or "").strip()
        try:
            amount = round(float((item.get("investedAmount") or {}).get("amount", 0)), 2)
        except (TypeError, ValueError):
            continue
        issuers.append({"originator": name, "outstanding": amount})
    return issuers


def get_csrf_token(session: requests.Session) -> str:
    """The account-statement page embeds a per-session `csrfToken` directly
    in its server-rendered HTML (no JS execution needed - verified
    2026-07-24)."""
    r = session.get(ACCOUNT_STATEMENT_PAGE_URL, timeout=20)
    _check_authenticated(r)
    if not r.ok:
        raise RuntimeError(f"Account statement page returned status {r.status_code}")

    match = re.search(r'csrfToken["\']?\s*:\s*["\']([^"\']+)', r.text)
    if not match:
        raise RuntimeError("Could not find 'csrfToken' in the account statement page HTML.")
    return match.group(1)


def fetch_statement_totals(session: requests.Session, from_date: date, to_date: date) -> dict:
    """Fetch gross interest received / withholding tax for an arbitrary
    [from_date, to_date] range via the Account Statement page's own
    summary endpoint - generalized 2026-08-14 (was
    fetch_current_month_statement_totals(), hardcoded to the current
    calendar month - kept below as a thin wrapper) so run() can ALSO query
    this once for the account's full since-inception range, needed by the
    XIRR/Cash drag/XIRR Intérêts block (see module docstring). See module
    docstring for the verified endpoint/fields/CSRF mechanism."""
    csrf_token = get_csrf_token(session)

    form = {
        "account_statement_filter[currency]": str(CURRENCY_ID),
        "account_statement_filter[fromDate]": from_date.strftime("%d.%m.%Y"),
        "account_statement_filter[toDate]": to_date.strftime("%d.%m.%Y"),
    }
    log.info("POST %s (fetching statement summary %s to %s)...", ACCOUNT_STATEMENT_SUMMARY_URL, from_date, to_date)
    r = session.post(
        ACCOUNT_STATEMENT_SUMMARY_URL,
        data=form,
        headers={"X-Requested-With": "XMLHttpRequest", "Anti-Csrf-Token": csrf_token},
        timeout=20,
    )
    log.info("POST account-statement/summary: status=%s", r.status_code)
    _check_authenticated(r)
    if not r.ok:
        raise RuntimeError(f"Account statement summary endpoint returned status {r.status_code}")

    data = r.json() or {}
    summary = (data.get("data") or {}).get("summary") or {}
    groups = summary.get("statementEntryGroups") or {}
    types = summary.get("types") or {}
    log.info("Raw statementEntryGroups: %r", groups)

    gross_interest_received_loans = 0.0
    gross_interest_received_obligations = 0.0
    withholding_tax = 0.0
    for type_id, raw_amount in groups.items():
        label = (types.get(type_id) or "").lower()
        try:
            amount = float(raw_amount)
        except (TypeError, ValueError):
            continue
        is_interest = "intér" in label or "inter" in label
        is_obligation = "obligata" in label or "obligation" in label
        if is_interest and is_obligation:
            # e.g. "Revenus des intérêts sur les obligations", "Intérêts
            # obligataires sur le rapprochement du transit" (type ids 151/143).
            gross_interest_received_obligations += amount
        elif is_interest:
            # Everything else interest-labelled (loans, loan buyback,
            # pending-payment interest, real estate, Smart Cash - see
            # module docstring) is bucketed as loan interest, so
            # 'intérêts brut prêts' + 'intérêts brut obligations' always
            # adds up to the full gross interest for this period.
            gross_interest_received_loans += amount
        elif (
            "prélèvement à la source" in label
            or "prelevement a la source" in label
            or "tax withholding" in label
            or "withholding" in label
        ):
            withholding_tax += abs(amount)

    gross_interest_received = gross_interest_received_loans + gross_interest_received_obligations

    return {
        "gross_interest_received": round(gross_interest_received, 2),
        "gross_interest_received_loans": round(gross_interest_received_loans, 2),
        "gross_interest_received_obligations": round(gross_interest_received_obligations, 2),
        "withholding_tax": round(withholding_tax, 2),
    }


def fetch_current_month_statement_totals(session: requests.Session) -> dict:
    """Thin wrapper around fetch_statement_totals() for the current
    calendar month (1st of the month through today)."""
    today = get_report_date()
    return fetch_statement_totals(session, today.replace(day=1), today)


def _extract_action_label(details: str) -> str:
    """Mintos' per-transaction 'details' field embeds a loan/ISIN link plus
    a trailing French/English action description, e.g. 'ID de transaction :
    ... - ISIN: ... (Prêt <a ...>70036404-01</a>) Investissement' or
    'ID de transaction : ... -  Dépôts' (no loan reference). Strips the
    HTML link tag, then returns just the trailing action text (after the
    last ')' if present, else after the last ' - ')."""
    text = _ACTION_LABEL_HTML_TAG.sub("", details or "")
    paren_idx = text.rfind(")")
    if paren_idx != -1:
        return text[paren_idx + 1:].strip()
    dash_idx = text.rfind(" - ")
    return text[dash_idx + 3:].strip() if dash_idx != -1 else text.strip()


def _is_deposit(label: str) -> bool:
    return "dépôt" in label.lower() or "depot" in label.lower()


def _is_withdrawal(label: str) -> bool:
    return "retrait" in label.lower() or "withdrawal" in label.lower()


def _is_bonus(label: str) -> bool:
    """No 'Bonus'-style transaction has ever occurred on this account
    (verified live 2026-08-14 against its full 2025-01-01-to-date history)
    - kept as a defensive keyword-only catch-all (French 'bonus'/'prime'/
    'parrainage', English 'cashback') in case Mintos ever introduces one,
    same conservative-but-untested pattern as PeerBerry's REFERRAL_FEE."""
    l = label.lower()
    return "bonus" in l or "prime" in l or "cashback" in l or "parrainage" in l


def fetch_transaction_ledger_page(session: requests.Session, csrf_token: str, cursor: str | None) -> dict:
    """Fetch one page of the Account Statement's per-transaction ledger
    (see module docstring for the verified request/response shape).
    `fromDate`/`toDate` are pinned to [MIN_STATEMENT_DATE, today] on every
    call; once `cursor` is supplied (from a previous page's `nextCursor`)
    it takes precedence for continuing pagination forward."""
    today = get_report_date()
    form = {
        "account_statement_filter[currency]": str(CURRENCY_ID),
        "account_statement_filter[fromDate]": MIN_STATEMENT_DATE.strftime("%d.%m.%Y"),
        "account_statement_filter[toDate]": today.strftime("%d.%m.%Y"),
        "account_statement_filter[maxResults]": str(TRANSACTIONS_PAGE_SIZE),
    }
    if cursor:
        form["account_statement_filter[cursor]"] = cursor
    r = session.post(
        ACCOUNT_STATEMENT_ITEMS_URL,
        data=form,
        headers={"X-Requested-With": "XMLHttpRequest", "Anti-Csrf-Token": csrf_token},
        timeout=20,
    )
    _check_authenticated(r)
    if not r.ok:
        raise RuntimeError(f"Account statement page endpoint returned status {r.status_code}")
    return r.json() or {}


def get_cached_transactions(session: requests.Session) -> list:
    """Return every account-statement transaction row since inception,
    fetching from the ledger API only the pages not already cached locally
    (in XIRR_CASHFLOWS_STATE_FILE). Unlike Loanch (newest-first, stop at an
    already-cached id), Mintos' ledger is OLDEST-first with an opaque
    forward-only `nextCursor` (see module docstring) - so this resumes
    pagination from the last cursor saved on a previous run (pointing just
    before that run's final, possibly-partial page) and de-dupes by
    `transactionId`, instead of scanning for an already-seen row."""
    state = load_state(XIRR_CASHFLOWS_STATE_FILE, XIRR_CASHFLOWS_STATE_DEFAULT)
    cached_entries = state.get("all_entries") or []
    cached_ids = {entry.get("transactionId") for entry in cached_entries}
    cursor = state.get("resume_cursor")

    csrf_token = get_csrf_token(session)

    log.info("Found %d cached transaction(s) - resuming pagination (cursor=%s)...", len(cached_entries), "yes" if cursor else "start of history")
    new_entries = []
    last_good_cursor = cursor
    for page_number in range(1, MAX_TRANSACTIONS_PAGES + 1):
        body = fetch_transaction_ledger_page(session, csrf_token, cursor)
        rows = (body.get("data") or {}).get("accountStatements") or []
        next_cursor = (body.get("data") or {}).get("nextCursor")
        log.info("Page %d: %d entrie(s) found.", page_number, len(rows))
        for row in rows:
            tid = row.get("transactionId")
            if tid in cached_ids:
                continue
            cached_ids.add(tid)
            new_entries.append(row)
        if next_cursor:
            last_good_cursor = next_cursor
            cursor = next_cursor
        else:
            break
    else:
        log.warning("Hit MAX_TRANSACTIONS_PAGES (%d) without reaching the end of the ledger - it may be incomplete.", MAX_TRANSACTIONS_PAGES)

    merged = cached_entries + new_entries
    save_state(XIRR_CASHFLOWS_STATE_FILE, {"all_entries": merged, "resume_cursor": last_good_cursor})
    log.info("Transaction ledger cache now holds %d entrie(s) (was %d before this run, %d new).", len(merged), len(cached_entries), len(new_entries))
    return merged


def compute_average_idle_cash(entries: list, start_date: str, end_date: str) -> float:
    """Day-weighted average uninvested-cash/wallet balance over
    [start_date, end_date] ('YYYY-MM-DD'), built directly from each ledger
    row's own `balance` field - verified live 2026-08-14 that this IS the
    account's real uninvested cash balance right after that transaction
    (the account's very last entry's `balance` matched accounts/978's
    `available.amount` exactly at the same instant), so no delta-
    accumulation off a separate opening-balance anchor is needed (unlike
    PeerBerry/Loanch/Swaper). Forward-fills the last known balance on days
    without a transaction. Never raises - returns 0.0 if dates can't be
    parsed or no entry is found at or before `end_date`.
    """
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        end = datetime.strptime(end_date, "%Y-%m-%d").date()
    except ValueError:
        return 0.0

    daily_balance: dict = {}
    for entry in entries:
        raw_date = entry.get("date")
        raw_balance = entry.get("balance")
        if not raw_date or raw_balance is None:
            continue
        try:
            entry_date = datetime.strptime(raw_date, "%d.%m.%Y").date()
            balance = float(raw_balance)
        except (TypeError, ValueError):
            continue
        if entry_date > end:
            continue
        # Entries are returned oldest-first, so the last one seen for a
        # given day is that day's real end-of-day balance.
        daily_balance[entry_date] = balance

    if not daily_balance:
        return 0.0

    running_balance = 0.0
    total_balance = 0.0
    day_count = 0
    current = min(daily_balance)
    while current <= end:
        running_balance = daily_balance.get(current, running_balance)
        if current >= start:
            total_balance += running_balance
            day_count += 1
        current += timedelta(days=1)

    if day_count == 0:
        return running_balance
    return total_balance / day_count


def _entry_date(entry: dict):
    raw = entry.get("date")
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%d.%m.%Y").date()
    except ValueError:
        return None


def _entry_amount(entry: dict) -> float:
    try:
        return float(entry.get("turnover") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _outstanding_delta_for_entry(label: str, turnover: float) -> float:
    """Signed change to the OUTSTANDING invested balance (the same loans+
    bonds sum fetch_portfolio_split() represents for the account's CURRENT
    state) that one ledger row represents - needed to reconstruct that
    balance for a past end_date (see reconstruct_outstanding() below).

    Deposits/withdrawals are external cashflows (handled separately as
    XIRR cashflows) and never touch the invested balance - always 0. Since
    `turnover` is already signed for its WALLET-balance impact (see module
    docstring), a NEGATIVE turnover on any other row means money left the
    wallet to fund an investment ("Investissement" or an unclassified
    variant) - outstanding increases by the same amount. A POSITIVE
    turnover on any other row is either real interest/tax/bonus income
    (wallet-only, doesn't touch principal) or a principal/capital return
    (buyback, agreement termination, bond capital decrease) that DOES
    reduce the outstanding balance - the sign alone can't distinguish the
    two, so the label's own text decides. An unrecognized positive-turnover
    label is conservatively treated as a principal return (not silently
    ignored) since mis-classifying a real principal event as neutral would
    understate how much capital had already come back out of the portfolio
    by a past date - not fully verified live, revisit if a future run's
    reconstructed closing balance looks off.
    """
    if _is_deposit(label) or _is_withdrawal(label):
        return 0.0
    if turnover < 0:
        return abs(turnover)
    if turnover > 0:
        if _is_bonus(label) or "intér" in label.lower() or "inter" in label.lower() or "withholding" in label.lower() or "prélèvement" in label.lower() or "prelevement" in label.lower():
            return 0.0
        return -abs(turnover)
    return 0.0


def reconstruct_outstanding(entries: list, end_date: date) -> float:
    """Reconstruct the OUTSTANDING invested balance (loans + bonds) as of
    an arbitrary past `end_date`, by replaying every ledger row dated on
    or before that date and applying _outstanding_delta_for_entry() to
    each - mirrors afranga_diversification.reconstruct_outstanding().
    Starts from 0 (before the account's first-ever transaction, outstanding
    is necessarily 0).

    NOTE: a 2026-09-08 version of this function excluded bonds entirely
    from the XIRR block (deposits/withdrawals still counted in full) -
    this silently understated the account's terminal value by the whole
    bonds position while still counting the cash that funded it, making
    XIRR persistently/artificially negative for any account holding bonds.
    Reverted 2026-09-08 (same day, found via a real account's cached
    ledger): bonds are included again."""
    outstanding = 0.0
    for entry in entries:
        entry_date = _entry_date(entry)
        if entry_date is None or entry_date > end_date:
            continue
        label = _extract_action_label(entry.get("details") or "")
        outstanding += _outstanding_delta_for_entry(label, _entry_amount(entry))
    return outstanding


def _wallet_balance_as_of(entries: list, end_date: date) -> float:
    """The wallet/uninvested-cash balance as of `end_date`, taken directly
    from the last ledger row (in chronological/cached order) dated on or
    before it - each row's own `balance` field is already the real
    uninvested-cash balance right after that transaction (see module
    docstring), so no delta-accumulation is needed."""
    balance = 0.0
    for entry in entries:
        entry_date = _entry_date(entry)
        if entry_date is None or entry_date > end_date:
            continue
        raw_balance = entry.get("balance")
        if raw_balance is None:
            continue
        try:
            balance = float(raw_balance)
        except (TypeError, ValueError):
            continue
    return balance


def compute_average_balances(entries: list, start_date, end_date, invested_closing_anchor: float = None) -> tuple:
    """Day-weighted average INVESTED ("outstanding") and NON-INVESTED
    (wallet cash) balances over [start_date, end_date] (`date` objects) -
    for the "solde moyen pondéré investi"/"solde moyen pondéré non
    investi" Sheet rows (added 2026-09-08). The invested side reuses the
    SAME per-entry classifier as reconstruct_outstanding() above
    (_outstanding_delta_for_entry), fed into the generic shared day-
    weighted-average helper - rebuilt from account inception
    (opening_balance=0.0) unless `invested_closing_anchor` is given (the
    real LIVE loans+bonds outstanding total, only meaningful for the real
    current month - no historical equivalent exists on Mintos), in which
    case the period's own events are replayed BACKWARDS from that real
    value instead, bounding any drift risk to just this month's own
    entries. The cash side is different and already immune to this: Mintos
    ledger rows carry a real running `balance` snapshot (forward-fill, see
    compute_average_idle_cash()'s docstring) rather than a per-row signed
    delta - converted here into an equivalent delta sequence (delta =
    balance_after - balance_before per entry, processed in ascending date
    order) so the SAME generic averaging helper can be reused for both
    sides uniformly, with no anchor needed. `entries` is expected to
    cover the account's full history (see get_cached_transactions()), so
    the running balance carried into `start_date` is accurate."""
    dated_entries = []
    for entry in entries:
        entry_date = _entry_date(entry)
        if entry_date is None:
            continue
        dated_entries.append((entry_date, entry))
    dated_entries.sort(key=lambda t: t[0])

    invested_events = []
    non_invested_events = []
    running_cash_balance = 0.0
    for entry_date, entry in dated_entries:
        label = _extract_action_label(entry.get("details") or "")
        invested_events.append((entry_date, _outstanding_delta_for_entry(label, _entry_amount(entry))))

        raw_balance = entry.get("balance")
        if raw_balance is not None:
            try:
                balance = float(raw_balance)
            except (TypeError, ValueError):
                balance = None
            if balance is not None:
                non_invested_events.append((entry_date, balance - running_cash_balance))
                running_cash_balance = balance

    if invested_closing_anchor is not None:
        period_invested = [(d, v) for d, v in invested_events if start_date <= d <= end_date]
        avg_invested = compute_time_weighted_average(
            period_invested, start_date, end_date,
            opening_balance=invested_closing_anchor - sum(v for _, v in period_invested),
        )
    else:
        avg_invested = compute_time_weighted_average(invested_events, start_date, end_date)
    avg_non_invested = compute_time_weighted_average(non_invested_events, start_date, end_date)
    return avg_invested, avg_non_invested


def _build_deposit_withdrawal_cashflows_as_of(entries: list, end_date: date) -> tuple:
    """Real Dépôts/Retrait cashflows (signed, XIRR convention) dated <=
    end_date, the earliest deposit date found (or None), and the lifetime
    bonus total over the same filtered range - shared building block for
    compute_xirr_block_as_of() (backfilled months)."""
    signed_cashflows = []
    deposit_dates = []
    lifetime_bonus = 0.0
    for entry in entries:
        entry_date = _entry_date(entry)
        if entry_date is None or entry_date > end_date:
            continue
        label = _extract_action_label(entry.get("details") or "")
        amount = _entry_amount(entry)
        if _is_deposit(label):
            signed_cashflows.append((entry_date, -amount))
            deposit_dates.append(entry_date)
        elif _is_withdrawal(label):
            signed_cashflows.append((entry_date, -amount))
        elif _is_bonus(label):
            lifetime_bonus += amount
    since_inception_date = min(deposit_dates) if deposit_dates else None
    return signed_cashflows, since_inception_date, lifetime_bonus


def compute_xirr_block_as_of(session: requests.Session, all_entries: list, end_date: date) -> dict:
    """Compute the FULL XIRR pie-chart block (XIRR, Cash drag, XIRR Bonus,
    XIRR Cash drag, XIRR Taxes, XIRR Frais, XIRR Intérêts) for a BACKFILLED
    (non-current) month, as of that month's own `end_date` - the
    historical equivalent of run()'s live-today block, mirroring
    afranga_diversification.compute_xirr_block_as_of(). "Cash drag" stays a
    MONTHLY figure (this backfilled month alone); everything else is a
    Shapley decomposition (see shared/xirr_shapley.py) of the
    since-inception XIRR gap through end_date. Returns a dict with any
    subset of {"XIRR", "Cash drag", "XIRR Bonus", "XIRR Cash drag", "XIRR
    Taxes", "XIRR Frais", "XIRR Intérêts"} that could actually be computed
    (soft-fail - a missing key means the existing Sheet cell is left
    untouched).
    """
    result: dict = {}

    outstanding_as_of = reconstruct_outstanding(all_entries, end_date)
    wallet_balance_as_of = _wallet_balance_as_of(all_entries, end_date)
    total_value_as_of = outstanding_as_of + wallet_balance_as_of

    base_cashflows, since_inception_date, lifetime_bonus_as_of = _build_deposit_withdrawal_cashflows_as_of(all_entries, end_date)
    if not base_cashflows:
        return result

    xirr_value = compute_xirr(base_cashflows + [(end_date, total_value_as_of)])
    if xirr_value is None:
        log.warning("Could not compute XIRR as of %s (backfilled month) from the reconstructed cashflows.", end_date)
        return result
    result["XIRR"] = xirr_value
    log.info(
        "Computed XIRR as of %s (backfilled month): %.2f%% (reconstructed outstanding=%.2f EUR, wallet balance=%.2f EUR).",
        end_date, xirr_value * 100, outstanding_as_of, wallet_balance_as_of,
    )

    if outstanding_as_of <= 0 or since_inception_date is None:
        if lifetime_bonus_as_of:
            xirr_without_bonus = compute_xirr(base_cashflows + [(end_date, total_value_as_of - lifetime_bonus_as_of)])
            if xirr_without_bonus is not None:
                result["XIRR Bonus"] = xirr_value - xirr_without_bonus
        else:
            result["XIRR Bonus"] = 0.0
        return result

    month_start_date = end_date.replace(day=1)
    avg_idle_cash_month = compute_average_idle_cash(all_entries, month_start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d"))
    try:
        month_statement_totals = fetch_statement_totals(session, month_start_date, end_date)
    except Exception:
        log.exception("Failed to fetch this backfilled month's statement totals - Cash drag as of %s will not be updated.", end_date)
        month_statement_totals = None

    if month_statement_totals is not None:
        cash_weight = avg_idle_cash_month / (avg_idle_cash_month + outstanding_as_of)
        monthly_yield_rate = month_statement_totals["gross_interest_received"] / outstanding_as_of
        result["Cash drag"] = cash_weight * monthly_yield_rate
        log.info("Computed Cash drag as of %s (backfilled month): %.2f%%.", end_date, result["Cash drag"] * 100)

    try:
        lifetime_statement_totals = fetch_statement_totals(session, since_inception_date, end_date)
    except Exception:
        log.exception("Failed to fetch since-inception statement totals as of %s - XIRR Cash drag/Taxes/Intérêts will not be updated.", end_date)
        if lifetime_bonus_as_of:
            xirr_without_bonus = compute_xirr(base_cashflows + [(end_date, total_value_as_of - lifetime_bonus_as_of)])
            if xirr_without_bonus is not None:
                result["XIRR Bonus"] = xirr_value - xirr_without_bonus
        else:
            result["XIRR Bonus"] = 0.0
        return result

    avg_idle_cash_lifetime = compute_average_idle_cash(all_entries, since_inception_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d"))
    cash_weight_lifetime = avg_idle_cash_lifetime / (avg_idle_cash_lifetime + outstanding_as_of)
    lifetime_yield_rate = lifetime_statement_totals["gross_interest_received"] / outstanding_as_of
    cash_drag_lifetime_total = cash_weight_lifetime * lifetime_yield_rate
    missed_earnings = cash_drag_lifetime_total * (avg_idle_cash_lifetime + outstanding_as_of)

    lifetime_withholding_tax = lifetime_statement_totals["withholding_tax"]
    lifetime_gross_interest = lifetime_statement_totals["gross_interest_received"]

    # Waterfall decomposition (switched from Shapley 2026-09-09, see
    # shared/xirr_waterfall.py's module docstring for why) - walks a true
    # 0%-return baseline up to total_value_as_of in the fixed order
    # Intérêts -> Cash drag -> Bonus -> Frais -> Taxes, using GROSS
    # interest (not net) at the Intérêts step and subtracting
    # missed_earnings right after - each euro counted exactly once, so
    # the shares sum EXACTLY to XIRR real (checked at runtime via a
    # warning log). Mintos has a genuine withholding-tax split (5% rate,
    # see module docstring) but no distinct platform-fee concept - "XIRR
    # Frais" is hardcoded to 0.0, skipped from the steps below.
    steps = [
        ("XIRR Intérêts", lifetime_gross_interest + missed_earnings),
        ("XIRR Cash drag", -missed_earnings),
        ("XIRR Bonus", lifetime_bonus_as_of),
        ("XIRR Taxes", -lifetime_withholding_tax),
    ]
    waterfall_shares = compute_waterfall_xirr_shares(
        base_cashflows, end_date, total_value_as_of, steps,
        log=log, log_context=f"Mintos as of {end_date}",
    )
    for name, value in waterfall_shares.items():
        if value is not None:
            result[name] = value
    result["XIRR Frais"] = 0.0
    log.info(
        "XIRR Waterfall shares as of %s (since-inception, missed earnings ~%.2f EUR): %r",
        end_date, missed_earnings, {k: round(v * 100, 4) for k, v in waterfall_shares.items() if v is not None},
    )

    return result


def run(session: requests.Session | None = None) -> None:
    """Runs the full fetch + Google Sheet write.

    `session` lets a caller hand off an already-authenticated
    `requests.Session` (e.g. `mintos_get_session.py`, right after a real
    manual login) instead of building one from the
    MINTOS_PHPSESSID/MINTOS_MW_SESSION_ID env vars - useful for a one-shot
    "log in by hand, then let the automation take over" flow. When omitted
    (the normal scheduled-run case), falls back to the env-var-based
    session as before.
    """
    if session is None:
        if not MINTOS_PHPSESSID or not MINTOS_MW_SESSION_ID:
            log.error(
                "MINTOS_PHPSESSID and MINTOS_MW_SESSION_ID environment variables are required. %s",
                SESSION_EXPIRED_MESSAGE,
            )
            sys.exit(1)
        session = build_session()

    log.info("Starting Mintos diversification run (pure HTTP, no Playwright).")

    try:
        account_summary = fetch_account_summary(session)
        originators = fetch_loan_originator_breakdown(session)
        bond_issuers = fetch_bond_issuer_breakdown(session)
        portfolio_split = fetch_portfolio_split(session)
        statement_totals = fetch_current_month_statement_totals(session)
    except Exception:
        log.exception("Failed to fetch Mintos account data.")
        sys.exit(1)

    log.info("Total invested (loans): %.2f EUR, available (non investi): %.2f EUR", account_summary["invested"], account_summary["available"])
    for o in originators:
        log.info("  %s: %.2f EUR", o["originator"], o["outstanding"])
    for o in bond_issuers:
        log.info("  %s (bond): %.2f EUR", o["originator"], o["outstanding"])

    # Same issuer can hold both loans and bonds (e.g. "ID Finance") - merge
    # by name, summing, into a single Sheet row per issuer.
    combined_originators = {}
    for o in originators + bond_issuers:
        combined_originators[o["originator"]] = combined_originators.get(o["originator"], 0) + o["outstanding"]

    net_interest_received = statement_totals["gross_interest_received"] - statement_totals["withholding_tax"]
    log.info(
        "This month's interest: gross=%.2f EUR, net=%.2f EUR, withholding_tax=%.2f EUR",
        statement_totals["gross_interest_received"], net_interest_received, statement_totals["withholding_tax"],
    )

    # "invested" (from accounts/978) only reflects the loans portfolio, not
    # bonds - the two "en cours" sub-rows below the Mintos row must sum to
    # loans+obligations only (invested, no cash).
    total_outstanding = portfolio_split["loans"] + portfolio_split["bonds"]
    # The "Mintos" row's own "total" cell represents the account's whole
    # balance including uninvested cash (per user request 2026-08-14,
    # matching Bienprêter/Iuvo/Bricks/Lande/etc.'s own convention) - it no
    # longer equals the sum of the "en cours prêts"/"en cours obligations"
    # sub-rows below it (those stay invested-only).
    total_with_cash = total_outstanding + account_summary["available"]

    amounts = {
        "total": total_with_cash,
        "gross_interest_received": statement_totals["gross_interest_received"],
        "net_interest_received": net_interest_received,
        "withholding_tax": statement_totals["withholding_tax"],
        "loans_outstanding": portfolio_split["loans"],
        "obligations_outstanding": portfolio_split["bonds"],
    }
    log.info("Amounts to write: %s", amounts)

    # "en cours prêts"/"en cours obligations" (like "total") only reflect the
    # account's CURRENT balance - Mintos has no endpoint to fetch a past
    # month's historical outstanding balance (unlike Lande's tax-report PDF),
    # so these are only written for the real current month, same convention
    # as skip_total elsewhere (see shared/report_date.is_current_month()).
    current_month = is_current_month()

    # Since-inception XIRR (money-weighted return) + this month's Cash drag
    # + the XIRR Bonus/Cash drag/Taxes-Frais/Intérêts pie-chart shares - see
    # module docstring for the real per-transaction ledger this is built
    # from.
    today_date = get_report_date()
    all_entries = None
    try:
        log.info("Fetching the since-inception account-statement ledger (cached where possible)...")
        all_entries = get_cached_transactions(session)
    except Exception:
        log.exception("Failed to fetch the account-statement ledger - XIRR/Cash drag will not be updated.")
        all_entries = None

    xirr_value = None
    signed_cashflows = None
    bonus_xirr_contribution = None
    since_inception_date = None
    lifetime_bonus = 0.0
    if current_month and all_entries:
        signed_cashflows = []
        deposit_dates = []
        for entry in all_entries:
            entry_date = _entry_date(entry)
            if entry_date is None:
                continue
            label = _extract_action_label(entry.get("details") or "")
            if _is_deposit(label):
                # `turnover` is already signed for its cash-balance impact
                # (Dépôts positive, Retrait negative) - negate for XIRR's
                # own convention (money going INTO the platform is negative).
                signed_cashflows.append((entry_date, -_entry_amount(entry)))
                deposit_dates.append(entry_date)
            elif _is_withdrawal(label):
                signed_cashflows.append((entry_date, -_entry_amount(entry)))
            elif _is_bonus(label):
                lifetime_bonus += _entry_amount(entry)

        since_inception_date = min(deposit_dates) if deposit_dates else None
        signed_cashflows.append((today_date, total_with_cash))

        xirr_value = compute_xirr(signed_cashflows)
        if xirr_value is None:
            log.warning("Could not compute XIRR from %d cashflow(s) - XIRR row will not be updated.", len(signed_cashflows) - 1)
        else:
            log.info(
                "Computed since-inception XIRR: %.2f%% (%d deposit/withdrawal cashflow(s), current total value %.2f EUR).",
                xirr_value * 100, len(signed_cashflows) - 1, total_with_cash,
            )
            # bonus_xirr_contribution computed jointly with Cash drag/
            # Taxes/Intérêts further below (Shapley game, added
            # 2026-09-09) once missed_earnings is known.

    cash_drag_value = None
    cash_drag_xirr_contribution = None
    taxes_xirr_contribution = None
    frais_xirr_contribution = None
    # XIRR Intérêts (added 2026-08-19, mirrors loanch_diversification.py's/
    # bienpreter_diversification.py's/afranga_diversification.py's/
    # iuvo_diversification.py's/lendermarket_diversification.py's own XIRR
    # Intérêts blocks): counterfactual XIRR share attributable to real net
    # interest received since inception. Unlike Loanch (no gross/
    # withholding-tax split), Mintos DOES have one
    # (fetch_statement_totals()'s gross_interest_received/withholding_tax,
    # 5% rate), so "lifetime net interest" here is
    # lifetime_statement_totals["gross_interest_received"] -
    # lifetime_statement_totals["withholding_tax"], reusing the same
    # lifetime_statement_totals dict already fetched below for Cash
    # drag/XIRR Taxes-Frais (not recomputed a second time).
    interest_xirr_contribution = None

    # Day-weighted average invested/non-invested balances (new Sheet rows
    # "solde moyen pondéré investi"/"non investi", added 2026-09-08) -
    # computed whenever all_entries is available, independent of
    # current_month, so this also works for a REPORT_DATE-backfilled past
    # month. Uses the REAL number of days in the period, never a
    # hardcoded 30. Moved ahead of Cash drag below (2026-09-11) so Cash
    # drag can be computed FROM these same two averages.
    avg_invested_balance = None
    avg_non_invested_balance = None
    if all_entries is not None:
        month_start_date = today_date.replace(day=1)
        avg_invested_balance, avg_non_invested_balance = compute_average_balances(
            all_entries, month_start_date, today_date,
            invested_closing_anchor=total_outstanding if current_month else None,
        )
        log.info(
            "Solde moyen pondéré - investi: %.2f EUR, non investi: %.2f EUR (%s to %s).",
            avg_invested_balance, avg_non_invested_balance, month_start_date, today_date,
        )

    # cash_weight/monthly_yield_rate use avg_invested_balance/
    # avg_non_invested_balance (same figures as the "solde moyen pondéré"
    # Sheet rows above) instead of the live total_outstanding snapshot
    # (fixed 2026-09-11), so this % is exactly reconstructible from those
    # two Sheet rows. The lifetime share below still uses total_outstanding
    # (no lifetime-average equivalent exists).
    if current_month and avg_invested_balance is not None and avg_invested_balance > 0 and all_entries is not None:
        today_str = today_date.strftime("%Y-%m-%d")
        cash_weight = avg_non_invested_balance / (avg_non_invested_balance + avg_invested_balance)
        monthly_yield_rate = statement_totals["gross_interest_received"] / avg_invested_balance
        cash_drag_value = cash_weight * monthly_yield_rate
        log.info(
            "Computed Cash drag: %.2f%% (avg non-invested balance %.2f EUR, cash weight %.2f%%, monthly yield %.2f%%).",
            cash_drag_value * 100, avg_non_invested_balance, cash_weight * 100, monthly_yield_rate * 100,
        )

        if xirr_value is not None and signed_cashflows is not None and since_inception_date is not None and total_outstanding > 0:
            avg_idle_cash_lifetime = compute_average_idle_cash(all_entries, since_inception_date.strftime("%Y-%m-%d"), today_str)
            cash_weight_lifetime = avg_idle_cash_lifetime / (avg_idle_cash_lifetime + total_outstanding)
            try:
                lifetime_statement_totals = fetch_statement_totals(session, since_inception_date, today_date)
            except Exception:
                log.exception("Failed to fetch lifetime statement totals - Cash drag/Taxes/Intérêts XIRR shares will not be updated.")
                lifetime_statement_totals = None

            if lifetime_statement_totals is not None:
                lifetime_yield_rate = lifetime_statement_totals["gross_interest_received"] / total_outstanding
                cash_drag_lifetime_total = cash_weight_lifetime * lifetime_yield_rate
                missed_earnings = cash_drag_lifetime_total * (avg_idle_cash_lifetime + total_outstanding)
                lifetime_withholding_tax = lifetime_statement_totals["withholding_tax"]
                lifetime_gross_interest = lifetime_statement_totals["gross_interest_received"]

                # Waterfall decomposition (switched from Shapley
                # 2026-09-09, see shared/xirr_waterfall.py's module
                # docstring for why) - walks a true 0%-return baseline up
                # to total_with_cash in the fixed order Intérêts -> Cash
                # drag -> Bonus -> Frais -> Taxes, using GROSS interest
                # (not net) at the Intérêts step and subtracting
                # missed_earnings right after - each euro counted exactly
                # once, so the shares sum EXACTLY to XIRR real (checked at
                # runtime via a warning log). Mintos has a genuine
                # withholding-tax split (5% rate, see module docstring)
                # but no distinct platform-fee concept - "XIRR Frais" is
                # hardcoded to 0.0, skipped from the steps below.
                steps = [
                    ("XIRR Intérêts", lifetime_gross_interest + missed_earnings),
                    ("XIRR Cash drag", -missed_earnings),
                    ("XIRR Bonus", lifetime_bonus),
                    ("XIRR Taxes", -lifetime_withholding_tax),
                ]
                waterfall_shares = compute_waterfall_xirr_shares(
                    signed_cashflows[:-1], today_date, total_with_cash, steps,
                    log=log, log_context="Mintos",
                )
                bonus_xirr_contribution = waterfall_shares.get("XIRR Bonus")
                cash_drag_xirr_contribution = waterfall_shares.get("XIRR Cash drag")
                taxes_xirr_contribution = waterfall_shares.get("XIRR Taxes")
                frais_xirr_contribution = 0.0
                interest_xirr_contribution = waterfall_shares.get("XIRR Intérêts")
                log.info(
                    "XIRR Waterfall shares (since-inception, avg idle cash %.2f EUR, missed earnings ~%.2f EUR): %r",
                    avg_idle_cash_lifetime, missed_earnings, {k: round(v * 100, 4) for k, v in waterfall_shares.items() if v is not None},
                )
    elif not current_month and all_entries is not None:
        try:
            xirr_block = compute_xirr_block_as_of(session, all_entries, today_date)
        except Exception:
            log.exception("Failed to compute the XIRR block as of %s.", today_date)
            xirr_block = {}
        xirr_value = xirr_block.get("XIRR")
        cash_drag_value = xirr_block.get("Cash drag")
        bonus_xirr_contribution = xirr_block.get("XIRR Bonus")
        cash_drag_xirr_contribution = xirr_block.get("XIRR Cash drag")
        taxes_xirr_contribution = xirr_block.get("XIRR Taxes")
        frais_xirr_contribution = xirr_block.get("XIRR Frais")
        interest_xirr_contribution = xirr_block.get("XIRR Intérêts")
        if xirr_value is None:
            log.warning("Could not compute XIRR as of %s from the reconstructed cashflows.", today_date)

    labeled_amounts = {
        "intérêts brut prêts": statement_totals["gross_interest_received_loans"],
        "intérêts brut obligations": statement_totals["gross_interest_received_obligations"],
        "prélèvements": statement_totals["withholding_tax"],
    }
    if current_month:
        labeled_amounts["en cours prêts"] = portfolio_split["loans"]
        labeled_amounts["en cours obligations"] = portfolio_split["bonds"]

    # Mintos' Sheet block was split (2026-07-29) into 4 individually-labeled
    # sub-rows instead of a single merged "intérêts brut" row directly below
    # the platform - fill_current_month_amounts() assumes THAT single-row
    # shape and would silently write into the wrong row ("en cours prêts")
    # here, so this uses the label-matching variant instead.
    # "prélèvements" (verified live 2026-08-05) sits several rows below the
    # "Mintos" row - the search is bounded dynamically (stops at the next
    # platform's own row), no hardcoded `max_rows` needed.
    fill_current_month_amounts_with_labels(
        platform=PLATFORM_LABEL,
        total=total_with_cash,
        labeled_amounts=labeled_amounts,
        skip_total=not current_month,
    )

    # "Cash drag"/"XIRR" and the XIRR Bonus/Cash drag/Taxes-Frais/Intérêts
    # pie-chart shares sit further below Mintos' block (rows already added
    # by the user, mirroring Afranga/Swaper/Lendermarket/PeerBerry/Loanch's
    # own blocks) - past "Bonus"/"prime"/"cashback"/"concours" (unused
    # here, Mintos has no bonus/cashback feature). Only included when
    # actually computed (current month only).
    # UPDATED 2026-08-19: "XIRR Intérêts" sits right after "XIRR
    # Taxes/Frais" (mirrors Loanch's/Bienprêter's/Afranga's/Iuvo's/
    # Lendermarket's own block layout) - this pushes the block one row
    # taller than it was before, so max_rows is bumped 18 -> 19 to keep the
    # search bounded before the next platform block. IMPORTANT: a "XIRR
    # Intérêts" row must exist in the Mintos block on the sheet itself
    # (right after "XIRR Taxes/Frais") for this new value to actually land
    # somewhere - this script fills an existing row by label, it doesn't
    # insert new labelled rows into this block.
    bonus_breakdown = {}
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
    if bonus_breakdown:
        fill_current_month_bonus_breakdown(platform=PLATFORM_LABEL, breakdown=bonus_breakdown)

    # "Répartition géographique": the "Mintos" row itself is a computed
    # cell in the Sheet (sums its own sub-rows) - only write the per-issuer
    # rows below it, same pattern as Swaper. Per-issuer amounts are also
    # only the CURRENT balance, so only written for the real current month.
    if current_month:
        geo_entries = [{"name": name, "amount": round(amount, 2)} for name, amount in combined_originators.items()]
        fill_geographic_repartition_amounts(geo_entries, platform="Mintos")
        fill_geographic_repartition_uninvested_amount("Mintos", account_summary["available"])


if __name__ == "__main__":
    run()