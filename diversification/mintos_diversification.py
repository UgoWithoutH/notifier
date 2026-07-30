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
from datetime import date

import requests
from dotenv import load_dotenv

from shared.google_sheet import fill_current_month_amounts_with_labels, fill_geographic_repartition_amounts

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("mintos_diversification")

ACCOUNT_STATEMENT_PAGE_URL = "https://www.mintos.com/fr/account-statement/"
ACCOUNT_STATEMENT_SUMMARY_URL = "https://www.mintos.com/webapp/api/fr/account-statement/summary/"
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


def fetch_account_summary(session: requests.Session) -> float:
    """Fetch this account's total invested (loans) amount. See module
    docstring for the verified (nested) 'invested' field."""
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

    try:
        return float(invested)
    except (TypeError, ValueError):
        raise RuntimeError(f"Could not parse 'invested.amount' out of {invested!r}.")


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


def fetch_current_month_statement_totals(session: requests.Session) -> dict:
    """Fetch this calendar month's interest received / withholding tax via
    the Account Statement page's own summary endpoint. See module docstring
    for the verified endpoint/fields/CSRF mechanism."""
    csrf_token = get_csrf_token(session)

    today = date.today()
    first = today.replace(day=1)
    form = {
        "account_statement_filter[currency]": str(CURRENCY_ID),
        "account_statement_filter[fromDate]": first.strftime("%d.%m.%Y"),
        "account_statement_filter[toDate]": today.strftime("%d.%m.%Y"),
    }
    log.info("POST %s (fetching this month's statement summary)...", ACCOUNT_STATEMENT_SUMMARY_URL)
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
        elif "prélèvement à la source" in label or "prelevement a la source" in label:
            withholding_tax += abs(amount)

    gross_interest_received = gross_interest_received_loans + gross_interest_received_obligations

    return {
        "gross_interest_received": round(gross_interest_received, 2),
        "gross_interest_received_loans": round(gross_interest_received_loans, 2),
        "gross_interest_received_obligations": round(gross_interest_received_obligations, 2),
        "withholding_tax": round(withholding_tax, 2),
    }


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
        invested = fetch_account_summary(session)
        originators = fetch_loan_originator_breakdown(session)
        bond_issuers = fetch_bond_issuer_breakdown(session)
        portfolio_split = fetch_portfolio_split(session)
        statement_totals = fetch_current_month_statement_totals(session)
    except Exception:
        log.exception("Failed to fetch Mintos account data.")
        sys.exit(1)

    log.info("Total invested (loans): %.2f EUR", invested)
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
    # bonds - the Mintos row's total must be loans+obligations combined so
    # it matches the sum of the two "en cours" sub-rows below it.
    total_outstanding = portfolio_split["loans"] + portfolio_split["bonds"]

    amounts = {
        "total": total_outstanding,
        "gross_interest_received": statement_totals["gross_interest_received"],
        "net_interest_received": net_interest_received,
        "withholding_tax": statement_totals["withholding_tax"],
        "loans_outstanding": portfolio_split["loans"],
        "obligations_outstanding": portfolio_split["bonds"],
    }
    log.info("Amounts to write: %s", amounts)

    # Mintos' Sheet block was split (2026-07-29) into 4 individually-labeled
    # sub-rows instead of a single merged "intérêts brut" row directly below
    # the platform - fill_current_month_amounts() assumes THAT single-row
    # shape and would silently write into the wrong row ("en cours prêts")
    # here, so this uses the label-matching variant instead.
    fill_current_month_amounts_with_labels(
        platform=PLATFORM_LABEL,
        total=total_outstanding,
        labeled_amounts={
            "en cours prêts": portfolio_split["loans"],
            "en cours obligations": portfolio_split["bonds"],
            "intérêts brut prêts": statement_totals["gross_interest_received_loans"],
            "intérêts brut obligations": statement_totals["gross_interest_received_obligations"],
        },
    )

    # "Répartition géographique": the "Mintos" row itself is a computed
    # cell in the Sheet (sums its own sub-rows) - only write the per-issuer
    # rows below it, same pattern as Swaper.
    geo_entries = [{"name": name, "amount": round(amount, 2)} for name, amount in combined_originators.items()]
    fill_geographic_repartition_amounts(geo_entries, platform="Mintos")


if __name__ == "__main__":
    run()
