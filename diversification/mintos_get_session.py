"""LOCAL-ONLY helper: logs into mintos.com with Playwright and prints the two
session cookies (PHPSESSID, MW_SESSION_ID) that mintos_diversification.py
needs to fetch data via pure `requests` (no Playwright at runtime).

Why this is a separate script (not folded into mintos_diversification.py):
Mintos's login is gated by Google reCAPTCHA Enterprise. Testing on
2026-07-24 showed it's adaptive/risk-based - a login attempt from a
datacenter IP (e.g. a GitHub Actions runner) is far more likely to trigger
an explicit interactive puzzle than one from a normal residential IP, and
even from a residential IP it can still occasionally show one. Per this
repo's security policy, CAPTCHAs are never programmatically solved/bypassed.
So: run THIS script locally (from your own machine/network) whenever the
session has gone stale - solve the puzzle by hand if one appears (you'll see
the browser window), then copy the two printed values into:
  - your local .env file (MINTOS_PHPSESSID=..., MINTOS_MW_SESSION_ID=...)
  - the GitHub repository secrets of the same names (Settings > Secrets and
    variables > Actions), so the scheduled/cron-job.org-triggered workflow
    can use them too.

The Mintos session cookie renews itself on every authenticated request (a
fresh `Set-Cookie` with a later `Expires` came back on every API call tested
on 2026-07-24), so as long as mintos_diversification.py (or anything else
using these cookies) runs at least once within roughly 15 minutes of the
last use, the session should keep sliding forward indefinitely without
needing to re-run this script. If the scheduled job's cadence is sparser
than that, or the account gets logged out for any other reason, the next
run will fail with a clear "session expired" error (see
mintos_diversification.py) telling you to re-run this script.

Required env vars: MINTOS_EMAIL, MINTOS_PASSWORD
Optional: MINTOS_TOTP_SECRET (automates the Google Authenticator 2FA step)
"""

import os
import sys
import logging

import pyotp
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from shared.browser_stealth import human_pause, human_mouse_wander, human_type

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("mintos_get_session")

LOGIN_URL = "https://www.mintos.com/fr/login/"

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


def _try_totp_login(page) -> bool:
    code_input = page.locator("input[name='code'], input[name='otp'], input[autocomplete='one-time-code']").first
    try:
        code_input.wait_for(timeout=10000)
    except PlaywrightTimeoutError:
        return False

    if not MINTOS_TOTP_SECRET:
        log.warning("Mintos is asking for a 2FA code but MINTOS_TOTP_SECRET is not set - complete it by hand.")
        return False

    log.info("2FA prompt detected, generating and submitting a TOTP code...")
    totp = pyotp.TOTP(MINTOS_TOTP_SECRET)
    human_type(code_input, totp.now())
    human_pause()

    for selector in ("button[type='submit']", "button:has-text('Confirmer')", "button:has-text('Valider')", "button:has-text('Vérifier')"):
        try:
            page.locator(selector).click(timeout=3000)
            break
        except PlaywrightTimeoutError:
            continue

    return True


def main() -> None:
    if not MINTOS_EMAIL or not MINTOS_PASSWORD:
        log.error("MINTOS_EMAIL and MINTOS_PASSWORD environment variables are required.")
        sys.exit(1)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(locale="fr-FR")
        page = context.new_page()

        log.info("Navigating to login page...")
        page.goto(LOGIN_URL, wait_until="domcontentloaded")
        human_mouse_wander(page)
        _dismiss_cookie_banner(page)

        if "/login" in page.url:
            log.info("Filling in credentials...")
            human_type(page.locator("input#login-username, input[name='username']").first, MINTOS_EMAIL)
            human_pause()
            human_type(page.locator("input#login-password, input[name='password']").first, MINTOS_PASSWORD)
            human_pause()

            for selector in ("button[type='submit']", "button:has-text('Se connecter')"):
                try:
                    page.locator(selector).click(timeout=3000)
                    break
                except PlaywrightTimeoutError:
                    continue

            _try_totp_login(page)

            log.info("If a CAPTCHA puzzle appears in the browser window, solve it by hand now.")
            try:
                page.wait_for_url(lambda url: "/login" not in url and "/otp" not in url, timeout=180000)
            except PlaywrightTimeoutError:
                log.error("Still not logged in after 3 minutes - aborting.")
                browser.close()
                sys.exit(1)

        log.info("Logged in, current URL: %s", page.url)

        cookies = context.cookies()
        wanted = {c["name"]: c["value"] for c in cookies if c["name"] in ("PHPSESSID", "MW_SESSION_ID")}
        browser.close()

    if "PHPSESSID" not in wanted or "MW_SESSION_ID" not in wanted:
        log.error("Could not find PHPSESSID/MW_SESSION_ID cookies after login - got: %r", list(wanted.keys()))
        sys.exit(1)

    print("\nSession captured. Update these in your local .env AND as GitHub repository secrets:\n")
    print(f"MINTOS_PHPSESSID={wanted['PHPSESSID']}")
    print(f"MINTOS_MW_SESSION_ID={wanted['MW_SESSION_ID']}")
    print("\n(These renew themselves on every authenticated request - re-run this script only if a run reports the session has expired.)")


if __name__ == "__main__":
    main()
