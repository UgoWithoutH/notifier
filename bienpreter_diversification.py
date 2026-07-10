"""Bienpreter dashboard balance fetcher.

Same family as afranga_diversification.py / monefit_diversification.py etc,
but simpler and different in one key way: Bienpreter is NOT broken down by
loan originator at all here - per the user's request, this just logs into
https://www.bienpreter.com, reads two figures on the dashboard
(https://www.bienpreter.com/u/tableau-de-bord):
  - "solde disponible" (available cash balance not yet invested)
  - "capital à recevoir" (capital still to be repaid on active investments)
sums them, and hands the single total to update_google_sheet() (currently a
skeleton, see its docstring) - no per-originator dict, just one number, no
email sent either.

Login form verified against the real page on 2026-07-09: a textbox with
accessible name "Email*", a textbox with accessible name "Mot de passe*",
and a "Se connecter" button (French locale is forced via the browser
context - the same page renders in English, with a "Login" button instead,
if the browser's locale isn't French). No 2FA/TOTP step was observed.

The dashboard's markup was inspected end-to-end against the real account
on 2026-07-09, so `fetch_balances()` uses precise structural selectors
rather than a generic heuristic:
- "Capital à recevoir" is a proper `<dl><dt>Capital à recevoir</dt><dd
  class="number">1 220,00 €</dd></dl>` pair - found via the `<dt>` whose
  text contains "recevoir", value read from its `nextElementSibling`
  (`<dd>`).
- "Solde disponible" is NOT a dt/dd pair - it's a `<p>Solde
  disponible<br><span class="number big">955,25 €</span></p>` block found
  via the `<p>` whose text starts with "solde disponible", value read from
  the nested `<span>`.
  Note: "955,25 €" (this exact balance) also appears twice more elsewhere
  on the page with NO nearby label at all - once in the top nav ("Solde :
  955,25 €") and once in a bare `<span class="number big">` - so a
  generic "scan every currency-looking text node and guess by nearby
  keyword" approach (as used in monefit_diversification.py) doesn't work
  reliably here: the account-summary panel groups multiple unrelated
  labeled values (Capital, Capital remboursé, Capital à recevoir, Intérêts
  ...) together in one shared container, so a same-ancestor-mentions-the-
  keyword heuristic matches multiple candidates ambiguously. Hence the
  precise selectors above instead.

Required env vars:
    BIENPRETER_EMAIL, BIENPRETER_PASSWORD -> Bienpreter account credentials
Optional:
    GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS    -> only needed once update_google_sheet()
                                              below is filled in (see google_sheet.py)
"""

import re
import os
import sys
import logging
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from browser_stealth import get_context_options, apply_stealth, human_pause, human_mouse_wander, human_type

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bienpreter_diversification")

LOGIN_URL = "https://www.bienpreter.com/connexion"
DASHBOARD_URL = "https://www.bienpreter.com/u/tableau-de-bord"
STORAGE_STATE_FILE = Path(__file__).parent / "bienpreter_diversification_storage_state.json"

BIENPRETER_EMAIL = os.environ.get("BIENPRETER_EMAIL")
BIENPRETER_PASSWORD = os.environ.get("BIENPRETER_PASSWORD")


def dismiss_cookie_banner(page) -> None:
    """Dismiss a cookie consent dialog if one shows up (defensive - none
    was observed while building this, but kept for safety/consistency with
    the other *_diversification.py scripts)."""
    for label in ["Accepter", "Tout accepter", "Accepter et fermer"]:
        try:
            page.get_by_role("button", name=label).click(timeout=3000)
            return
        except PlaywrightTimeoutError:
            continue


def login(page) -> None:
    """Log in to Bienpreter using BIENPRETER_EMAIL/BIENPRETER_PASSWORD.

    Selectors verified against the real login form on 2026-07-09 (French
    locale): a textbox with accessible name "Email*", a textbox with
    accessible name "Mot de passe*", and a "Se connecter" button. No 2FA
    step was observed.
    """
    log.info("Navigating to login page...")
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    dismiss_cookie_banner(page)
    human_mouse_wander(page)

    # If a previous session was restored (see STORAGE_STATE_FILE) and is
    # still valid, Bienpreter redirects away from /connexion immediately -
    # nothing else to do.
    page.wait_for_timeout(1000)
    if "/connexion" not in page.url:
        log.info("Reused a previous session, already logged in at %s", page.url)
        return

    log.info("Filling in credentials...")
    human_type(page.get_by_role("textbox", name="Email*"), BIENPRETER_EMAIL)
    human_pause()
    human_type(page.get_by_role("textbox", name="Mot de passe*"), BIENPRETER_PASSWORD)
    human_pause()
    page.get_by_role("button", name="Se connecter").click()

    for _ in range(40):
        if "/connexion" not in page.url:
            break
        page.wait_for_timeout(500)
    else:
        raise RuntimeError(f"Still on the login page after submitting credentials: {page.url}")
    log.info("Logged in successfully, current URL: %s", page.url)


def _parse_amount(text: str):
    """Parse a currency-formatted amount (e.g. "955,25 €", "1 220 €",
    "€1,234.56") into a float, without assuming a fixed locale - whichever
    of ',' or '.' appears last is treated as the decimal separator, the
    other (or repeats of it) as thousands separators."""
    if not text:
        return None
    cleaned = text.replace("\xa0", " ").strip()
    cleaned = re.sub(r"[^\d.,\s-]", "", cleaned).replace(" ", "")
    if not cleaned:
        return None

    has_comma, has_dot = "," in cleaned, "." in cleaned
    if has_comma and has_dot:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif has_comma:
        last_part = cleaned.rsplit(",", 1)[-1]
        if len(last_part) == 2:
            cleaned = cleaned.replace(",", "", cleaned.count(",") - 1).replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")

    try:
        return float(cleaned)
    except ValueError:
        return None


def _extract_amounts(page) -> dict:
    """Read "Solde disponible" and "Capital à recevoir" off the dashboard
    via the precise selectors verified on 2026-07-09 (see module
    docstring). Returns the raw (unparsed) text of each, or None if not
    found."""
    return page.evaluate(
        """
        () => {
            const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();

            let soldeDisponible = null;
            const soldeP = Array.from(document.querySelectorAll('p')).find((p) => norm(p.textContent).startsWith('solde disponible'));
            if (soldeP) {
                const span = soldeP.querySelector('span');
                soldeDisponible = span ? span.textContent.trim() : null;
            }

            let capitalARecevoir = null;
            const dt = Array.from(document.querySelectorAll('dt')).find((d) => norm(d.textContent).includes('recevoir'));
            if (dt && dt.nextElementSibling) {
                capitalARecevoir = dt.nextElementSibling.textContent.trim();
            }

            return { soldeDisponible, capitalARecevoir };
        }
        """
    )


def fetch_balances(page) -> dict:
    """Navigate to the dashboard and read "solde disponible" and "capital
    à recevoir", returning both as floats. See module docstring for the
    verified selectors."""
    page.goto(DASHBOARD_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(2000)  # let the SPA render the dashboard widgets

    raw = _extract_amounts(page)
    log.info("Raw values read from the dashboard: %r", raw)

    if not raw.get("soldeDisponible"):
        raise RuntimeError("Could not find 'Solde disponible' on the Bienpreter dashboard.")
    if not raw.get("capitalARecevoir"):
        raise RuntimeError("Could not find 'Capital à recevoir' on the Bienpreter dashboard.")

    available_balance = _parse_amount(raw["soldeDisponible"])
    capital_to_receive = _parse_amount(raw["capitalARecevoir"])
    if available_balance is None:
        raise RuntimeError(f"Could not parse 'Solde disponible' out of {raw['soldeDisponible']!r}.")
    if capital_to_receive is None:
        raise RuntimeError(f"Could not parse 'Capital à recevoir' out of {raw['capitalARecevoir']!r}.")

    return {"available_balance": available_balance, "capital_to_receive": capital_to_receive}


def update_google_sheet(total: float) -> None:
    """Skeleton: write the total (solde disponible + capital à recevoir)
    into the Google Sheet. Not implemented yet on purpose - no
    per-originator mapping needed here (unlike the other
    *_diversification.py scripts), just a single cell, e.g.:

        from google_sheet import get_latest_dashboard_worksheet, SPREADSHEET_ID
        worksheet = get_latest_dashboard_worksheet(SPREADSHEET_ID)
        ...  # look up the right cell for Bienpreter and write `total`

    Left as a no-op for now so running this script never requires
    GOOGLE_SHEET_ID/GOOGLE_CREDENTIALS to be set.
    """
    log.info("update_google_sheet() is not implemented yet - skipping (total=%.2f EUR).", total)


def run(headless: bool = True) -> None:
    if not BIENPRETER_EMAIL or not BIENPRETER_PASSWORD:
        log.error("BIENPRETER_EMAIL and BIENPRETER_PASSWORD environment variables are required.")
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
            balances = fetch_balances(page)
        except Exception:
            log.exception("Failed to log in or fetch Bienpreter balances.")
            browser.close()
            sys.exit(1)

        # Persist cookies/local storage so the next run can skip login
        # while the session remains valid.
        context.storage_state(path=str(STORAGE_STATE_FILE))
        browser.close()

    total = balances["available_balance"] + balances["capital_to_receive"]
    log.info(
        "Bienpreter: solde disponible=%.2f EUR + capital à recevoir=%.2f EUR = %.2f EUR",
        balances["available_balance"], balances["capital_to_receive"], total,
    )

    update_google_sheet(total)


if __name__ == "__main__":
    # Set headless=False locally (e.g. via `python bienpreter_diversification.py --show`)
    # to watch the browser and debug the login flow if selectors need adjusting.
    run(headless="--show" not in sys.argv)
