"""PeerBerry portfolio "distribution by loan originators" fetcher.

Logs into peerberry.com by reusing monitors.peerberry_monitor.login() (not
duplicated here - same dependency direction as swaper/lendermarket:
login()/handle_two_factor()/dismiss_cookie_banner() live in the monitor
module, which has no google_sheet dependency, and this module imports from
it, not the other way around) and fetches the per-loan-originator investment
breakdown that's shown on the Overview page
under Investments > "Loan originators" (amount invested + % of the
portfolio, one row per originator). No email is sent - the amounts are just
logged and handed to fill_current_month_amounts() (see google_sheet.py) so
they can be filled into a Google Sheet.

The breakdown itself is NOT re-fetched via a dedicated API call when
switching that dropdown on the site - it's already loaded once and only
becomes visible via `GET https://api.peerberry.com/v1/investor/overview/originators`,
which fires the first time that view is selected. Verified against the real
account on 2026-07-09: response is a JSON array of
`{"originator": "Lendplus ZA", "originatorId": 56, "company": "Aventus Group",
"companyId": 1, "iso2": "ZA", "amount": "1091.02", "part": "10.90"}`. This is
called directly (via the browser's own `fetch()`, so it reuses the
authenticated session) instead of clicking through the dropdown, using an
`Authorization: Bearer <token>` header built from the `app_token` cookie set
at login - a plain `credentials: 'include'` fetch gets rejected by CORS (the
API's `Access-Control-Allow-Origin` is a wildcard, which browsers refuse to
pair with credentialed requests).

Also fetches this calendar month's "Interest income" from the Account
Summary page (https://peerberry.com/en/client/statement/account-summary) -
see fetch_current_month_interest_income() below, same idea as
swaper_diversification.fetch_current_month_interest_received().

Required env vars:
    PEERBERRY_EMAIL, PEERBERRY_PASSWORD    -> PeerBerry account credentials
Optional:
    PEERBERRY_TOTP_SECRET                  -> base32 secret used to set up
                                               Google Authenticator, needed
                                               if 2FA is enabled on the account
    GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS     -> used to write this month's
                                               totals to the Google Sheet via
                                               fill_current_month_amounts()
                                               (see google_sheet.py)
"""

import sys
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

from playwright.sync_api import sync_playwright

from shared.browser_stealth import get_context_options, apply_stealth
from shared.google_sheet import fill_current_month_amounts, fill_geographic_repartition_amounts
from monitors.peerberry_monitor import login, PEERBERRY_EMAIL, PEERBERRY_PASSWORD

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("peerberry_diversification")

ORIGINATORS_API_URL = "https://api.peerberry.com/v1/investor/overview/originators"
STATEMENT_URL = "https://peerberry.com/en/client/statement/account-summary"
ACCOUNT_SUMMARY_API_URL = "https://api.peerberry.com/v2/investor/account-summary"
STORAGE_STATE_FILE = Path(__file__).parent / "peerberry_diversification_storage_state.json"
# The Account Summary page's default "This month" period (verified 2026-07-10
# by capturing its own request) = 1st of the current month through TODAY, not
# the full calendar month - same semantics as Swaper/Afranga/Lendermarket's
# equivalents. Pin the timezone explicitly rather than relying on the
# executing machine's local clock (e.g. UTC on a CI runner).
REPORT_TIMEZONE = ZoneInfo("Europe/Paris")


def fetch_originator_distribution(page) -> list:
    """Fetch the per-loan-originator investment breakdown via PeerBerry's own
    API, using the `app_token` JWT cookie set at login as a bearer token
    (see module docstring for why a plain cookie-based fetch doesn't work).
    """
    log.info("Requesting originators API...")
    result = page.evaluate(
        """
        async (url) => {
            const match = document.cookie.match(/(?:^|; )app_token=([^;]+)/);
            const token = match ? decodeURIComponent(match[1]) : null;
            if (!token) {
                return { ok: false, status: 0, body: null, error: 'app_token cookie not found' };
            }
            const res = await fetch(url, { headers: { Authorization: 'Bearer ' + token } });
            return { ok: res.ok, status: res.status, body: await res.json().catch(() => null) };
        }
        """,
        ORIGINATORS_API_URL,
    )

    log.info("Originators API response: ok=%s status=%s", result.get("ok"), result.get("status"))
    if not result.get("ok"):
        raise RuntimeError(
            f"Originators API request failed (status={result.get('status')}, error={result.get('error')})"
        )

    body = result.get("body") or []
    log.info("Originators API returned %d raw entry(ies).", len(body))
    return body


def normalize_originators(payload: list) -> list:
    """Parse the raw API payload into {"originator", "company", "iso2",
    "amount", "part"} dicts with numeric amount/part, sorted by amount
    descending."""
    originators = []
    for entry in payload:
        try:
            amount = float(entry.get("amount"))
        except (TypeError, ValueError):
            amount = 0.0
        try:
            part = float(entry.get("part"))
        except (TypeError, ValueError):
            part = 0.0
        originators.append(
            {
                "originator": entry.get("originator") or "Unknown",
                "company": entry.get("company"),
                "iso2": entry.get("iso2"),
                "amount": amount,
                "part": part,
            }
        )
    originators.sort(key=lambda o: o["amount"], reverse=True)
    return originators


def fetch_current_month_interest_income(page) -> float:
    """Fetch this calendar month's "Interest income" total, as shown on the
    Account Summary page (https://peerberry.com/en/client/statement/account-summary).

    Verified against the real account on 2026-07-10: the page's default
    "This month" period (opening date = 1st of the current month, closing
    date = today) triggers `GET
    https://api.peerberry.com/v2/investor/account-summary?period=&startDate=<1st>&endDate=<today>`,
    returning `{"openingBalance": "6.20", "closingBalance": "0.98",
    "operations": {"DEPOSIT": "5000.00", "INVESTMENT": "-6578.71",
    "INTEREST": "9.88", "PRINCIPAL": "1563.61"}}` - `operations.INTEREST`
    matched the page's displayed "Interest income +€9.88" exactly (matches
    the user-supplied reference value). Like the originators endpoint (see
    module docstring), this is on api.peerberry.com so it needs the
    `app_token` cookie sent as an `Authorization: Bearer` header (a plain
    `credentials: 'include'` fetch fails CORS).
    """
    now = datetime.now(REPORT_TIMEZONE)
    start_date = now.replace(day=1).strftime("%Y-%m-%d")
    end_date = now.strftime("%Y-%m-%d")
    log.info("Requesting account-summary API for %s to %s...", start_date, end_date)

    result = page.evaluate(
        """
        async ([url, startDate, endDate]) => {
            const match = document.cookie.match(/(?:^|; )app_token=([^;]+)/);
            const token = match ? decodeURIComponent(match[1]) : null;
            if (!token) {
                return { ok: false, status: 0, body: null, error: 'app_token cookie not found' };
            }
            const qs = new URLSearchParams({ period: '', startDate, endDate }).toString();
            const res = await fetch(`${url}?${qs}`, { headers: { Authorization: 'Bearer ' + token } });
            return { ok: res.ok, status: res.status, body: await res.json().catch(() => null) };
        }
        """,
        [ACCOUNT_SUMMARY_API_URL, start_date, end_date],
    )

    log.info("Account summary API response: ok=%s status=%s", result.get("ok"), result.get("status"))
    if not result.get("ok"):
        raise RuntimeError(
            f"Account summary API request failed (status={result.get('status')}, error={result.get('error')})"
        )

    operations = (result.get("body") or {}).get("operations") or {}
    log.info("Raw 'operations' block from the account summary API: %r", operations)
    try:
        interest_income = float(operations.get("INTEREST") or 0.0)
    except (TypeError, ValueError):
        log.warning("Could not parse 'INTEREST' value %r as a float - defaulting to 0.0.", operations.get("INTEREST"))
        interest_income = 0.0

    log.info("Parsed this month's Interest income: %.2f EUR", interest_income)
    return interest_income


def run(headless: bool = True) -> None:
    if not PEERBERRY_EMAIL or not PEERBERRY_PASSWORD:
        log.error("PEERBERRY_EMAIL and PEERBERRY_PASSWORD environment variables are required.")
        sys.exit(1)

    log.info("Starting PeerBerry diversification run (headless=%s, storage_state_exists=%s).", headless, STORAGE_STATE_FILE.exists())

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        storage_state = str(STORAGE_STATE_FILE) if STORAGE_STATE_FILE.exists() else None
        context = browser.new_context(
            storage_state=storage_state,
            locale="en-US",
            **get_context_options(),
        )
        apply_stealth(context)
        page = context.new_page()

        try:
            login(page)
            payload = fetch_originator_distribution(page)
        except Exception:
            log.exception("Failed to log in or fetch the loan originator distribution.")
            browser.close()
            sys.exit(1)

        try:
            log.info("Navigating to the account summary page to fetch this month's Interest income...")
            page.goto(STATEMENT_URL, wait_until="domcontentloaded")
            interest_income = fetch_current_month_interest_income(page)
        except Exception:
            log.exception("Failed to fetch this month's Interest income - defaulting to 0.0.")
            interest_income = 0.0

        # Persist cookies/local storage so the next run can skip login (and
        # 2FA) while the session remains valid.
        context.storage_state(path=str(STORAGE_STATE_FILE))
        browser.close()

    originators = normalize_originators(payload)
    log.info("Fetched distribution for %d loan originator(s).", len(originators))
    for o in originators:
        log.info("  %s (%s, %s): %.2f EUR (%.2f%%)", o["originator"], o["company"], o["iso2"], o["amount"], o["part"])

    log.info("This month's Interest income: %.2f EUR", interest_income)

    # PeerBerry's account-summary API has no gross/net/withholding-tax
    # breakdown (unlike Afranga/Bienpreter) - interest_income is mapped to
    # both gross_interest_received/net_interest_received since it's the
    # only real figure on hand, withholding_tax defaults to 0.0. Same
    # standardized dict shape as every other *_diversification.py, plus the
    # platform-specific interest_income field kept alongside it.
    amounts = {
        "total": sum(o["amount"] for o in originators),
        "gross_interest_received": interest_income,
        "net_interest_received": interest_income,
        "withholding_tax": 0.0,
        "interest_income": interest_income,
    }
    fill_current_month_amounts(
        platform="PeerBerry",
        amounts=amounts
    )

    loan_originators = [
        {"name": o["originator"], "amount": o["amount"]}
        for o in originators
    ]

    fill_geographic_repartition_amounts(loan_originators)


if __name__ == "__main__":
    # Set headless=False locally (e.g. via `python peerberry_monitor.py --show`)
    # to watch the browser and debug the login flow if selectors need adjusting.
    run(headless="--show" not in sys.argv)
