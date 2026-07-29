"""LOCAL-ONLY helper: opens a real, visible browser on mintos.com and
automatically fills in your email/password (MINTOS_EMAIL/MINTOS_PASSWORD)
and, if a 2FA page appears, your TOTP code (MINTOS_TOTP_SECRET) - then
captures the resulting session cookies and immediately runs the full
mintos_diversification.py fetch + Google Sheet update using that fresh
session, with no copy-pasting required. The ONLY thing you might still
need to do by hand is solve a CAPTCHA puzzle, IF Mintos happens to show
one (see below) - everything else is automatic.

Why the CAPTCHA can't be automated (but everything else now is, as of
2026-07-29): Mintos's login is gated by Google reCAPTCHA Enterprise (site
key `6Ldx1tcpAAAAAHgB7BUqc2A4h1Jn8ECfq416N2wT`), confirmed ADAPTIVE/
risk-based - it can show a real interactive puzzle even for a fully
genuine login with correct credentials, unpredictably (verified live
2026-07-29: the exact same credentials triggered a real interactive
puzzle on one run). Per this repo's security policy, CAPTCHAs are never
solved/bypassed programmatically, so there is no reliable way to automate
that ONE step - if it appears, this script pauses and asks you to solve
it in the visible window, then continues automatically from there
(including automating the 2FA step that follows, so you don't need to
type your TOTP code either).

Usage: `python -m diversification.mintos_get_session`
1. A visible Chromium window opens on the Mintos login page.
2. Email/password are filled and submitted automatically.
3. IF a CAPTCHA puzzle appears (unpredictable), the script pauses and
   asks you to solve it in the window, then press Enter here.
4. IF a 2FA page appears, the TOTP code is filled and submitted
   automatically (tries the current/previous/next 30s window for clock-
   drift safety, same pattern as afranga_diversification.py).
5. The script detects the moment you're fully logged in (URL leaves
   /login entirely), automatically captures the PHPSESSID/MW_SESSION_ID
   session cookies, and immediately calls
   `mintos_diversification.run(session=...)` with them - the account's
   current data is fetched and written to the Google Sheet right away.
6. The two cookie values are also printed at the end - copy them into your
   local .env and the GitHub repository secrets (Settings > Secrets and
   variables > Actions) of the same names, so the scheduled/cron-job.org
   -triggered workflow can keep reusing this session headlessly afterward
   without needing a fresh manual login every time.

The Mintos session cookie renews itself on every authenticated request (a
fresh `Set-Cookie` with a later `Expires` came back on every API call tested
2026-07-24), so as long as something using these cookies runs at least once
within roughly 15 minutes of the last use, the session should keep sliding
forward indefinitely without needing to re-run this script. If the
scheduled job's cadence is sparser than that, or the account gets logged out
for any other reason, the next scheduled run will fail with a clear
"session expired" error (see mintos_diversification.py) telling you to
re-run this script.

Required env vars: MINTOS_EMAIL, MINTOS_PASSWORD, MINTOS_TOTP_SECRET (used
only locally by this helper to fill the login/2FA forms - never sent
anywhere but Mintos's own login form) - GOOGLE_SHEET_ID/GOOGLE_CREDENTIALS
are still needed (transitively, by mintos_diversification.run()) to write
the fetched data to the Sheet.
"""

import os
import sys
import time
import logging

import pyotp
import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from diversification.mintos_diversification import run as run_diversification

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("mintos_get_session")

LOGIN_URL = "https://www.mintos.com/fr/login/"
LOGIN_WAIT_TIMEOUT_MS = 15_000  # after auto-submitting credentials, how long to wait before assuming a CAPTCHA is blocking

MINTOS_EMAIL = os.environ.get("MINTOS_EMAIL")
MINTOS_PASSWORD = os.environ.get("MINTOS_PASSWORD")
MINTOS_TOTP_SECRET = os.environ.get("MINTOS_TOTP_SECRET")


def _dismiss_cookie_banner(page) -> None:
    for selector in ("#onetrust-accept-btn-handler", "button:has-text('Tout accepter')", "button:has-text('Accept all')"):
        try:
            page.locator(selector).click(timeout=4000)
            return
        except PlaywrightTimeoutError:
            continue


def _reached_twofactor_or_past_login(url: str) -> bool:
    return "/login/twofactor" in url or "/login" not in url


def _submit_credentials(page) -> None:
    page.locator("#login-username").fill(MINTOS_EMAIL)
    page.locator("#login-password").fill(MINTOS_PASSWORD)
    page.locator("[data-testid='login-button']").click()
    try:
        page.wait_for_url(_reached_twofactor_or_past_login, timeout=LOGIN_WAIT_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        log.warning("Still on the login page after submitting credentials - likely a CAPTCHA puzzle (Mintos's reCAPTCHA is adaptive/unpredictable).")
        input("\nPlease solve the CAPTCHA puzzle (and/or fix credentials) in the browser window, THEN come back here and press Enter...\n")


def _submit_totp(page) -> bool:
    """Tries 3 candidate TOTP codes (current/previous/next 30s window, same
    resilience pattern as afranga_diversification.py/lendermarket_monitor.py)
    against the 2FA form. Returns True once the page moves off /twofactor."""
    totp = pyotp.TOTP(MINTOS_TOTP_SECRET)
    now = int(time.time())
    for candidate in (totp.at(now), totp.at(now - 30), totp.at(now + 30)):
        page.get_by_label("Code à 6\xa0chiffres").fill(candidate)
        page.get_by_role("button", name="Se connecter").click()
        try:
            page.wait_for_url(lambda u: "/login/twofactor" not in u, timeout=8000)
            return True
        except PlaywrightTimeoutError:
            continue
    return False


def build_session_from_cookies(cookies: dict) -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    session.cookies.set("PHPSESSID", cookies["PHPSESSID"], domain="www.mintos.com", path="/")
    session.cookies.set("MW_SESSION_ID", cookies["MW_SESSION_ID"], domain="www.mintos.com", path="/")
    return session


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(locale="fr-FR")
        page = context.new_page()

        log.info("Navigating to the Mintos login page...")
        page.goto(LOGIN_URL, wait_until="domcontentloaded")
        _dismiss_cookie_banner(page)

        if "/login" in page.url and MINTOS_EMAIL and MINTOS_PASSWORD:
            log.info("Filling email/password automatically...")
            _submit_credentials(page)

        if "/login/twofactor" in page.url and MINTOS_TOTP_SECRET:
            log.info("Filling 2FA code automatically...")
            if not _submit_totp(page):
                log.warning("All 3 TOTP candidates were rejected.")

        if "/login" in page.url:
            input(
                f"\nStill on {page.url} - please finish logging in manually "
                "in the browser window, THEN come back here and press Enter...\n"
            )

        log.info("Login detected, current URL: %s", page.url)

        raw_cookies = context.cookies()
        wanted = {c["name"]: c["value"] for c in raw_cookies if c["name"] in ("PHPSESSID", "MW_SESSION_ID")}
        browser.close()

    if "PHPSESSID" not in wanted or "MW_SESSION_ID" not in wanted:
        log.error("Could not find PHPSESSID/MW_SESSION_ID cookies after login - got: %r", list(wanted.keys()))
        sys.exit(1)

    log.info("Session captured - taking over: running the Mintos diversification fetch now...")
    session = build_session_from_cookies(wanted)
    run_diversification(session=session)

    print("\nDone. To let the scheduled/cron-job.org-triggered workflow reuse this session")
    print("headlessly afterward, also update these in your local .env AND as GitHub repository secrets:\n")
    print(f"MINTOS_PHPSESSID={wanted['PHPSESSID']}")
    print(f"MINTOS_MW_SESSION_ID={wanted['MW_SESSION_ID']}")


if __name__ == "__main__":
    main()
