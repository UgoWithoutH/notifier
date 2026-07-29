"""LOCAL-ONLY helper: opens your REAL Chrome (a plain OS process, NOT
launched through Playwright) on lande.finance's login page so YOU log in
by hand, then automatically takes over once you're logged in - it
captures the resulting session cookies and immediately runs the full
lande_diversification.py fetch + Google Sheet update using that fresh
session, with no copy-pasting required.

Why login is manual here, AND why it's launched so differently from every
other *_get_session.py in this repo (e.g. mintos_get_session.py, which
just opens a normal headed Playwright browser): lande.finance's login page
is protected by a Cloudflare Turnstile "I'm not a robot" managed challenge
that DETECTS Playwright/CDP-driven automation specifically and loops the
challenge forever - confirmed 2026-07-29 across TWO separate mitigation
attempts (plain bundled Chromium; real Chrome via `channel="chrome"` PLUS
this repo's shared/browser_stealth.py anti-detection patches), both
failed identically. The block is tied to Playwright's CDP launch/automation
signature itself (same category as the Go & Grow Keycloak block documented
elsewhere in this repo before ITS pure-HTTP rewrite), not a UA/fingerprint
-level tell fixable via JS patches - so no amount of stealth-patching a
Playwright-launched browser can get past it.

WORKAROUND (verified working 2026-07-29): launch the user's REAL,
already-installed Chrome as a plain `subprocess.Popen(...)` (completely
outside Playwright's control - no `--enable-automation`, no CDP launch
signature at all) with `--remote-debugging-port` and a dedicated
`--user-data-dir` (kept, not a temp dir, so a future run may not always
need a fresh full manual login if Cloudflare's clearance/session persists
- unverified how long that lasts). The user logs in 100% manually in that
genuinely non-automated window (the Turnstile checkbox passes normally
here). ONLY AFTER that, this script attaches via Playwright's
`connect_over_cdp()` - purely to read the resulting cookies (cf_clearance,
lande_session, XSRF-TOKEN) - never to drive any further navigation/clicks
that could re-trigger detection.

Usage: `python -m diversification.lande_get_session`
1. Your real Chrome opens (a separate profile, not your everyday one) on
   the Lande login page.
2. Log in yourself: email, password, and the "I'm not a robot" checkbox -
   all manual, in a real, non-automated browser window.
3. Once you're on the /investor page (or any logged-in page), come back
   to this terminal and press Enter.
4. The script attaches to that browser via CDP, captures the 3 needed
   cookies, and immediately calls `lande_diversification.run(session=...)`
   with them - the account's current data is fetched and written to the
   Google Sheet right away.
5. The three cookie values are also printed at the end - copy them into
   your local .env and the GitHub repository secrets (Settings > Secrets
   and variables > Actions) of the same names, so the scheduled/
   cron-job.org-triggered workflow can keep reusing this session
   headlessly afterward without needing a fresh manual login every time.

Lande's cf_clearance/session cookie lifetimes are NOT specifically
characterized yet (unlike Mintos's confirmed self-renewing sliding
window) - if the scheduled job's run eventually fails with a "session
expired" error (see lande_diversification.py), just re-run this script.

Required env vars: none (login is fully manual) - GOOGLE_SHEET_ID/
GOOGLE_CREDENTIALS are still needed (transitively, by
lande_diversification.run()) to write the fetched data to the Sheet.
"""

import os
import subprocess
import sys
import time
import logging

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from diversification.lande_diversification import run as run_diversification

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("lande_get_session")

LOGIN_URL = "https://lande.finance/fr/investor"
DEBUG_PORT = 9333
CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]
PROFILE_DIR = os.path.join(os.environ.get("TEMP", "."), "lande_get_session_chrome_profile")


def _find_chrome() -> str:
    for path in CHROME_CANDIDATES:
        if os.path.exists(path):
            return path
    raise RuntimeError(
        f"Could not find chrome.exe in any of: {CHROME_CANDIDATES}. "
        "Install Google Chrome, or edit CHROME_CANDIDATES in this file "
        "with your real chrome.exe path."
    )


def build_session_from_cookies(cookies: dict) -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    session.cookies.set("cf_clearance", cookies["cf_clearance"], domain=".lande.finance", path="/")
    session.cookies.set("lande_session", cookies["lande_session"], domain=".lande.finance", path="/")
    session.cookies.set("XSRF-TOKEN", cookies["XSRF-TOKEN"], domain=".lande.finance", path="/")
    return session


def main() -> None:
    chrome_path = _find_chrome()
    log.info("Launching real Chrome (%s) with a separate profile, remote debugging on :%s ...", chrome_path, DEBUG_PORT)
    subprocess.Popen([
        chrome_path,
        f"--remote-debugging-port={DEBUG_PORT}",
        f"--user-data-dir={PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        LOGIN_URL,
    ])
    time.sleep(2)

    input(
        "\nA real Chrome window should now be open. Please log in manually "
        "(email, password, the 'I'm not a robot' checkbox) until you're on "
        "the /investor page, THEN come back here and press Enter...\n"
    )

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://localhost:{DEBUG_PORT}")
        context = browser.contexts[0]
        raw_cookies = context.cookies()
        # Don't close the browser (it's the user's real Chrome process) -
        # just disconnect Playwright from it.
        browser.close()

    wanted = {c["name"]: c["value"] for c in raw_cookies if c["name"] in ("cf_clearance", "lande_session", "XSRF-TOKEN")}
    missing = [name for name in ("cf_clearance", "lande_session", "XSRF-TOKEN") if name not in wanted]
    if missing:
        log.error("Could not find cookie(s) %s after login - got: %r", missing, list(wanted.keys()))
        sys.exit(1)

    log.info("Session captured - taking over: running the Lande diversification fetch now...")
    session = build_session_from_cookies(wanted)
    run_diversification(session=session)

    print("\nDone. To let the scheduled/cron-job.org-triggered workflow reuse this session")
    print("headlessly afterward, also update these in your local .env AND as GitHub repository secrets:\n")
    print(f"LANDE_CF_CLEARANCE={wanted['cf_clearance']}")
    print(f"LANDE_LANDE_SESSION={wanted['lande_session']}")
    print(f"LANDE_XSRF_TOKEN={wanted['XSRF-TOKEN']}")


if __name__ == "__main__":
    main()
