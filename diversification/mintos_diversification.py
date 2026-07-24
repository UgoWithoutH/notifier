"""Mintos loan portfolio fetcher (this month's interest + invested total).

Same family as bricks_diversification.py / monefit_diversification.py: pure
`requests` (no Playwright at runtime), reads the account's invested total +
this calendar month's interest received / withholding tax, then hands them
to fill_current_month_amounts() / fill_geographic_repartition_amounts()
(see google_sheet.py) so they can be filled into the Google Sheet. No email
is sent - same as the other diversification scripts.

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
  (matches the "Prêts" figure on the Portfolio Dashboard).
- `GET /webapp/api/marketplace-api/v1/accounts/978/portfolio-distributions`
  -> `{"loanOriginators": {"distribution": [{"name": "ID Finance", "total":
  {"currency": "EUR", "amount": "169.34..."}, "count": 51}, ...]},
  "countries": {"distribution": [...]}, ...}` - fetched here only for
  informational logging (see fetch_loan_originator_breakdown()); the Sheet's
  "Répartition géographique" section only has a single "Mintos" row (not one
  per loan originator, verified 2026-07-24), so only the aggregate
  "invested" total is actually written there.
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

Required env vars:
    MINTOS_PHPSESSID, MINTOS_MW_SESSION_ID -> session cookies, see
                                               mintos_get_session.py
    GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS     -> used to write this month's
                                               totals to the Google Sheet via
                                               fill_current_month_amounts()
                                               (see google_sheet.py)
"""

import os
import re
import sys
import logging
from datetime import date

import requests
from dotenv import load_dotenv

from shared.google_sheet import fill_current_month_amounts, fill_geographic_repartition_amounts

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("mintos_diversification")

ACCOUNT_STATEMENT_PAGE_URL = "https://www.mintos.com/fr/account-statement/"
ACCOUNT_STATEMENT_SUMMARY_URL = "https://www.mintos.com/webapp/api/fr/account-statement/summary/"
CURRENCY_ID = 978  # ISO 4217 numeric code for EUR
ACCOUNT_URL = f"https://www.mintos.com/webapp/api/marketplace-api/v1/accounts/{CURRENCY_ID}"
PORTFOLIO_DISTRIBUTIONS_URL = f"https://www.mintos.com/webapp/api/marketplace-api/v1/accounts/{CURRENCY_ID}/portfolio-distributions"
PLATFORM_LABEL = "Mintos"

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


def fetch_loan_originator_breakdown(session: requests.Session) -> list:
    """Fetch the per-loan-originator EUR breakdown, for informational
    logging only (the Sheet's 'Répartition géographique' section only has a
    single aggregate 'Mintos' row, not one per originator - see module
    docstring)."""
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

    gross_interest_received = 0.0
    withholding_tax = 0.0
    for type_id, raw_amount in groups.items():
        label = (types.get(type_id) or "").lower()
        try:
            amount = float(raw_amount)
        except (TypeError, ValueError):
            continue
        if "intér" in label or "inter" in label:
            gross_interest_received += amount
        elif "prélèvement à la source" in label or "prelevement a la source" in label:
            withholding_tax += abs(amount)

    return {
        "gross_interest_received": round(gross_interest_received, 2),
        "withholding_tax": round(withholding_tax, 2),
    }


def run() -> None:
    if not MINTOS_PHPSESSID or not MINTOS_MW_SESSION_ID:
        log.error(
            "MINTOS_PHPSESSID and MINTOS_MW_SESSION_ID environment variables are required. %s",
            SESSION_EXPIRED_MESSAGE,
        )
        sys.exit(1)

    log.info("Starting Mintos diversification run (pure HTTP, no Playwright).")
    session = build_session()

    try:
        invested = fetch_account_summary(session)
        originators = fetch_loan_originator_breakdown(session)
        statement_totals = fetch_current_month_statement_totals(session)
    except Exception:
        log.exception("Failed to fetch Mintos account data.")
        sys.exit(1)

    log.info("Total invested (loans): %.2f EUR", invested)
    for o in originators:
        log.info("  %s: %.2f EUR", o["originator"], o["outstanding"])

    net_interest_received = statement_totals["gross_interest_received"] - statement_totals["withholding_tax"]
    log.info(
        "This month's interest: gross=%.2f EUR, net=%.2f EUR, withholding_tax=%.2f EUR",
        statement_totals["gross_interest_received"], net_interest_received, statement_totals["withholding_tax"],
    )

    amounts = {
        "total": invested,
        "gross_interest_received": statement_totals["gross_interest_received"],
        "net_interest_received": net_interest_received,
        "withholding_tax": statement_totals["withholding_tax"],
    }
    fill_current_month_amounts(platform=PLATFORM_LABEL, amounts=amounts)

    # The Sheet's "Répartition géographique" section only has a single
    # aggregate "Mintos" row (verified 2026-07-24), not one per loan
    # originator - so only the platform-level invested total is written
    # here, unlike Swaper (which writes one row per originator).
    fill_geographic_repartition_amounts([{"name": PLATFORM_LABEL, "amount": invested}])


if __name__ == "__main__":
    run()
