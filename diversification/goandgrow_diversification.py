"""Go & Grow (Bondora's savings product, https://app.goandgrow.eu/en/gogrow)
balance and statement fetcher.

Same overall data flow/Sheet-writing conventions as monefit_diversification.py
("Crowdlending savings" is a single savings product, no per-loan-originator
split), but a fundamentally different LOGIN TECHNIQUE: Go & Grow's Cloudflare
bot-management blocks Playwright/CDP-driven navigation unconditionally (every
request gets an empty `content-length: 0` response), even with a valid,
authenticated session cookie already seeded into a fresh context - confirmed
reproducible across the bundled Chromium, `channel="chrome"` and
`channel="msedge"`. A plain, non-CDP HTTP client (Python's `requests`) is NOT
blocked at all - neither for the initial GET nor for the POST/form-submission
login requests - so this script logs in via a pure-HTTP replay of the site's
Keycloak/OIDC login flow instead of using Playwright, which also happens to
satisfy running unattended in GitHub Actions trivially (no browser/Chromium
install needed at all, just `requests`).

Login flow (Keycloak/OIDC, `response_mode=form_post`), verified end-to-end
against the real account on 2026-07-17:
    1. `GET https://app.goandgrow.eu/en/gogrow` redirects (via
       `sso.bondora.com`) to a real Keycloak login page with a `<form>`
       whose `action` URL and hidden inputs (`session_code`, `execution`,
       `client_id`, `tab_id`, `client_data`, `credentialId`, ...) must be
       replayed verbatim - only `username`/`password` need to be filled in.
    2. POSTing that form returns a page containing a SECOND hidden
       auto-submit `<form>` (OIDC callback: `code`, `iss`, `id_token`,
       `state`, `session_state`, `continue`) targeting
       `https://app.goandgrow.eu/signin-oidc`.
    3. POSTing THAT form lands on the real, authenticated dashboard HTML
       with a full session cookie jar (`AppAuthCookie`, `accessToken`,
       `bsid`, Keycloak session cookies, etc.) - no JS execution needed
       anywhere in this exchange.
No 2FA/TOTP step was observed on this account.

The dashboard/statements HTML pages are just an SPA shell (data is rendered
client-side by a React app loaded via an iframe) with no server-rendered
balance/statement figures - the real data comes from a separate JSON API
discovered by grepping that SPA's JS bundle for the string "api.prd.goandgrow":
    - `GET https://api.prd.goandgrow.eu/investor/api/v2/gogrow` -> a JSON
      array of "goals" (this account currently has exactly one, named
      "Go & Grow" itself, since no custom sub-goal was created), each with
      `ClientValue` (current balance), `TotalDeposits`, `TotalEarnings`,
      `AnnualTargetReturnRate`.
    - `GET https://api.prd.goandgrow.eu/investor/api/v2/statements` -> a
      JSON array of ledger entries: `{"Type": "Deposit"|"Return", ...,
      "Amount": <float>, "Date": <ISO datetime>, "Balance": <float>}`.
      "Return" entries are Go & Grow's daily interest accrual ("Go & Grow
      returns"); "Deposit" entries are capital movements, not income.
      No bonus/cashback/contest-labelled entry TYPE has been observed yet
      on this brand-new account (created 2026-07-13) - any entry whose
      Type is neither "Deposit" nor "Return" is treated as
      bonus/cashback/contest income by default (see
      fetch_current_month_statement_totals() below), so a real one will be
      picked up automatically once it occurs; its exact Type label should
      be double-checked against the log output at that point.
    Both endpoints accept the plain `requests.Session()`'s cookies with no
    extra bearer-token/CORS workaround needed (same as most other
    *_diversification.py's directly-called JSON APIs).

Required env vars:
    GOANDGROW_EMAIL, GOANDGROW_PASSWORD  -> Go & Grow account credentials
                                             (falls back to the legacy
                                             GOANDGROW_EMAIL/GOANDGROW_PASSWORD
                                             names if the new ones aren't set)
Optional:
    GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS   -> used to write this month's totals
                                              to the Google Sheet via
                                              fill_current_month_amounts() /
                                              fill_current_month_bonus_breakdown()
                                              (see shared/google_sheet.py)

No session/cookie persistence (unlike the Playwright-based scripts'
`storage_state.json` files) - the pure-HTTP login flow above is lightweight
enough to just run fresh every time, no benefit to caching it.
"""

import os
import sys
import logging
from datetime import datetime
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()

from shared.google_sheet import fill_current_month_amounts, fill_current_month_bonus_breakdown, fill_geographic_repartition_amounts
from shared.report_date import get_report_now

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("goandgrow_diversification")

LOGIN_URL = "https://app.goandgrow.eu/en/gogrow"
GOALS_API_URL = "https://api.prd.goandgrow.eu/investor/api/v2/gogrow"
STATEMENTS_API_URL = "https://api.prd.goandgrow.eu/investor/api/v2/statements"
PLATFORM_LABEL = "Go & Grow"
REPORT_TIMEZONE = ZoneInfo("Europe/Paris")

GOANDGROW_EMAIL = os.environ.get("GOANDGROW_EMAIL") or os.environ.get("GOANDGROW_EMAIL")
GOANDGROW_PASSWORD = os.environ.get("GOANDGROW_PASSWORD") or os.environ.get("GOANDGROW_PASSWORD")


class _LoginFormParser(HTMLParser):
    """Extracts the first `<form>`'s `action` plus all of its `<input>`
    name/value pairs (including hidden fields like `session_code`,
    `execution`, `credentialId`, `code`, `id_token`, ...) - both login
    steps' forms need to be replayed verbatim, only overriding
    username/password on the first one."""

    def __init__(self):
        super().__init__()
        self.action = None
        self.inputs = {}
        self._in_form = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form" and self.action is None:
            self._in_form = True
            self.action = attrs.get("action")
        elif tag == "input" and self._in_form:
            name = attrs.get("name")
            if name:
                self.inputs[name] = attrs.get("value", "")


def _find_form(html: str):
    parser = _LoginFormParser()
    parser.feed(html)
    if not parser.action:
        raise RuntimeError("No <form> found in the page - the login flow may have changed.")
    return parser.action, parser.inputs


def login(session: requests.Session) -> None:
    """Log in to Go & Grow via a pure-HTTP replay of its Keycloak/OIDC
    `response_mode=form_post` flow - see module docstring for the full
    3-step exchange. No browser/Playwright involved at all."""
    log.info("Starting Go & Grow login (pure HTTP, no browser - see module docstring)...")

    resp = session.get(LOGIN_URL, timeout=30)
    resp.raise_for_status()
    action, inputs = _find_form(resp.text)
    inputs["username"] = GOANDGROW_EMAIL
    inputs["password"] = GOANDGROW_PASSWORD
    log.info("Submitting credentials to the Keycloak login form...")
    resp2 = session.post(action, data=inputs, timeout=30)
    resp2.raise_for_status()

    action2, inputs2 = _find_form(resp2.text)
    log.info("Submitting the OIDC callback form...")
    resp3 = session.post(action2, data=inputs2, timeout=30)
    resp3.raise_for_status()

    if len(resp3.text) < 5000:
        raise RuntimeError(
            f"Login likely failed - unexpectedly short final page "
            f"(url={resp3.url}, length={len(resp3.text)})."
        )
    log.info("Logged in successfully, dashboard length: %d", len(resp3.text))


def fetch_goals(session: requests.Session) -> list:
    """GET the account's Go & Grow goal(s) - a JSON array, each entry
    having at least `ClientValue` (current balance), `TotalDeposits` and
    `TotalEarnings`. Most accounts only have the single default goal
    (named "Go & Grow" itself), but this sums across ALL of them so a
    multi-goal account's total balance is still correct."""
    resp = session.get(GOALS_API_URL, timeout=30, headers={"Accept": "application/json"})
    resp.raise_for_status()
    goals = resp.json() or []
    log.info("Fetched %d Go & Grow goal(s): %r", len(goals), goals)
    return goals


def fetch_total_balance(goals: list) -> float:
    total = sum(float(g.get("ClientValue") or 0.0) for g in goals)
    return round(total, 2)


def fetch_current_month_statement_totals(session: requests.Session) -> dict:
    """Fetch this calendar month's interest ("Return"-type entries) and
    bonus/cashback/contest totals from the statements API (see module
    docstring), filtering entries by date in Python since this endpoint
    doesn't take a date-range query param (unlike most other platforms'
    equivalents) - it returns the full history, so REPORT_DATE-driven
    "this month" filtering happens locally here.
    """
    now = get_report_now(REPORT_TIMEZONE)
    start_date = now.replace(day=1).date()
    end_date = now.date()
    log.info("Requesting statements API, filtering locally for %s to %s...", start_date, end_date)

    resp = session.get(STATEMENTS_API_URL, timeout=30, headers={"Accept": "application/json"})
    resp.raise_for_status()
    entries = resp.json() or []
    log.info("Fetched %d statement entr(y/ies).", len(entries))

    interest_received = 0.0
    bonus_cashback_contest = 0.0
    unknown_types = set()

    for entry in entries:
        raw_date = entry.get("Date")
        try:
            entry_date = datetime.fromisoformat(raw_date).date()
        except (TypeError, ValueError):
            log.warning("Could not parse statement entry date %r - skipping this entry.", raw_date)
            continue
        if not (start_date <= entry_date <= end_date):
            continue

        entry_type = (entry.get("Type") or "").strip()
        try:
            amount = float(entry.get("Amount") or 0.0)
        except (TypeError, ValueError):
            amount = 0.0

        if entry_type == "Return":
            interest_received += amount
        elif entry_type == "Deposit":
            continue  # capital movement, not income
        else:
            # No bonus/cashback/contest-labelled entry Type has been seen
            # yet on this account (see module docstring) - treat anything
            # else as bonus/cashback/contest income by default.
            bonus_cashback_contest += amount
            unknown_types.add(entry_type)

    if unknown_types:
        log.info(
            "Statement entry Type(s) treated as bonus/cashback/contest this run: %s",
            unknown_types,
        )

    interest_received = round(interest_received, 2)
    bonus_cashback_contest = round(bonus_cashback_contest, 2)
    log.info(
        "This month's (%s to %s) totals: interest_received=%.2f, bonus_cashback_contest=%.2f",
        start_date, end_date, interest_received, bonus_cashback_contest,
    )
    return {
        "interest_received": interest_received,
        "bonus_cashback_contest": bonus_cashback_contest,
    }


def run() -> None:
    if not GOANDGROW_EMAIL or not GOANDGROW_PASSWORD:
        log.error("GOANDGROW_EMAIL and GOANDGROW_PASSWORD environment variables are required.")
        sys.exit(1)

    log.info("Starting Go & Grow diversification run (pure HTTP, no browser).")

    session = requests.Session()
    # Deliberately no browser-like User-Agent spoofing here: besides being
    # unnecessary (Cloudflare doesn't block this pure-HTTP client, see
    # module docstring), a spoofed Chrome UA has been observed to trip up
    # an unrelated LOCAL corporate proxy's obsolete-browser block when
    # testing on that network.
    session.headers.update({
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })

    try:
        login(session)
        goals = fetch_goals(session)
        balance = fetch_total_balance(goals)
    except Exception:
        log.exception("Failed to log in or fetch the Go & Grow balance.")
        sys.exit(1)

    try:
        statement_totals = fetch_current_month_statement_totals(session)
    except Exception:
        log.exception("Failed to fetch this month's statement totals - defaulting to 0.0.")
        statement_totals = {"interest_received": 0.0, "bonus_cashback_contest": 0.0}

    log.info("Go & Grow balance: %.2f EUR", balance)
    log.info(
        "This month's interest: %.2f EUR, bonus/cashback/contest: %.2f EUR",
        statement_totals["interest_received"], statement_totals["bonus_cashback_contest"],
    )

    # Go & Grow's statements API has no gross/net/withholding-tax
    # breakdown - "interest_received" (summed "Return"-type entries) is
    # mapped to both gross_interest_received/net_interest_received,
    # withholding_tax defaults to 0.0. Same standardized dict shape as
    # every other *_diversification.py.
    amounts = {
        "total": balance,
        "gross_interest_received": statement_totals["interest_received"],
        "net_interest_received": statement_totals["interest_received"],
        "withholding_tax": 0.0,
        "bonus_cashback_contest": statement_totals["bonus_cashback_contest"],
    }
    fill_current_month_amounts(
        platform=PLATFORM_LABEL,
        amounts=amounts,
        section="Crowdlending savings",
    )

    # No bonus/cashback/contest statement entry Type has been observed yet
    # on this account (see module docstring) - everything currently
    # defaults into "prime", same catch-all convention used by
    # monefit_diversification.py until a real one shows up and its exact
    # Type label can be mapped to the right sub-row.
    fill_current_month_bonus_breakdown(
        platform=PLATFORM_LABEL,
        breakdown={"prime": statement_totals["bonus_cashback_contest"]},
        section="Crowdlending savings",
    )

    fill_geographic_repartition_amounts([{"name": PLATFORM_LABEL, "amount": balance}])


if __name__ == "__main__":
    run()
