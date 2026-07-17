"""Afranga portfolio diversification (by loan originator) fetcher.

Logs into afranga.com via pure HTTP (email/password + a single-field Google
Authenticator TOTP code, form-urlencoded POSTs against a Laravel CSRF `_token`
- unlike Swaper/Lendermarket/PeerBerry's JSON APIs) and fetches every active
investment from the "My investments" (https://afranga.com/profile/my-investments)
page, then groups them by loan originator and sums the outstanding
(remaining) investment amount per originator. No email is sent - the
amounts are just logged and handed to fill_current_month_amounts() (see
google_sheet.py) so they can be filled into a Google Sheet, mirroring
peerberry_diversification.py / lendermarket_diversification.py.

Auth flow verified 2026-07-18 via a real browser network capture:
  1. GET https://afranga.com/login -> parse the `_token` hidden input value
     out of the HTML form (regex only - no bs4 installed, same approach as
     bienpreter_diversification.py).
  2. POST https://afranga.com/login  form-urlencoded body
     `_token=<token>&email=<email>&password=<password>` -> 200, same URL
     (a normal Laravel form re-render, not a redirect) if 2FA is required
     next.
  3. If 2FA is enabled, parse a (possibly different) `_token` out of the
     2FA page's HTML, then POST https://afranga.com/2fa/verify
     form-urlencoded body `_token=<token>&one_time_password=<TOTP code>` ->
     200, redirects to https://afranga.com/profile/overview on success.
  4. From then on the session's cookies (a Laravel `XSRF-TOKEN` + session
     cookie pair, handled transparently by `requests.Session()`) are enough
     for every subsequent authenticated GET - no bearer token, no extra
     headers needed.

Unlike PeerBerry/Lendermarket, Afranga has no JSON API for this - "My
investments" is an old-school server-rendered (Laravel + jQuery) page: the
table is populated by
`GET https://afranga.com/profile/my-investments/refresh?<filters>&limit=N`
which returns an HTML fragment (a `<table id="myInvestmentsTable">`), not
JSON. Verified against the real account on 2026-07-09 (and re-verified via
pure HTTP on 2026-07-18):
- Requires a `_token` CSRF query param, read from `input[name='_token']` on
  the my-investments page itself (a hidden field of the filter form).
- `limit` must be one of the dropdown's allowed values (25/50/100/250) -
  250 is used here to fetch everything in one call; larger values return
  HTTP 400. No offset/page param was found, so accounts with more than 250
  active investments would be truncated (logged as a warning if the exact
  cap is hit - fine for now, revisit if/when the portfolio grows that big).
- Each `<tr>` has the loan originator as an `<img alt="X logo">` (never as
  plain text) in the 5th `<td>`, and the outstanding amount as
  "€ 1 234.56"-formatted text in the 11th `<td>` ("Outstanding Investment"
  column - the remaining/still-invested capital, as opposed to "Invested
  Amount" which is the original amount before any repayments). The last row
  is always a "Total:" summary row, not a real investment (its first `<td>`
  has no Loan ID link), and is skipped.
- Parsed via regex only (no HTML parser dependency), same approach as
  bienpreter_diversification.py: split the fragment into `<tr>...</tr>`
  blocks, then each block into `<td>...</td>` cells.

Also fetches this calendar month's "Gross interest received" and
"Withholding Tax" from the Account Statement page
(https://afranga.com/profile/account-statement) - see
fetch_current_month_statement_totals() below, same idea as
swaper_diversification.fetch_current_month_interest_received() /
loanch_diversification.fetch_current_month_statement_totals().

Required env vars:
    AFRANGA_EMAIL, AFRANGA_PASSWORD    -> Afranga account credentials
Optional:
    AFRANGA_TOTP_SECRET                -> base32 secret used to set up
                                           Google Authenticator, needed if
                                           2FA is enabled on the account
    GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS -> used to write this month's totals to
                                           the Google Sheet via
                                           fill_current_month_amounts() (see
                                           google_sheet.py)
"""

import os
import re
import sys
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pyotp
import requests
from dotenv import load_dotenv

load_dotenv()

from shared.google_sheet import fill_current_month_amounts, fill_current_month_bonus_breakdown, fill_geographic_repartition_amounts
from shared.report_date import get_report_now

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("afranga_diversification")

LOGIN_URL = "https://afranga.com/login"
TWO_FA_URL = "https://afranga.com/2fa/verify"
MY_INVESTMENTS_URL = "https://afranga.com/profile/my-investments"
REFRESH_URL = "https://afranga.com/profile/my-investments/refresh"
STATEMENT_PAGE_URL = "https://afranga.com/profile/account-statement"
STATEMENT_REFRESH_URL = "https://afranga.com/profile/account-statement/refresh"
BONUS_CASHBACK_HISTORY_URL = "https://afranga.com/profile/bonus-cashback/history"
MAX_LIMIT = 250  # largest value offered by the page's own rows-per-page dropdown
# Afranga's own "Current Month" quick filter on the Account Statement page
# (verified 2026-07-10 by capturing its request) uses the CURRENT calendar
# month up to TODAY (createdAt[from] = 1st of the month, createdAt[to] =
# today) - same "this month, not the full month" semantics as Swaper's
# equivalent filter. Pin the timezone explicitly (rather than relying on the
# executing machine's local clock, e.g. UTC on a CI runner) so "today"/"this
# month" are computed in the account's own local time.
REPORT_TIMEZONE = ZoneInfo("Europe/Paris")

AFRANGA_EMAIL = os.environ.get("AFRANGA_EMAIL")
AFRANGA_PASSWORD = os.environ.get("AFRANGA_PASSWORD")
AFRANGA_TOTP_SECRET = os.environ.get("AFRANGA_TOTP_SECRET")

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}

_TOKEN_RE = re.compile(r'name="_token"\s+value="([^"]+)"')


def _extract_csrf_token(html: str) -> str:
    m = _TOKEN_RE.search(html)
    if not m:
        raise RuntimeError("Could not find the CSRF _token in the page HTML.")
    return m.group(1)


def login(session: requests.Session) -> None:
    """Log in to Afranga using AFRANGA_EMAIL/PASSWORD (and
    AFRANGA_TOTP_SECRET if 2FA is enabled). See the module docstring for
    the full flow, verified 2026-07-18 via a real browser network capture.
    """
    log.info("Fetching the login page for a fresh CSRF token...")
    r = session.get(LOGIN_URL, headers=_HEADERS, timeout=20)
    r.raise_for_status()
    token = _extract_csrf_token(r.text)

    log.info("Submitting credentials...")
    r = session.post(
        LOGIN_URL,
        data={"_token": token, "email": AFRANGA_EMAIL, "password": AFRANGA_PASSWORD},
        headers={**_HEADERS, "Referer": LOGIN_URL, "Origin": "https://afranga.com"},
        timeout=20,
    )
    r.raise_for_status()

    if r.url.rstrip("/") != LOGIN_URL.rstrip("/"):
        log.info("Logged in successfully (no 2FA prompt), current URL: %s", r.url)
        return

    if "one_time_password" not in r.text:
        raise RuntimeError(f"Still on the login page after submitting credentials, and no 2FA prompt found: {r.url}")

    if not AFRANGA_TOTP_SECRET:
        raise RuntimeError(
            "Afranga is asking for a 2FA code but AFRANGA_TOTP_SECRET is not set. "
            "Set it to the base32 secret used to configure Google Authenticator."
        )

    log.info("2FA prompt detected, generating and submitting TOTP code...")
    token = _extract_csrf_token(r.text)
    totp = pyotp.TOTP(AFRANGA_TOTP_SECRET)
    # Guard against submitting a code right as its 30s window is about to
    # roll over - the network round-trip can push the server-side check
    # past the boundary and reject an otherwise-valid code (this exact
    # failure shape - "still on the login page" with no other error - was
    # seen in a real GitHub Actions run, matching a known rollover issue
    # already worked around in swaper_monitor.py/lendermarket_monitor.py).
    remaining = 30 - (int(time.time()) % 30)
    if remaining < 5:
        time.sleep(remaining + 1)
    code = totp.now()
    r = session.post(
        TWO_FA_URL,
        data={"_token": token, "one_time_password": code},
        headers={**_HEADERS, "Referer": LOGIN_URL, "Origin": "https://afranga.com"},
        timeout=20,
    )
    r.raise_for_status()

    if r.url.rstrip("/") == LOGIN_URL.rstrip("/"):
        log.info("TOTP code rejected (likely rolled over), retrying once with a fresh code...")
        code = totp.now()
        r = session.post(
            TWO_FA_URL,
            data={"_token": token, "one_time_password": code},
            headers={**_HEADERS, "Referer": LOGIN_URL, "Origin": "https://afranga.com"},
            timeout=20,
        )
        r.raise_for_status()
        if r.url.rstrip("/") == LOGIN_URL.rstrip("/"):
            raise RuntimeError("Afranga rejected the TOTP code (still on the login page).")

    log.info("Logged in successfully, current URL: %s", r.url)


def fetch_investments(session: requests.Session) -> list:
    """Fetch every active investment by calling the same HTML-fragment
    endpoint the "My investments" page itself uses (see module docstring
    for the verified shape/quirks). Parses the response via regex and
    returns a list of {"originator", "outstanding"} dicts (amounts as
    floats), skipping the trailing "Total:" summary row.
    """
    r = session.get(MY_INVESTMENTS_URL, headers=_HEADERS, timeout=20)
    r.raise_for_status()
    token = _extract_csrf_token(r.text)

    log.info("Requesting My investments refresh endpoint (limit=%d)...", MAX_LIMIT)
    r = session.get(
        REFRESH_URL,
        params={
            "_token": token,
            "interest_rate_percent[from]": "", "interest_rate_percent[to]": "",
            "period[from]": "", "period[to]": "",
            "created_at[from]": "", "created_at[to]": "",
            "invested_amount[from]": "", "invested_amount[to]": "",
            "loan[type]": "", "loan[early_repayment_possible]": "",
            "limit": str(MAX_LIMIT),
        },
        headers={**_HEADERS, "X-Requested-With": "XMLHttpRequest"},
        timeout=20,
    )
    log.info("My investments refresh endpoint response: status=%s", r.status_code)
    if not r.ok:
        raise RuntimeError(f"My investments refresh endpoint returned status {r.status_code}")

    tbody_m = re.search(r"<tbody[^>]*>(.*?)</tbody>", r.text, re.DOTALL)
    tbody_html = tbody_m.group(1) if tbody_m else ""
    raw_rows = re.findall(r"<tr[^>]*>(.*?)</tr>", tbody_html, re.DOTALL)
    log.info("Parsed %d raw row(s) from the My investments table (before filtering the Total row).", len(raw_rows))

    investments = []
    for row_html in raw_rows:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.DOTALL)
        if len(cells) < 11:
            continue
        loan_id_m = re.search(r">\s*(\d+)\s*<", cells[1])
        originator_m = re.search(r'alt="([^"]+)"', cells[4])
        if not loan_id_m or not originator_m:
            continue  # the trailing "Total:" row has no loan ID / originator
        outstanding_m = re.search(r"€\s*([\d\s.,]+)", cells[10])
        investments.append(
            {
                "originator": originator_m.group(1),
                "outstanding": _parse_amount(outstanding_m.group(1) if outstanding_m else None),
            }
        )

    log.info("%d row(s) remain after filtering out the trailing Total row.", len(investments))

    if len(investments) >= MAX_LIMIT:
        log.warning(
            "Fetched exactly the max page size (%d) - the portfolio may have more investments than "
            "this endpoint can return in one call (no pagination param is known to work here).",
            MAX_LIMIT,
        )

    return investments


def _parse_amount(text: str) -> float:
    """Parse a "€ 1 234.56"-formatted amount into a float."""
    if not text:
        return 0.0
    cleaned = text.replace("€", "").replace("\xa0", "").replace(" ", "").strip()
    cleaned = re.sub(r"[^\d.]", "", cleaned)
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def aggregate_by_originator(investments: list) -> list:
    """Group investments by loan originator and sum the outstanding
    (remaining) investment amount for each - one entry per originator,
    sorted by amount descending."""
    totals = {}
    for inv in investments:
        originator = re.sub(r"\s*logo$", "", inv.get("originator") or "Unknown", flags=re.IGNORECASE).strip()
        totals[originator] = totals.get(originator, 0.0) + inv.get("outstanding", 0.0)

    originators = [{"originator": name, "outstanding": amount} for name, amount in totals.items()]
    originators.sort(key=lambda o: o["outstanding"], reverse=True)
    return originators


def fetch_current_month_statement_totals(session: requests.Session) -> dict:
    """Fetch this calendar month's "Gross interest received" and
    "Withholding Tax" totals, as shown in the "Transaction Summary" panel
    of the Account Statement page (https://afranga.com/profile/account-statement),
    via the same HTML-fragment endpoint the page's own "Current Month" quick
    filter uses (same idea as
    swaper_diversification.fetch_current_month_interest_received() /
    loanch_diversification.fetch_current_month_statement_totals()).

    Verified against the real account on 2026-07-10 (and re-verified via
    pure HTTP on 2026-07-18):

    1. Clicking "Current Month" on the Account Statement page sends
       `GET https://afranga.com/profile/account-statement/refresh?_token=...&createdAt[from]=<1st of month, dd.mm.yyyy>&createdAt[to]=<today, dd.mm.yyyy>`
       - reproduced here the same way (needs the same `_token` CSRF param
       as fetch_investments(), read from the same `input[name='_token']`
       field, present on this page too).
    2. The response is an HTML fragment (like the my-investments refresh
       endpoint) whose "Transaction Summary" panel has one `<div
       class="row">` per summary line, each with a `.text-18-400-gray`
       label `<div>` and a sibling `.text-18-400` value `<div>` (e.g.
       "Deposited funds" / "€ 500.00"). The "Gross interest received" row's
       label actually reads "Gross interest received (net € X.XX)" (the net
       amount is baked into the label itself) - matched here by stripping
       the "(net ...)" suffix rather than an exact string. "Withholding
       Tax" matches exactly. Confirmed against June 2026 data: Gross
       interest received = 5.51 EUR, Withholding Tax = 0.57 EUR.
    3. Both rows are only rendered when non-zero (e.g. they were entirely
       absent when fetching the empty first-10-days-of-July range during
       exploration) - a missing row is treated as 0.0, not an error.
    """
    now = get_report_now(REPORT_TIMEZONE)
    start_date = now.replace(day=1).strftime("%d.%m.%Y")
    end_date = now.strftime("%d.%m.%Y")

    r = session.get(STATEMENT_PAGE_URL, headers=_HEADERS, timeout=20)
    r.raise_for_status()
    token = _extract_csrf_token(r.text)

    log.info("Requesting Account statement refresh endpoint for %s to %s...", start_date, end_date)
    r = session.get(
        STATEMENT_REFRESH_URL,
        params={"_token": token, "createdAt[from]": start_date, "createdAt[to]": end_date},
        headers={**_HEADERS, "X-Requested-With": "XMLHttpRequest"},
        timeout=20,
    )
    log.info("Account statement refresh endpoint response: status=%s", r.status_code)
    if not r.ok:
        raise RuntimeError(f"Account statement refresh endpoint returned status {r.status_code}")

    # Each summary line is a "row" block with a label div (text-18-400-gray)
    # followed by a value div containing the amount - capture the label
    # text and everything up to the next row block, then pull the first €
    # amount out of that trailing chunk.
    rows = re.findall(
        r'text-18-400-gray">(.*?)</div>(.*?)(?=<div class="row\b|\Z)',
        r.text,
        re.DOTALL,
    )
    parsed_rows = []
    for label_html, rest_html in rows:
        label = re.sub(r"<[^>]+>", " ", label_html)
        label = re.sub(r"\s+", " ", label).strip()
        label = re.split(r"\(net", label)[0].strip()
        amount_m = re.search(r"€\s*([\d\s.,]+)", rest_html)
        parsed_rows.append({"label": label, "value": amount_m.group(1) if amount_m else None})

    log.info("Found %d summary row(s) in the Transaction Summary panel: %r", len(parsed_rows), [r["label"] for r in parsed_rows])

    gross_interest_received = 0.0
    withholding_tax = 0.0
    for row in parsed_rows:
        label = row.get("label") or ""
        if label.startswith("Gross interest received"):
            gross_interest_received = _parse_amount(row.get("value"))
        elif label == "Withholding Tax":
            withholding_tax = _parse_amount(row.get("value"))

    log.info(
        "Parsed statement totals: gross_interest_received=%.2f, withholding_tax=%.2f",
        gross_interest_received, withholding_tax,
    )
    return {"gross_interest_received": gross_interest_received, "withholding_tax": withholding_tax}


def fetch_current_month_bonus_cashback(session: requests.Session) -> float:
    """Fetch this calendar month's paid Bonus Cashback total, by scraping
    the "Recent Completed Campaigns" table on
    https://afranga.com/profile/bonus-cashback/history and summing the
    "Bonus Paid" amount of every row whose "Payout Date" falls within the
    current calendar month (1st of the month through today).

    Discovered on 2026-07-17 via a deeper nav-link crawl (going beyond the
    Account Statement page previously checked) - completely missed before:
    a dedicated "Bonus Cashback Campaigns" page (linked from the profile
    overview page), with lifetime totals ("Total Earned"/"Total Paid"/
    "Pending") plus a per-campaign history table. No JSON API backs this
    page (only marketing/analytics beacons were seen in a network capture)
    - it's server-rendered HTML, so the table itself is scraped here, same
    general approach as the Transaction Summary panel above.

    Table column layout re-verified via pure HTTP on 2026-07-18 by dumping
    the raw `<td>` cells of a real row: Campaign(0), Period(1), Bonus
    Rate(2), Investments(3), Total Invested(4), Bonus Earned(5), Bonus
    Paid(6), Payout Date(7), Action(8) - note this is one column to the
    LEFT of what the `<thead><th>` text list alone would suggest (it has a
    leading blank/icon header with no corresponding data cell), so the
    indices are hardcoded here rather than derived from the header text.

    Unlike Swaper's referral bonus page (a single lifetime total with no
    per-event date), each completed campaign here DOES have its own real
    "Payout Date" (e.g. "2025-09-24"), so a genuine "this month" figure can
    be computed by filtering on it - no lifetime-vs-monthly ambiguity here.
    Verified against the real account on 2026-07-17 (and re-verified via
    pure HTTP on 2026-07-18): only one completed campaign exists so far
    (Lendivo, paid out 2025-09-24, 5.00 EUR), so this month's total is
    correctly 0.00 - a real computed result, not a hardcoded assumption.
    """
    BONUS_PAID_IDX = 6
    PAYOUT_DATE_IDX = 7

    now = get_report_now(REPORT_TIMEZONE)
    start_date = now.replace(day=1).date()
    end_date = now.date()
    log.info("Requesting Bonus Cashback history page to sum this month's (%s to %s) paid bonuses...", start_date, end_date)

    r = session.get(BONUS_CASHBACK_HISTORY_URL, headers=_HEADERS, timeout=20)
    r.raise_for_status()

    table_m = re.search(r"<table[^>]*>(.*?)</table>", r.text, re.DOTALL)
    table_html = table_m.group(1) if table_m else ""
    tbody_m = re.search(r"<tbody[^>]*>(.*?)</tbody>", table_html, re.DOTALL)
    tbody_html = tbody_m.group(1) if tbody_m else ""
    raw_rows = re.findall(r"<tr[^>]*>(.*?)</tr>", tbody_html, re.DOTALL)

    rows = []
    for row_html in raw_rows:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.DOTALL)
        if len(cells) <= max(BONUS_PAID_IDX, PAYOUT_DATE_IDX):
            continue
        bonus_paid_text = re.sub(r"<[^>]+>", " ", cells[BONUS_PAID_IDX])
        bonus_paid_text = re.sub(r"\s+", " ", bonus_paid_text).strip()
        payout_date_text = re.sub(r"<[^>]+>", " ", cells[PAYOUT_DATE_IDX])
        payout_date_text = re.sub(r"\s+", " ", payout_date_text).strip()
        rows.append({"bonusPaid": bonus_paid_text, "payoutDate": payout_date_text})

    log.info("Found %d completed campaign row(s) in the Bonus Cashback history table: %r", len(rows), rows)

    total = 0.0
    for row in rows:
        payout_date_match = re.search(r"\d{4}-\d{2}-\d{2}", row.get("payoutDate") or "")
        if not payout_date_match:
            continue
        payout_date = datetime.strptime(payout_date_match.group(), "%Y-%m-%d").date()
        if start_date <= payout_date <= end_date:
            total += _parse_amount(row.get("bonusPaid"))

    log.info("This month's paid Bonus Cashback total: %.2f EUR", total)
    return total


def run() -> None:
    if not AFRANGA_EMAIL or not AFRANGA_PASSWORD:
        log.error("AFRANGA_EMAIL and AFRANGA_PASSWORD environment variables are required.")
        sys.exit(1)

    log.info("Starting Afranga diversification run (pure HTTP, no browser).")

    session = requests.Session()
    try:
        login(session)
        investments = fetch_investments(session)
    except Exception:
        log.exception("Failed to log in or fetch Afranga investments.")
        sys.exit(1)

    try:
        statement_totals = fetch_current_month_statement_totals(session)
    except Exception:
        log.exception("Failed to fetch this month's Gross interest received/Withholding Tax - defaulting both to 0.0.")
        statement_totals = {"gross_interest_received": 0.0, "withholding_tax": 0.0}

    try:
        bonus_cashback = fetch_current_month_bonus_cashback(session)
    except Exception:
        log.exception("Failed to fetch this month's Bonus Cashback total - defaulting to 0.0.")
        bonus_cashback = 0.0

    originators = aggregate_by_originator(investments)
    log.info("Fetched %d active investment(s) across %d loan originator(s).", len(investments), len(originators))
    for o in originators:
        log.info("  %s: %.2f EUR", o["originator"], o["outstanding"])

    statement_totals["total"] = sum(o["outstanding"] for o in originators)
    statement_totals["net_interest_received"] = (
        statement_totals["gross_interest_received"] - statement_totals["withholding_tax"]
    )
    statement_totals["bonus_cashback_contest"] = bonus_cashback
    log.info(
        "This month's statement totals: total=%.2f EUR, gross_interest_received=%.2f EUR, "
        "net_interest_received=%.2f EUR, withholding_tax=%.2f EUR",
        statement_totals["total"], statement_totals["gross_interest_received"],
        statement_totals["net_interest_received"], statement_totals["withholding_tax"],
    )

    fill_current_month_amounts(
        platform="Afranga",
        amounts=statement_totals
    )

    # Afranga's bonus feature is literally called "Bonus Cashback
    # Campaigns" - a "cashback", not a prime/concours - written to its own
    # dedicated sub-row, never to the "Bonus" row itself (a SUM formula
    # over prime/cashback/concours).
    fill_current_month_bonus_breakdown(
        platform="Afranga",
        breakdown={"cashback": statement_totals["bonus_cashback_contest"]},
    )

    loan_originators = [
        {"name": o["originator"], "amount": o["outstanding"]}
        for o in originators
    ]

    fill_geographic_repartition_amounts(loan_originators)


if __name__ == "__main__":
    run()
