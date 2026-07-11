"""Lendermarket portfolio diversification (by loan originator) fetcher.

Logs into Lendermarket (reusing lendermarket_monitor.login(), which already
handles email/password + TOTP 2FA - not duplicated here) and fetches every
active investment via the site's own "Mes investissements"
(https://app.lendermarket.com/fr/investissements) API, then groups them by
loan originator ("fournisseur de crédit") and sums the remaining principal
("capital restant") per originator. No email is sent - the amounts are just
logged and handed to fill_current_month_amounts() (see google_sheet.py) so
they can be filled into a Google Sheet, mirroring
peerberry_diversification.py.

API verified against the real account on 2026-07-09:
`GET https://api.lendermarket.com/claims/v1/investor/getInvestorInvestments?activeInvestments=1&currency=EUR&page=N`
-> `{"data": [...], "meta": {"current_page", "last_page", ...}}`, one entry
per active investment:
`{"remainingPrincipal": "42.42", "lender": {"displayName": "Creditstar Sweden", ...}, "loan": {"lender": {...}, ...}, ...}`
(the top-level `lender` and `loan.lender` are the same originator, kept as a
fallback). 73 active investments fit on a single page (per_page=100) at the
time of writing, but this paginates via `meta.last_page` defensively. Uses
the browser's own `fetch()` with `credentials: 'include'` - unlike
PeerBerry's API, Lendermarket's doesn't reject credentialed cross-origin
requests (no CORS error), so no manual bearer-token header is needed here.

Also fetches this calendar month's "Intérêts reçus" + "Intérêts de retard
reçus" (summed into one "interest received" figure, per explicit user
instructions) and "Primes promotionnelles et bonus" from the Account
Statement page (https://app.lendermarket.com/fr/statement) - see
fetch_current_month_statement_totals() below, same idea as
loanch_diversification.fetch_current_month_statement_totals() /
swaper_diversification.fetch_current_month_interest_received() /
afranga_diversification.fetch_current_month_statement_totals() /
bienpreter_diversification.fetch_current_month_interest_totals(). Unlike
Bienpreter, Lendermarket has a clean, ready-made JSON summary endpoint for
this - no gross/net/tax reconstruction needed here.

Required env vars:
    LENDERMARKET_EMAIL, LENDERMARKET_PASSWORD  -> Lendermarket credentials
Optional:
    LENDERMARKET_TOTP_SECRET                   -> base32 secret used to set up
                                                   Google Authenticator, needed
                                                   if 2FA is enabled on the account
    GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS         -> used to write this month's
                                                   totals to the Google Sheet
                                                   via fill_current_month_amounts()
                                                   (see google_sheet.py)
"""

import logging
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from google_sheet import fill_current_month_amounts

load_dotenv()

from playwright.sync_api import sync_playwright

from browser_stealth import get_context_options, apply_stealth
from lendermarket_monitor import login, LENDERMARKET_EMAIL, LENDERMARKET_PASSWORD

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("lendermarket_diversification")

INVESTMENTS_PAGE_URL = "https://app.lendermarket.com/fr/investissements"
INVESTMENTS_API_URL = "https://api.lendermarket.com/claims/v1/investor/getInvestorInvestments"
STATEMENT_PAGE_URL = "https://app.lendermarket.com/fr/statement"
STATEMENT_SUMMARY_API_URL = "https://api.lendermarket.com/ledger/v1/investor/getInvestorAccountStatementSummary"
STORAGE_STATE_FILE = Path(__file__).parent / "lendermarket_diversification_storage_state.json"
# Lendermarket is a French-facing platform (app.lendermarket.com/fr/...);
# "this month" below means the current calendar month up to TODAY (1st of
# the month through today, NOT the full month) - same semantics as the
# equivalent quick filters on Swaper/Afranga/Bienpreter, matched here by
# reproducing the exact date range the page's own "Mois en cours" filter
# sends. Pinned explicitly rather than relying on the executing machine's
# local clock (e.g. UTC on a CI runner).
REPORT_TIMEZONE = ZoneInfo("Europe/Paris")


def fetch_investments(page) -> list:
    """Fetch every active investment across all pages of the investments API
    (see module docstring for the verified response shape and why a plain
    credentialed fetch works here, unlike PeerBerry's)."""
    investments = []
    page_number = 1
    while True:
        log.info("Requesting investments API page %d...", page_number)
        result = page.evaluate(
            """
            async ([url, pageNumber]) => {
                const res = await fetch(`${url}?activeInvestments=1&currency=EUR&page=${pageNumber}`, { credentials: 'include' });
                return { ok: res.ok, status: res.status, body: await res.json().catch(() => null) };
            }
            """,
            [INVESTMENTS_API_URL, page_number],
        )
        log.info("Investments API page %d response: ok=%s status=%s", page_number, result.get("ok"), result.get("status"))
        if not result.get("ok"):
            raise RuntimeError(f"Investments API returned status {result.get('status')} on page {page_number}")

        body = result.get("body") or {}
        page_investments = body.get("data") or []
        investments.extend(page_investments)
        log.info("Page %d: %d investment(s) found (running total: %d).", page_number, len(page_investments), len(investments))

        meta = body.get("meta") or {}
        if page_number >= (meta.get("last_page") or 1):
            break
        page_number += 1

    return investments


def aggregate_by_lender(investments: list) -> list:
    """Group investments by loan originator (fournisseur de crédit) and sum
    the remaining principal (capital restant) for each - one entry per
    lender, sorted by remaining amount descending."""
    totals = {}
    for inv in investments:
        lender_name = (
            (inv.get("lender") or {}).get("displayName")
            or ((inv.get("loan") or {}).get("lender") or {}).get("displayName")
            or "Fournisseur inconnu"
        )
        try:
            remaining = float(inv.get("remainingPrincipal"))
        except (TypeError, ValueError):
            remaining = 0.0
        totals[lender_name] = totals.get(lender_name, 0.0) + remaining

    lenders = [{"lender": lender, "remaining_principal": amount} for lender, amount in totals.items()]
    lenders.sort(key=lambda l: l["remaining_principal"], reverse=True)
    return lenders


def fetch_current_month_statement_totals(page) -> dict:
    """Fetch this calendar month's "Intérêts reçus", "Intérêts de retard
    reçus" and "Primes promotionnelles et bonus", as shown in the summary
    panel of the Account Statement page
    (https://app.lendermarket.com/fr/statement), via the same JSON API the
    page's own "Mois en cours" quick filter calls.

    Verified against the real account on 2026-07-10 (network capture while
    the statement page loaded with its default date range, which already
    matches "Mois en cours" - 1st of the current month through TODAY):

        GET https://api.lendermarket.com/ledger/v1/investor/getInvestorAccountStatementSummary?currency=EUR&startDate=2026-07-01&endDate=2026-07-10
        -> {"data": {"investorReceivedInterestsAmount": "7.32",
                     "investorReceivedDelayedInterestsAmount": "0.03",
                     "investorBonusesAmount": "0.00", ...}}

    which matched the page's own displayed "Intérêts reçus" (7,32 €),
    "Intérêts de retard reçus" (0,03 €) and "Primes promotionnelles et
    bonus" (0,00 €) figures exactly. Per explicit user instructions, the
    first two are summed into a single "interest received" figure; unlike
    PeerBerry/Loanch's APIs, this endpoint accepts a plain credentialed
    fetch (`credentials: 'include'`) with no CORS/bearer-token workaround
    needed, same as fetch_investments() above.

    Uses REPORT_TIMEZONE (Europe/Paris) rather than the executing machine's
    local clock to decide what "this month" means, so this stays correct
    regardless of where/when (e.g. a UTC CI runner around midnight) this
    script actually runs.
    """
    now = datetime.now(REPORT_TIMEZONE)
    start_date = now.replace(day=1).strftime("%Y-%m-%d")
    end_date = now.strftime("%Y-%m-%d")
    log.info("Requesting account statement summary API for %s to %s...", start_date, end_date)

    result = page.evaluate(
        """
        async ([url, startDate, endDate]) => {
            const params = new URLSearchParams({ currency: 'EUR', startDate, endDate });
            const res = await fetch(`${url}?${params.toString()}`, { credentials: 'include' });
            return { ok: res.ok, status: res.status, body: await res.json().catch(() => null) };
        }
        """,
        [STATEMENT_SUMMARY_API_URL, start_date, end_date],
    )
    log.info("Account statement summary API response: ok=%s status=%s", result.get("ok"), result.get("status"))
    if not result.get("ok"):
        raise RuntimeError(f"Account statement summary API returned status {result.get('status')}")

    data = (result.get("body") or {}).get("data") or {}
    log.info("Raw statement summary data: %r", data)
    try:
        interest_received = float(data.get("investorReceivedInterestsAmount") or 0.0)
    except (TypeError, ValueError):
        log.warning("Could not parse 'investorReceivedInterestsAmount' %r - defaulting to 0.0.", data.get("investorReceivedInterestsAmount"))
        interest_received = 0.0
    try:
        delayed_interest_received = float(data.get("investorReceivedDelayedInterestsAmount") or 0.0)
    except (TypeError, ValueError):
        log.warning("Could not parse 'investorReceivedDelayedInterestsAmount' %r - defaulting to 0.0.", data.get("investorReceivedDelayedInterestsAmount"))
        delayed_interest_received = 0.0
    try:
        bonuses = float(data.get("investorBonusesAmount") or 0.0)
    except (TypeError, ValueError):
        log.warning("Could not parse 'investorBonusesAmount' %r - defaulting to 0.0.", data.get("investorBonusesAmount"))
        bonuses = 0.0

    log.info(
        "Parsed statement totals: interest_received=%.2f (incl. delayed=%.2f), bonuses=%.2f",
        interest_received + delayed_interest_received, delayed_interest_received, bonuses,
    )
    return {
        "interest_received": interest_received + delayed_interest_received,
        "bonuses": bonuses,
    }


def run(headless: bool = True) -> None:
    if not LENDERMARKET_EMAIL or not LENDERMARKET_PASSWORD:
        log.error("LENDERMARKET_EMAIL and LENDERMARKET_PASSWORD environment variables are required.")
        sys.exit(1)

    log.info("Starting Lendermarket diversification run (headless=%s, storage_state_exists=%s).", headless, STORAGE_STATE_FILE.exists())

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        storage_state = str(STORAGE_STATE_FILE) if STORAGE_STATE_FILE.exists() else None
        context = browser.new_context(
            storage_state=storage_state,
            locale="fr-FR",
            **get_context_options(),
        )
        apply_stealth(context, languages="['fr-FR', 'fr']")
        page = context.new_page()

        try:
            login(page)
            page.goto(INVESTMENTS_PAGE_URL, wait_until="domcontentloaded")
            investments = fetch_investments(page)
        except Exception:
            log.exception("Failed to log in or fetch Lendermarket investments.")
            browser.close()
            sys.exit(1)

        try:
            log.info("Navigating to the account statement page to fetch this month's statement totals...")
            page.goto(STATEMENT_PAGE_URL, wait_until="domcontentloaded")
            statement_totals = fetch_current_month_statement_totals(page)
        except Exception:
            log.exception("Failed to fetch this month's interest received/bonuses - defaulting both to 0.0.")
            statement_totals = {"interest_received": 0.0, "bonuses": 0.0}

        # Persist cookies/local storage so the next run can skip login (and
        # 2FA) while the session remains valid.
        context.storage_state(path=str(STORAGE_STATE_FILE))
        browser.close()

    lenders = aggregate_by_lender(investments)
    log.info("Fetched %d active investment(s) across %d loan originator(s).", len(investments), len(lenders))
    for l in lenders:
        log.info("  %s: %.2f EUR", l["lender"], l["remaining_principal"])

    log.info(
        "This month's statement totals: interest_received=%.2f EUR, bonuses=%.2f EUR",
        statement_totals["interest_received"], statement_totals["bonuses"],
    )

    # Lendermarket's statement summary API has no gross/net/withholding-tax
    # breakdown (unlike Afranga/Bienpreter) - interest_received is mapped to
    # both gross_interest_received/net_interest_received since it's the
    # only real figure on hand, withholding_tax defaults to 0.0. Same
    # standardized dict shape as every other *_diversification.py, plus the
    # platform-specific interest_received/bonuses fields kept alongside it.
    amounts = {
        "total": sum(l["remaining_principal"] for l in lenders),
        "gross_interest_received": statement_totals["interest_received"],
        "net_interest_received": statement_totals["interest_received"],
        "withholding_tax": 0.0,
        "interest_received": statement_totals["interest_received"],
        "bonuses": statement_totals["bonuses"],
    }
    fill_current_month_amounts(
        platform="Lendermarket",
        amounts=amounts
    )


if __name__ == "__main__":
    # Set headless=False locally (e.g. via `python lendermarket_diversification.py --show`)
    # to watch the browser and debug the login flow if selectors need adjusting.
    run(headless="--show" not in sys.argv)
