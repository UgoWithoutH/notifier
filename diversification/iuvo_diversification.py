"""Iuvo (iuvo-group.com) portfolio balance + loan-originator diversification
fetcher.

REWRITTEN 2026-07-27 to use plain `requests` instead of Playwright (no
browser at all) - same technique as bricks_diversification.py/
monefit_diversification.py/goandgrow_diversification.py. The original
Playwright-based version (verified working end-to-end the same day, prior
to this rewrite) was replaced after network-capturing a real login: despite
Cloudflare fronting tbp2p.iuvo-group.com (`cf-ray`/`server: cloudflare`
response headers, and a passive `cdn-cgi/challenge-platform` JS beacon
observed during normal page loads), a direct non-browser `requests` POST to
the login endpoint is NOT blocked - confirmed 2026-07-27 end-to-end against
the real account (login + balance/originator breakdown + date-filtered
account statement all succeeded with a plain `requests.Session()`). No
storage_state/cookie persistence across runs is implemented either, same
as bricks/monefit/goandgrow - logging in fresh every run is cheap.

Underlying architecture: legacy "TeleBid/TB p2p" white-label engine (the
same backend engine also runs Swaper - see swaper_diversification.py -
but that's a separate deployment; don't assume this pure-HTTP technique
transfers there without re-testing).

Auth mechanism: `POST https://tbp2p.iuvo-group.com/p2p-ui/?p0=login;=en_US;
randn=<random 0..1>` with form-urlencoded body `login=<email>&password=
<password>` returns `{"result": {"session_token": "<32-char hex>", ...},
"status": {"status": "ok"}}` - this `session_token` (NOT a cookie) must be
passed as the `p2=` query-string param on every subsequent
tbp2p.iuvo-group.com API call. `token_expiration_seconds` is only 300 (5
minutes), but a full run of this script takes a few seconds, well within
that window - no refresh logic implemented. A `PHPSESSID` cookie is also
set by the main iuvo-group.com WordPress site (harmless/unused by the API
calls themselves; `requests.Session()` carries it automatically anyway).

Data sources (both real endpoints, found 2026-07-27 by capturing a
Playwright-driven browser's network traffic during exploration):
  - `GET https://tbp2p.iuvo-group.com/p2p-ui/v2/app?p0=overview_page;
    p2=<token>;lang=en_US&screen_width=1280&screen_height=720` -> a
    server-rendered HTML page (NOT a JSON API) that embeds a
    `var investors = [{...}];` JavaScript literal directly in a `<script>`
    tag - this is FULL-PRECISION JSON (unlike the "Chart by loans" widget
    rendered from the same data, which visually rounds to the nearest
    whole EUR). `investors[0].accountBalance` has: `availableFunds` =
    "Available Funds", `investedFunds` = "Receivables in P2P",
    `productInvestedFunds` = "Receivables in iuvoSAVE", `totalAmount` =
    "Total" (verified 2026-07-27: 0.00 + 1000.00 + 0.00 = 1000.00 EUR,
    same real-account figures as the original Playwright version).
    `investmentsByLoanOriginator` is a list of
    `{"aggregator": <name>, "value": <exact float>, "percentage": <pct>}`
    - `value` is used DIRECTLY as each originator's amount (no more
    percentage-of-total computation trick needed like Swaper/Bricks, since
    this `value` is already full precision - confirmed 2026-07-27:
    VivaCredit value=1000.0, exact match with investedFunds).
  - `GET https://tbp2p.iuvo-group.com/p2p-ui/v2/app?p0=
    account_statement_grouped_page;p2=<token>;lang=en_US&screen_width=
    1280&screen_height=720` (no extra params) - called ONCE first just to
    scrape `investor_account_id` (a stable per-currency-account id, e.g.
    29101 for this EUR account) out of the pre-selected
    `<option value="29101" selected="selected">EUR (€)</option>` in the
    page's own "Accounts" filter `<select>` - then the SAME endpoint is
    called AGAIN with `investor_account_id=<id>&trans_category=all&
    date_from=<1st of month>&date_to=<today>&p2=<token>&lang=en_US&
    screen_width=...&screen_height=...` added, which returns the actual
    date-filtered `<table class="table table-bordered p2p-table">` (this
    table's rows are technically malformed HTML - missing `</tr>` closing
    tags - so it's parsed with a regex matching each row's
    `<a class="btn p2p-trans" value="TYPE">...</a></td><td class="
    (positive|negative)-turnover">AMOUNT</td>` pair directly, rather than
    per-`<tr>` DOM/BeautifulSoup parsing, verified 2026-07-27 to correctly
    extract both rows of a real 2-row statement). Same `trans_type`
    classification convention as the original Playwright version:
    `payment_interest`/`payment_interest_buyback`/`payment_interest_early`
    summed into gross interest, `bonus` summed into
    bonus_cashback_contest. Iuvo has no separate withholding-tax
    transaction type, so net_interest_received == gross_interest_received
    and withholding_tax defaults to 0.0 (same convention as
    Swaper/Loanch/etc.).

Required env vars:
    IUVO_EMAIL, IUVO_PASSWORD            -> Iuvo account credentials
Optional:
    GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS   -> used to write this month's
                                             totals/breakdown to the Google
                                             Sheet via
                                             fill_current_month_amounts()/
                                             fill_geographic_repartition_amounts()
                                             (see google_sheet.py)
"""

import json
import logging
import os
import random
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

import requests

try:
    from shared.google_sheet import fill_current_month_amounts, fill_geographic_repartition_amounts
    from shared.report_date import get_report_now
except ModuleNotFoundError:
    # Support direct execution (python diversification/iuvo_diversification.py)
    # where the project root may not be on sys.path.
    project_root = Path(__file__).resolve().parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    from shared.google_sheet import fill_current_month_amounts, fill_geographic_repartition_amounts
    from shared.report_date import get_report_now

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("iuvo_diversification")

LOGIN_PAGE_URL = "https://iuvo-group.com/en/login/"
API_BASE = "https://tbp2p.iuvo-group.com"
# Pin the timezone explicitly (rather than relying on the executing
# machine's local clock, e.g. UTC on a CI runner) so "today"/"this month"
# are computed in the account's own local time, same pattern as every
# other *_diversification.py.
REPORT_TIMEZONE = ZoneInfo("Europe/Paris")

# Verified 2026-07-27 against the real `trans_type` filter dropdown's
# option values and the account statement transaction rows' `value`
# attributes (see module docstring).
INTEREST_TRANS_TYPES = {"payment_interest", "payment_interest_buyback", "payment_interest_early"}
BONUS_TRANS_TYPES = {"bonus"}

IUVO_EMAIL = os.environ.get("IUVO_EMAIL")
IUVO_PASSWORD = os.environ.get("IUVO_PASSWORD")


def _parse_amount(text):
    """Parse a currency-formatted amount (e.g. "1000.00", "-1000.00")
    into a float. Iuvo's own figures use '.' as the decimal separator and
    ',' only as a thousands separator (never both meaning decimals)."""
    if text is None:
        return None
    cleaned = text.replace("\xa0", " ").replace("EUR", "").replace("€", "").strip()
    cleaned = cleaned.replace(" ", "").replace(",", "")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def login(session: requests.Session) -> str:
    """Log in to Iuvo via a direct POST to its legacy TeleBid backend (no
    browser - see module docstring). Returns the `session_token` ("p2")
    that must be passed on every subsequent API call."""
    log.info("Logging in to Iuvo as %s...", IUVO_EMAIL)
    # Cheap warm-up GET of the login page - not strictly required (the
    # login POST works fine without it too), but mirrors a real browser's
    # navigation and picks up the WordPress PHPSESSID cookie.
    session.get(LOGIN_PAGE_URL, timeout=30)

    resp = session.post(
        f"{API_BASE}/p2p-ui/?p0=login;=en_US;randn={random.random()}",
        data={"login": IUVO_EMAIL, "password": IUVO_PASSWORD},
        headers={"Referer": LOGIN_PAGE_URL, "Origin": "https://iuvo-group.com"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Iuvo login failed: HTTP {resp.status_code} - {resp.text[:300]}")

    data = resp.json()
    if (data.get("status") or {}).get("status") != "ok":
        raise RuntimeError(f"Iuvo login failed: {data!r}")

    session_token = (data.get("result") or {}).get("session_token")
    if not session_token:
        raise RuntimeError(f"Iuvo login did not return a session_token: {data!r}")

    log.info("Logged in successfully.")
    return session_token


def fetch_balance_and_originators(session: requests.Session, session_token: str) -> dict:
    """Fetch the "Account Balance" widget's exact figures + per-loan-
    originator breakdown from the overview_page's embedded
    `var investors = [...]` JS literal. See module docstring."""
    log.info("Fetching Iuvo overview page (balance + loan-originator breakdown)...")
    resp = session.get(
        f"{API_BASE}/p2p-ui/v2/app",
        params={
            "p0": "overview_page", "p2": session_token, "lang": "en_US",
            "screen_width": 1280, "screen_height": 720,
        },
        headers={"Referer": "https://iuvo-group.com/en/dashboard/"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Iuvo overview_page returned HTTP {resp.status_code}")

    match = re.search(r"var investors = (\[.*?\]);", resp.text)
    if not match:
        raise RuntimeError("Could not find 'var investors = [...]' in the overview_page response.")
    investors = json.loads(match.group(1))
    if not investors:
        raise RuntimeError("The 'investors' array in the overview_page response is empty.")

    balance = investors[0].get("accountBalance") or {}
    log.info("Raw accountBalance payload: %r", balance)
    total = _parse_amount(str(balance.get("totalAmount")))
    invested_funds = _parse_amount(str(balance.get("investedFunds")))
    if total is None or invested_funds is None:
        raise RuntimeError(f"Could not parse 'totalAmount'/'investedFunds' out of {balance!r}")

    originators = []
    for entry in balance.get("investmentsByLoanOriginator") or []:
        name = entry.get("aggregator")
        value = entry.get("value")
        if name is None or value is None:
            continue
        originators.append({"name": str(name).strip(), "amount": round(float(value), 2)})

    return {
        "total": total,
        "receivables_p2p": invested_funds,
        "originators": originators,
    }


def _find_investor_account_id(html: str) -> str:
    match = re.search(
        r'id="investor_account_id".*?<option value="(\d+)"\s*(?:\n\s*)?selected="selected"',
        html, re.S,
    )
    if not match:
        raise RuntimeError("Could not find the selected 'investor_account_id' option in the account-statement page.")
    return match.group(1)


def fetch_current_month_interest(session: requests.Session, session_token: str) -> dict:
    """Fetch this calendar month's interest/bonus received from the
    date-filtered Account Statement page. See module docstring for the
    verified two-step mechanism (fetch investor_account_id, then re-fetch
    with the date range added)."""
    now = get_report_now(REPORT_TIMEZONE)
    date_from = now.replace(day=1).date().isoformat()
    date_to = now.date().isoformat()

    log.info("Fetching Iuvo account-statement page (to find investor_account_id)...")
    resp = session.get(
        f"{API_BASE}/p2p-ui/v2/app",
        params={
            "p0": "account_statement_grouped_page", "p2": session_token, "lang": "en_US",
            "screen_width": 1280, "screen_height": 720,
        },
        headers={"Referer": "https://iuvo-group.com/en/account-statement/"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Iuvo account_statement_grouped_page returned HTTP {resp.status_code}")
    investor_account_id = _find_investor_account_id(resp.text)

    log.info(
        "Fetching Iuvo account-statement transactions (investor_account_id=%s, date range %s to %s)...",
        investor_account_id, date_from, date_to,
    )
    resp = session.get(
        f"{API_BASE}/p2p-ui/v2/app",
        params={
            "p0": "account_statement_grouped_page",
            "investor_account_id": investor_account_id,
            "trans_category": "all",
            "date_from": date_from,
            "date_to": date_to,
            "p2": session_token,
            "lang": "en_US",
            "screen_width": 1280,
            "screen_height": 720,
        },
        headers={"Referer": "https://iuvo-group.com/en/account-statement/"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Iuvo filtered account_statement_grouped_page returned HTTP {resp.status_code}")

    rows = re.findall(
        r'<a class="btn p2p-trans" value="([a-zA-Z_]*)"[^>]*>.*?</a>\s*</td>\s*'
        r'<td class="(?:positive|negative)-turnover">(-?[\d.,]+)</td>',
        resp.text, re.S,
    )
    log.info("Raw account-statement transaction rows for %s to %s: %r", date_from, date_to, rows)

    gross_interest_received = 0.0
    bonus_cashback_contest = 0.0
    for trans_type, amount_text in rows:
        amount = _parse_amount(amount_text)
        if amount is None:
            continue
        if trans_type in INTEREST_TRANS_TYPES:
            gross_interest_received += amount
        elif trans_type in BONUS_TRANS_TYPES:
            bonus_cashback_contest += amount

    gross_interest_received = round(gross_interest_received, 2)
    bonus_cashback_contest = round(bonus_cashback_contest, 2)
    log.info(
        "Parsed this month's totals: gross_interest_received=%.2f EUR, bonus_cashback_contest=%.2f EUR",
        gross_interest_received, bonus_cashback_contest,
    )
    return {
        "gross_interest_received": gross_interest_received,
        "net_interest_received": gross_interest_received,
        "withholding_tax": 0.0,
        "bonus_cashback_contest": bonus_cashback_contest,
    }


def run() -> None:
    if not IUVO_EMAIL or not IUVO_PASSWORD:
        log.error("IUVO_EMAIL and IUVO_PASSWORD environment variables are required.")
        sys.exit(1)

    log.info("Starting Iuvo diversification run (pure-HTTP, no browser).")

    session = requests.Session()

    try:
        session_token = login(session)
        balance_data = fetch_balance_and_originators(session, session_token)
    except Exception:
        log.exception("Failed to log in or fetch Iuvo's balance/loan-originator breakdown.")
        sys.exit(1)

    try:
        log.info("Fetching this month's interest received from the account statement...")
        interest_totals = fetch_current_month_interest(session, session_token)
    except Exception:
        log.exception("Failed to fetch this month's interest received - defaulting to 0.0.")
        interest_totals = {
            "gross_interest_received": 0.0, "net_interest_received": 0.0,
            "withholding_tax": 0.0, "bonus_cashback_contest": 0.0,
        }

    log.info(
        "Iuvo balance: total=%.2f EUR, receivables_in_p2p=%.2f EUR across %d loan originator(s):",
        balance_data["total"], balance_data["receivables_p2p"], len(balance_data["originators"]),
    )
    for o in balance_data["originators"]:
        log.info("  %s: %.2f EUR", o["name"], o["amount"])
    log.info("This month's interest received: %.2f EUR", interest_totals["gross_interest_received"])

    amounts = {
        "total": balance_data["total"],
        "gross_interest_received": interest_totals["gross_interest_received"],
        "net_interest_received": interest_totals["net_interest_received"],
        "withholding_tax": interest_totals["withholding_tax"],
        "bonus_cashback_contest": interest_totals["bonus_cashback_contest"],
        "receivables_p2p": balance_data["receivables_p2p"],
    }

    fill_current_month_amounts(platform="Iuvo", amounts=amounts)

    fill_geographic_repartition_amounts(balance_data["originators"], platform="Iuvo")


if __name__ == "__main__":
    run()
