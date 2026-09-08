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
  plain text) in the 6th `<td>`, and the outstanding amount as
  "€ 1 234.56"-formatted text in the 12th `<td>` ("Outstanding Investment"
  column - the remaining/still-invested capital, as opposed to "Invested
  Amount" which is the original amount before any repayments). The last row
  is always a "Total:" summary row, not a real investment (its first `<td>`
  has no Loan ID link), and is skipped. NOTE: a leading `<td>` (a "Sell on
  Secondary market" checkbox, added by Afranga at some point after this was
  first verified) shifts every column index by +1 versus earlier versions
  of this comment - re-verify live (e.g. via a throwaway probe script) if
  investments start silently disappearing again (0 parsed rows despite a
  non-zero "Invested Funds" on the overview page is the telltale symptom).
- Parsed via regex only (no HTML parser dependency), same approach as
  bienpreter_diversification.py: split the fragment into `<tr>...</tr>`
  blocks, then each block into `<td>...</td>` cells.

Also fetches this calendar month's "Gross interest received" and
"Withholding Tax" from the Account Statement page
(https://afranga.com/profile/account-statement) - see
fetch_current_month_statement_totals() below, same idea as
swaper_diversification.fetch_current_month_interest_received() /
loanch_diversification.fetch_current_month_statement_totals().

Also fetches the account's uninvested wallet balance ("non investi" row
added 2026-08-10 under Afranga's block in "Répartition géographique") -
found live 2026-08-10 on the /profile/overview page (the same page
login() lands on): a Livewire component
`id="walletUninvestedLiveWire"` renders "€ 4.60" right next to a
"Balance:" label (also mirrored, same value, in the mobile nav menu) -
see fetch_uninvested_balance() below. Cross-checked against the same
page's "Account Balance" figure (invested total + this uninvested
balance, confirmed to match exactly: 6015.24 + 4.60 = 6019.84).

Also computes a since-inception XIRR (money-weighted return) plus this
month's Cash drag and the XIRR Bonus / XIRR Cash drag / XIRR Taxes/Frais
pie-chart shares, mirroring swaper_diversification.py's own XIRR block -
see that module's docstring for the full methodology. Afranga's own
Account Statement page's "Details" panel (fetched here via the same
`/profile/account-statement/refresh` HTML-fragment endpoint as the
"Transaction Summary" panel, just paginated with a wide date range) gives
every individual dated transaction (with a "direction-in"/"direction-out"
CSS class per row - "in" = debits the uninvested wallet, "out" = credits
it, verified 2026-08-14 against a real account: "Investments in loans"/
"Withdrawn funds" are "in", everything else - "Deposited funds", "Interest
received", "Bonus received", "Cashback bonus", "Registration Bonus",
"Interest/Principal received from early repayment" - is "out"), which
plays the same role here as Swaper's account-entries API rows: the
"Deposited funds"/"Withdrawn funds" rows become the XIRR cashflow list,
and EVERY row (any label) feeds compute_average_idle_cash()'s day-by-day
uninvested-cash reconstruction. Unlike Swaper, Afranga DOES have real
withholding tax (see fetch_statement_totals()'s "withholding_tax" field),
so taxes_xirr_contribution is genuinely computed here (not hardcoded to
0.0) via the same counterfactual-XIRR technique as Cash drag/Bonus: add
back the lifetime withholding tax to today's final value, recompute XIRR,
`taxes_xirr_contribution = xirr_real - xirr_with_taxes_cancelled` - a
NEGATIVE value (same sign convention as Cash drag: a cost already baked
into xirr_real, not a hypothetical gain).

Added 2026-08-18: XIRR Intérêts, the counterfactual XIRR share
attributable to real net interest received since inception (mirrors
bienpreter_diversification.py's own XIRR Intérêts block exactly - same
counterfactual-XIRR pattern as Bonus/Cash drag/Taxes above, just with the
lifetime net interest total - lifetime gross interest received minus
lifetime withholding tax, both already available on
`lifetime_statement_totals` here, no extra fetch needed - subtracted from
today's total account value instead of the lifetime bonus total). This
exists because "Intérêts" was previously only ever a RESIDUAL on the
spreadsheet/dashboard side (XIRR - XIRR Bonus - XIRR Cash drag - XIRR
Taxes/Frais), which can legitimately go negative when the bonus's
counterfactual XIRR share is disproportionately large relative to the
account's real underlying (non-bonus) performance - that's not a bug,
it's the correct signal that the account's return is propped up almost
entirely by the bonus. XIRR Intérêts instead gives a genuine,
independently-measured figure (same category of computation as
Bonus/Taxes, not a derived leftover), so the two can be compared/sanity-
checked against each other on the sheet/dashboard side. As with
Bienprêter, a "XIRR Intérêts" row must already exist in the Afranga block
on the sheet itself (right after "XIRR Taxes/Frais") for this new value to
land anywhere - fill_current_month_bonus_breakdown() fills an existing row
by label, it doesn't insert new labelled rows. `max_rows` is bumped
18 -> 19 to keep the search bounded past this now-taller block.

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
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pyotp
import requests
from dotenv import load_dotenv

load_dotenv()

from shared.google_sheet import (
    fill_current_month_amounts,
    fill_current_month_bonus_breakdown,
    fill_geographic_repartition_amounts,
    fill_geographic_repartition_uninvested_amount,
)
from shared.report_date import get_report_now, is_current_month
from shared.state import load_state, save_state
from shared.weighted_average import INVESTED_BALANCE_LABEL, NON_INVESTED_BALANCE_LABEL, compute_time_weighted_average
from shared.xirr import compute_xirr

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("afranga_diversification")

LOGIN_URL = "https://afranga.com/login"
TWO_FA_URL = "https://afranga.com/2fa/verify"
PROFILE_OVERVIEW_URL = "https://afranga.com/profile/overview"
MY_INVESTMENTS_URL = "https://afranga.com/profile/my-investments"
REFRESH_URL = "https://afranga.com/profile/my-investments/refresh"
STATEMENT_PAGE_URL = "https://afranga.com/profile/account-statement"
STATEMENT_REFRESH_URL = "https://afranga.com/profile/account-statement/refresh"
BONUS_CASHBACK_HISTORY_URL = "https://afranga.com/profile/bonus-cashback/history"
MAX_LIMIT = 250  # largest value offered by the page's own rows-per-page dropdown
# Cache of every Account Statement "Details" row ever fetched (see
# get_cached_account_details() below) - same incremental-fetch idea as
# swaper_diversification.XIRR_CASHFLOWS_STATE_FILE, avoids re-fetching the
# account's entire history on every monthly run.
XIRR_CASHFLOWS_STATE_FILE = Path(__file__).parent / "afranga_xirr_cashflows_state.json"
XIRR_CASHFLOWS_STATE_DEFAULT = {"cashflows": [], "all_entries": [], "last_fetched_date": None}
# XIRR is a since-inception money-weighted return (not per-month) - this
# start date is early enough to cover any real account's full history.
XIRR_HISTORY_START_DATE = date(2000, 1, 1)
MAX_XIRR_PAGES = 20
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

    # Diagnostic only (no secret/code values logged): compare Afranga's
    # server-reported clock (Date response header) to our local clock. A
    # real GitHub Actions run rejected the code AND its immediate retry
    # (< 1s apart, i.e. same 30s window both times - see repo memory for
    # details), proving this wasn't a boundary-rollover flake but either a
    # wrong AFRANGA_TOTP_SECRET or clock skew between us and the server.
    server_date_header = r.headers.get("Date")
    if server_date_header:
        try:
            server_time = parsedate_to_datetime(server_date_header)
            skew = (datetime.now(timezone.utc) - server_time).total_seconds()
            log.info("Clock check: local vs. Afranga server Date header skew = %.1fs", skew)
        except Exception:
            pass

    # Try the current 30s window first, then the previous and next ones.
    # A same-window retry is a no-op (proven by the GH Actions log above),
    # so genuine resilience against clock skew requires trying adjacent
    # windows with distinct code values, not re-calling totp.now().
    now = time.time()
    candidates = [totp.at(now), totp.at(now - 30), totp.at(now + 30)]
    r = None
    for attempt, code in enumerate(candidates, start=1):
        r = session.post(
            TWO_FA_URL,
            data={"_token": token, "one_time_password": code},
            headers={**_HEADERS, "Referer": LOGIN_URL, "Origin": "https://afranga.com"},
            timeout=20,
        )
        r.raise_for_status()
        if r.url.rstrip("/") != LOGIN_URL.rstrip("/"):
            break
        log.info("TOTP code rejected (attempt %d/%d)...", attempt, len(candidates))
        # Laravel re-renders the 2FA form on every rejected submission with
        # a FRESH `_token` - reusing the stale one on the next attempt would
        # get rejected regardless of whether the TOTP code itself is right
        # (confirmed root cause of a real GitHub Actions failure where all 3
        # distinct candidate codes were rejected). Re-extract it before the
        # next attempt, if there is one.
        if attempt < len(candidates):
            try:
                token = _extract_csrf_token(r.text)
            except RuntimeError:
                log.warning("Could not refresh the CSRF _token after a rejected TOTP attempt; reusing the previous one.")
    else:
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
        if len(cells) < 12:
            continue
        # Indices are +1 vs. an older verification of this page - Afranga
        # added a leading checkbox `<td>` ("Sell on Secondary market") since.
        loan_id_m = re.search(r">\s*(\d+)\s*<", cells[2])
        originator_m = re.search(r'alt="([^"]+)"', cells[5])
        if not loan_id_m or not originator_m:
            continue  # the trailing "Total:" row has no loan ID / originator
        outstanding_m = re.search(r"€\s*([\d\s.,]+)", cells[11])
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


def fetch_uninvested_balance(session: requests.Session) -> float:
    """Fetch the wallet's uninvested balance ("non investi") from the
    /profile/overview page's Livewire component. See module docstring."""
    r = session.get(PROFILE_OVERVIEW_URL, headers=_HEADERS, timeout=20)
    r.raise_for_status()

    match = re.search(r'id="walletUninvestedLiveWire"[^>]*>\s*€\s*([\d\s.,]+?)\s*<', r.text)
    if not match:
        raise RuntimeError("Could not find the uninvested wallet balance ('walletUninvestedLiveWire') on the profile overview page.")
    return _parse_amount(match.group(1))


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


def fetch_statement_totals(session: requests.Session, start_date: date, end_date: date) -> dict:
    """Fetch the "Transaction Summary" panel of the Account Statement page
    (https://afranga.com/profile/account-statement) for an arbitrary
    [start_date, end_date] range, via the same HTML-fragment endpoint the
    page's own "Current Month" quick filter uses (same idea as
    swaper_diversification.fetch_statement_totals()). Generalized
    2026-08-14 (was fetch_current_month_statement_totals(), hardcoded to
    the current calendar month - kept below as a thin wrapper) so run()
    can ALSO fetch since-inception ("lifetime") totals, needed to compute
    the XIRR Bonus / XIRR Taxes/Frais / XIRR Intérêts counterfactual
    shares over the SAME since-inception period as XIRR itself.

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
    3. Rows are only rendered when non-zero (e.g. they were entirely
       absent when fetching the empty first-10-days-of-July range during
       exploration) - a missing row is treated as 0.0, not an error.
    4. Also parses "Opening balance <date>"/"Closing balance <date>" (the
       first/last summary rows, no "(net ...)" suffix to strip) - the real
       uninvested-cash balance at the start/end of the queried range,
       needed by run() for compute_average_idle_cash()'s "Cash drag"
       reconstruction (same role as Swaper's openingBalance/closingBalance
       API fields). And "Bonus received"/"Cashback bonus"/"Registration
       Bonus" (summed into "bonus_total") - queried over the account's
       full history, this gives the LIFETIME bonus total needed to isolate
       bonus_xirr_contribution the same way as
       swaper_diversification.py's referral_bonus_earned, without having
       to re-sum the (paginated) Details rows for it.
    """
    start_str = start_date.strftime("%d.%m.%Y")
    end_str = end_date.strftime("%d.%m.%Y")

    r = session.get(STATEMENT_PAGE_URL, headers=_HEADERS, timeout=20)
    r.raise_for_status()
    token = _extract_csrf_token(r.text)

    log.info("Requesting Account statement refresh endpoint for %s to %s...", start_str, end_str)
    r = session.get(
        STATEMENT_REFRESH_URL,
        params={"_token": token, "createdAt[from]": start_str, "createdAt[to]": end_str},
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
    opening_balance = 0.0
    closing_balance = 0.0
    bonus_total = 0.0
    for row in parsed_rows:
        label = row.get("label") or ""
        if label.startswith("Gross interest received"):
            gross_interest_received = _parse_amount(row.get("value"))
        elif label == "Withholding Tax":
            withholding_tax = _parse_amount(row.get("value"))
        elif label.startswith("Opening balance"):
            opening_balance = _parse_amount(row.get("value"))
        elif label.startswith("Closing balance"):
            closing_balance = _parse_amount(row.get("value"))
        elif label in ("Bonus received", "Cashback bonus", "Registration Bonus"):
            bonus_total += _parse_amount(row.get("value"))

    log.info(
        "Parsed statement totals: gross_interest_received=%.2f, withholding_tax=%.2f, "
        "opening_balance=%.2f, closing_balance=%.2f, bonus_total=%.2f",
        gross_interest_received, withholding_tax, opening_balance, closing_balance, bonus_total,
    )
    return {
        "gross_interest_received": gross_interest_received,
        "withholding_tax": withholding_tax,
        "opening_balance": opening_balance,
        "closing_balance": closing_balance,
        "bonus_total": bonus_total,
    }


def fetch_current_month_statement_totals(session: requests.Session) -> dict:
    """Thin wrapper around fetch_statement_totals() for the current
    calendar month (1st of the month through today) - see that function's
    docstring for the endpoint/parsing details."""
    now = get_report_now(REPORT_TIMEZONE)
    return fetch_statement_totals(session, now.replace(day=1).date(), now.date())


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


# Each Details row's Net Amount <div> carries a "direction-in"/"direction-out"
# class alongside its own transaction ID (e.g. "direction-out | 692745") -
# verified 2026-08-14 against a real account: "in" = debits the uninvested
# wallet (Investments in loans, Withdrawn funds), "out" = credits it
# (everything else - Deposited funds, Interest received (regular and early
# repayment), Principal received from early repayment, Bonus received,
# Cashback bonus, Registration Bonus). Generic (keyed off the CSS class, not
# a hardcoded label list), so any future transaction type is still handled
# correctly without code changes.
_DETAILS_DIRECTION_RE = re.compile(r"direction-(in|out)\s*\|\s*(\d+)")


def _parse_statement_details_rows(html: str) -> list:
    """Parse every row of the Account Statement page's "Details" table
    (the per-transaction ledger, as opposed to the "Transaction Summary"
    aggregate panel parsed by fetch_statement_totals()) out of one HTML
    fragment page. Returns a list of {"transaction_id", "date"
    ("YYYY-MM-DD"), "label", "net_amount", "direction"} dicts - "label" is
    the last " - "-separated segment of the "Details" cell (e.g. "Deposited
    funds", "Interest received", "Bonus received"), which is where Afranga
    puts the actual transaction type regardless of whether a loan/
    originator/campaign prefix is also present on that same cell.
    """
    tbody_m = re.search(r"account-statement-table.*?<tbody>(.*?)</tbody>", html, re.DOTALL)
    if not tbody_m:
        return []

    rows = []
    for row_html in re.findall(r"<tr>(.*?)</tr>", tbody_m.group(1), re.DOTALL):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.DOTALL)
        if len(cells) < 6:
            continue

        date_text = re.sub(r"<[^>]+>", " ", cells[0])
        date_text = re.sub(r"\s+", " ", date_text).strip()
        try:
            row_date = datetime.strptime(date_text, "%d.%m.%Y").date()
        except ValueError:
            continue

        details_text = re.sub(r"<[^>]+>", " ", cells[1].split("<br")[0])
        details_text = re.sub(r"\s+", " ", details_text).strip()
        parts = [p.strip() for p in details_text.split(" - ") if p.strip()]
        label = parts[-1] if parts else "Unknown"

        net_cell = cells[5]
        direction_m = _DETAILS_DIRECTION_RE.search(net_cell)
        amount_m = re.search(r"€\s*([\d\s.,]+)", net_cell)

        rows.append({
            "transaction_id": direction_m.group(2) if direction_m else None,
            "date": row_date.strftime("%Y-%m-%d"),
            "label": label,
            "net_amount": _parse_amount(amount_m.group(1) if amount_m else None),
            "direction": direction_m.group(1) if direction_m else None,
        })
    return rows


def fetch_account_statement_details(session: requests.Session, start_date: date, end_date: date) -> list:
    """Fetch EVERY row of the Account Statement page's "Details" table
    within [start_date, end_date], paginated via MAX_LIMIT (250, the
    largest page size the page itself offers)/MAX_XIRR_PAGES - same role
    as swaper_diversification._fetch_account_entries_pages(). Total record
    count is read off the page's own "Showing X to Y of Z results" text.
    """
    start_str = start_date.strftime("%d.%m.%Y")
    end_str = end_date.strftime("%d.%m.%Y")

    r = session.get(STATEMENT_PAGE_URL, headers=_HEADERS, timeout=20)
    r.raise_for_status()
    token = _extract_csrf_token(r.text)

    all_rows = []
    page_number = 1
    total_records = None
    while page_number <= MAX_XIRR_PAGES:
        log.info("Requesting Account statement Details page %d for %s to %s...", page_number, start_str, end_str)
        r = session.get(
            STATEMENT_REFRESH_URL,
            params={
                "_token": token, "createdAt[from]": start_str, "createdAt[to]": end_str,
                "limit": str(MAX_LIMIT), "page": str(page_number),
            },
            headers={**_HEADERS, "X-Requested-With": "XMLHttpRequest"},
            timeout=20,
        )
        if not r.ok:
            raise RuntimeError(f"Account statement refresh endpoint returned status {r.status_code} (Details page {page_number})")

        page_rows = _parse_statement_details_rows(r.text)
        all_rows.extend(page_rows)

        total_m = re.search(r"of\s*<span[^>]*>\s*(\d+)\s*</span>\s*results", r.text)
        total_records = int(total_m.group(1)) if total_m else None
        log.info("Details page %d: %d row(s) found (total_records=%s).", page_number, len(page_rows), total_records)

        if total_records is None or len(page_rows) == 0:
            break
        if page_number * MAX_LIMIT >= total_records:
            break
        page_number += 1
    else:
        log.warning("Hit MAX_XIRR_PAGES (%d) without exhausting total_records=%s - Details history may be incomplete.", MAX_XIRR_PAGES, total_records)

    return all_rows


def _cash_delta_for_detail_row(row: dict) -> float:
    """Signed change to the uninvested-cash balance a single Details row
    represents, based on its "direction-in"/"direction-out" CSS class (see
    _DETAILS_DIRECTION_RE's comment above) - a row with neither class is
    treated as cash-neutral (logged) rather than guessed at."""
    if row["direction"] == "in":
        return -abs(row["net_amount"] or 0.0)
    if row["direction"] == "out":
        return abs(row["net_amount"] or 0.0)
    log.warning("Details row with no direction-in/out class (label=%r) - treating as cash-neutral (0 impact).", row.get("label"))
    return 0.0


def compute_average_idle_cash(rows: list, opening_balance: float, closing_balance: float, start_date: str, end_date: str) -> float:
    """Reconstruct the uninvested-cash balance for EVERY day in
    [start_date, end_date] ("YYYY-MM-DD" strings) from the raw Details rows
    (every transaction label, not just Deposited/Withdrawn funds) and
    return the day-weighted average - same technique and rationale as
    swaper_diversification.compute_average_idle_cash() (see its docstring).
    Falls back to the simple 2-point average if `rows` is empty or the
    dates can't be parsed - never raises.
    """
    if not rows:
        return (opening_balance + closing_balance) / 2

    try:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        end = datetime.strptime(end_date, "%Y-%m-%d").date()
    except ValueError:
        return (opening_balance + closing_balance) / 2

    daily_deltas: dict = {}
    for row in rows:
        raw_date = row.get("date")
        if not raw_date:
            continue
        daily_deltas[raw_date] = daily_deltas.get(raw_date, 0.0) + _cash_delta_for_detail_row(row)

    running_balance = opening_balance
    total_balance = 0.0
    day_count = 0
    current = start
    while current <= end:
        running_balance += daily_deltas.get(current.strftime("%Y-%m-%d"), 0.0)
        total_balance += running_balance
        day_count += 1
        current += timedelta(days=1)

    if day_count == 0:
        return (opening_balance + closing_balance) / 2

    if abs(running_balance - closing_balance) > 0.05:
        log.warning(
            "Reconstructed closing balance (%.2f EUR) from all Details rows doesn't match the Summary panel's own "
            "closing balance (%.2f EUR) - the average idle cash below may be slightly off.",
            running_balance, closing_balance,
        )

    return total_balance / day_count


def get_cached_account_details(session: requests.Session, end_date: date) -> tuple:
    """Return `(cashflows, all_rows)` since account inception, fetching
    from the Account Statement "Details" table only the rows NOT already
    cached locally (in XIRR_CASHFLOWS_STATE_FILE) - same incremental-fetch
    idea as swaper_diversification.get_cached_account_cashflows() (see its
    docstring for the full rationale). `cashflows` = "Deposited funds"/
    "Withdrawn funds"-only (for XIRR). `all_rows` = every Details row
    regardless of label, needed by compute_average_idle_cash().

    Re-fetches starting from the cached `last_fetched_date` itself (not the
    day after) so a row booked on that same day, added on Afranga's side
    after the previous run already fetched it, isn't missed - duplicates
    are dropped by de-duplicating on (transaction_id, label, date,
    net_amount): transaction_id ALONE is not a unique row key here (e.g.
    the separate Principal/Interest/Bonus rows of one early repayment all
    share the same transaction_id), so label must be part of the key too.
    """
    state = load_state(XIRR_CASHFLOWS_STATE_FILE, XIRR_CASHFLOWS_STATE_DEFAULT)
    cached_cashflows = state["cashflows"]
    cached_all_rows = state.get("all_entries", [])
    start_date = (
        datetime.strptime(state["last_fetched_date"], "%Y-%m-%d").date()
        if state["last_fetched_date"] else XIRR_HISTORY_START_DATE
    )

    if start_date > end_date:
        # Cache already covers past end_date (e.g. a live run advanced it,
        # then a backfill run asked for an earlier REPORT_DATE) - skip the
        # fetch instead of sending an inverted start>end range to the API.
        log.info(
            "Cache already covers up to %s (requested end date %s) - skipping fetch, using cached data only.",
            start_date, end_date,
        )
        return cached_cashflows, cached_all_rows

    log.info(
        "Found %d cached XIRR cashflow(s) (last fetched up to %s) - fetching only new Details rows from %s to %s...",
        len(cached_cashflows), state["last_fetched_date"], start_date, end_date,
    )
    new_rows = fetch_account_statement_details(session, start_date, end_date)
    new_cashflows = [row for row in new_rows if row["label"] in ("Deposited funds", "Withdrawn funds")]

    # A single transaction_id can legitimately cover several DISTINCT rows
    # (e.g. an early repayment splits into separate "Principal received from
    # early repayment"/"Interest received from early repayment"/"Bonus
    # received" rows, all sharing one transaction_id) - the label must be
    # part of the dedup key too, or genuinely different rows collapse into
    # one (confirmed live 2026-08-14: reconstructed lifetime closing balance
    # was off by ~350 EUR before this fix).
    seen = set()
    merged_cashflows = []
    for entry in cached_cashflows + new_cashflows:
        key = (entry.get("transaction_id"), entry["label"], entry["date"], entry["net_amount"])
        if key in seen:
            continue
        seen.add(key)
        merged_cashflows.append(entry)

    seen_rows = set()
    merged_all_rows = []
    for row in cached_all_rows + new_rows:
        key = (row.get("transaction_id"), row["label"], row["date"], row["net_amount"])
        if key in seen_rows:
            continue
        seen_rows.add(key)
        merged_all_rows.append(row)

    save_state(XIRR_CASHFLOWS_STATE_FILE, {
        "cashflows": merged_cashflows, "all_entries": merged_all_rows,
        "last_fetched_date": end_date.strftime("%Y-%m-%d"),
    })
    log.info(
        "XIRR cashflow cache now holds %d cashflow(s)/%d total row(s) (was %d/%d before this run).",
        len(merged_cashflows), len(merged_all_rows), len(cached_cashflows), len(cached_all_rows),
    )
    return merged_cashflows, merged_all_rows


# Labels whose net_amount moves money FROM the uninvested wallet INTO an
# active loan investment (increases the invested principal, "outstanding").
# Confirmed 2026-08-14 (see the direction-in/out comment above): this is the
# ONLY "in"-direction label that represents a real new investment (the
# other "in" label, "Withdrawn funds", is a cash withdrawal to the bank,
# not an investment).
_OUTSTANDING_INCREASE_LABELS = {"Investments in loans"}
# Labels CONFIRMED to only ever touch the uninvested wallet balance, never
# the invested principal - interest/tax/fees/deposits-withdrawals-to-bank/
# bonuses. Used below to distinguish "known safe to ignore" from "unknown,
# could secretly be a repayment type". DELIBERATELY does NOT include
# "Cancellation fee" - the Transaction Summary panel shows "Interest
# refund/Principal refund from investment cancellation" as one combined
# label, but it's UNVERIFIED whether the Details table always renders a
# SEPARATE "Principal refund..." row alongside "Cancellation fee" for a
# real cancellation, or whether "Cancellation fee" is sometimes the ONLY
# row representing that event - if the latter, silently treating it as
# neutral would leave a cancelled investment's capital stuck in the
# reconstructed outstanding forever, with no warning since it'd be in this
# "known" set. Leaving it OUT so it falls through to the explicit
# unrecognized-label warning below until a real cancellation's Details rows
# are inspected live and this can be classified with confidence.
_OUTSTANDING_NEUTRAL_LABELS = {
    "Deposited funds", "Withdrawn funds", "Withholding Tax",
    "Interest received", "Bonus received", "Cashback bonus",
    "Registration Bonus",
}


def _outstanding_delta_for_label(label: str, net_amount: float) -> float:
    """Signed change to the INVESTED principal ("outstanding", the same
    figure fetch_investments()'s live my-investments table sums for the
    account's CURRENT active investments) that one Details row represents.

    - "Investments in loans" -> +amount (money moves wallet -> loans).
    - Any OTHER label containing "principal" (case-insensitive, e.g.
      "Principal received from early repayment", "Principal refund from
      investment cancellation", or a plain/regular-repayment "Principal
      received" if that exact label is ever seen) -> -amount (money moves
      loans -> wallet) - matched by substring rather than an exhaustive
      exact-label list, since Afranga's own naming convention consistently
      puts the word "Principal" in every principal-moving label seen so
      far, and this is meant to catch a repayment TYPE that hasn't been
      individually observed/hardcoded yet (per the explicit "cover ALL
      repayment types, not just early repayment" requirement).
    - Any label containing "savesmart" (case-insensitive, e.g. "SaveSmart
      early withdrawal(+fee)") -> -amount too - an early exit from an
      invested SaveSmart product also returns capital to the wallet.
    - A label already confirmed to be wallet-only (interest, tax, fees,
      deposits/withdrawals to the bank, bonuses) -> 0 (no effect).
    - Anything else is UNKNOWN - rather than silently treating it as 0 (which
      could silently corrupt a past-date outstanding reconstruction if it
      actually represents an investment/repayment), this logs an explicit
      warning so a genuinely new label gets noticed and classified above.
    """
    if label in _OUTSTANDING_INCREASE_LABELS:
        return abs(net_amount or 0.0)
    lowered = label.lower()
    if "principal" in lowered or "savesmart" in lowered:
        return -abs(net_amount or 0.0)
    if label in _OUTSTANDING_NEUTRAL_LABELS:
        return 0.0
    log.warning(
        "Unrecognized transaction label %r while reconstructing the invested-principal (outstanding) balance - "
        "treating it as having NO effect on outstanding. If this label actually represents an investment or a "
        "repayment/refund of principal, any outstanding reconstruction (and XIRR computed from it for a past "
        "date) covering a period containing this row will be WRONG - verify and classify it explicitly above.",
        label,
    )
    return 0.0


def reconstruct_outstanding(all_detail_rows: list, end_date: date) -> float:
    """Reconstruct the INVESTED principal ("outstanding") as of an
    arbitrary past `end_date`, by replaying every Details row dated on or
    before that date (in `all_detail_rows`, expected to cover the account's
    FULL history - guaranteed by get_cached_account_details()'s
    incremental cache starting from XIRR_HISTORY_START_DATE) and applying
    _outstanding_delta_for_label() to each. Starts from 0 (before the
    account's first-ever transaction, outstanding is necessarily 0) and
    accumulates forward chronologically (row order in `all_detail_rows`
    doesn't matter - every row's own date is checked individually).
    """
    outstanding = 0.0
    for row in all_detail_rows:
        raw_date = row.get("date")
        if not raw_date:
            continue
        try:
            row_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except ValueError:
            continue
        if row_date > end_date:
            continue
        outstanding += _outstanding_delta_for_label(row.get("label") or "", row.get("net_amount"))
    return outstanding


def compute_average_balances(all_detail_rows: list, start_date: date, end_date: date) -> tuple:
    """Day-weighted average INVESTED ("outstanding") and NON-INVESTED
    (wallet cash) balances over [start_date, end_date] - for the "solde
    moyen pondéré investi"/"solde moyen pondéré non investi" Sheet rows
    (added 2026-09-08). Reuses the SAME per-row classifiers as
    reconstruct_outstanding()/compute_average_idle_cash() above
    (_outstanding_delta_for_label/_cash_delta_for_detail_row), just fed
    into the generic shared day-weighted-average helper instead of a
    point-in-time replay - works identically for the real current month or
    a REPORT_DATE-backfilled past month (start_date/end_date are whatever
    the caller passes). `all_detail_rows` is expected to cover the
    account's FULL history (see reconstruct_outstanding()'s docstring), so
    the running balance carried into `start_date` is accurate with no
    separate opening-balance anchor needed."""
    invested_events = []
    non_invested_events = []
    for row in all_detail_rows:
        raw_date = row.get("date")
        if not raw_date:
            continue
        try:
            row_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except ValueError:
            continue
        label = row.get("label") or ""
        invested_events.append((row_date, _outstanding_delta_for_label(label, row.get("net_amount"))))
        non_invested_events.append((row_date, _cash_delta_for_detail_row(row)))

    avg_invested = compute_time_weighted_average(invested_events, start_date, end_date)
    avg_non_invested = compute_time_weighted_average(non_invested_events, start_date, end_date)
    return avg_invested, avg_non_invested


def _build_since_inception_cashflows_as_of(xirr_cashflow_rows: list, end_date: date) -> list:
    """Real Deposited/Withdrawn funds cashflows, signed and dated, filtered
    to date<=end_date - the shared "real external cashflows so far" list
    used by every XIRR-as-of computation below (no terminal value
    appended yet).
    """
    signed_cashflows = []
    for row in xirr_cashflow_rows:
        try:
            row_date = datetime.strptime(row["date"], "%Y-%m-%d").date()
        except ValueError:
            log.warning("Skipping an XIRR cashflow row with an unparseable date: %r", row)
            continue
        if row_date > end_date:
            continue
        is_deposit = row["label"] == "Deposited funds"
        signed_amount = -row["net_amount"] if is_deposit else row["net_amount"]
        signed_cashflows.append((row_date, signed_amount))
    return signed_cashflows


def _warn_if_wallet_balance_mismatch(all_detail_rows: list, end_date: date, closing_balance_as_of: float) -> None:
    """Cross-check: independently reconstruct the wallet's own cash balance
    (via the SAME per-row direction-in/out deltas compute_average_idle_cash()
    uses) and warn if it diverges >0.05 EUR from Afranga's own reported
    closing_balance_as_of - either a classification gap here or a display
    issue on Afranga's side, so any XIRR-as-of figure built from that
    terminal value shouldn't be trusted blindly.
    """
    reconstructed_wallet_balance = 0.0
    for row in all_detail_rows:
        raw_date = row.get("date")
        if not raw_date:
            continue
        try:
            row_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except ValueError:
            continue
        if row_date > end_date:
            continue
        reconstructed_wallet_balance += _cash_delta_for_detail_row(row)
    if abs(reconstructed_wallet_balance - closing_balance_as_of) > 0.05:
        log.warning(
            "Reconstructed wallet balance from all Details rows (%.2f EUR) as of %s doesn't match Afranga's own "
            "reported closing balance (%.2f EUR) for the same date - the terminal value used for this XIRR-as-of "
            "computation may be wrong (missing/misclassified transaction label, or a Summary-panel display issue "
            "on Afranga's side) - don't trust this figure without investigating further.",
            reconstructed_wallet_balance, end_date, closing_balance_as_of,
        )


def compute_xirr_as_of(session: requests.Session, all_detail_rows: list, xirr_cashflow_rows: list, end_date: date):
    """Compute the since-inception XIRR as of an arbitrary past `end_date`
    instead of always "today" - see compute_xirr_block_as_of() below for
    the full pie-chart-breakdown equivalent; this is just the plain XIRR
    figure, kept as its own function for any caller that only needs that.
    Terminal value = reconstruct_outstanding(end_date) + closing balance at
    end_date (via the existing date-range-aware fetch_statement_totals()).
    Returns None if compute_xirr() can't find a solution.
    """
    outstanding_as_of = reconstruct_outstanding(all_detail_rows, end_date)
    closing_balance_as_of = fetch_statement_totals(session, XIRR_HISTORY_START_DATE, end_date)["closing_balance"]
    _warn_if_wallet_balance_mismatch(all_detail_rows, end_date, closing_balance_as_of)

    total_value_as_of = outstanding_as_of + closing_balance_as_of
    signed_cashflows = _build_since_inception_cashflows_as_of(xirr_cashflow_rows, end_date)
    signed_cashflows.append((end_date, total_value_as_of))

    log.info(
        "XIRR as of %s: reconstructed outstanding=%.2f EUR, closing balance=%.2f EUR, total value=%.2f EUR, %d cashflow(s).",
        end_date, outstanding_as_of, closing_balance_as_of, total_value_as_of, len(signed_cashflows),
    )
    return compute_xirr(signed_cashflows)


def compute_xirr_block_as_of(session: requests.Session, all_detail_rows: list, xirr_cashflow_rows: list, end_date: date) -> dict:
    """Compute the FULL XIRR pie-chart block (XIRR, Cash drag, XIRR Bonus,
    XIRR Cash drag, XIRR Taxes/Frais, XIRR Intérêts) for a BACKFILLED
    (non-current) month, as of that month's own `end_date` - the historical
    equivalent of run()'s live-today block below. Two different time
    scales are used, mirroring the current-month logic exactly:

    - "Cash drag" is a MONTHLY figure (this backfilled month alone, 1st of
      the month through end_date) - its own opening/closing balance and
      gross interest come from fetch_statement_totals() over that monthly
      range, not a lifetime one.
    - Everything else is SINCE-INCEPTION through end_date (same scale as
      XIRR itself), via the same counterfactual-XIRR technique as the
      current-month block: neutralize one component (lifetime bonus/cash
      drag/withholding tax/net interest, all recomputed over inception ->
      end_date, NOT inception -> today) in the terminal value, recompute
      XIRR, take the difference. The terminal reference value is the same
      reconstructed-outstanding + closing-balance total compute_xirr_as_of()
      uses, not today's live total.

    Returns a dict with any subset of {"XIRR", "Cash drag", "XIRR Bonus",
    "XIRR Cash drag", "XIRR Taxes/Frais", "XIRR Intérêts"} that could
    actually be computed - a missing key means "couldn't be computed", same
    soft-fail convention as everywhere else in this module (an existing
    Sheet cell is left untouched rather than overwritten with a wrong/0
    value).
    """
    result: dict = {}

    outstanding_as_of = reconstruct_outstanding(all_detail_rows, end_date)
    closing_balance_as_of = fetch_statement_totals(session, XIRR_HISTORY_START_DATE, end_date)["closing_balance"]
    _warn_if_wallet_balance_mismatch(all_detail_rows, end_date, closing_balance_as_of)
    total_value_as_of = outstanding_as_of + closing_balance_as_of

    base_cashflows = _build_since_inception_cashflows_as_of(xirr_cashflow_rows, end_date)
    xirr_value = compute_xirr(base_cashflows + [(end_date, total_value_as_of)])
    if xirr_value is None:
        log.warning("Could not compute XIRR as of %s (backfilled month) from the reconstructed cashflows.", end_date)
        return result
    result["XIRR"] = xirr_value
    log.info("Computed XIRR as of %s (backfilled month): %.2f%%.", end_date, xirr_value * 100)

    if outstanding_as_of <= 0:
        # Nothing invested at end_date - Cash drag/the pie shares below all
        # divide by the invested amount, same guard as the current-month
        # block's own "if current_month and total_invested > 0" check.
        return result

    month_start_date = end_date.replace(day=1)
    month_statement_totals = fetch_statement_totals(session, month_start_date, end_date)
    avg_idle_cash = compute_average_idle_cash(
        all_detail_rows, month_statement_totals["opening_balance"], month_statement_totals["closing_balance"],
        month_start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d"),
    )
    cash_weight = avg_idle_cash / (avg_idle_cash + outstanding_as_of)
    monthly_yield_rate = month_statement_totals["gross_interest_received"] / outstanding_as_of
    result["Cash drag"] = cash_weight * monthly_yield_rate
    log.info(
        "Computed Cash drag as of %s (backfilled month): %.2f%% (avg idle cash %.2f EUR, cash weight %.2f%%, monthly yield %.2f%%).",
        end_date, result["Cash drag"] * 100, avg_idle_cash, cash_weight * 100, monthly_yield_rate * 100,
    )

    deposit_dates = [
        row["date"] for row in xirr_cashflow_rows
        if row["label"] == "Deposited funds" and row["date"] <= end_date.strftime("%Y-%m-%d")
    ]
    if not deposit_dates:
        return result
    since_inception_date = datetime.strptime(min(deposit_dates), "%Y-%m-%d").date()
    lifetime_statement_totals_as_of = fetch_statement_totals(session, since_inception_date, end_date)

    lifetime_bonus_total = lifetime_statement_totals_as_of["bonus_total"]
    if lifetime_bonus_total:
        xirr_without_bonus = compute_xirr(base_cashflows + [(end_date, total_value_as_of - lifetime_bonus_total)])
        if xirr_without_bonus is not None:
            result["XIRR Bonus"] = xirr_value - xirr_without_bonus
            log.info("XIRR share - bonus as of %s: %.2f points.", end_date, result["XIRR Bonus"] * 100)
    else:
        result["XIRR Bonus"] = 0.0

    years_elapsed = max((end_date - since_inception_date).days / 365.25, 1 / 365.25)
    avg_idle_cash_lifetime = compute_average_idle_cash(
        all_detail_rows, lifetime_statement_totals_as_of["opening_balance"], lifetime_statement_totals_as_of["closing_balance"],
        since_inception_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d"),
    )
    cash_weight_lifetime = avg_idle_cash_lifetime / (avg_idle_cash_lifetime + outstanding_as_of)
    lifetime_yield_rate = lifetime_statement_totals_as_of["gross_interest_received"] / outstanding_as_of
    cash_drag_lifetime_total = cash_weight_lifetime * lifetime_yield_rate
    missed_earnings = cash_drag_lifetime_total * (avg_idle_cash_lifetime + outstanding_as_of)
    xirr_with_cash_invested = compute_xirr(base_cashflows + [(end_date, total_value_as_of + missed_earnings)])
    if xirr_with_cash_invested is not None:
        result["XIRR Cash drag"] = xirr_value - xirr_with_cash_invested
        log.info(
            "XIRR share - cash drag as of %s: %.4f points (since-inception, %.2f years, missed earnings ~%.2f EUR).",
            end_date, result["XIRR Cash drag"] * 100, years_elapsed, missed_earnings,
        )

    lifetime_withholding_tax = lifetime_statement_totals_as_of["withholding_tax"]
    if lifetime_withholding_tax:
        xirr_with_taxes_cancelled = compute_xirr(base_cashflows + [(end_date, total_value_as_of + lifetime_withholding_tax)])
        if xirr_with_taxes_cancelled is not None:
            result["XIRR Taxes/Frais"] = xirr_value - xirr_with_taxes_cancelled
            log.info("XIRR share - taxes/frais as of %s: %.4f points (lifetime withholding tax %.2f EUR).", end_date, result["XIRR Taxes/Frais"] * 100, lifetime_withholding_tax)
    else:
        result["XIRR Taxes/Frais"] = 0.0

    lifetime_net_interest = lifetime_statement_totals_as_of["gross_interest_received"] - lifetime_withholding_tax
    if lifetime_net_interest:
        xirr_without_interest = compute_xirr(base_cashflows + [(end_date, total_value_as_of - lifetime_net_interest)])
        if xirr_without_interest is not None:
            result["XIRR Intérêts"] = xirr_value - xirr_without_interest
            log.info(
                "XIRR share - intérêts as of %s: %.4f points (lifetime net interest %.2f EUR = %.2f gross - %.2f taxes).",
                end_date, result["XIRR Intérêts"] * 100, lifetime_net_interest,
                lifetime_statement_totals_as_of["gross_interest_received"], lifetime_withholding_tax,
            )
    else:
        result["XIRR Intérêts"] = 0.0

    return result


def run() -> None:
    if not AFRANGA_EMAIL or not AFRANGA_PASSWORD:
        log.error("AFRANGA_EMAIL and AFRANGA_PASSWORD environment variables are required.")
        sys.exit(1)

    # XIRR (like "total" elsewhere in this repo) is a LIVE-only snapshot
    # metric (it needs TODAY's real total account value as its final
    # cashflow) - it can't be meaningfully backfilled for a past REPORT_DATE
    # month, so it's only ever computed/written for the real current month.
    current_month = is_current_month()

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
        statement_totals = {
            "gross_interest_received": 0.0, "withholding_tax": 0.0,
            "opening_balance": 0.0, "closing_balance": 0.0, "bonus_total": 0.0,
        }

    try:
        bonus_cashback = fetch_current_month_bonus_cashback(session)
    except Exception:
        log.exception("Failed to fetch this month's Bonus Cashback total - defaulting to 0.0.")
        bonus_cashback = 0.0

    try:
        uninvested_balance = fetch_uninvested_balance(session)
    except Exception:
        log.exception("Failed to fetch the uninvested wallet balance - 'non investi' will not be updated.")
        uninvested_balance = None

    originators = aggregate_by_originator(investments)
    log.info("Fetched %d active investment(s) across %d loan originator(s).", len(investments), len(originators))
    for o in originators:
        log.info("  %s: %.2f EUR", o["originator"], o["outstanding"])

    total_invested = sum(o["outstanding"] for o in originators)
    # "total" ("en cours") written to the Sheet is invested + uninvested,
    # per user request 2026-08-14 (matching Bienprêter/Iuvo/Bricks/Lande's
    # own convention) - falls back to invested-only if the uninvested
    # balance couldn't be fetched. `total_invested` itself stays
    # invested-only below, since it feeds the Cash drag/XIRR math.
    statement_totals["total"] = total_invested + uninvested_balance if uninvested_balance is not None else total_invested
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

    # Since-inception "Deposited funds"/"Withdrawn funds" cashflows (for
    # XIRR) + every Details row (for compute_average_idle_cash()'s Cash
    # drag reconstruction), cached incrementally - same idea as
    # swaper_diversification.py's own XIRR block (see that module's
    # docstring for the full methodology).
    xirr_cashflow_rows = None
    lifetime_statement_totals = None
    all_detail_rows = None
    # today_date is actually "the date XIRR is computed as of" - respects
    # REPORT_DATE, so during a month-range backfill run this is the END of
    # whichever past month is being processed, not the real calendar today.
    today_date = get_report_now(REPORT_TIMEZONE).date()
    try:
        log.info("Fetching the since-inception XIRR cashflows + all Details rows (cached where possible)...")
        xirr_cashflow_rows, all_detail_rows = get_cached_account_details(session, today_date)

        # The lifetime Bonus/Cash drag/Taxes/Intérêts pie-chart shares still
        # only make sense for the real current month (they lean on THIS
        # month's own live-fetched statement_totals) - only fetch this for
        # current_month, a backfilled month only needs xirr_cashflow_rows/
        # all_detail_rows for the plain XIRR-as-of computation below.
        if current_month:
            deposit_dates = [row["date"] for row in xirr_cashflow_rows if row["label"] == "Deposited funds"]
            if deposit_dates:
                since_inception_date = datetime.strptime(min(deposit_dates), "%Y-%m-%d").date()
                log.info("Fetching since-inception statement totals (%s to %s)...", since_inception_date, today_date)
                lifetime_statement_totals = fetch_statement_totals(session, since_inception_date, today_date)
    except Exception:
        log.exception("Failed to fetch the XIRR cashflow history - XIRR will not be updated.")
        xirr_cashflow_rows = None

    # XIRR is a since-inception money-weighted return: every real
    # deposit/withdrawal ever made is a signed cashflow at its real date,
    # plus today's real total account value as the final "as if withdrawn
    # today" positive cashflow.
    xirr_value = None
    signed_cashflows = None
    total_account_value = None
    # Bonus's own share of XIRR - isolated by recomputing XIRR with the
    # lifetime Bonus received/Cashback bonus/Registration Bonus total
    # (already baked into the account's live balance) subtracted from the
    # final "as if withdrawn today" cashflow.
    bonus_xirr_contribution = None
    # Declared here (not just inside the current-month "Cash drag" section
    # further below) so the backfill branch right below can also populate
    # them via compute_xirr_block_as_of().
    cash_drag_value = None
    cash_drag_xirr_contribution = None
    taxes_xirr_contribution = None
    interest_xirr_contribution = None
    if current_month and xirr_cashflow_rows is not None and uninvested_balance is not None:
        total_account_value = total_invested + uninvested_balance
        signed_cashflows = []
        for row in xirr_cashflow_rows:
            try:
                row_date = datetime.strptime(row["date"], "%Y-%m-%d").date()
            except ValueError:
                log.warning("Skipping an XIRR cashflow row with an unparseable date: %r", row)
                continue
            is_deposit = row["label"] == "Deposited funds"
            signed_amount = -row["net_amount"] if is_deposit else row["net_amount"]
            signed_cashflows.append((row_date, signed_amount))

        signed_cashflows.append((today_date, total_account_value))

        xirr_value = compute_xirr(signed_cashflows)
        if xirr_value is None:
            log.warning("Could not compute XIRR from %d cashflow(s) - XIRR row will not be updated.", len(signed_cashflows))
        else:
            log.info(
                "Computed since-inception XIRR: %.2f%% (%d deposit/withdrawal cashflow(s), current total value %.2f EUR).",
                xirr_value * 100, len(xirr_cashflow_rows), total_account_value,
            )

            if lifetime_statement_totals is not None:
                lifetime_bonus_total = lifetime_statement_totals["bonus_total"]
                if lifetime_bonus_total:
                    cashflows_without_bonus = signed_cashflows[:-1] + [
                        (today_date, total_account_value - lifetime_bonus_total)
                    ]
                    xirr_without_bonus = compute_xirr(cashflows_without_bonus)
                    if xirr_without_bonus is not None:
                        bonus_xirr_contribution = xirr_value - xirr_without_bonus
                        log.info("Bonus's own share of XIRR: %.2f points.", bonus_xirr_contribution * 100)
                else:
                    bonus_xirr_contribution = 0.0
    elif not current_month and xirr_cashflow_rows is not None and all_detail_rows is not None:
        # Backfilled (past) month: there's no LIVE total account value for
        # that date, so reconstruct it instead of skipping the whole XIRR
        # block entirely - see compute_xirr_block_as_of()'s docstring for
        # the full methodology (Cash drag on this month's own scale,
        # everything else since-inception through this month's end_date).
        try:
            xirr_block = compute_xirr_block_as_of(session, all_detail_rows, xirr_cashflow_rows, today_date)
        except Exception:
            log.exception("Failed to compute the XIRR block as of %s.", today_date)
            xirr_block = {}
        xirr_value = xirr_block.get("XIRR")
        cash_drag_value = xirr_block.get("Cash drag")
        bonus_xirr_contribution = xirr_block.get("XIRR Bonus")
        cash_drag_xirr_contribution = xirr_block.get("XIRR Cash drag")
        taxes_xirr_contribution = xirr_block.get("XIRR Taxes/Frais")
        interest_xirr_contribution = xirr_block.get("XIRR Intérêts")
        if xirr_value is None:
            log.warning("Could not compute XIRR as of %s from the reconstructed cashflows.", today_date)

    # Cash drag: how much this month's return was diluted by cash sitting
    # idle (not invested) instead of earning interest - same definition as
    # swaper_diversification.py's own Cash drag (see its docstring):
    #   cash_weight        = avg_idle_cash_this_month / (avg_idle_cash_this_month + total_invested)
    #   monthly_yield_rate = gross_interest_received_this_month / total_invested
    if current_month and total_invested > 0:
        month_start_date = get_report_now(REPORT_TIMEZONE).replace(day=1).strftime("%Y-%m-%d")
        today_date_str = today_date.strftime("%Y-%m-%d")
        avg_idle_cash = compute_average_idle_cash(
            all_detail_rows or [], statement_totals["opening_balance"], statement_totals["closing_balance"],
            month_start_date, today_date_str,
        )
        cash_weight = avg_idle_cash / (avg_idle_cash + total_invested)
        monthly_yield_rate = statement_totals["gross_interest_received"] / total_invested
        cash_drag_value = cash_weight * monthly_yield_rate
        log.info(
            "Computed Cash drag: %.2f%% (avg idle cash %.2f EUR, cash weight %.2f%%, monthly yield %.2f%%).",
            cash_drag_value * 100, avg_idle_cash, cash_weight * 100, monthly_yield_rate * 100,
        )

        if xirr_value is not None and signed_cashflows is not None and lifetime_statement_totals is not None and xirr_cashflow_rows:
            deposit_dates = [row["date"] for row in xirr_cashflow_rows if row["label"] == "Deposited funds"]
            if deposit_dates:
                since_inception_date = datetime.strptime(min(deposit_dates), "%Y-%m-%d").date()
                years_elapsed = max((today_date - since_inception_date).days / 365.25, 1 / 365.25)
                avg_idle_cash_lifetime = compute_average_idle_cash(
                    all_detail_rows or [], lifetime_statement_totals["opening_balance"], lifetime_statement_totals["closing_balance"],
                    since_inception_date.strftime("%Y-%m-%d"), today_date_str,
                )
                cash_weight_lifetime = avg_idle_cash_lifetime / (avg_idle_cash_lifetime + total_invested)
                lifetime_yield_rate = lifetime_statement_totals["gross_interest_received"] / total_invested
                cash_drag_lifetime_total = cash_weight_lifetime * lifetime_yield_rate
                missed_earnings = cash_drag_lifetime_total * (avg_idle_cash_lifetime + total_invested)
                cashflows_with_cash_invested = signed_cashflows[:-1] + [
                    (today_date, total_account_value + missed_earnings)
                ]
                xirr_with_cash_invested = compute_xirr(cashflows_with_cash_invested)
                if xirr_with_cash_invested is not None:
                    cash_drag_xirr_contribution = xirr_value - xirr_with_cash_invested
                    log.info(
                        "XIRR share - cash drag: %.4f points (since-inception, %.2f years, missed earnings ~%.2f EUR).",
                        cash_drag_xirr_contribution * 100, years_elapsed, missed_earnings,
                    )

            lifetime_withholding_tax = lifetime_statement_totals["withholding_tax"]
            if lifetime_withholding_tax:
                cashflows_with_taxes_cancelled = signed_cashflows[:-1] + [
                    (today_date, total_account_value + lifetime_withholding_tax)
                ]
                xirr_with_taxes_cancelled = compute_xirr(cashflows_with_taxes_cancelled)
                if xirr_with_taxes_cancelled is not None:
                    taxes_xirr_contribution = xirr_value - xirr_with_taxes_cancelled
                    log.info("XIRR share - taxes/frais: %.4f points (lifetime withholding tax %.2f EUR).", taxes_xirr_contribution * 100, lifetime_withholding_tax)
            else:
                taxes_xirr_contribution = 0.0

            # XIRR Intérêts: same counterfactual pattern as Bonus/Cash
            # drag/Taxes above, but for the real net interest received
            # since inception (lifetime gross interest minus lifetime
            # withholding tax, both already available on
            # lifetime_statement_totals - no extra fetch needed here,
            # unlike Bienprêter which has to re-sum its operations rows).
            lifetime_net_interest = (
                lifetime_statement_totals["gross_interest_received"] - lifetime_withholding_tax
            )
            if lifetime_net_interest:
                cashflows_without_interest = signed_cashflows[:-1] + [
                    (today_date, total_account_value - lifetime_net_interest)
                ]
                xirr_without_interest = compute_xirr(cashflows_without_interest)
                if xirr_without_interest is not None:
                    interest_xirr_contribution = xirr_value - xirr_without_interest
                    log.info(
                        "XIRR share - intérêts: %.4f points (lifetime net interest %.2f EUR = %.2f gross - %.2f taxes).",
                        interest_xirr_contribution * 100, lifetime_net_interest,
                        lifetime_statement_totals["gross_interest_received"], lifetime_withholding_tax,
                    )
            else:
                interest_xirr_contribution = 0.0

    # Day-weighted average invested/non-invested balances (new Sheet rows
    # "solde moyen pondéré investi"/"non investi", added 2026-09-08) -
    # computed whenever all_detail_rows is available, independent of
    # current_month/Cash drag/XIRR above, so this also works for a
    # REPORT_DATE-backfilled past month. Uses the REAL number of days in
    # the reporting period (see compute_time_weighted_average()), never a
    # hardcoded 30.
    avg_invested_balance = None
    avg_non_invested_balance = None
    if all_detail_rows is not None:
        month_start_date = today_date.replace(day=1)
        avg_invested_balance, avg_non_invested_balance = compute_average_balances(
            all_detail_rows, month_start_date, today_date
        )
        log.info(
            "Solde moyen pondéré - investi: %.2f EUR, non investi: %.2f EUR (%s to %s).",
            avg_invested_balance, avg_non_invested_balance, month_start_date, today_date,
        )

    # "total" is the LIVE sum of active investments' outstanding amounts
    # PLUS the uninvested wallet balance (see above) - no confirmed
    # historical/closing-balance equivalent for a past month (2026-08-06
    # investigation), so skip it for a backfilled month rather than write a
    # live-today figure under a past month's column.
    fill_current_month_amounts(
        platform="Afranga",
        amounts=statement_totals,
        skip_total=not current_month,
    )

    # Afranga's bonus feature is literally called "Bonus Cashback
    # Campaigns" - a "cashback", not a prime/concours - written to its own
    # dedicated sub-row, never to the "Bonus" row itself (a SUM formula
    # over prime/cashback/concours). "prélèvements" gets the real
    # withholding tax on gross interest, same as Bienprêter's equivalent row.
    # "XIRR"/"Cash drag" and the XIRR Bonus/Cash drag/Taxes-Frais/Intérêts
    # pie-chart shares are appended past the default max_rows=6 bound (same
    # as swaper_diversification.py) - only included when actually computed.
    # "XIRR Intérêts" (added 2026-08-18) sits right after "XIRR
    # Taxes/Frais" - this pushes the block one row taller than before, so
    # `max_rows` is bumped 18 -> 19 to keep the search bounded before the
    # next platform block. IMPORTANT: a "XIRR Intérêts" row must exist in
    # the Afranga block on the sheet itself (right after "XIRR
    # Taxes/Frais") for this new value to actually land somewhere - this
    # script fills an existing row by label, it doesn't insert new
    # labelled rows into this block.
    bonus_breakdown = {
        "cashback": statement_totals["bonus_cashback_contest"],
        "prélèvements": statement_totals["withholding_tax"],
    }
    if xirr_value is not None:
        bonus_breakdown["XIRR"] = xirr_value
    if cash_drag_value is not None:
        bonus_breakdown["Cash drag"] = cash_drag_value
    if bonus_xirr_contribution is not None:
        bonus_breakdown["XIRR Bonus"] = bonus_xirr_contribution
    if cash_drag_xirr_contribution is not None:
        bonus_breakdown["XIRR Cash drag"] = cash_drag_xirr_contribution
    if taxes_xirr_contribution is not None:
        bonus_breakdown["XIRR Taxes/Frais"] = taxes_xirr_contribution
    if interest_xirr_contribution is not None:
        bonus_breakdown["XIRR Intérêts"] = interest_xirr_contribution
    if avg_invested_balance is not None:
        bonus_breakdown[INVESTED_BALANCE_LABEL] = avg_invested_balance
    if avg_non_invested_balance is not None:
        bonus_breakdown[NON_INVESTED_BALANCE_LABEL] = avg_non_invested_balance
    fill_current_month_bonus_breakdown(
        platform="Afranga",
        breakdown=bonus_breakdown,
        max_rows=19,
    )

    loan_originators = [
        {"name": o["originator"], "amount": o["outstanding"]}
        for o in originators
    ]

    if current_month:
        fill_geographic_repartition_amounts(loan_originators, platform="Afranga")
        if uninvested_balance is not None:
            fill_geographic_repartition_uninvested_amount("Afranga", uninvested_balance)


if __name__ == "__main__":
    run()