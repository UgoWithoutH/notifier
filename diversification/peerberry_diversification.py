"""PeerBerry portfolio "distribution by loan originators" fetcher.

Logs into peerberry.com via pure HTTP by reusing
monitors.peerberry_monitor.login() (not duplicated here - same dependency
direction as swaper/lendermarket: login() lives in the monitor module,
which has no google_sheet dependency, and this module imports from it, not
the other way around) and fetches the per-loan-originator investment
breakdown that's shown on the Overview page under Investments > "Loan
originators" (amount invested + % of the portfolio, one row per
originator). No email is sent - the amounts are just logged and handed to
fill_current_month_amounts() (see google_sheet.py) so they can be filled
into a Google Sheet.

`GET https://api.peerberry.com/v1/investor/overview/originators` (using the
`access_token` returned by monitors.peerberry_monitor.login() as an
`Authorization: Bearer` header, same as every other authenticated call).
Verified against the real account on 2026-07-09 (and re-verified via pure
HTTP on 2026-07-18): response is a JSON array of
`{"originator": "Lendplus ZA", "originatorId": 56, "company": "Aventus Group",
"companyId": 1, "iso2": "ZA", "amount": "1091.02", "part": "10.90"}`.

Also fetches this calendar month's "Interest income" from the Account
Summary API - see fetch_current_month_statement_totals() below, same idea as
swaper_diversification.fetch_current_month_interest_received(). That same
API also exposes a "closingBalance" field, used to fill in a real
historical "total" for a backfilled past month (month-range run) instead
of only ever having today's live originator-distribution total - see
fetch_current_month_statement_totals()'s docstring.

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
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()

from shared.google_sheet import fill_current_month_amounts, fill_geographic_repartition_amounts
from shared.report_date import get_report_now, is_current_month
from monitors.peerberry_monitor import login, PEERBERRY_EMAIL, PEERBERRY_PASSWORD, _HEADERS

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("peerberry_diversification")

ORIGINATORS_API_URL = "https://api.peerberry.com/v1/investor/overview/originators"
ACCOUNT_SUMMARY_API_URL = "https://api.peerberry.com/v2/investor/account-summary"
# The Account Summary page's default "This month" period (verified 2026-07-10
# by capturing its own request) = 1st of the current month through TODAY, not
# the full calendar month - same semantics as Swaper/Afranga/Lendermarket's
# equivalents. Pin the timezone explicitly rather than relying on the
# executing machine's local clock (e.g. UTC on a CI runner).
REPORT_TIMEZONE = ZoneInfo("Europe/Paris")


def fetch_originator_distribution(session: requests.Session) -> list:
    """Fetch the per-loan-originator investment breakdown via PeerBerry's own
    API (see module docstring)."""
    log.info("Requesting originators API...")
    r = session.get(ORIGINATORS_API_URL, headers=_HEADERS, timeout=20)
    log.info("Originators API response: status=%s", r.status_code)
    if not r.ok:
        raise RuntimeError(f"Originators API request failed (status={r.status_code})")

    body = r.json() or []
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


def fetch_current_month_statement_totals(session: requests.Session) -> dict:
    """Fetch this calendar month's "Interest income" total, as shown on the
    Account Summary page (https://peerberry.com/en/client/statement/account-summary).

    Verified against the real account on 2026-07-10 (and re-verified via
    pure HTTP on 2026-07-18): the page's default "This month" period
    (opening date = 1st of the current month, closing date = today)
    triggers `GET
    https://api.peerberry.com/v2/investor/account-summary?period=&startDate=<1st>&endDate=<today>`,
    returning `{"openingBalance": "6.20", "closingBalance": "0.98",
    "operations": {"DEPOSIT": "5000.00", "INVESTMENT": "-6578.71",
    "INTEREST": "9.88", "PRINCIPAL": "1563.61"}}` - `operations.INTEREST`
    matched the page's displayed "Interest income +€9.88" exactly (matches
    the user-supplied reference value).

    Also returns "closing_balance": that same response's top-level
    `closingBalance` field. `endDate` is REPORT_DATE-aware (the last day of
    the target month for a month-range backfill run - see
    scripts/run_diversification_for_month_range.sh), so `closingBalance` IS
    the account's real total invested+cash balance as of that exact date -
    same idea as Monefit/Go & Grow's own `closing_balance`, letting a
    backfilled past month get a real historical "total" instead of
    skip_total. `None` if the field is missing/unparseable.
    """
    now = get_report_now(REPORT_TIMEZONE)
    start_date = now.replace(day=1).strftime("%Y-%m-%d")
    end_date = now.strftime("%Y-%m-%d")
    log.info("Requesting account-summary API for %s to %s...", start_date, end_date)

    r = session.get(
        ACCOUNT_SUMMARY_API_URL,
        params={"period": "", "startDate": start_date, "endDate": end_date},
        headers=_HEADERS,
        timeout=20,
    )
    log.info("Account summary API response: status=%s", r.status_code)
    if not r.ok:
        raise RuntimeError(f"Account summary API request failed (status={r.status_code})")

    body = r.json() or {}
    operations = body.get("operations") or {}
    log.info("Raw account-summary API body: %r", body)
    try:
        interest_income = float(operations.get("INTEREST") or 0.0)
    except (TypeError, ValueError):
        log.warning("Could not parse 'INTEREST' value %r as a float - defaulting to 0.0.", operations.get("INTEREST"))
        interest_income = 0.0
    try:
        closing_balance = round(float(body["closingBalance"]), 2) if body.get("closingBalance") is not None else None
    except (TypeError, ValueError):
        log.warning("Could not parse 'closingBalance' value %r.", body.get("closingBalance"))
        closing_balance = None

    log.info("Parsed this month's Interest income: %.2f EUR, closing_balance=%s", interest_income, closing_balance)
    return {"interest_income": interest_income, "closing_balance": closing_balance}


def run() -> None:
    if not PEERBERRY_EMAIL or not PEERBERRY_PASSWORD:
        log.error("PEERBERRY_EMAIL and PEERBERRY_PASSWORD environment variables are required.")
        sys.exit(1)

    log.info("Starting PeerBerry diversification run (pure HTTP, no browser).")

    session = requests.Session()
    try:
        login(session)
        payload = fetch_originator_distribution(session)
    except Exception:
        log.exception("Failed to log in or fetch the loan originator distribution.")
        sys.exit(1)

    try:
        statement_totals = fetch_current_month_statement_totals(session)
    except Exception:
        log.exception("Failed to fetch this month's Interest income - defaulting to 0.0.")
        statement_totals = {"interest_income": 0.0, "closing_balance": None}
    interest_income = statement_totals["interest_income"]

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
    # Verified 2026-07-17 (dumping the raw account-summary API response
    # over the last 180 days): PeerBerry's `operations` block only ever has
    # DEPOSIT/INVESTMENT/INTEREST/PRINCIPAL keys, no bonus/cashback/contest
    # category - bonus_cashback_contest defaults to 0.0, not a placeholder
    # for a future fetch.
    current_month = is_current_month()
    live_total = sum(o["amount"] for o in originators)
    closing_balance = statement_totals["closing_balance"]
    # For the real current month, always use the live originator-distribution
    # total (matches the account's live invested balance). For a backfilled
    # past month (a month-range run), the account-summary API's own
    # `closingBalance` for that month's end date IS the real historical
    # total - use it instead of skipping the total entirely, falling back to
    # skip_total only if that field couldn't be fetched/parsed this run.
    total = live_total if current_month else (closing_balance if closing_balance is not None else live_total)
    skip_total = not current_month and closing_balance is None

    amounts = {
        "total": total,
        "gross_interest_received": interest_income,
        "net_interest_received": interest_income,
        "withholding_tax": 0.0,
        "bonus_cashback_contest": 0.0,
        "interest_income": interest_income,
    }

    fill_current_month_amounts(
        platform="PeerBerry",
        amounts=amounts,
        skip_total=skip_total,
    )

    loan_originators = [
        {"name": o["originator"], "amount": o["amount"]}
        for o in originators
    ]

    if current_month:
        fill_geographic_repartition_amounts(loan_originators, platform="Peerberry")


if __name__ == "__main__":
    run()
