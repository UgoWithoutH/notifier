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
- **"total" (the account balance figure) comes from the "Compte de
  résultat" (tax report) button, same period as the interest above** -
  found 2026-08-04 (correcting the earlier note here that assumed this was
  unusable): the button's real link is
  `GET https://lande.finance/fr/investor/transactions/tax-report?search=1&
  start_date=<DD.MM.YYYY>&end_date=<DD.MM.YYYY>` and IS a real
  `application/pdf` response (confirmed via Playwright's
  `context.request.get()`, which - unlike `page.goto()` - returns the raw
  PDF bytes instead of Chrome's built-in PDF-viewer's empty embedder
  shell). Parsed with PyMuPDF (`fitz`, new dependency) - the PDF's plain
  text includes a line "2. Account value at the end of the period"
  immediately followed by a line like "946.57 EUR" - that's the figure
  used for "total", NOT the investor overview page's always-CURRENT
  `id="total_balance"` value (which was used before this fix, but is wrong
  for any REPORT_DATE-driven past month - it only ever reflects today's
  live balance). Fetching the SAME period as the interest means requesting
  an old month (via REPORT_DATE/run_manual_platform.ps1's date prompt)
  now gives that month's real historical end-of-period balance instead of
  today's balance. Verified live 2026-08-04 (period 01.07.2026-31.07.2026):
  946.57 EUR, matching the previously-verified 2026-07-29 live balance.

Real bonus transactions DO occur on this account (confirmed live
2026-08-14, correcting the earlier claim here that none had been seen):
"Bonus d'affiliation" and "Bonus de parrainage" entries on the
transactions page, both positive amounts. `bonus_cashback_contest` still
defaults to 0.0 (no dedicated cashback-style transaction type has been
seen, only bonus) - the two real bonus types are instead folded into the
XIRR Bonus share below, same as Loanch's "prime"/Loanch's `total_bonus`.

Also computes a since-inception XIRR (money-weighted return) plus this
month's Cash drag and the XIRR Bonus / XIRR Cash drag / XIRR Taxes/Frais
pie-chart shares - added 2026-08-14 per explicit user request, mirroring
Afranga/Swaper/Lendermarket/PeerBerry/Loanch/Mintos's own XIRR blocks.
Unlike those platforms, Lande has no JSON transaction API - every
transaction type is scraped straight off the server-rendered
`<article>` blocks on /investor/transactions (same markup already used
by fetch_current_month_interest()), enumerated live 2026-08-14 by
paginating the ENTIRE account history (01.01.2015 to today): `Intérêt`
(interest, internal), `Principal` (principal repaid, internal),
`Investissement` (money moved into a loan, internal), `Demande de
retrait` (withdrawal - EXTERNAL cashflow), `Dépôt par virement bancaire`
(deposit - EXTERNAL cashflow), `Bonus d'affiliation` / `Bonus de
parrainage` (real bonus money, own XIRR Bonus share). Every entry's
amount is already SIGNED for its real cash-balance impact (green "+"
increases the wallet, red "-" decreases it - verified against the
"Fonds disponibles" figure), so `compute_average_idle_cash()` can
reconstruct a running daily wallet balance from scratch (starting at 0
on the very first transaction's date, same trick as
loanch_diversification.py - no separate opening-balance anchor needed).
No withholding-tax-style transaction has ever been observed on this
account - any future/unrecognized label (i.e. none of the 7 known ones
above) is conservatively folded into the XIRR Taxes/Frais share instead
of being silently dropped, same defensive catch-all as Loanch's
unclassified `transaction_type` bucket. Since this account's full history
is currently only ~11 pages (~160 rows), the full range is simply
re-fetched every run - no incremental cache file, unlike the much larger
Mintos/PeerBerry/Loanch ledgers (revisit this if Lande's history ever
grows large enough to make that slow).

Added 2026-08-19: XIRR Intérêts, the counterfactual XIRR share
attributable to real net interest received since inception (mirrors
loanch_diversification.py's/mintos_diversification.py's/
bienpreter_diversification.py's/afranga_diversification.py's/
iuvo_diversification.py's/lendermarket_diversification.py's own XIRR
Intérêts blocks). Like Loanch (and unlike Mintos), Lande has no separate
gross/withholding-tax split on interest (net == gross, tax = 0.0 - see
above), so "lifetime net interest" here is simply the sum of every
"Intérêt"-labelled entry's own signed amount since inception
(`lifetime_interest`, already computed in the Cash drag lifetime block
below for the lifetime yield rate - reused, not recomputed). Same
reasoning as the other platforms: "Intérêts" was previously only ever a
RESIDUAL on the spreadsheet/dashboard side (XIRR - XIRR Bonus - XIRR Cash
drag - XIRR Taxes/Frais), which can legitimately go negative or misleading
depending on the other shares' relative size - XIRR Intérêts instead
gives a genuine, independently-measured figure (same category of
computation as Bonus/Cash drag/Taxes, not a derived leftover), so the two
can be compared/sanity-checked against each other on the sheet/dashboard
side.

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

import html
import os
import re
import sys
import logging
from datetime import datetime, timedelta

import fitz
import requests
from dotenv import load_dotenv

from shared.google_sheet import (
    fill_current_month_amounts,
    fill_current_month_bonus_breakdown,
    fill_geographic_repartition_amounts,
    fill_geographic_repartition_uninvested_amount,
)
from shared.report_date import get_report_date, is_current_month
from shared.xirr import compute_xirr

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("lande_diversification")

TRANSACTIONS_URL = "https://lande.finance/fr/investor/transactions"
TAX_REPORT_URL = "https://lande.finance/fr/investor/transactions/tax-report"
OVERVIEW_URL = "https://lande.finance/fr/investor"
PLATFORM_LABEL = "Lande"
MAX_TRANSACTIONS_PAGES = 200  # safety net, see bienpreter_diversification.py's identical pattern
TRANSACTIONS_PAGE_SIZE = 15  # confirmed via the page's own "Showing X to Y of Z" footer
# Since-inception range used by the XIRR/Cash drag block below (see module
# docstring) - well before this account's real inception (~Sept 2025), just
# needs to be far enough back to capture the entire history.
SINCE_INCEPTION_START_DATE = "01.01.2015"

_TRANSACTION_LABEL_RE = re.compile(r'class="capitalize">([^<]+)<')
_TRANSACTION_DATE_RE = re.compile(r'<p class="mt-1 m-0 text-xs text-neutral-500">([\d.]+)</p>')
_TRANSACTION_AMOUNT_RE = re.compile(r'<span class="text-(?:brand-green|rose-600)">([+-])€[\s\xa0]*([\d.,\s\xa0]+?)</span>')

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


def fetch_account_value_at_period_end(session: requests.Session, start_date: str, end_date: str) -> float:
    """Fetch the account's balance as of the END of the given period (same
    French DD.MM.YYYY dates as fetch_current_month_interest()) via the
    "Compte de résultat" tax-report PDF. See module docstring for the
    verified URL/PDF-text format."""
    params = {"search": "1", "start_date": start_date, "end_date": end_date}
    log.info("GET %s (fetching tax report PDF for %s - %s)...", TAX_REPORT_URL, start_date, end_date)
    r = session.get(TAX_REPORT_URL, params=params, timeout=30)
    log.info("GET tax-report: status=%s content-type=%s", r.status_code, r.headers.get("content-type"))
    _check_authenticated(r)
    if not r.ok:
        raise RuntimeError(f"Tax report PDF request returned status {r.status_code}")

    with fitz.open(stream=r.content, filetype="pdf") as doc:
        text = "\n".join(page.get_text() for page in doc)

    match = re.search(r"account value at the end of the period\s*\n\s*(-?[\d.,\s]+?)\s*EUR", text, re.IGNORECASE)
    if not match:
        raise RuntimeError("Could not find 'Account value at the end of the period' in the tax report PDF.")

    try:
        return _parse_amount(match.group(1))
    except ValueError:
        raise RuntimeError(f"Could not parse account value out of {match.group(1)!r}.")


def _parse_transaction_article(body: str) -> dict | None:
    """Parse one <article> block's label/date/signed-amount - see module
    docstring for the verified markup and the 7 known labels. Returns None
    if any of the 3 pieces can't be found/parsed (defensive, should not
    normally happen)."""
    label_match = _TRANSACTION_LABEL_RE.search(body)
    date_match = _TRANSACTION_DATE_RE.search(body)
    amount_match = _TRANSACTION_AMOUNT_RE.search(body)
    if not label_match or not date_match or not amount_match:
        return None

    label = html.unescape(label_match.group(1))
    try:
        entry_date = datetime.strptime(date_match.group(1), "%d.%m.%Y").date()
    except ValueError:
        return None

    sign = -1 if amount_match.group(1) == "-" else 1
    try:
        amount = sign * _parse_amount(amount_match.group(2))
    except ValueError:
        return None

    return {"date": entry_date, "label": label, "amount": amount}


def fetch_transactions(session: requests.Session, start_date: str, end_date: str) -> list:
    """Fetch EVERY transaction row (all types, not just interest) in
    [start_date, end_date] (French DD.MM.YYYY), paginating the
    transactions page (generalized 2026-08-14 from what used to be
    fetch_current_month_interest()'s inline interest-only scan - see
    fetch_current_month_interest() below and module docstring's XIRR/Cash
    drag section). Returns a list of {"date": <date>, "label": <str,
    already HTML-unescaped>, "amount": <float, already SIGNED for its real
    cash-balance impact>}."""
    entries = []
    page = 1
    while page <= MAX_TRANSACTIONS_PAGES:
        params = {"search": "1", "start_date": start_date, "end_date": end_date, "page": page}
        log.info("GET %s (page %s, fetching transactions %s - %s)...", TRANSACTIONS_URL, page, start_date, end_date)
        r = session.get(TRANSACTIONS_URL, params=params, timeout=20)
        log.info("GET transactions page %s: status=%s", page, r.status_code)
        _check_authenticated(r)
        if not r.ok:
            raise RuntimeError(f"Transactions page returned status {r.status_code}")

        articles = r.text.split("<article")[1:]
        if not articles:
            log.info("Page %s has no transactions - stopping pagination.", page)
            break

        page_parsed = 0
        for article in articles:
            body = article.split("</article>")[0]
            parsed = _parse_transaction_article(body)
            if parsed:
                entries.append(parsed)
                page_parsed += 1
            else:
                log.warning("Could not parse a transaction article on page %s - skipping it.", page)

        log.info("Page %s: %s transaction(s), %s parsed (running total: %s).", page, len(articles), page_parsed, len(entries))

        if len(articles) < TRANSACTIONS_PAGE_SIZE:
            # Fewer than a full page's worth of rows -> this was the last page.
            break
        page += 1
    else:
        log.warning("Hit MAX_TRANSACTIONS_PAGES (%s) without an empty/partial page.", MAX_TRANSACTIONS_PAGES)

    return entries


def fetch_current_month_interest(session: requests.Session, start_date: str, end_date: str) -> float:
    """Fetch the given period's (same French DD.MM.YYYY dates as
    fetch_account_value_at_period_end()) gross interest received by
    summing every "Intérêt"-labelled entry's amount from fetch_transactions()."""
    entries = fetch_transactions(session, start_date, end_date)
    gross_interest = sum(e["amount"] for e in entries if _is_interest(e["label"]))
    return round(gross_interest, 2)


def _is_deposit(label: str) -> bool:
    return "dépôt" in label.lower() or "depot" in label.lower()


def _is_withdrawal(label: str) -> bool:
    return "retrait" in label.lower()


def _is_bonus(label: str) -> bool:
    """Real (not hypothetical) transaction types on this account -
    "Bonus d'affiliation" and "Bonus de parrainage" (confirmed live
    2026-08-14, see module docstring) - matched by the generic "bonus"
    keyword so any future bonus-style label is also caught."""
    return "bonus" in label.lower()


def _is_interest(label: str) -> bool:
    return "intér" in label.lower() or "inter" in label.lower()


def _is_known_internal_movement(label: str) -> bool:
    """"Principal" (repaid principal) / "Investissement" (money moved into
    a loan) - internal movements between cash and the invested portfolio,
    already reflected via the running balance used by
    compute_average_idle_cash(), never external cashflows for XIRR."""
    l = label.lower()
    return "principal" in l or "investissement" in l


def compute_average_idle_cash(entries: list, start_date, end_date) -> float:
    """Day-weighted average uninvested-cash/wallet balance over
    [start_date, end_date] (date objects), reconstructed from scratch off
    every transaction's own signed `amount` - same trick as
    loanch_diversification.py's equivalent (Lande has no separate
    "balance" field per transaction, unlike Mintos/PeerBerry). Since
    fetch_transactions() is always called back to
    SINCE_INCEPTION_START_DATE (i.e. real account inception, see module
    docstring), the running balance can simply start at 0 on the day of
    the account's very first transaction."""
    daily_deltas: dict = {}
    for entry in entries:
        entry_date = entry.get("date")
        if entry_date is None or entry_date > end_date:
            continue
        daily_deltas[entry_date] = daily_deltas.get(entry_date, 0.0) + entry["amount"]

    if not daily_deltas:
        return 0.0

    running_balance = 0.0
    total_balance = 0.0
    day_count = 0
    current = min(daily_deltas)
    while current <= end_date:
        running_balance += daily_deltas.get(current, 0.0)
        if current >= start_date:
            total_balance += running_balance
            day_count += 1
        current += timedelta(days=1)

    if day_count == 0:
        return running_balance
    return total_balance / day_count


def fetch_available_funds(session: requests.Session) -> float:
    """Fetch the uninvested cash balance ("non investi") from the investor
    overview page's "Fonds disponibles" figure (verified live 2026-08-10):
    `GET /fr/investor` renders a "Valeur du compte" card with
    `id="total_balance"` (always-CURRENT, see module docstring) followed by
    a `<dl>` breaking it down into "Fonds disponibles" (uninvested cash) /
    "Fonds investis" / "Fonds réservés", which sum back to total_balance."""
    r = session.get(OVERVIEW_URL, timeout=20)
    _check_authenticated(r)
    if not r.ok:
        raise RuntimeError(f"Investor overview page returned status {r.status_code}")

    match = re.search(r"Fonds disponibles.*?€[\s\xa0]*([\d.,\s\xa0]+?)\s*</dd>", r.text, re.DOTALL)
    if not match:
        raise RuntimeError("Could not find 'Fonds disponibles' on the investor overview page.")

    return _parse_amount(match.group(1))


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

    report_date = get_report_date()
    start_date = report_date.replace(day=1).strftime("%d.%m.%Y")
    end_date = report_date.strftime("%d.%m.%Y")

    try:
        total = fetch_account_value_at_period_end(session, start_date, end_date)
    except Exception:
        log.exception("Failed to fetch Lande account value at period end.")
        sys.exit(1)

    log.info("Account value at period end (%s - %s): %.2f EUR", start_date, end_date, total)

    try:
        gross_interest_received = fetch_current_month_interest(session, start_date, end_date)
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

    # No skip_total here (unlike every other *_diversification.py): "total"
    # already comes from the tax-report PDF for the SAME requested period
    # (see module docstring), so it's a real historical figure for a
    # REPORT_DATE-backfilled month too, not just a live snapshot.
    fill_current_month_amounts(platform=PLATFORM_LABEL, amounts=amounts)

    current_month = is_current_month()

    # "Répartition géographique" has a single "Lande" aggregate row (no
    # per-borrower sub-rows below it, unlike Mintos/Swaper) - same value as
    # the Crowdlending section's total.
    if current_month:
        fill_geographic_repartition_amounts([{"name": PLATFORM_LABEL, "amount": total}])

        # "non investi" row (added 2026-08-10): "Fonds disponibles" on the
        # investor overview page (/fr/investor) - a LIVE-only snapshot (no
        # date param, like Afranga's walletUninvestedLiveWire), hence only
        # written for the real current month.
        available_funds = None
        try:
            available_funds = fetch_available_funds(session)
            fill_geographic_repartition_uninvested_amount(PLATFORM_LABEL, available_funds)
        except Exception:
            log.exception("Failed to fetch/update Lande's 'non investi' row.")

        # Since-inception XIRR (money-weighted return) + this month's Cash
        # drag + the XIRR Bonus/Cash drag/Taxes-Frais/Intérêts pie-chart
        # shares - LIVE-only snapshot metrics (need TODAY's real total
        # account value as the final cashflow), see module docstring for
        # the scraped transaction ledger this is built from.
        today_date = report_date
        all_entries = None
        try:
            log.info("Fetching the since-inception transaction ledger (%s - %s)...", SINCE_INCEPTION_START_DATE, end_date)
            all_entries = fetch_transactions(session, SINCE_INCEPTION_START_DATE, end_date)
        except Exception:
            log.exception("Failed to fetch the transaction ledger - XIRR/Cash drag will not be updated.")

        total_invested = (total - available_funds) if available_funds is not None else None

        xirr_value = None
        signed_cashflows = None
        bonus_xirr_contribution = None
        since_inception_date = None
        lifetime_bonus = 0.0
        if all_entries:
            signed_cashflows = []
            deposit_dates = []
            for entry in all_entries:
                label = entry["label"]
                # `amount` is already signed for its cash-balance impact
                # (deposit positive, withdrawal negative) - negate for
                # XIRR's own convention (money going INTO the platform is
                # negative, money coming back OUT is positive).
                if _is_deposit(label):
                    signed_cashflows.append((entry["date"], -entry["amount"]))
                    deposit_dates.append(entry["date"])
                elif _is_withdrawal(label):
                    signed_cashflows.append((entry["date"], -entry["amount"]))
                elif _is_bonus(label):
                    lifetime_bonus += entry["amount"]

            since_inception_date = min(deposit_dates) if deposit_dates else None
            signed_cashflows.append((today_date, total))

            xirr_value = compute_xirr(signed_cashflows)
            if xirr_value is None:
                log.warning("Could not compute XIRR from %d cashflow(s) - XIRR row will not be updated.", len(signed_cashflows) - 1)
            else:
                log.info(
                    "Computed since-inception XIRR: %.2f%% (%d deposit/withdrawal cashflow(s), current total value %.2f EUR).",
                    xirr_value * 100, len(signed_cashflows) - 1, total,
                )
                if lifetime_bonus:
                    cashflows_without_bonus = signed_cashflows[:-1] + [(today_date, total - lifetime_bonus)]
                    xirr_without_bonus = compute_xirr(cashflows_without_bonus)
                    if xirr_without_bonus is not None:
                        bonus_xirr_contribution = xirr_value - xirr_without_bonus
                        log.info("Bonus's own share of XIRR: %.2f points.", bonus_xirr_contribution * 100)
                else:
                    bonus_xirr_contribution = 0.0

        cash_drag_value = None
        cash_drag_xirr_contribution = None
        taxes_xirr_contribution = None
        # XIRR Intérêts (added 2026-08-19, mirrors loanch/mintos/bienpreter/
        # afranga/iuvo/lendermarket's own XIRR Intérêts blocks): Lande has
        # no gross/withholding-tax split (net == gross here, see module
        # docstring), so "lifetime net interest" is just lifetime_interest,
        # already summed just below for the Cash drag lifetime yield rate -
        # reused, not recomputed.
        interest_xirr_contribution = None
        if all_entries is not None and total_invested is not None and total_invested > 0:
            month_start = today_date.replace(day=1)
            avg_idle_cash_this_month = compute_average_idle_cash(all_entries, month_start, today_date)
            cash_weight = avg_idle_cash_this_month / (avg_idle_cash_this_month + total_invested)
            monthly_yield_rate = gross_interest_received / total_invested
            cash_drag_value = cash_weight * monthly_yield_rate
            log.info(
                "Computed Cash drag: %.2f%% (avg idle cash %.2f EUR, cash weight %.2f%%, monthly yield %.2f%%).",
                cash_drag_value * 100, avg_idle_cash_this_month, cash_weight * 100, monthly_yield_rate * 100,
            )

            if xirr_value is not None and signed_cashflows is not None and since_inception_date is not None:
                avg_idle_cash_lifetime = compute_average_idle_cash(all_entries, since_inception_date, today_date)
                cash_weight_lifetime = avg_idle_cash_lifetime / (avg_idle_cash_lifetime + total_invested)
                lifetime_interest = sum(e["amount"] for e in all_entries if _is_interest(e["label"]))
                lifetime_yield_rate = lifetime_interest / total_invested
                cash_drag_lifetime_total = cash_weight_lifetime * lifetime_yield_rate
                missed_earnings = cash_drag_lifetime_total * (avg_idle_cash_lifetime + total_invested)
                cashflows_with_cash_invested = signed_cashflows[:-1] + [(today_date, total + missed_earnings)]
                xirr_with_cash_invested = compute_xirr(cashflows_with_cash_invested)
                if xirr_with_cash_invested is not None:
                    cash_drag_xirr_contribution = xirr_value - xirr_with_cash_invested
                    log.info(
                        "XIRR share - cash drag: %.4f points (since-inception, avg idle cash %.2f EUR, missed earnings ~%.2f EUR).",
                        cash_drag_xirr_contribution * 100, avg_idle_cash_lifetime, missed_earnings,
                    )

                # No withholding-tax-style transaction has ever been
                # observed on this account (see module docstring) - any
                # future/unrecognized label (none of the 7 known ones) is
                # conservatively bucketed here as Taxes/Frais, same
                # defensive catch-all as Loanch's unclassified
                # transaction_type bucket.
                def _is_classified(label: str) -> bool:
                    return _is_interest(label) or _is_deposit(label) or _is_withdrawal(label) or _is_bonus(label) or _is_known_internal_movement(label)

                lifetime_unclassified = sum(e["amount"] for e in all_entries if not _is_classified(e["label"]))
                if lifetime_unclassified:
                    cashflows_with_fees_cancelled = signed_cashflows[:-1] + [(today_date, total - lifetime_unclassified)]
                    xirr_with_fees_cancelled = compute_xirr(cashflows_with_fees_cancelled)
                    if xirr_with_fees_cancelled is not None:
                        taxes_xirr_contribution = xirr_value - xirr_with_fees_cancelled
                        log.info("XIRR share - taxes/frais: %.4f points (lifetime unclassified amount %.2f EUR).", taxes_xirr_contribution * 100, lifetime_unclassified)
                else:
                    taxes_xirr_contribution = 0.0

                # XIRR Intérêts (added 2026-08-19): same counterfactual
                # pattern as XIRR Bonus/XIRR Taxes-Frais above -
                # lifetime_interest (real "Intérêt"-labelled entries since
                # inception, already summed above for Cash drag's lifetime
                # yield rate) is reused here rather than recomputed.
                if lifetime_interest:
                    cashflows_without_interest = signed_cashflows[:-1] + [(today_date, total - lifetime_interest)]
                    xirr_without_interest = compute_xirr(cashflows_without_interest)
                    if xirr_without_interest is not None:
                        interest_xirr_contribution = xirr_value - xirr_without_interest
                        log.info(
                            "XIRR share - intérêts: %.4f points (lifetime net interest %.2f EUR, no withholding tax on Lande).",
                            interest_xirr_contribution * 100, lifetime_interest,
                        )
                else:
                    interest_xirr_contribution = 0.0

        # "Cash drag"/"XIRR" and the XIRR Bonus/Cash drag/Taxes-Frais/
        # Intérêts pie-chart shares sit further below Lande's block (rows
        # already added by the user, verified live at rows 242-246 under
        # "Lande"), past "Bonus"/"cashback"/"Rendements %". Only included
        # when actually computed.
        # UPDATED 2026-08-19: "XIRR Intérêts" sits right after "XIRR
        # Taxes/Frais" (mirrors Loanch's/Mintos's/Bienprêter's/Afranga's/
        # Iuvo's/Lendermarket's own block layout) - this pushes the block
        # one row taller than it was before, so max_rows is bumped 12 -> 13
        # to keep the search bounded before the next platform block.
        # IMPORTANT: a "XIRR Intérêts" row must exist in the Lande block on
        # the sheet itself (right after "XIRR Taxes/Frais") for this new
        # value to actually land somewhere - this script fills an existing
        # row by label, it doesn't insert new labelled rows into this
        # block.
        bonus_breakdown = {}
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
        if bonus_breakdown:
            fill_current_month_bonus_breakdown(platform=PLATFORM_LABEL, breakdown=bonus_breakdown, max_rows=13)


if __name__ == "__main__":
    run()