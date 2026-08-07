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
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import pyotp
import requests
from dotenv import load_dotenv

load_dotenv()

from shared.google_sheet import fill_current_month_amounts, fill_current_month_bonus_breakdown, fill_geographic_repartition_amounts
from shared.report_date import get_report_now, is_current_month

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("loanch_diversification")

LOGIN_PAGE_URL = "https://loanch.com/fr/login"
# Confirmed via a real network capture on 2026-07-27 - see module docstring.
API_SESSION_URL = "https://api.loanch.com/_allauth/browser/v1/auth/session"
API_LOGIN_URL = "https://api.loanch.com/_allauth/browser/v1/auth/login"
API_2FA_URL = "https://api.loanch.com/_allauth/browser/v1/auth/2fa/authenticate"
INVESTMENTS_API_URL = "https://api.loanch.com/api/v1/investments"
STATEMENT_API_URL = "https://api.loanch.com/api/v1/statement-report"
PAGE_SIZE = 100
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
    code = pyotp.TOTP(LOANCH_TOTP_SECRET).now()
    csrf_token = session.cookies.get("csrftoken") or csrf_token
    r = session.post(
        API_2FA_URL,
        json={"code": code},
        headers={**_HEADERS, "X-CSRFToken": csrf_token},
        timeout=20,
    )
    data = r.json() if r.content else {}
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


def run() -> None:
    if not LOANCH_EMAIL or not LOANCH_PASSWORD:
        log.error("LOANCH_EMAIL and LOANCH_PASSWORD environment variables are required.")
        sys.exit(1)

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
    amounts = {
        "total": sum(o["amount"] for o in originators),
        "gross_interest_received": statement_totals["interest_paid"],
        "net_interest_received": statement_totals["interest_paid"],
        "withholding_tax": 0.0,
        "bonus_cashback_contest": statement_totals["rewards"],
        "interest_paid": statement_totals["interest_paid"],
        "rewards": statement_totals["rewards"],
    }
    current_month = is_current_month()

    fill_current_month_amounts(
        platform="Loanch",
        amounts=amounts,
        skip_total=not current_month,
    )

    # Loanch's "total_bonus" ("rewards") is a promotional/referral reward -
    # a "prime", not a cashback/concours - written to its own dedicated
    # sub-row, never to the "Bonus" row itself (a SUM formula over
    # prime/cashback/concours).
    fill_current_month_bonus_breakdown(
        platform="Loanch",
        breakdown={"prime": statement_totals["rewards"]},
    )

    loan_originators = [
        {"name": o["originator"], "amount": o["amount"]}
        for o in originators
    ]

    if current_month:
        fill_geographic_repartition_amounts(loan_originators, platform="Loanch")


if __name__ == "__main__":
    run()
