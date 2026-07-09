"""Lendermarket loan availability monitor.

Mirrors swaper_monitor.py's notification-gate logic exactly, with the
account balance as the same outer gate:
  - balance <= 0 -> nothing to notify, regardless of loans; every segment's
    gate is reset (so a fresh alert fires next time money becomes
    available again).
  - balance > 0 -> per loan segment (configured in LOAN_SEGMENTS below,
    mirroring the filtered listing pages the user actually watches): notify
    once while the segment has loans, only reset that segment's gate once
    it drops back to 0 loans. Fluctuations in the loan count while staying
    above 0 do NOT re-open the gate (avoids spamming on every small change)
    - only an actual return to 0 does, exactly like Swaper's balance.

Since the balance now needs to be known up front to apply that outer gate,
this logs into Lendermarket (email/password + TOTP, same idea as
swaper_monitor.py) on every run to fetch it from
`ledger/v1/investor/getInvestorAccountSummary`, which requires an
authenticated session (verified: HTTP 401 "Unauthenticated" without one).
Checking loan availability itself only needs the public, unauthenticated
`claims/v1/public/getActiveLoans` endpoint - verified on 2026-07-08 by
comparing its JSON output against the real filtered listing pages:
    https://app.lendermarket.com/fr/listes-des-prets/non-reglemente?...
    https://app.lendermarket.com/fr/listes-des-prets/reglemente?...

Required env vars:
    LENDERMARKET_EMAIL, LENDERMARKET_PASSWORD  -> Lendermarket credentials
    SMTP_HOST, SMTP_USER, SMTP_PASSWORD,       -> outgoing mail server
    EMAIL_TO                                   -> notification recipient
Optional:
    SMTP_PORT (default 587), EMAIL_FROM (default SMTP_USER)
    LENDERMARKET_TOTP_SECRET                   -> base32 secret used to set up
                                                   Google Authenticator, needed
                                                   if 2FA is enabled on the account
"""

import json
import logging
import os
from pathlib import Path
from urllib import request, parse, error

import pyotp
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from notifier import send_lendermarket_email
from state import load_state, save_state
from notification_gate import should_notify
from browser_stealth import get_context_options, apply_stealth
from cron_schedule import ensure_schedule

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("lendermarket_monitor")

LENDERMARKET_EMAIL = os.environ.get("LENDERMARKET_EMAIL")
LENDERMARKET_PASSWORD = os.environ.get("LENDERMARKET_PASSWORD")
LENDERMARKET_TOTP_SECRET = os.environ.get("LENDERMARKET_TOTP_SECRET")
LENDERMARKET_CRON_JOB_ID = os.environ.get("LENDERMARKET_CRON_JOB_ID")

LOANS_API_URL = "https://api.lendermarket.com/claims/v1/public/getActiveLoans"
BALANCE_API_URL = "https://api.lendermarket.com/ledger/v1/investor/getInvestorAccountSummary?currency=EUR"
LOGIN_URL = "https://app.lendermarket.com/fr/connexion"

STATE_FILE = Path(__file__).parent / "lendermarket_state.json"
STORAGE_STATE_FILE = Path(__file__).parent / "lendermarket_storage_state.json"
CRON_SCHEDULE_STATE_FILE = Path(__file__).parent / "lendermarket_cron_schedule_state.json"

# Verified against the real filtered listing pages on 2026-07-08.
LOAN_SEGMENTS = [
    {
        "key": "non_reglemente",
        "label": "Prêts non réglementés",
        "regulation_status": "UNREGULATED",
        "lenders": [
            "9babf437-5bf8-41fb-840d-6edf7012e408",
            "9babf437-6970-48e6-8175-62ef53465eba",
            "9babf437-6ccb-4ae2-a22f-c887e9e3696c",
        ],
        "max_remaining_term_in_days": 360,
        "page_url": (
            "https://app.lendermarket.com/fr/listes-des-prets/non-reglemente"
            "?lenders=9babf437-5bf8-41fb-840d-6edf7012e408,"
            "9babf437-6970-48e6-8175-62ef53465eba,"
            "9babf437-6ccb-4ae2-a22f-c887e9e3696c&maxRemainingTermInDays=360"
        ),
    },
    {
        "key": "reglemente",
        "label": "Prêts réglementés",
        "regulation_status": "REGULATED",
        "lenders": [
            "9ffdd9b6-bde3-445b-a3df-f2f57b94afe7",
            "9d501521-54f8-4aa2-b975-6b34f8aac5a0",
        ],
        "max_remaining_term_in_days": 360,
        "page_url": (
            "https://app.lendermarket.com/fr/listes-des-prets/reglemente"
            "?lenders=9ffdd9b6-bde3-445b-a3df-f2f57b94afe7,"
            "9d501521-54f8-4aa2-b975-6b34f8aac5a0&maxRemainingTermInDays=360"
        ),
    },
]

# Per-segment notification gate (see notification_gate.py), shared with
# swaper_monitor.py: notify once while a segment has loans, reset only when
# it drops back to 0 (fluctuations while staying above 0 don't re-open it).
DEFAULT_STATE = {"gates": {}}


def fetch_active_loans(segment: dict) -> list:
    """Fetch the currently active loans for one segment via the public API,
    with the same lenders/regulationStatus/maxRemainingTermInDays filters
    used by the corresponding listing page."""
    params = [
        ("maxRemainingTermInDays", str(segment["max_remaining_term_in_days"])),
        ("regulationStatus", segment["regulation_status"]),
    ]
    params += [("lenders[]", lender_id) for lender_id in segment["lenders"]]
    url = f"{LOANS_API_URL}?{parse.urlencode(params)}"

    req = request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read())
    except error.HTTPError as exc:
        log.error("Lendermarket API returned HTTP %s for segment '%s'.", exc.code, segment["label"])
        return []
    except Exception:
        log.exception("Failed to fetch loans for segment '%s'.", segment["label"])
        return []

    return payload.get("data") or []


def aggregate_by_lender(loans: list) -> list:
    """Group loans by lender (fournisseur de crédit), returning one entry per
    lender with the loan count, total investable amount, and min/max
    interest rate - sorted by lender name."""
    buckets = {}
    for loan in loans:
        lender_name = (loan.get("lender") or {}).get("displayName") or "Fournisseur inconnu"
        bucket = buckets.setdefault(
            lender_name,
            {"lender": lender_name, "count": 0, "total_amount": 0.0, "min_rate": None, "max_rate": None},
        )

        bucket["count"] += 1

        amount = loan.get("investableAmount") or loan.get("loanAmount")
        try:
            bucket["total_amount"] += float(amount)
        except (TypeError, ValueError):
            pass

        rate = loan.get("interestRate")
        try:
            rate = float(rate)
        except (TypeError, ValueError):
            rate = None
        if rate is not None:
            bucket["min_rate"] = rate if bucket["min_rate"] is None else min(bucket["min_rate"], rate)
            bucket["max_rate"] = rate if bucket["max_rate"] is None else max(bucket["max_rate"], rate)

    return sorted(buckets.values(), key=lambda b: b["lender"])


def _first_locator(page, selectors: list, timeout: int = 8000):
    """Return the first selector that actually renders (waiting for it, since
    this is a client-rendered Next.js app - the form doesn't exist in the
    initial HTML, only after client-side hydration)."""
    for selector in selectors:
        locator = page.locator(selector)
        try:
            locator.first.wait_for(state="visible", timeout=timeout)
            return locator.first
        except PlaywrightTimeoutError:
            continue
    return None


def login(page) -> None:
    """Log into Lendermarket using LENDERMARKET_EMAIL/PASSWORD (and
    LENDERMARKET_TOTP_SECRET if 2FA is enabled).

    Selectors verified against the real login form on 2026-07-08 via manual
    browser automation: `input[type='email']` / `input[type='password']` and
    a "Se connecter" submit button; the 2FA prompt uses 6 separate one-digit
    boxes (accessible name "Please enter OTP character N") and a "Continuer"
    button.
    """
    page.goto(LOGIN_URL, wait_until="domcontentloaded")

    for label in ["Tout accepter", "Accepter tout", "Refuser"]:
        try:
            page.get_by_role("button", name=label).click(timeout=3000)
            break
        except PlaywrightTimeoutError:
            continue

    if "/connexion" not in page.url and "/otp" not in page.url:
        log.info("Reused a previous session, already logged in at %s", page.url)
        return

    # This is a client-rendered Next.js app: right after domcontentloaded the
    # auth check (and a possible redirect away from /connexion) hasn't run
    # yet. Give it a short grace period before assuming a login form is
    # actually needed.
    try:
        page.wait_for_url(lambda url: "/connexion" not in url and "/otp" not in url, timeout=4000)
        log.info("Reused a previous session, already logged in at %s", page.url)
        return
    except PlaywrightTimeoutError:
        pass

    if "/connexion" in page.url:
        email_input = _first_locator(page, ["input[type='email']", "input[name='email']"])
        password_input = _first_locator(page, ["input[type='password']", "input[name='password']"])
        if email_input is None or password_input is None:
            raise RuntimeError("Could not locate the Lendermarket login form fields.")

        email_input.click()
        email_input.fill(LENDERMARKET_EMAIL)
        password_input.click()
        password_input.fill(LENDERMARKET_PASSWORD)

        submitted = False
        for label in ["Se connecter", "Connexion", "Login"]:
            try:
                page.get_by_role("button", name=label).click(timeout=3000)
                submitted = True
                break
            except PlaywrightTimeoutError:
                continue
        if not submitted:
            raise RuntimeError("Could not find the Lendermarket login submit button.")

    handle_two_factor(page)

    page.wait_for_url(lambda url: "/connexion" not in url and "/otp" not in url, timeout=20000)
    # The redirect fires before the session is fully established server-side
    # (the balance API can still 401 right after) - wait for the dashboard to
    # actually render authenticated content before considering login done.
    try:
        page.get_by_text("Solde disponible", exact=True).wait_for(timeout=10000)
    except PlaywrightTimeoutError:
        log.warning("Did not see the authenticated dashboard content after login - session may not be ready yet.")
    log.info("Logged in successfully, current URL: %s", page.url)


def handle_two_factor(page) -> None:
    otp_input = page.get_by_role("textbox", name="Please enter OTP character 1")
    try:
        otp_input.wait_for(timeout=8000)
    except PlaywrightTimeoutError:
        return  # no 2FA prompt shown, nothing to do

    if not LENDERMARKET_TOTP_SECRET:
        raise RuntimeError(
            "Lendermarket is asking for a 2FA code but LENDERMARKET_TOTP_SECRET is not set. "
            "Set it to the base32 secret used to configure Google Authenticator."
        )

    log.info("2FA prompt detected, generating and submitting TOTP code...")
    code = pyotp.TOTP(LENDERMARKET_TOTP_SECRET).now()
    otp_input.click()
    otp_input.type(code, delay=80)

    page.get_by_role("button", name="Continuer").click()


def fetch_account_balance(page) -> float | None:
    """Fetch the investor's available balance (EUR).

    Uses the browser's own `fetch()` (via page.evaluate), not Playwright's
    separate `APIRequestContext` - the latter has its own TLS trust store and
    can fail with "self-signed certificate in certificate chain" behind
    corporate TLS-inspecting proxies, while the real browser correctly trusts
    the OS certificate store. This endpoint requires authentication
    (verified: HTTP 401 without a session), which the browser's cookies
    provide automatically.

    Retries a couple of times on HTTP 401: verified in production
    (2026-07-08) that right after a fresh 2FA login, the session can still
    return 401 here for a moment before becoming fully usable server-side
    (a backend-side race, not something controllable from here) - the same
    request succeeds a couple of minutes later on the next run.
    """
    result = None
    for attempt in range(3):
        try:
            result = page.evaluate(
                """
                async (url) => {
                    const res = await fetch(url, { credentials: 'include' });
                    return { ok: res.ok, status: res.status, body: await res.json().catch(() => null) };
                }
                """,
                BALANCE_API_URL,
            )
        except Exception:
            log.exception("Failed to fetch the Lendermarket account balance.")
            return None

        if result["ok"]:
            break
        log.warning("Balance API returned HTTP %s (attempt %d/3).", result["status"], attempt + 1)
        if attempt < 2:
            page.wait_for_timeout(2000)

    if result is None or not result["ok"]:
        return None

    payload = result["body"]
    balance = (payload.get("data") or {}).get("investorAvailableBalanceAmount")
    try:
        return float(balance)
    except (TypeError, ValueError):
        return None


def fetch_balance_via_login() -> float | None:
    if not LENDERMARKET_EMAIL or not LENDERMARKET_PASSWORD:
        log.warning("LENDERMARKET_EMAIL/PASSWORD not set, skipping account balance lookup.")
        return None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        storage_state = str(STORAGE_STATE_FILE) if STORAGE_STATE_FILE.exists() else None
        context = browser.new_context(
            storage_state=storage_state,
            locale="fr-FR",
            **get_context_options(),
        )
        apply_stealth(context, languages="['fr-FR', 'fr']")
        page = context.new_page()

        balance = None
        try:
            login(page)
            balance = fetch_account_balance(page)
        except Exception:
            log.exception("Failed to log into Lendermarket to fetch the account balance.")
        finally:
            context.storage_state(path=str(STORAGE_STATE_FILE))
            browser.close()

    return balance


def run() -> None:
    state = load_state(STATE_FILE, DEFAULT_STATE)
    gates = state.setdefault("gates", {})

    # Same rule as Swaper (see notification_gate.py): a segment is only
    # really "available" when there's money to invest AND at least one loan
    # listed. If the balance couldn't be determined (login/fetch failed),
    # don't let that silently gate segments closed - assume money's fine and
    # fall back to loan availability alone.
    balance = fetch_balance_via_login()
    log.info("Account balance: %s", f"{balance:.2f} €" if balance is not None else "unavailable")

    # Same cron-job.org speed-up/slow-down as Swaper (see cron_schedule.py):
    # poll faster while there's money to invest. Skipped when the balance
    # couldn't be determined, rather than guessing and possibly slowing down
    # polling incorrectly.
    if balance is not None:
        mode = "30m" if balance < 10 else "2m"
        ensure_schedule(mode, cron_job_id=LENDERMARKET_CRON_JOB_ID, state_file=CRON_SCHEDULE_STATE_FILE)

    newly_available = {}

    for segment in LOAN_SEGMENTS:
        loans = fetch_active_loans(segment)
        count = len(loans)
        log.info("Segment '%s': %d loan(s) currently available.", segment["label"], count)

        available = (balance is None or balance >= 10) and count > 0
        send, was_reset = should_notify(gates, segment["key"], available)

        if was_reset:
            log.info("Segment '%s': resetting notification gate.", segment["label"])

        log.info(
            "Notification decision context: segment='%s', loans_count=%d, available=%s",
            segment["label"],
            count,
            available,
        )

        if send:
            log.info("Notification decision: SEND (reason=loans_available_and_gate_open). loans_count=%d", count)
            newly_available[segment["key"]] = {
                "label": segment["label"],
                "page_url": segment["page_url"],
                "lenders": aggregate_by_lender(loans),
            }
        elif available:
            log.info("Notification decision: SKIP (reason=already_notified_for_current_cycle).")
        else:
            log.info("Notification decision: SKIP (reason=balance < 10 or no loans available).")

    if newly_available:
        log.info("Sending notification for %d segment(s) with newly available loans.", len(newly_available))
        send_lendermarket_email(balance, newly_available)
    else:
        log.info("Nothing new to notify.")

    save_state(STATE_FILE, state)


if __name__ == "__main__":
    run()
