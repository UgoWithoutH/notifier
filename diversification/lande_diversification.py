"""Lande Finance (lande.finance) account balance + this month's interest
fetcher.

Same family as mintos_diversification.py: pure `requests` (no Playwright at
runtime), authenticated with 3 session cookies captured by the separate
LOCAL-only lande_get_session.py helper (see that file's docstring for why
login can't be automated here). No email is sent - same as every other
diversification script; feeds fill_current_month_amounts() (see
google_sheet.py) so it can be filled into the Google Sheet.

Why login is manual (mirrors Mintos, but for a different underlying
reason): lande.finance's login page is protected by a Cloudflare Turnstile
"I'm not a robot" managed challenge that DETECTS Playwright/CDP-driven
automation and loops the challenge forever - confirmed 2026-07-29 across
TWO separate mitigation attempts (plain bundled Chromium; real Chrome via
`channel="chrome"` PLUS this repo's shared/browser_stealth.py patches -
navigator.webdriver override, realistic UA/viewport, human-like mouse
movement), both failed identically (URL's `__cf_chl_rt_tk` challenge token
changes on every retry, session never reaches /investor). This matches the
same class of blocker already documented for Go & Grow's Keycloak login
before its pure-HTTP rewrite: the CDP automation protocol itself is the
detected signal, not a UA/fingerprint-level tell fixable via JS patches.
WORKAROUND THAT WORKS (verified 2026-07-29): launch the user's REAL Chrome
as a plain OS subprocess (NOT through Playwright's launch mechanism at
all, so none of Playwright's automation flags/CDP launch signature are
present) with `--remote-debugging-port`, let the user log in 100%
manually (the Turnstile challenge passes normally in this genuinely
non-automated browser), THEN attach Playwright via `connect_over_cdp()`
purely to read the resulting cookies - see lande_get_session.py. Once
past login, the actual DATA endpoints are NOT bot-protected at all: a
plain `requests.Session()` seeded with the captured cookies (cf_clearance,
lande_session, XSRF-TOKEN) gets normal 200 responses with zero further
challenge (verified live 2026-07-29 - same "browser-only-for-the-hard-part,
pure-HTTP-for-the-easy-part" split already used for Mintos, for a
different underlying gate).

Session cookies (LANDE_CF_CLEARANCE / LANDE_LANDE_SESSION /
LANDE_XSRF_TOKEN env vars): unlike Mintos's self-renewing cookies, Lande's
`cf_clearance`/session lifetimes are NOT specifically characterized here -
if a run fails with the "session expired" message below, just re-run
lande_get_session.py locally to get fresh values.

Data sources (server-rendered HTML - lande.finance is a Laravel app, NOT a
JSON API - confirmed via the XSRF-TOKEN/lande_session cookie naming and
verified live 2026-07-29):
- `GET https://lande.finance/fr/investor` -> account overview page, embeds
  `<p ... id="total_balance"> € 946.57 </p>` (literal `&nbsp;` between the
  euro sign and the amount in the raw HTML) - this is the account's total
  balance ("Fonds disponibles" + "Fonds investis" + "Fonds réservés"),
  used as-is for the "total" figure.
- `GET https://lande.finance/fr/investor/transactions?search=1&
  start_date=<DD.MM.YYYY>&end_date=<DD.MM.YYYY>&page=<N>` -> paginated
  (15 rows/page, confirmed via the page's own "Showing X to Y of Z"
  footer) list of `<article>` blocks, one per transaction. Each interest
  entry looks like:
  `<span class="capitalize">Intérêt</span>...<span class="text-brand-green">
  +€&nbsp;1.14</span>` (loan link + date also present, not needed here).
  Other transaction types seen: "Principal", "Demande de retrait"
  (withdrawal request) - ignored, only "Intérêt"-labelled entries are
  summed for gross interest. No separate withholding-tax transaction type
  was found on this account (net == gross, tax = 0.0, same convention as
  Swaper/Loanch/Iuvo/etc. for platforms with no tax breakdown). Pagination
  continues until a page returns zero `<article>` blocks (capped by
  MAX_TRANSACTIONS_PAGES as a safety net, same pattern as
  bienpreter_diversification.py's operations pagination).
- The "Compte de résultat" link the user mentioned
  (`/fr/investor/transactions/tax-report?...`) turns out to render a PDF
  (confirmed 2026-07-29 - Chrome's own PDF viewer loaded it, the raw HTML
  response is just an empty PDF-embedder shell) - NOT used here, since the
  transactions page already gives a full per-entry interest breakdown that
  can be summed directly, no PDF parsing needed.

No bonus/cashback data has been observed on this account (only
Intérêt/Principal/Demande de retrait transaction types seen in the range
checked) - `bonus_cashback_contest` defaults to 0.0, same "not a discovered
bug" convention as every other platform with no such feature confirmed yet.
The Sheet's "Lande" block (Crowdlending section) already has its own
"cashback" sub-row, so if a real cashback transaction type is ever
observed on this account, wire it up via fill_current_month_bonus_breakdown()
(see google_sheet.py) - deliberately NOT done here yet, no real data seen.

The "Répartition géographique" section also already has a single "Lande"
aggregate row (under a "Crowdlending agricole" sub-header, verified live
2026-07-29) with NO per-borrower sub-rows below it (unlike Mintos/Swaper's
per-issuer breakdown rows) - written via fill_geographic_repartition_amounts()
with just the account's total balance, same single-row pattern already
used for Go & Grow's aggregate row.

run() accepts an optional pre-built `requests.Session` (see
lande_get_session.py) for a one-shot "log in by hand, then let this take
over" flow - the env vars below are only required when calling run() with
no session (the normal scheduled/headless case).

Required env vars:
    LANDE_CF_CLEARANCE, LANDE_LANDE_SESSION, LANDE_XSRF_TOKEN
                                            -> session cookies, see
                                               lande_get_session.py
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

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("lande_diversification")

INVESTOR_PAGE_URL = "https://lande.finance/fr/investor"
TRANSACTIONS_URL = "https://lande.finance/fr/investor/transactions"
PLATFORM_LABEL = "Lande"
MAX_TRANSACTIONS_PAGES = 50  # safety net, see bienpreter_diversification.py's identical pattern

LANDE_CF_CLEARANCE = os.environ.get("LANDE_CF_CLEARANCE")
LANDE_LANDE_SESSION = os.environ.get("LANDE_LANDE_SESSION")
LANDE_XSRF_TOKEN = os.environ.get("LANDE_XSRF_TOKEN")

_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

SESSION_EXPIRED_MESSAGE = (
    "Lande session looks expired or invalid (got redirected to /login or "
    "couldn't find the expected data on the page). Run "
    "`python -m diversification.lande_get_session` locally (from your own "
    "machine, not CI) to log in again, then update LANDE_CF_CLEARANCE/"
    "LANDE_LANDE_SESSION/LANDE_XSRF_TOKEN in your local .env and in the "
    "GitHub repository secrets."
)


def build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": _USER_AGENT})
    s.cookies.set("cf_clearance", LANDE_CF_CLEARANCE, domain=".lande.finance", path="/")
    s.cookies.set("lande_session", LANDE_LANDE_SESSION, domain=".lande.finance", path="/")
    s.cookies.set("XSRF-TOKEN", LANDE_XSRF_TOKEN, domain=".lande.finance", path="/")
    return s


def _check_authenticated(r: requests.Response) -> None:
    if r.status_code in (401, 403) or "/login" in r.url:
        raise RuntimeError(SESSION_EXPIRED_MESSAGE)


def _parse_amount(text: str) -> float:
    cleaned = text.replace("\xa0", "").replace(" ", "").replace(",", "").strip()
    return float(cleaned)


def fetch_account_balance(session: requests.Session) -> float:
    """Fetch the account's total balance from the investor overview page's
    own server-rendered `id="total_balance"` element. See module docstring
    for the verified markup."""
    log.info("GET %s (fetching account balance)...", INVESTOR_PAGE_URL)
    r = session.get(INVESTOR_PAGE_URL, timeout=20)
    log.info("GET /fr/investor: status=%s", r.status_code)
    _check_authenticated(r)
    if not r.ok:
        raise RuntimeError(f"Investor page returned status {r.status_code}")

    match = re.search(r'id="total_balance">\s*€[\s\xa0]*([\d.,\s\xa0]+?)\s*</p>', r.text)
    if not match:
        raise RuntimeError(SESSION_EXPIRED_MESSAGE)

    try:
        return _parse_amount(match.group(1))
    except ValueError:
        raise RuntimeError(f"Could not parse total_balance out of {match.group(1)!r}.")


def fetch_current_month_interest(session: requests.Session) -> float:
    """Fetch this calendar month's (1st of month through today) gross
    interest received by paginating the transactions page and summing
    every "Intérêt"-labelled entry's amount. See module docstring for the
    verified markup/pagination behavior."""
    today = date.today()
    first = today.replace(day=1)
    start_date = first.strftime("%d.%m.%Y")
    end_date = today.strftime("%d.%m.%Y")

    gross_interest = 0.0
    page = 1
    while page <= MAX_TRANSACTIONS_PAGES:
        params = {"search": "1", "start_date": start_date, "end_date": end_date, "page": page}
        log.info("GET %s (page %s, fetching this month's transactions)...", TRANSACTIONS_URL, page)
        r = session.get(TRANSACTIONS_URL, params=params, timeout=20)
        log.info("GET transactions page %s: status=%s", page, r.status_code)
        _check_authenticated(r)
        if not r.ok:
            raise RuntimeError(f"Transactions page returned status {r.status_code}")

        articles = r.text.split("<article")[1:]
        if not articles:
            log.info("Page %s has no transactions - stopping pagination.", page)
            break

        page_interest_count = 0
        for article in articles:
            body = article.split("</article>")[0]
            if ">Intérêt<" not in body:
                continue
            amount_match = re.search(r'\+€[\s\xa0]*([\d.,\s\xa0]+?)</span>', body)
            if not amount_match:
                continue
            try:
                gross_interest += _parse_amount(amount_match.group(1))
                page_interest_count += 1
            except ValueError:
                log.warning("Could not parse interest amount %r on page %s.", amount_match.group(1), page)

        log.info("Page %s: %s transaction(s), %s interest entrie(s) found (running total: %.2f).",
                  page, len(articles), page_interest_count, gross_interest)

        if len(articles) < 15:
            # Fewer than a full page's worth of rows -> this was the last page.
            break
        page += 1
    else:
        log.warning("Hit MAX_TRANSACTIONS_PAGES (%s) without an empty/partial page.", MAX_TRANSACTIONS_PAGES)

    return round(gross_interest, 2)


def run(session: requests.Session | None = None) -> None:
    """Runs the full fetch + Google Sheet write.

    `session` lets a caller hand off an already-authenticated
    `requests.Session` (e.g. lande_get_session.py, right after a real
    manual login) instead of building one from the LANDE_CF_CLEARANCE/
    LANDE_LANDE_SESSION/LANDE_XSRF_TOKEN env vars - useful for a one-shot
    "log in by hand, then let the automation take over" flow. When
    omitted (the normal scheduled-run case), falls back to the env-var-
    based session as before.
    """
    if session is None:
        if not LANDE_CF_CLEARANCE or not LANDE_LANDE_SESSION or not LANDE_XSRF_TOKEN:
            log.error(
                "LANDE_CF_CLEARANCE, LANDE_LANDE_SESSION and LANDE_XSRF_TOKEN "
                "environment variables are required. %s",
                SESSION_EXPIRED_MESSAGE,
            )
            sys.exit(1)
        session = build_session()

    log.info("Starting Lande diversification run (pure HTTP, no Playwright).")

    try:
        total = fetch_account_balance(session)
    except Exception:
        log.exception("Failed to fetch Lande account balance.")
        sys.exit(1)

    log.info("Total balance: %.2f EUR", total)

    try:
        gross_interest_received = fetch_current_month_interest(session)
    except Exception:
        log.exception("Failed to fetch Lande this month's interest - defaulting to 0.0.")
        gross_interest_received = 0.0

    log.info("This month's gross interest received: %.2f EUR", gross_interest_received)

    amounts = {
        "total": total,
        "gross_interest_received": gross_interest_received,
        "net_interest_received": gross_interest_received,
        "withholding_tax": 0.0,
        "bonus_cashback_contest": 0.0,
    }
    log.info("Amounts to write: %s", amounts)

    from shared.google_sheet import fill_current_month_amounts, fill_geographic_repartition_amounts
    fill_current_month_amounts(platform=PLATFORM_LABEL, amounts=amounts)

    # "Répartition géographique" has a single "Lande" aggregate row (no
    # per-borrower sub-rows below it, unlike Mintos/Swaper) - same value as
    # the Crowdlending section's total.
    fill_geographic_repartition_amounts([{"name": PLATFORM_LABEL, "amount": total}])


if __name__ == "__main__":
    run()
