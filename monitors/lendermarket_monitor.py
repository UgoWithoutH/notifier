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

Login + balance lookup is pure HTTP (no browser needed) - verified
2026-07-18 via a real browser network capture that the whole auth flow
(including the TOTP 2FA challenge) has NO reCAPTCHA or other client-side-JS
requirement, unlike Swaper:
  1. GET  /users/v1/auth/getCsrfToken   -> sets XSRF-TOKEN + users_session
     cookies (Laravel/Sanctum-style CSRF).
  2. POST /users/v1/auth/login          json={"email","password"}, header
     `x-xsrf-token: unquote(cookies["XSRF-TOKEN"])`. Response may require a
     TOTP_CHALLENGE step. The XSRF-TOKEN cookie is refreshed on every
     response - always re-read it from the session right before the next
     call, never reuse a stale value.
  3. POST /users/v1/auth/submitTotpChallenge  json={"code": <TOTP>}, same
     xsrf header pattern. Response contains `data.currentInvestor.investorId`
     - required as an `X-INVESTOR-ID` header on every authenticated call
     below (without it: 401 "Unauthenticated" even with valid cookies/xsrf -
     this was the non-obvious missing piece, found via the response's
     `access-control-allow-headers` listing `X-INVESTOR-ID`).
  4. Authenticated GETs (e.g. the account summary balance) need
     x-xsrf-token (refreshed again) + X-INVESTOR-ID.

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

Invest-structure exploration (added 2026-07-23): for each lender selected in
the Google Sheet (shared.google_sheet.get_selected_lendermarket_lenders() -
same "Répartition géographique" block convention as PeerBerry's own
get_selected_peerberry_loan_originators(), but starting at the
"Lendermarket" cell and stopping at "Loanch"), this module ALSO checks that
lender's own availability using the exact filters (minInterestRate,
minRemainingTermInDays, maxRemainingTermInDays, single lender ID) from the
user's own filtered listing URLs - see LENDER_INVEST_FILTERS below - and, for
any lender with newly-available loans, captures diagnostic data meant to
help figure out, later, how to auto-invest on Lendermarket (mirroring
swaper_monitor.py's own exploration, added the same day, for the same
reason: the real invest HTTP request/HTML structure is unknown, and hasn't
been able to be explored live/interactively since no matching loans were up
at the time this was written).

`capture_lendermarket_invest_exploration()` reuses the already-working
pure-HTTP login() (see above: Laravel/Sanctum-style CSRF cookies) rather
than reimplementing the login UI in Playwright (its selectors have never
been verified, unlike Swaper's) - it injects the same session cookies into
a fresh Playwright browser context (`context.add_cookies()`), assuming
they're set on the shared `.lendermarket.com` parent domain so the
app.lendermarket.com frontend picks them up too. This assumption is NOT
confirmed - if wrong, the captured page will just show a login redirect
instead of the real listing, which is reported as
`frontend_session_established: false` in the diagnostics rather than
failing the whole run. Response listeners only ever get registered on that
freshly-authenticated page (nothing during the HTTP login itself). NO
invest/confirm button is ever clicked - purely passive capture, same
real-money safety boundary as Swaper's/PeerBerry's explorations. The result
is emailed (see shared.notifier.send_lendermarket_invest_exploration_
email()) as a JSON attachment whenever at least one selected lender has
newly-available loans, so the user can pass that file back to build the
real auto-invest bot once genuine loan/invest markup has actually been
observed.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib import request, parse, error
from urllib.parse import unquote

import pyotp
import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from shared.notifier import send_lendermarket_email, send_lendermarket_invest_exploration_email
from shared.google_sheet import get_selected_lendermarket_lenders
from shared.state import load_state, save_state
from shared.notification_gate import should_notify
from shared.cron_schedule import ensure_schedule

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("lendermarket_monitor")

LENDERMARKET_EMAIL = os.environ.get("LENDERMARKET_EMAIL")
LENDERMARKET_PASSWORD = os.environ.get("LENDERMARKET_PASSWORD")
LENDERMARKET_TOTP_SECRET = os.environ.get("LENDERMARKET_TOTP_SECRET")
LENDERMARKET_CRON_JOB_ID = os.environ.get("LENDERMARKET_CRON_JOB_ID")

API_BASE = "https://api.lendermarket.com"
CSRF_URL = f"{API_BASE}/users/v1/auth/getCsrfToken"
LOGIN_URL = f"{API_BASE}/users/v1/auth/login"
TOTP_URL = f"{API_BASE}/users/v1/auth/submitTotpChallenge"
LOANS_API_URL = f"{API_BASE}/claims/v1/public/getActiveLoans"
BALANCE_API_URL = f"{API_BASE}/ledger/v1/investor/getInvestorAccountSummary"

STATE_FILE = Path(__file__).parent / "lendermarket_state.json"
CRON_SCHEDULE_STATE_FILE = Path(__file__).parent / "lendermarket_cron_schedule_state.json"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://app.lendermarket.com/",
    "Accept": "application/json",
    "Content-Type": "application/json",
}

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

# Per-lender filter configs for the invest-structure exploration (added
# 2026-07-23) - one entry per lender the user actually watches for a future
# auto-invest bot, selected via the Google Sheet (see
# get_selected_lendermarket_lenders()). Verified 2026-07-23 against the
# user's own filtered listing URLs - the `lender_id`s reuse the same UUIDs
# already in LOAN_SEGMENTS above, but each has its own (stricter)
# minInterestRate cutoff the aggregate segments above don't apply.
LENDER_INVEST_FILTERS = {
    "Dineo": {
        "lender_id": "9d501521-54f8-4aa2-b975-6b34f8aac5a0",
        "regulation_status": "REGULATED",
        "min_interest_rate": 8,
        "min_remaining_term_in_days": 1,
        "max_remaining_term_in_days": 360,
        "page_url": (
            "https://app.lendermarket.com/fr/listes-des-prets/reglemente"
            "?minInterestRate=8&minRemainingTermInDays=1&maxRemainingTermInDays=360"
            "&lenders=9d501521-54f8-4aa2-b975-6b34f8aac5a0"
        ),
    },
    "Creditstar Spain": {
        "lender_id": "9babf437-637c-47b4-b0e7-937c30fa587c",
        "regulation_status": "UNREGULATED",
        "min_interest_rate": 10,
        "min_remaining_term_in_days": 1,
        "max_remaining_term_in_days": 360,
        "page_url": (
            "https://app.lendermarket.com/fr/listes-des-prets/non-reglemente"
            "?minInterestRate=10&minRemainingTermInDays=1&maxRemainingTermInDays=360"
            "&lenders=9babf437-637c-47b4-b0e7-937c30fa587c"
        ),
    },
    "Creditstar Sweden": {
        "lender_id": "9babf437-6ccb-4ae2-a22f-c887e9e3696c",
        "regulation_status": "UNREGULATED",
        "min_interest_rate": 9,
        "min_remaining_term_in_days": 1,
        "max_remaining_term_in_days": 360,
        "page_url": (
            "https://app.lendermarket.com/fr/listes-des-prets/non-reglemente"
            "?minInterestRate=9&minRemainingTermInDays=1&maxRemainingTermInDays=360"
            "&lenders=9babf437-6ccb-4ae2-a22f-c887e9e3696c"
        ),
    },
    "Creditstar Denmark": {
        "lender_id": "9babf437-6970-48e6-8175-62ef53465eba",
        "regulation_status": "UNREGULATED",
        "min_interest_rate": 9,
        "min_remaining_term_in_days": 1,
        "max_remaining_term_in_days": 360,
        "page_url": (
            "https://app.lendermarket.com/fr/listes-des-prets/non-reglemente"
            "?minInterestRate=9&minRemainingTermInDays=1&maxRemainingTermInDays=360"
            "&lenders=9babf437-6970-48e6-8175-62ef53465eba"
        ),
    },
    "Creditstar Czech": {
        "lender_id": "9babf437-5bf8-41fb-840d-6edf7012e408",
        "regulation_status": "UNREGULATED",
        "min_interest_rate": 9,
        "min_remaining_term_in_days": 1,
        "max_remaining_term_in_days": 360,
        "page_url": (
            "https://app.lendermarket.com/fr/listes-des-prets/non-reglemente"
            "?minInterestRate=9&minRemainingTermInDays=1&maxRemainingTermInDays=360"
            "&lenders=9babf437-5bf8-41fb-840d-6edf7012e408"
        ),
    },
}

# Caps for the invest-exploration diagnostics JSON attachment/HTML dump,
# same idea and same values as swaper_monitor.py's equivalents.
MAX_INVEST_EXPLORATION_CHARS = 500_000
MAX_LISTING_PAGE_HTML_CHARS = 200_000


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


def fetch_active_loans_for_lender(config: dict) -> list:
    """Same public API as fetch_active_loans(), but for a single lender
    with its own minInterestRate/minRemainingTermInDays cutoffs (one entry
    of LENDER_INVEST_FILTERS) - used by the invest-exploration capture
    below to check availability exactly like the user's own filtered
    listing URLs (see module docstring, added 2026-07-23)."""
    params = [
        ("minInterestRate", str(config["min_interest_rate"])),
        ("minRemainingTermInDays", str(config["min_remaining_term_in_days"])),
        ("maxRemainingTermInDays", str(config["max_remaining_term_in_days"])),
        ("regulationStatus", config["regulation_status"]),
        ("lenders[]", config["lender_id"]),
    ]
    url = f"{LOANS_API_URL}?{parse.urlencode(params)}"

    req = request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read())
    except error.HTTPError as exc:
        log.error("Lendermarket API returned HTTP %s while checking lender availability.", exc.code)
        return []
    except Exception:
        log.exception("Failed to fetch active loans for a specific lender.")
        return []

    return payload.get("data") or []


def _match_lender_filter(sheet_name: str, filters: dict) -> str | None:
    """Match a lender name as written in the Google Sheet against
    LENDER_INVEST_FILTERS' keys - same exact/substring, case-insensitive
    matching idea as peerberry_invest_bot._match_selected_originator(), in
    case spelling isn't 100% identical between the sheet and this file.
    Returns the matching filter key, or None if none matched."""
    value = sheet_name.strip().lower()
    if not value:
        return None
    for key in filters:
        if key.strip().lower() == value:
            return key
    for key in filters:
        key_lower = key.strip().lower()
        if key_lower in value or value in key_lower:
            return key
    return None


def _record_lendermarket_api_response(captured: list, response) -> None:
    """`page.on("response", ...)` handler used by
    capture_lendermarket_invest_exploration() to passively capture
    Lendermarket's own API traffic while Playwright browses a filtered
    listing page - see that function's docstring. Belt-and-braces skips
    anything login/auth/password/totp-related in the URL too (same
    precaution as swaper_monitor.py's equivalent), even though nothing
    login-related should ever happen at this point in the flow.
    """
    url = response.url
    if "lendermarket.com" not in url:
        return
    lower_url = url.lower()
    if any(keyword in lower_url for keyword in ("login", "auth", "password", "totp")):
        return
    entry = {"method": response.request.method, "url": url, "status": response.status}
    try:
        entry["body"] = response.text()[:20000]
    except Exception:
        entry["body"] = None
    captured.append(entry)


def capture_lendermarket_invest_exploration(lenders_with_loans: list) -> dict | None:
    """Best-effort Playwright-based capture of the invest-structure
    diagnostics for the Google-Sheet-selected Lendermarket lenders that
    currently have newly-available loans - see module docstring for the
    full rationale/safety boundary (mirrors swaper_monitor.py's own
    exploration: NO invest/confirm button is ever clicked here either).

    `lenders_with_loans`: list of (lender_name, loans) tuples, `lender_name`
    being a key of LENDER_INVEST_FILTERS.

    UNVERIFIED cookie-sharing assumption (see module docstring): reuses the
    already-working pure-HTTP login() and injects those same session
    cookies into a fresh Playwright browser context, assuming they're set
    on the shared `.lendermarket.com` parent domain rather than scoped to
    api.lendermarket.com only. If that assumption is wrong, the listing
    page will redirect to /login instead of showing the real content -
    detected here and reported per-lender as
    `frontend_session_established: false` rather than failing loudly.

    Returns the diagnostics dict, or None if the HTTP login itself failed
    (nothing to capture in that case).
    """
    try:
        session = requests.Session()
        investor_id = login(session)
    except Exception:
        log.exception("Invest-exploration: failed to log in via HTTP, skipping capture.")
        return None

    cookies_for_playwright = [
        {
            "name": cookie.name,
            "value": cookie.value,
            "domain": cookie.domain,
            "path": cookie.path or "/",
        }
        for cookie in session.cookies
    ]

    diagnostics = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "investor_id": investor_id,
        "lenders": {},
    }

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(locale="fr-FR")
            if cookies_for_playwright:
                context.add_cookies(cookies_for_playwright)
            page = context.new_page()

            for lender_name, loans in lenders_with_loans:
                config = LENDER_INVEST_FILTERS[lender_name]
                captured_calls = []
                handler = lambda response: _record_lendermarket_api_response(captured_calls, response)
                page.on("response", handler)

                frontend_logged_in = False
                listing_html = None
                try:
                    page.goto(config["page_url"], wait_until="domcontentloaded", timeout=30000)
                    frontend_logged_in = "/login" not in page.url
                    try:
                        listing_html = page.evaluate(
                            "() => (document.querySelector('main') || document.body).outerHTML"
                        )
                    except Exception:
                        log.exception("Could not capture the listing page HTML for lender '%s'.", lender_name)
                except Exception:
                    log.exception("Failed to navigate to the listing page for lender '%s'.", lender_name)
                finally:
                    page.remove_listener("response", handler)

                if listing_html and len(listing_html) > MAX_LISTING_PAGE_HTML_CHARS:
                    listing_html = listing_html[:MAX_LISTING_PAGE_HTML_CHARS] + "\n... (truncated)"

                diagnostics["lenders"][lender_name] = {
                    "loans_count": len(loans),
                    "loans_api_payload": loans,
                    "listing_page_url": config["page_url"],
                    "frontend_session_established": frontend_logged_in,
                    "captured_api_calls": captured_calls,
                    "listing_page_html": listing_html,
                }

            browser.close()
    except Exception:
        log.exception("Invest-exploration: Playwright capture failed.")

    return diagnostics


def _xsrf_headers(session: requests.Session, investor_id: str | None = None) -> dict:
    headers = dict(_HEADERS)
    xsrf = session.cookies.get("XSRF-TOKEN")
    if xsrf:
        headers["x-xsrf-token"] = unquote(xsrf)
    if investor_id:
        headers["X-INVESTOR-ID"] = investor_id
    return headers


def login(session: requests.Session) -> str:
    """Log into Lendermarket via pure HTTP (email/password + TOTP 2FA if
    enabled) and return the authenticated `investorId`, needed as an
    `X-INVESTOR-ID` header on every subsequent authenticated call.

    See the module docstring for the full CSRF/XSRF + TOTP flow, verified
    2026-07-18 via a real browser network capture.
    """
    if not LENDERMARKET_EMAIL or not LENDERMARKET_PASSWORD:
        raise RuntimeError("LENDERMARKET_EMAIL/LENDERMARKET_PASSWORD environment variables are required.")

    r = session.get(CSRF_URL, headers=_HEADERS, timeout=20)
    r.raise_for_status()

    r = session.post(
        LOGIN_URL,
        json={"email": LENDERMARKET_EMAIL, "password": LENDERMARKET_PASSWORD},
        headers=_xsrf_headers(session),
        timeout=20,
    )
    r.raise_for_status()
    data = r.json().get("data") or {}
    mandatory_steps = (data.get("person") or {}).get("mandatorySteps") or []
    needs_totp = any(step.get("stepName") == "TOTP_CHALLENGE" for step in mandatory_steps)

    if needs_totp:
        if not LENDERMARKET_TOTP_SECRET:
            raise RuntimeError(
                "Lendermarket is asking for a 2FA code but LENDERMARKET_TOTP_SECRET is not set. "
                "Set it to the base32 secret used to configure Google Authenticator."
            )
        log.info("2FA prompt detected, generating and submitting TOTP code...")
        totp = pyotp.TOTP(LENDERMARKET_TOTP_SECRET)
        # Guard against submitting a code right as its 30s window is about
        # to roll over - the network round-trip can push the server-side
        # check past the boundary and get rejected as "Invalid code
        # provided" even though the code was valid when generated.
        remaining = 30 - (int(time.time()) % 30)
        if remaining < 5:
            time.sleep(remaining + 1)
        code = totp.now()
        r = session.post(TOTP_URL, json={"code": code}, headers=_xsrf_headers(session), timeout=20)
        if r.status_code == 422:
            log.info("TOTP code rejected (likely rolled over), retrying once with a fresh code.")
            code = totp.now()
            r = session.post(TOTP_URL, json={"code": code}, headers=_xsrf_headers(session), timeout=20)
        r.raise_for_status()
        data = r.json().get("data") or {}

    investor_id = (data.get("currentInvestor") or {}).get("investorId")
    if not investor_id:
        raise RuntimeError("Lendermarket login succeeded but no investorId was returned.")
    log.info("Logged in successfully, investorId=%s", investor_id)
    return investor_id


def fetch_account_balance(session: requests.Session, investor_id: str) -> float | None:
    """Fetch the investor's available balance (EUR)."""
    try:
        r = session.get(
            BALANCE_API_URL,
            params={"currency": "EUR"},
            headers=_xsrf_headers(session, investor_id),
            timeout=20,
        )
        r.raise_for_status()
    except Exception:
        log.exception("Failed to fetch the Lendermarket account balance.")
        return None

    data = r.json().get("data") or {}
    balance = data.get("investorAvailableBalanceAmount")
    try:
        return float(balance)
    except (TypeError, ValueError):
        return None


def fetch_balance_via_login() -> float | None:
    if not LENDERMARKET_EMAIL or not LENDERMARKET_PASSWORD:
        log.warning("LENDERMARKET_EMAIL/PASSWORD not set, skipping account balance lookup.")
        return None

    session = requests.Session()
    try:
        investor_id = login(session)
        return fetch_account_balance(session, investor_id)
    except Exception:
        log.exception("Failed to log into Lendermarket to fetch the account balance.")
        return None


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

    # Invest-structure exploration (added 2026-07-23) - see module
    # docstring. Independent of the segment-level notification above: uses
    # its own per-lender filters/notification gates (namespaced
    # "lender_invest_<name>" so they never collide with the segment gates).
    try:
        selected_lender_names = get_selected_lendermarket_lenders()
    except Exception:
        log.exception("Could not read selected Lendermarket lenders from the Google Sheet, skipping invest-exploration.")
        selected_lender_names = []

    lenders_needing_exploration = []
    for sheet_name in selected_lender_names:
        filter_key = _match_lender_filter(sheet_name, LENDER_INVEST_FILTERS)
        if filter_key is None:
            log.warning(
                "Selected Lendermarket lender '%s' from the Google Sheet doesn't match any known filter config, skipping.",
                sheet_name,
            )
            continue

        config = LENDER_INVEST_FILTERS[filter_key]
        loans = fetch_active_loans_for_lender(config)
        count = len(loans)
        log.info("Lender '%s' (invest-exploration filters): %d loan(s) currently available.", filter_key, count)

        gate_key = f"lender_invest_{filter_key}"
        send_flag, was_reset = should_notify(gates, gate_key, count > 0)
        if was_reset:
            log.info("Lender '%s': resetting invest-exploration notification gate.", filter_key)

        if send_flag:
            lenders_needing_exploration.append((filter_key, loans))

    if lenders_needing_exploration:
        log.info(
            "Capturing Lendermarket invest-structure exploration diagnostics for: %s",
            [name for name, _ in lenders_needing_exploration],
        )
        diagnostics = capture_lendermarket_invest_exploration(lenders_needing_exploration)
        if diagnostics:
            invest_exploration_text = json.dumps(diagnostics, indent=2, ensure_ascii=False, default=str)
            if len(invest_exploration_text) > MAX_INVEST_EXPLORATION_CHARS:
                invest_exploration_text = invest_exploration_text[:MAX_INVEST_EXPLORATION_CHARS] + "\n... (truncated)"
            send_lendermarket_invest_exploration_email(len(lenders_needing_exploration), invest_exploration_text)

    save_state(STATE_FILE, state)


if __name__ == "__main__":
    run()
