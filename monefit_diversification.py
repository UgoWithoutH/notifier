"""Monefit (SmartSaver) account balance fetcher.

Same idea as afranga_diversification.py / peerberry_diversification.py /
lendermarket_diversification.py / loanch_diversification.py, but simpler:
Monefit SmartSaver is a single savings product (not a marketplace split
across many loan originators), so there's nothing to group/aggregate - this
just logs into https://smartsaver.monefit.com, reads the account balance
shown on the summary page (https://smartsaver.monefit.com/en/summary), and
hands it to update_google_sheet() as a single-entry list using "monefit" as
the loan originator label, mirroring the {"originator", "outstanding"} shape
used by afranga_diversification.py. No email is sent - same as the other
diversification scripts.

Verified end-to-end against the real account on 2026-07-09:
- Cookiebot cookie banner: "Allow all" button.
- Login form: a textbox with accessible name "email", a textbox with
  accessible name "password", and a "Log in" button (disabled until both
  fields have content - filling them enables it automatically). No 2FA/TOTP
  step is shown on this account (unlike Swaper/Lendermarket/PeerBerry/
  Afranga/Loanch, all of which use Google Authenticator) - login goes
  straight from submitting credentials to /en/summary.
  `handle_two_factor()` below is a best-effort/defensive no-op if no
  code-entry field shows up, and only does something if MONEFIT_TOTP_SECRET
  is set AND such a field is actually detected - kept in case 2FA gets
  enabled later, safe to leave unset otherwise.
- The summary page has no dedicated JSON API/test-id for the balance
  either, so `fetch_balance()` uses a generic heuristic instead of a
  hardcoded selector: it scans all text nodes for a currency-looking amount
  (containing "€"/"EUR") and prefers the one whose surrounding block
  mentions "Total Wealth" (the actual label used on the real account, NOT
  "Balance") or "balance"; otherwise it falls back to the first money-looking
  amount found on the page. All candidates are logged so a mismatch is easy
  to spot and fix.

Required env vars:
    MONEFIT_EMAIL, MONEFIT_PASSWORD    -> Monefit SmartSaver account credentials
Optional:
    MONEFIT_TOTP_SECRET                 -> base32 secret used to set up
                                            Google Authenticator, only used if
                                            a 2FA prompt is actually detected
    GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS  -> only needed once update_google_sheet()
                                            below is filled in (see google_sheet.py)
"""

import os
import re
import sys
import logging
from pathlib import Path

import pyotp
from dotenv import load_dotenv

load_dotenv()

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from browser_stealth import get_context_options, apply_stealth, human_pause, human_mouse_wander, human_type

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("monefit_diversification")

LOGIN_URL = "https://smartsaver.monefit.com/en/login"
SUMMARY_URL = "https://smartsaver.monefit.com/en/summary"
STORAGE_STATE_FILE = Path(__file__).parent / "monefit_diversification_storage_state.json"
LOAN_ORIGINATOR_LABEL = "monefit"

MONEFIT_EMAIL = os.environ.get("MONEFIT_EMAIL")
MONEFIT_PASSWORD = os.environ.get("MONEFIT_PASSWORD")
MONEFIT_TOTP_SECRET = os.environ.get("MONEFIT_TOTP_SECRET")


def dismiss_cookie_banner(page) -> None:
    """Dismiss the Cookiebot consent dialog if it shows up (verified
    present on the login page on 2026-07-09: an "Allow all" button)."""
    try:
        page.get_by_role("button", name="Allow all").click(timeout=5000, force=True)
    except PlaywrightTimeoutError:
        return  # banner never appeared, nothing to do


def handle_two_factor(page) -> None:
    """Best-effort/defensive: no 2FA prompt was observed on the plain login
    form while building this (no test account available), so this only
    acts if a code-entry field actually shows up - otherwise it's a no-op,
    same defensive pattern as the other *_diversification.py scripts.
    """
    try:
        otp_input = page.get_by_role("textbox", name=re.compile("code", re.IGNORECASE)).first
        otp_input.wait_for(timeout=6000)
    except PlaywrightTimeoutError:
        return  # no 2FA prompt shown, nothing to do

    if not MONEFIT_TOTP_SECRET:
        raise RuntimeError(
            "Monefit is asking for a 2FA code but MONEFIT_TOTP_SECRET is not set. "
            "Set it to the base32 secret used to configure Google Authenticator."
        )

    log.info("2FA prompt detected, generating and submitting TOTP code...")
    code = pyotp.TOTP(MONEFIT_TOTP_SECRET).now()
    human_type(otp_input, code)
    human_pause()

    for label in ["Verify", "Confirm", "Submit", "Continue"]:
        try:
            page.get_by_role("button", name=label).click(timeout=5000)
            return
        except PlaywrightTimeoutError:
            continue
    log.info("No explicit 2FA submit button found/clicked - the code likely auto-submitted already.")


def login(page) -> None:
    """Log in to Monefit SmartSaver using MONEFIT_EMAIL/PASSWORD (and
    MONEFIT_TOTP_SECRET if a 2FA prompt actually shows up).

    Selectors verified against the real login form on 2026-07-09: a
    textbox with accessible name "email", a textbox with accessible name
    "password", and a "Log in" button.
    """
    log.info("Navigating to login page...")
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    dismiss_cookie_banner(page)
    human_mouse_wander(page)

    # If a previous session was restored (see STORAGE_STATE_FILE) and is
    # still valid, Monefit redirects away from /en/login immediately -
    # nothing else to do.
    page.wait_for_timeout(1000)
    if "/login" not in page.url:
        log.info("Reused a previous session, already logged in at %s", page.url)
        return

    log.info("Filling in credentials...")
    human_type(page.get_by_role("textbox", name="email"), MONEFIT_EMAIL)
    human_pause()
    human_type(page.get_by_role("textbox", name="password"), MONEFIT_PASSWORD)
    human_pause()
    page.get_by_role("button", name="Log in", exact=True).click()

    handle_two_factor(page)

    for _ in range(40):
        if "/login" not in page.url:
            break
        page.wait_for_timeout(500)
    else:
        raise RuntimeError(f"Still on the login page after submitting credentials/2FA: {page.url}")
    log.info("Logged in successfully, current URL: %s", page.url)


def _parse_amount(text: str):
    """Parse a currency-formatted amount (e.g. "€1,234.56", "€ 1 234.56",
    "1.234,56 €") into a float, without assuming a fixed locale - whichever
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


def fetch_balance(page) -> float:
    """Navigate to the summary page and read the account balance.

    No fixed selector is used (the real DOM couldn't be inspected without
    logging in first) - instead this scans all text nodes for a
    currency-looking amount ("€"/"EUR"), preferring one whose surrounding
    block also mentions "balance". All candidates found are logged so a
    wrong pick is easy to spot and fix with a proper selector later.
    """
    page.goto(SUMMARY_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(2000)  # let the SPA render the balance widget

    candidates = page.evaluate(
        """
        () => {
            const moneyRegex = /(€|EUR)\\s?-?[\\d][\\d.,\\s]*|-?[\\d][\\d.,\\s]*\\s?(€|EUR)/;
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            const seen = new Set();
            const results = [];
            let node;
            while ((node = walker.nextNode())) {
                const text = node.textContent.trim();
                if (!text || !moneyRegex.test(text) || seen.has(text)) continue;
                seen.add(text);
                const block = node.parentElement ? node.parentElement.closest('div, section, li') : null;
                results.push({ text, context: block ? block.innerText.slice(0, 150) : '' });
            }
            return results;
        }
        """
    )

    if not candidates:
        raise RuntimeError("No currency-looking amount found on the Monefit summary page.")

    log.info("Found %d candidate amount(s) on the summary page:", len(candidates))
    for c in candidates:
        log.info("  text=%r context=%r", c["text"], c["context"])

    # Verified against the real account on 2026-07-09: the main figure is
    # labeled "Total Wealth" (not "Balance") on the summary page.
    preferred_keywords = ("total wealth", "balance")
    balance_candidates = [
        c for c in candidates if any(k in c["context"].lower() for k in preferred_keywords)
    ]
    chosen = balance_candidates[0] if balance_candidates else candidates[0]

    amount = _parse_amount(chosen["text"])
    if amount is None:
        raise RuntimeError(f"Could not parse a balance amount out of {chosen['text']!r}.")

    return amount


def update_google_sheet(originators: list) -> None:
    """Skeleton: write the balance into the Google Sheet. Mirrors
    afranga_diversification.update_google_sheet() - not implemented yet on
    purpose, fill in the actual cell/row mapping once you know which cell
    should hold Monefit's balance, e.g.:

        from google_sheet import get_latest_dashboard_worksheet, SPREADSHEET_ID
        worksheet = get_latest_dashboard_worksheet(SPREADSHEET_ID)
        for o in originators:
            ...  # look up the right cell for o["originator"] (== "monefit") and write o["outstanding"]

    Left as a no-op for now so running this script never requires
    GOOGLE_SHEET_ID/GOOGLE_CREDENTIALS to be set.
    """
    log.info("update_google_sheet() is not implemented yet - skipping (%d entry(ies) available).", len(originators))


def run(headless: bool = True) -> None:
    if not MONEFIT_EMAIL or not MONEFIT_PASSWORD:
        log.error("MONEFIT_EMAIL and MONEFIT_PASSWORD environment variables are required.")
        sys.exit(1)

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
            balance = fetch_balance(page)
        except Exception:
            log.exception("Failed to log in or fetch the Monefit balance.")
            browser.close()
            sys.exit(1)

        # Persist cookies/local storage so the next run can skip login (and
        # 2FA) while the session remains valid.
        context.storage_state(path=str(STORAGE_STATE_FILE))
        browser.close()

    originators = [{"originator": LOAN_ORIGINATOR_LABEL, "outstanding": balance}]
    log.info("Monefit balance: %.2f EUR", balance)

    update_google_sheet(originators)


if __name__ == "__main__":
    # Set headless=False locally (e.g. via `python monefit_diversification.py --show`)
    # to watch the browser and debug the login flow if selectors need adjusting.
    run(headless="--show" not in sys.argv)
