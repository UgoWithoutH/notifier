"""Lendermarket portfolio diversification (by loan originator) fetcher.

Logs into Lendermarket (reusing lendermarket_monitor.login(), which already
handles email/password + TOTP 2FA - not duplicated here) and fetches every
active investment via the site's own "Mes investissements"
(https://app.lendermarket.com/fr/investissements) API, then groups them by
loan originator ("fournisseur de crédit") and sums the remaining principal
("capital restant") per originator. No email is sent - the amounts are just
logged and handed to update_google_sheet() (currently a skeleton, see its
docstring) so they can be filled into a Google Sheet, mirroring
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

Required env vars:
    LENDERMARKET_EMAIL, LENDERMARKET_PASSWORD  -> Lendermarket credentials
Optional:
    LENDERMARKET_TOTP_SECRET                   -> base32 secret used to set up
                                                   Google Authenticator, needed
                                                   if 2FA is enabled on the account
    GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS         -> only needed once update_google_sheet()
                                                   below is filled in (see google_sheet.py)
"""

import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from playwright.sync_api import sync_playwright

from browser_stealth import get_context_options, apply_stealth
from lendermarket_monitor import login, LENDERMARKET_EMAIL, LENDERMARKET_PASSWORD

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("lendermarket_diversification")

INVESTMENTS_PAGE_URL = "https://app.lendermarket.com/fr/investissements"
INVESTMENTS_API_URL = "https://api.lendermarket.com/claims/v1/investor/getInvestorInvestments"
STORAGE_STATE_FILE = Path(__file__).parent / "lendermarket_diversification_storage_state.json"


def fetch_investments(page) -> list:
    """Fetch every active investment across all pages of the investments API
    (see module docstring for the verified response shape and why a plain
    credentialed fetch works here, unlike PeerBerry's)."""
    investments = []
    page_number = 1
    while True:
        result = page.evaluate(
            """
            async ([url, pageNumber]) => {
                const res = await fetch(`${url}?activeInvestments=1&currency=EUR&page=${pageNumber}`, { credentials: 'include' });
                return { ok: res.ok, status: res.status, body: await res.json().catch(() => null) };
            }
            """,
            [INVESTMENTS_API_URL, page_number],
        )
        if not result.get("ok"):
            raise RuntimeError(f"Investments API returned status {result.get('status')} on page {page_number}")

        body = result.get("body") or {}
        investments.extend(body.get("data") or [])

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


def update_google_sheet(lenders: list) -> None:
    """Skeleton: write the per-lender remaining-capital amounts into the
    Google Sheet. Mirrors peerberry_diversification.update_google_sheet() -
    not implemented yet on purpose, fill in the actual cell/row mapping once
    you know which cells should hold which lender's amount, e.g.:

        from google_sheet import get_latest_dashboard_worksheet, SPREADSHEET_ID
        worksheet = get_latest_dashboard_worksheet(SPREADSHEET_ID)
        for l in lenders:
            ...  # look up the right cell for l["lender"] and write l["remaining_principal"]

    Left as a no-op for now so running this script never requires
    GOOGLE_SHEET_ID/GOOGLE_CREDENTIALS to be set.
    """
    log.info("update_google_sheet() is not implemented yet - skipping (%d lender(s) available).", len(lenders))


def run(headless: bool = True) -> None:
    if not LENDERMARKET_EMAIL or not LENDERMARKET_PASSWORD:
        log.error("LENDERMARKET_EMAIL and LENDERMARKET_PASSWORD environment variables are required.")
        sys.exit(1)

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

        # Persist cookies/local storage so the next run can skip login (and
        # 2FA) while the session remains valid.
        context.storage_state(path=str(STORAGE_STATE_FILE))
        browser.close()

    lenders = aggregate_by_lender(investments)
    log.info("Fetched %d active investment(s) across %d loan originator(s).", len(investments), len(lenders))
    for l in lenders:
        log.info("  %s: %.2f EUR", l["lender"], l["remaining_principal"])

    update_google_sheet(lenders)


if __name__ == "__main__":
    # Set headless=False locally (e.g. via `python lendermarket_diversification.py --show`)
    # to watch the browser and debug the login flow if selectors need adjusting.
    run(headless="--show" not in sys.argv)
