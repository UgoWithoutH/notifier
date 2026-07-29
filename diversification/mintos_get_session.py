"""LOCAL-ONLY helper: opens a real, visible browser on mintos.com so YOU log
in by hand (email, password, TOTP code, and any CAPTCHA puzzle Mintos may
show), then automatically takes over once you're logged in - it captures
the resulting session cookies and immediately runs the full
mintos_diversification.py fetch + Google Sheet update using that fresh
session, with no copy-pasting required.

Why login is manual here (not automated like every other platform's
*_diversification.py in this repo): Mintos's login is gated by Google
reCAPTCHA Enterprise (site key `6Ldx1tcpAAAAAHgB7BUqc2A4h1Jn8ECfq416N2wT`),
confirmed ADAPTIVE/risk-based - it can show a real interactive puzzle even
for a fully genuine login with correct credentials, unpredictably. Per this
repo's security policy, CAPTCHAs are never solved/bypassed programmatically,
so there is no reliable way to automate this step at all. Instead: run this
script BY HAND (never in CI) whenever you want a fresh Mintos report - type
your email/password/TOTP code yourself in the opened window, solve the
puzzle if one appears, and the script does the rest.

Usage: `python -m diversification.mintos_get_session`
1. A visible Chromium window opens on the Mintos login page.
2. Log in yourself: email, password, 2FA code, CAPTCHA - all manual.
3. The script polls in the background and detects the moment you're logged
   in (URL leaves /login), automatically captures the PHPSESSID/
   MW_SESSION_ID session cookies, and immediately calls
   `mintos_diversification.run(session=...)` with them - the account's
   current data is fetched and written to the Google Sheet right away, no
   further action needed from you.
4. The two cookie values are also printed at the end - copy them into your
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

Required env vars: none (login is fully manual) - GOOGLE_SHEET_ID/
GOOGLE_CREDENTIALS are still needed (transitively, by
mintos_diversification.run()) to write the fetched data to the Sheet.
"""

import sys
import logging

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from diversification.mintos_diversification import run as run_diversification

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("mintos_get_session")

LOGIN_URL = "https://www.mintos.com/fr/login/"
LOGIN_WAIT_TIMEOUT_MS = 600_000  # 10 minutes - manual typing + possibly solving a CAPTCHA takes time


def _dismiss_cookie_banner(page) -> None:
    for selector in ("#onetrust-accept-btn-handler", "button:has-text('Tout accepter')", "button:has-text('Accept all')"):
        try:
            page.locator(selector).click(timeout=4000)
            return
        except PlaywrightTimeoutError:
            continue


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

        if "/login" in page.url:
            log.info(
                "Please log in manually in the opened browser window now "
                "(email, password, 2FA code, and any CAPTCHA puzzle). "
                "Waiting up to %d minutes...",
                LOGIN_WAIT_TIMEOUT_MS // 60000,
            )
            try:
                page.wait_for_url(lambda url: "/login" not in url, timeout=LOGIN_WAIT_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                log.error("Still not logged in after %d minutes - aborting.", LOGIN_WAIT_TIMEOUT_MS // 60000)
                browser.close()
                sys.exit(1)

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
