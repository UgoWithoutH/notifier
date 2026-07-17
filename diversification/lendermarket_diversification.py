"""Lendermarket portfolio diversification (by loan originator) fetcher.

Logs into Lendermarket (reusing lendermarket_monitor.login(), which already
handles email/password + TOTP 2FA over pure HTTP - not duplicated here) and
fetches every active investment via the site's own "Mes investissements"
API, then groups them by loan originator ("fournisseur de crédit") and sums
the remaining principal ("capital restant") per originator. No email is
sent - the amounts are just logged and handed to fill_current_month_amounts()
(see google_sheet.py) so they can be filled into a Google Sheet, mirroring
peerberry_diversification.py.

API verified against the real account on 2026-07-09 (and re-verified via
pure HTTP on 2026-07-18):
`GET https://api.lendermarket.com/claims/v1/investor/getInvestorInvestments?activeInvestments=1&currency=EUR&page=N`
-> `{"data": [...], "meta": {"current_page", "last_page", ...}}`, one entry
per active investment:
`{"remainingPrincipal": "42.42", "lender": {"displayName": "Creditstar Sweden", ...}, "loan": {"lender": {...}, ...}, ...}`
(the top-level `lender` and `loan.lender` are the same originator, kept as a
fallback). Paginates via `meta.last_page` defensively. Requires the same
`x-xsrf-token` + `X-INVESTOR-ID` headers as every other authenticated
Lendermarket call (see lendermarket_monitor.py's module docstring for the
full auth flow).

Also fetches this calendar month's "Intérêts reçus" + "Intérêts de retard
reçus" (summed into one "interest received" figure, per explicit user
instructions) and "Primes promotionnelles et bonus" from the Account
Statement summary API - see fetch_current_month_statement_totals() below,
same idea as loanch_diversification.fetch_current_month_statement_totals()
/ swaper_diversification.fetch_current_month_interest_received() /
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
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

from shared.google_sheet import fill_current_month_amounts, fill_current_month_bonus_breakdown, fill_geographic_repartition_amounts
from shared.report_date import get_report_now

load_dotenv()

from monitors.lendermarket_monitor import login, LENDERMARKET_EMAIL, LENDERMARKET_PASSWORD, _xsrf_headers

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("lendermarket_diversification")

INVESTMENTS_API_URL = "https://api.lendermarket.com/claims/v1/investor/getInvestorInvestments"
STATEMENT_SUMMARY_API_URL = "https://api.lendermarket.com/ledger/v1/investor/getInvestorAccountStatementSummary"
# Lendermarket is a French-facing platform (app.lendermarket.com/fr/...);
# "this month" below means the current calendar month up to TODAY (1st of
# the month through today, NOT the full month) - same semantics as the
# equivalent quick filters on Swaper/Afranga/Bienpreter, matched here by
# reproducing the exact date range the page's own "Mois en cours" filter
# sends. Pinned explicitly rather than relying on the executing machine's
# local clock (e.g. UTC on a CI runner).
REPORT_TIMEZONE = ZoneInfo("Europe/Paris")


def fetch_investments(session: requests.Session, investor_id: str) -> list:
    """Fetch every active investment across all pages of the investments API
    (see module docstring for the verified response shape)."""
    investments = []
    page_number = 1
    while True:
        log.info("Requesting investments API page %d...", page_number)
        r = session.get(
            INVESTMENTS_API_URL,
            params={"activeInvestments": 1, "currency": "EUR", "page": page_number},
            headers=_xsrf_headers(session, investor_id),
            timeout=20,
        )
        log.info("Investments API page %d response: status=%s", page_number, r.status_code)
        if not r.ok:
            raise RuntimeError(f"Investments API returned status {r.status_code} on page {page_number}")

        body = r.json() or {}
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


def fetch_current_month_statement_totals(session: requests.Session, investor_id: str) -> dict:
    """Fetch this calendar month's "Intérêts reçus", "Intérêts de retard
    reçus" and "Primes promotionnelles et bonus", as shown in the summary
    panel of the Account Statement page
    (https://app.lendermarket.com/fr/statement), via the same JSON API the
    page's own "Mois en cours" quick filter calls.

    Verified against the real account on 2026-07-10 (and re-verified via
    pure HTTP on 2026-07-18):

        GET https://api.lendermarket.com/ledger/v1/investor/getInvestorAccountStatementSummary?currency=EUR&startDate=2026-07-01&endDate=2026-07-10
        -> {"data": {"investorReceivedInterestsAmount": "7.32",
                     "investorReceivedDelayedInterestsAmount": "0.03",
                     "investorBonusesAmount": "0.00", ...}}

    which matched the page's own displayed "Intérêts reçus" (7,32 €),
    "Intérêts de retard reçus" (0,03 €) and "Primes promotionnelles et
    bonus" (0,00 €) figures exactly. Per explicit user instructions, the
    first two are summed into a single "interest received" figure.

    Uses REPORT_TIMEZONE (Europe/Paris) rather than the executing machine's
    local clock to decide what "this month" means, so this stays correct
    regardless of where/when (e.g. a UTC CI runner around midnight) this
    script actually runs.
    """
    now = get_report_now(REPORT_TIMEZONE)
    start_date = now.replace(day=1).strftime("%Y-%m-%d")
    end_date = now.strftime("%Y-%m-%d")
    log.info("Requesting account statement summary API for %s to %s...", start_date, end_date)

    r = session.get(
        STATEMENT_SUMMARY_API_URL,
        params={"currency": "EUR", "startDate": start_date, "endDate": end_date},
        headers=_xsrf_headers(session, investor_id),
        timeout=20,
    )
    log.info("Account statement summary API response: status=%s", r.status_code)
    if not r.ok:
        raise RuntimeError(f"Account statement summary API returned status {r.status_code}")

    data = (r.json() or {}).get("data") or {}
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


def run() -> None:
    if not LENDERMARKET_EMAIL or not LENDERMARKET_PASSWORD:
        log.error("LENDERMARKET_EMAIL and LENDERMARKET_PASSWORD environment variables are required.")
        sys.exit(1)

    log.info("Starting Lendermarket diversification run (pure HTTP, no browser).")

    session = requests.Session()
    try:
        investor_id = login(session)
        investments = fetch_investments(session, investor_id)
    except Exception:
        log.exception("Failed to log in or fetch Lendermarket investments.")
        sys.exit(1)

    try:
        statement_totals = fetch_current_month_statement_totals(session, investor_id)
    except Exception:
        log.exception("Failed to fetch this month's interest received/bonuses - defaulting both to 0.0.")
        statement_totals = {"interest_received": 0.0, "bonuses": 0.0}

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
    # "bonuses" (investorBonusesAmount, "Primes promotionnelles et bonus")
    # was already fetched but only kept as a platform-specific extra field,
    # never surfaced under the standardized name - dissociated here too
    # via bonus_cashback_contest so it's ready to be written to its own
    # Sheet cell, separate from interest.
    amounts = {
        "total": sum(l["remaining_principal"] for l in lenders),
        "gross_interest_received": statement_totals["interest_received"],
        "net_interest_received": statement_totals["interest_received"],
        "withholding_tax": 0.0,
        "bonus_cashback_contest": statement_totals["bonuses"],
        "interest_received": statement_totals["interest_received"],
        "bonuses": statement_totals["bonuses"],
    }

    fill_current_month_amounts(
        platform="Lendermarket",
        amounts=amounts
    )

    # Lendermarket's "investorBonusesAmount" IS literally labelled "Primes
    # promotionnelles et bonus" on the platform itself - a "prime", not a
    # cashback/concours - written to its own dedicated sub-row, never to
    # the "Bonus" row itself (a SUM formula over prime/cashback/concours).
    fill_current_month_bonus_breakdown(
        platform="Lendermarket",
        breakdown={"prime": statement_totals["bonuses"]},
    )

    loan_originators = [
        {"name": l["lender"], "amount": l["remaining_principal"]}
        for l in lenders
    ]

    fill_geographic_repartition_amounts(loan_originators)


if __name__ == "__main__":
    run()
