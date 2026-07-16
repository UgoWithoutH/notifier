"""Bricks (app.bricks.co) portfolio balance fetcher.

Same family as bienpreter_diversification.py / monefit_diversification.py:
Bricks is a French real-estate crowdfunding platform (immobilier), not
broken down by loan originator here - per the user's request this just
logs in, reads two figures shown in the "Patrimoine" widget on the
Accueil (home) page (https://app.bricks.co/):
  - "Investissements en cours" (currently invested capital)
  - "Solde total" (cash wallet balance, itself the sum of "Solde
    principal" + "Solde cadeau" shown right below it)
and hands total = investissements_en_cours + solde_total to
fill_current_month_amounts() - same pattern as
bienpreter_diversification.py (which similarly sums two dashboard
figures into one total), mirroring every other *_diversification.py.

Verified against the real account on 2026-07-15:
- Landing page (unauthenticated) shows "Créer un compte" / "Se connecter"
  buttons - clicking "Se connecter" navigates to https://app.bricks.co/login
  (a real route change, not a modal).
- Login form: `input#login-email-input` (type=email), `input#login-
  password-input` (type=password), submit button "Se connecter" (same
  accessible name as the landing page's nav button - not ambiguous since
  each is clicked on its own page/route). No 2FA/OTP step was observed.
- Login is SLOW (~15-20s observed): the backend chains `POST
  api.bricks.co/api/auth/sign-in/email` -> `GET .../auth/get-session` ->
  `GET .../customers/me` before the SPA (a heavy Expo/React-Native-Web app,
  uses react-native-skia canvases for charts) finishes re-rendering the
  logged-in Accueil page - poll for the nav bar's "Accueil" button rather
  than a single short wait.
- Once on the Accueil page, its "Patrimoine" widget (found further down
  the page, no need to scroll/click anything - the whole widget is present
  in the DOM, `document.body.innerText` just needs enough time to include
  it) has this exact structure (verified: investissements en cours =
  3087.77 EUR, solde total = 399.76 EUR = solde principal 399.49 + solde
  cadeau 0.27 EUR, user-supplied reference values, exact match):
    Patrimoine
    <valeur du compte>
    Valeur de votre compte
    Investissements en cours
    <montant>
    Bricks en cours / Projets en cours (counts, not amounts)
    Solde total
    <montant>
    Solde principal
    <montant>
    Solde cadeau
    <montant>
    Revenus
    <montant depuis le début> (cumulative since account opening, NOT a
    "this month" figure like the other platforms' interest fields - not
    used here to avoid conflating the two)
  `fetch_balances()` scans all text nodes in document order (a
  React-Native-Web app has no semantic dt/dd-style markup to hook a CSS
  selector onto, unlike Bienpreter) and, for each exact label match, takes
  the first following text node containing "€" - reliable here because
  each label is immediately followed by its value in the DOM, no
  intervening unrelated amount (unlike a generic "scan the whole page"
  heuristic, which failed for Bienpreter - see that module's docstring).

Also fetches this calendar month's gross/net interest received and
withholding tax (like every other *_diversification.py's equivalent) from
the "Suivi" > "Revenus" page (https://app.bricks.co/portfolio/revenues) -
see fetch_current_month_revenue_totals() below. Unlike every other
platform, that page's whole UI (chart + detail panel) is rendered on a
react-native-skia canvas (via a canvaskit WASM build) - there is NO
accessible DOM text at all to scrape (`document.body.innerText` is empty
besides the nav bar), so this instead calls the same JSON API the page
itself calls: `GET api.bricks.co/investor/portfolio/revenue?startDate=
<yyyy-mm>&endDate=<yyyy-mm>` (MONTH granularity, not day-level like other
platforms' equivalents - using the current month for both start/end
covers month-to-date). Verified against the real account on 2026-07-15
(cross-checked the full-history range against the Accueil page's own
"Revenus ... perçus depuis le début" = 527.61 EUR - exact match):
`revenuesTotal.untaxedTotal` (cents) = gross interest received (already
includes obligationCoupons + referrals + boostedBalanceGain - referrals/
boosted-balance gains pass through untaxed, confirmed by the full-range
response's totals reconciling exactly), `revenuesTotal.taxedTotal` (cents)
= net interest received (after tax), `withholding_tax` = gross - net.

IMPORTANT (2026-07-15): fill_current_month_amounts() IS called, despite a
known Sheet layout issue the user was made aware of and chose to accept:
the "Bricks" row (under the "Crowdfunding immobilier" section) has NO
blank spacer row below it like every other platform - the very next row
is "Bourse" (a different, unrelated section). fill_current_month_amounts()
always writes the platform's own row (total) AND the row directly below
it (gross_interest_received) at the current-month column - here that
second write lands on Bourse's row instead of a spacer. The user was
asked twice whether to insert a blank row first and explicitly said not
to worry about it and to just call the Sheet function anyway - don't
"fix" this by skipping the Sheet call again without being asked.

Required env vars:
    BRICKS_EMAIL, BRICKS_PASSWORD       -> Bricks account credentials
Optional:
    GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS  -> will be used to write this
                                            month's totals to the Google
                                            Sheet via
                                            fill_current_month_amounts()
                                            (see google_sheet.py) once the
                                            Sheet's row layout issue above
                                            is resolved
"""

import os
import re
import sys
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from shared.browser_stealth import get_context_options, apply_stealth, human_pause, human_mouse_wander, human_type
from shared.google_sheet import fill_current_month_amounts

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bricks_diversification")

HOME_URL = "https://app.bricks.co/"
REVENUE_API_URL = "https://api.bricks.co/investor/portfolio/revenue"
STORAGE_STATE_FILE = Path(__file__).parent / "bricks_diversification_storage_state.json"
# Bricks' revenue endpoint is aggregated by MONTH (not day like every other
# platform's equivalent) - using the current month for both startDate/endDate
# gives month-to-date totals. Pinned explicitly rather than relying on the
# executing machine's local clock (e.g. UTC on a CI runner), same pattern as
# every other *_diversification.py's REPORT_TIMEZONE.
REPORT_TIMEZONE = ZoneInfo("Europe/Paris")

BRICKS_EMAIL = os.environ.get("BRICKS_EMAIL")
BRICKS_PASSWORD = os.environ.get("BRICKS_PASSWORD")


def dismiss_cookie_banner(page) -> None:
    """Dismiss a cookie consent dialog if one shows up (defensive - none
    was observed while building this, but kept for safety/consistency with
    the other *_diversification.py scripts)."""
    for label in ["Accepter", "Tout accepter", "Accepter et fermer", "J'accepte"]:
        try:
            page.get_by_role("button", name=label).click(timeout=3000)
            return
        except PlaywrightTimeoutError:
            continue


def login(page) -> None:
    """Log in to Bricks using BRICKS_EMAIL/BRICKS_PASSWORD.

    Selectors verified against the real login form on 2026-07-15 (see
    module docstring). No 2FA step was observed.
    """
    log.info("Navigating to the home page...")
    page.goto(HOME_URL, wait_until="domcontentloaded")
    dismiss_cookie_banner(page)
    human_mouse_wander(page)
    page.wait_for_timeout(3000)

    # If a previous session was restored (see STORAGE_STATE_FILE) and is
    # still valid, the home page already shows the logged-in dashboard
    # (nav bar with "Accueil"/"Projets"/"Suivi") instead of the marketing
    # landing page - nothing else to do.
    try:
        page.get_by_role("button", name="Accueil").wait_for(timeout=5000)
        log.info("Reused a previous session, already logged in.")
        return
    except PlaywrightTimeoutError:
        pass

    log.info("Not logged in yet, navigating to the login form...")
    page.get_by_role("button", name="Se connecter").click()
    page.wait_for_url("**/login", timeout=10000)

    log.info("Filling in credentials...")
    human_type(page.locator("#login-email-input"), BRICKS_EMAIL)
    human_pause()
    human_type(page.locator("#login-password-input"), BRICKS_PASSWORD)
    human_pause()
    page.get_by_role("button", name="Se connecter").click()

    # Login is slow (verified ~15-20s: sign-in/email -> get-session ->
    # customers/me chain, plus the heavy Expo/React-Native-Web bundle
    # re-rendering) - poll for the nav bar rather than a single fixed wait.
    log.info("Waiting for the logged-in dashboard to appear (can take up to ~30s)...")
    for _ in range(60):
        try:
            page.get_by_role("button", name="Accueil").wait_for(timeout=1000)
            break
        except PlaywrightTimeoutError:
            continue
    else:
        # DIAGNOSTIC (added 2026-07-16 after 2 consecutive timeouts here):
        # log what's actually on screen (URL/title/visible text, no
        # credentials involved) so the next CI failure's log explains
        # *why* the nav bar never showed up (still on /login with an error
        # message? a bot-check interstitial? redirected somewhere else?)
        # instead of just "it didn't appear".
        try:
            visible_text = page.evaluate("() => document.body.innerText.slice(0, 1000)")
        except Exception:
            visible_text = "<could not read page text>"
        log.error(
            "Login timeout diagnostics: url=%s title=%r visible_text=%r",
            page.url, page.title(), visible_text,
        )
        raise RuntimeError(
            "Still not logged in after submitting credentials (nav bar never appeared).")
    log.info("Logged in successfully, current URL: %s", page.url)


def _parse_amount(text: str):
    """Parse a currency-formatted amount (e.g. "3 087,77 €", "399,76 €")
    into a float, without assuming a fixed locale - whichever of ',' or '.'
    appears last is treated as the decimal separator, the other (or
    repeats of it, incl. narrow no-break spaces used as thousands
    separators) as thousands separators."""
    if not text:
        return None
    cleaned = text.replace("\xa0", " ").replace("\u202f", " ").strip()
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
    """Scan every text node on the Accueil page in document order and, for
    each exact label match, return the first following text node
    containing "€" (see module docstring for why this is reliable here -
    each label is immediately followed by its value, no intervening
    unrelated amount)."""
    return page.evaluate(
        """
        () => {
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            const texts = [];
            let node;
            while ((node = walker.nextNode())) {
                const t = node.textContent.trim();
                if (t) texts.push(t);
            }

            const valueAfterLabel = (label) => {
                const idx = texts.findIndex((t) => t === label);
                if (idx === -1) return null;
                for (let i = idx + 1; i < texts.length; i++) {
                    if (texts[i].includes('\u20ac')) return texts[i];
                }
                return null;
            };

            return {
                investissementsEnCours: valueAfterLabel('Investissements en cours'),
                soldeTotal: valueAfterLabel('Solde total'),
                soldePrincipal: valueAfterLabel('Solde principal'),
                soldeCadeau: valueAfterLabel('Solde cadeau'),
            };
        }
        """
    )


def fetch_balances(page) -> dict:
    """Navigate to the Accueil (home) page and read "Investissements en
    cours" / "Solde total" (and, as a bonus, its "Solde principal"/"Solde
    cadeau" breakdown) from the "Patrimoine" widget, returning all four as
    floats. See module docstring for the verified structure."""
    page.goto(HOME_URL, wait_until="domcontentloaded")
    # Heavy Expo/React-Native-Web SPA (uses react-native-skia canvases) -
    # verified it needs ~15s to fully render the Patrimoine widget.
    page.wait_for_timeout(15000)

    raw = _extract_amounts(page)
    log.info("Raw values read from the Patrimoine widget: %r", raw)

    if not raw.get("investissementsEnCours"):
        raise RuntimeError("Could not find 'Investissements en cours' on the Bricks Accueil page.")
    if not raw.get("soldeTotal"):
        raise RuntimeError("Could not find 'Solde total' on the Bricks Accueil page.")

    investments_en_cours = _parse_amount(raw["investissementsEnCours"])
    solde_total = _parse_amount(raw["soldeTotal"])
    solde_principal = _parse_amount(raw.get("soldePrincipal")) or 0.0
    solde_cadeau = _parse_amount(raw.get("soldeCadeau")) or 0.0

    if investments_en_cours is None:
        raise RuntimeError(f"Could not parse 'Investissements en cours' out of {raw['investissementsEnCours']!r}.")
    if solde_total is None:
        raise RuntimeError(f"Could not parse 'Solde total' out of {raw['soldeTotal']!r}.")

    return {
        "investments_en_cours": investments_en_cours,
        "solde_total": solde_total,
        "solde_principal": solde_principal,
        "solde_cadeau": solde_cadeau,
    }


def fetch_current_month_revenue_totals(page) -> dict:
    """Fetch this calendar month's gross/net interest received and
    withholding tax from the same JSON API the "Suivi" > "Revenus" page
    (https://app.bricks.co/portfolio/revenues) itself calls - that page's
    whole UI is rendered on a react-native-skia canvas (no accessible DOM
    text to scrape at all, unlike the Accueil page), so this replays the
    request directly instead.

    Verified against the real account on 2026-07-15 (see module docstring
    for the full reconciliation, incl. cross-checking the full-history
    range against the Accueil page's "Revenus ... perçus depuis le début"
    = 527.61 EUR, exact match): `GET .../investor/portfolio/revenue?
    startDate=<yyyy-mm>&endDate=<yyyy-mm>` (MONTH granularity - the current
    month for both gives month-to-date) returns `{"revenuesTotal":
    {"untaxedTotal": <cents>, "taxedTotal": <cents>, ...}}` -
    `untaxedTotal` = gross interest received (already includes
    obligationCoupons + referrals + boostedBalanceGain), `taxedTotal` =
    net interest received (after tax), `withholding_tax` = gross - net.
    """
    month_str = datetime.now(REPORT_TIMEZONE).strftime("%Y-%m")
    log.info("Requesting Bricks revenue endpoint for %s...", month_str)

    result = page.evaluate(
        """
        async ([monthStr, apiUrl]) => {
            const params = new URLSearchParams({ startDate: monthStr, endDate: monthStr });
            const res = await fetch(`${apiUrl}?${params.toString()}`, { credentials: 'include' });
            const json = await res.json();
            return { ok: res.ok, status: res.status, json };
        }
        """,
        [month_str, REVENUE_API_URL],
    )
    log.info("Bricks revenue endpoint response: ok=%s status=%s", result.get("ok"), result.get("status"))
    if not result.get("ok"):
        raise RuntimeError(f"Bricks revenue endpoint returned status {result.get('status')}")

    revenues_total = (result.get("json") or {}).get("revenuesTotal") or {}
    log.info("Raw revenuesTotal for %s: %r", month_str, revenues_total)

    try:
        gross_interest_received = round(float(revenues_total.get("untaxedTotal") or 0) / 100, 2)
    except (TypeError, ValueError):
        log.warning("Could not parse 'untaxedTotal' %r - defaulting to 0.0.", revenues_total.get("untaxedTotal"))
        gross_interest_received = 0.0
    try:
        net_interest_received = round(float(revenues_total.get("taxedTotal") or 0) / 100, 2)
    except (TypeError, ValueError):
        log.warning("Could not parse 'taxedTotal' %r - defaulting to 0.0.", revenues_total.get("taxedTotal"))
        net_interest_received = 0.0

    withholding_tax = round(gross_interest_received - net_interest_received, 2)

    log.info(
        "Parsed revenue totals for %s: gross_interest_received=%.2f, net_interest_received=%.2f, withholding_tax=%.2f",
        month_str, gross_interest_received, net_interest_received, withholding_tax,
    )
    return {
        "gross_interest_received": gross_interest_received,
        "net_interest_received": net_interest_received,
        "withholding_tax": withholding_tax,
    }


def run(headless: bool = True) -> None:
    if not BRICKS_EMAIL or not BRICKS_PASSWORD:
        log.error("BRICKS_EMAIL and BRICKS_PASSWORD environment variables are required.")
        sys.exit(1)

    log.info("Starting Bricks diversification run (headless=%s, storage_state_exists=%s).",
              headless, STORAGE_STATE_FILE.exists())

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
            log.exception("Failed to log in or fetch Bricks balances.")
            browser.close()
            sys.exit(1)

        try:
            log.info("Fetching this month's revenue totals...")
            revenue_totals = fetch_current_month_revenue_totals(page)
        except Exception:
            log.exception(
                "Failed to fetch this month's gross/net interest received/withholding tax - "
                "defaulting all three to 0.0."
            )
            revenue_totals = {"gross_interest_received": 0.0, "net_interest_received": 0.0, "withholding_tax": 0.0}

        # Persist cookies/local storage so the next run can skip login
        # while the session remains valid.
        context.storage_state(path=str(STORAGE_STATE_FILE))
        browser.close()

    total = round(balances["investments_en_cours"] + balances["solde_total"], 2)
    log.info(
        "Bricks balances: investments_en_cours=%.2f EUR, solde_total=%.2f EUR "
        "(solde_principal=%.2f EUR, solde_cadeau=%.2f EUR), total=%.2f EUR",
        balances["investments_en_cours"], balances["solde_total"],
        balances["solde_principal"], balances["solde_cadeau"], total,
    )

    amounts = {
        "total": total,
        "gross_interest_received": revenue_totals["gross_interest_received"],
        "net_interest_received": revenue_totals["net_interest_received"],
        "withholding_tax": revenue_totals["withholding_tax"],
        "investments_en_cours": balances["investments_en_cours"],
        "solde_total": balances["solde_total"],
        "solde_principal": balances["solde_principal"],
        "solde_cadeau": balances["solde_cadeau"],
    }
    log.info(
        "This month's revenue totals: gross_interest_received=%.2f EUR, net_interest_received=%.2f EUR, "
        "withholding_tax=%.2f EUR",
        amounts["gross_interest_received"], amounts["net_interest_received"], amounts["withholding_tax"],
    )

    # Per the user's explicit decision (2026-07-15, after being warned
    # twice about the Sheet layout issue described in the module
    # docstring), fill_current_month_amounts() IS called even though the
    # "Bricks" row has no blank spacer row below it - the
    # gross_interest_received write below will land on "Bourse"'s row
    # instead. Don't revert this to a log-only skeleton without being
    # asked again.
    fill_current_month_amounts(
        platform="Bricks",
        amounts=amounts,
        section="Crowdfunding immobilier",
    )


if __name__ == "__main__":
    # Set headless=False locally (e.g. via `python bricks_diversification.py --show`)
    # to watch the browser and debug the login flow if selectors need adjusting.
    run(headless="--show" not in sys.argv)
