"""Swaper loan monitor.

Logs into swaper.com with Playwright, fetches the available loans and the
uninvested account balance via the site's own API (using the authenticated
browser session so cookies are sent automatically), and sends an email
notification only once per positive-balance cycle: when balance is > 0 and
at least one manual loan is available. No further notifications are sent while
balance stays > 0 at the same amount. The notification gate is reset when
balance drops back to 0 or when the balance amount changes.

Required env vars:
    SWAPER_EMAIL, SWAPER_PASSWORD          -> Swaper account credentials
    SMTP_HOST, SMTP_USER, SMTP_PASSWORD,   -> outgoing mail server
    EMAIL_TO                               -> notification recipient
Optional:
    SMTP_PORT (default 587), EMAIL_FROM (default SMTP_USER)
    SWAPER_TOTP_SECRET                     -> base32 secret used to set up
                                               Google Authenticator, needed
                                               if 2FA is enabled on the account

REAL auto-invest bot (added 2026-07-25, explicit user decision - real money,
no more click-and-abort safety net): Swaper's manual loan inventory is
extremely transient (a single loan can appear and be grabbed by another
investor within minutes), so there's no reliable way to interactively catch
one via a one-off manual script - instead this run() is ALREADY re-executed
periodically by cron-job.org (see shared/cron_schedule.py) triggering the
GitHub Actions workflow, so whenever loans happen to be available AND the
account balance is >= MIN_INVESTMENT_AMOUNT on a given run,
`_invest_available_loans()` is called right there, in the same
already-logged-in Playwright session: for each available loan (in listing
order), it fills the row's amount input with min(money left, loan's own
amount) and clicks its real "+" icon - a REAL click that really reaches
Swaper's server (nothing is blocked/aborted anymore). The real request AND
response of each investment call are passively captured by the already-
registered `_record_api_response()` listener. A summary
(shared.notifier.send_swaper_investment_summary_email()) is emailed every
time at least one investment was attempted (not one-time - real money moves
every time). If an unrecognized confirmation modal ever appears after a
click (never observed yet on Swaper, unlike PeerBerry), investing stops
immediately rather than guessing which button to click with real money on
the line, and the modal's HTML is included in the summary for manual review.

Per-originator filtering + budget split (added 2026-07-25, same day, per
explicit user request - mirrors the PeerBerry/Lendermarket "x" flag
convention in the Google Sheet): instead of investing into the aggregate/
unfiltered loan list, only the loan originators flagged with "x" in the
Google Sheet (shared.google_sheet.get_selected_swaper_loan_originators(),
reading the block between the "Swaper" and "Crowdlending savings" cells)
are considered. Each selected originator is checked individually for
CURRENT loan availability via `fetch_loans_for_originator()`, which uses
swaper.com's own "Loan originators" filter - NOT by driving the site's
custom JS multiselect dropdown widget (reverse-engineering its click/
toggle behaviour turned out to be unreliable - see repo memory), but by
intercepting the page's own outgoing `/rest/public/loans` request via
`page.route()` and rewriting its JSON body to add
`"groups": [originator_name]` before letting it through unmodified
otherwise (confirmed via real DevTools captures, provided directly by the
user, that the endpoint accepts the originator's plain display name
directly - not an opaque id). The available balance is then split EVENLY
across only the originators that currently have >=1 loan available (see
`_split_budget_across_available_originators()`: 1 originator with loans ->
gets the full balance, 2 -> 50/50 each, etc. - per explicit user spec), and
`_invest_available_loans()` is called once per such originator with its
own budget cap.

Per-loan share computation, no sub-minimum leftover (added 2026-07-25,
same day, explicit user request - "je ne veux pas de reste < 10e"):
within one originator's own budget, loans are no longer filled greedily
in listing order (which could strand an unusable remainder below
MIN_INVESTMENT_AMOUNT once the last loan couldn't fully absorb what was
left). `_compute_swaper_loan_shares()` (exact same algorithm as
monitors/lendermarket_monitor.py's `_compute_loan_shares()`) instead
splits an originator's budget EQUALLY across its own currently available
loans, capping any loan whose own `amount` is smaller than its equal
share (redistributing the excess across the other loans in the same
pass), and drops loans from consideration if the equal share would fall
below the minimum (funding fewer loans instead, each still getting at
least the minimum) - so a budget only goes unspent within one originator
if that originator's own loans genuinely can't absorb it all (originators
remain fully independent of each other, no cross-originator
redistribution, matching the same accepted design already used for
Lendermarket's lenders). `_invest_available_loans()` now takes this
precomputed `{loan_id: amount}` share map directly and targets each
loan's specific row by its visible `number` (rather than always
`rows.first`, which assumed - only true for the old greedy-fill logic -
that an invested loan's row always disappears/reorders predictably;
under equal-split most loans are only PARTIALLY invested and stay
visible, so the specific row must be targeted precisely).

The earlier one-time "invest-structure exploration" capture (HTML dump of
the loans page + a dedicated one-time email, added 2026-07-23 to help
figure out how to build the invest bot before it existed) was removed on
2026-07-25 once the real bot above was built and confirmed working - it
ended up implemented via real Playwright UI clicks, not a reconstructed
HTTP call, so that exploration data was no longer needed. The passive
`/rest/` API call capture (`_record_api_response()`/`captured_api_calls`)
is KEPT - it now only feeds the real investment summary email, to verify
each real investment call actually succeeded.

Balance-independent per-originator availability check (added 2026-07-26,
explicit user request - "j'ai pas besoin d'attendre d'avoir des sous sur
mon compte pour te donner tout ce dont tu auras besoin"): each selected
originator's current loan availability is now checked via
`fetch_loans_for_originator()` EVERY run regardless of the account
balance - only the actual investing step (budget split + real clicks)
stays gated behind `balance_now >= MIN_INVESTMENT_AMOUNT`. This means the
passive `/rest/` capture above always has loans-listing/filter API calls
to show, even on a run with nothing to invest. A diagnostics email
(`shared.notifier.send_swaper_api_structure_email()`) ships that captured
structure without waiting for a real investment, gated on there being at
least one loan available for a selected originator AND balance >=
MIN_INVESTMENT_AMOUNT (2026-07-26 follow-up requests) - it's skipped if a
real investment summary email was sent instead this run (a strict
superset). No anti-spam cooldown (removed 2026-07-26, explicit user
request) - it's sent every run those conditions hold. The real
invest-call structure itself still can't be observed without a real
investment actually happening (real money moving, per the 2026-07-25
decision to drop click-and-abort captures).

Sheet-driven minInterestRate + per-country cap (added 2026-07-31, mirrors
the same feature already built for Lendermarket): `shared.google_sheet.
get_swaper_min_interest_rate()`/`get_swaper_country_allocations()` read
the same cell layout convention as PeerBerry/Lendermarket (cell 1 column
left of "Swaper" itself = minInterestRate, 2 columns left = per-country
cap threshold %). Unlike PeerBerry/Lendermarket, Swaper's
`/rest/public/loans` endpoint has no known/verified server-side
interest-rate filter param, so `_filter_loans_by_min_interest_rate()`
applies it client-side on each fetched loan's `interestRatePerYear`
instead. The per-country cap is checked ONCE per run (not continuously
re-polled, same one-shot design as Lendermarket's): an originator whose
mapped country is already at/above `threshold_percentage`% of the total
Swaper budget (balance + every country's already-invested amount) is
excluded entirely from this run's budget split, tracked in
`country_blocked_originators` and shown in
`send_swaper_investment_summary_email()`'s body alongside the
minInterestRate used and a per-country invested/threshold breakdown. Both
Sheet reads are soft-fail (fall back to the module default / disable
country blocking on a read error), same pattern as every other soft-fail
Sheet read in this repo.
"""


import json
import os
import random
import sys
import time
import logging
from datetime import datetime, timezone
from pathlib import Path

import pyotp
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from shared.notifier import send_swaper_email, send_swaper_investment_summary_email, send_swaper_api_structure_email
from shared.state import load_state, save_state
from shared.cron_schedule import ensure_schedule
from shared.notification_gate import should_notify
from shared.google_sheet import (
    get_selected_swaper_loan_originators,
    get_swaper_min_interest_rate,
    get_swaper_country_allocations,
)
from shared.browser_stealth import get_context_options, apply_stealth, human_pause, human_mouse_wander, human_type

DEFAULT_STATE = {
    "gates": {},
}

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("swaper_monitor")

LOGIN_URL = "https://swaper.com/en/login"
STATE_FILE = Path(__file__).parent / "swaper_state.json"
STORAGE_STATE_FILE = Path(__file__).parent / "swaper_storage_state.json"
CRON_SCHEDULE_STATE_FILE = Path(__file__).parent / "cron_schedule_state.json"

SWAPER_EMAIL = os.environ.get("SWAPER_EMAIL")
SWAPER_PASSWORD = os.environ.get("SWAPER_PASSWORD")
SWAPER_TOTP_SECRET = os.environ.get("SWAPER_TOTP_SECRET")
SWAPER_CRON_JOB_ID = os.environ.get("SWAPER_CRON_JOB_ID")

# Minimum amount (EUR) Swaper's manual loans accept per investment - below
# this, don't even attempt a click (mirrors MIN_INVESTMENT_AMOUNT in
# monitors/peerberry_invest_bot.py).
MIN_INVESTMENT_AMOUNT = float(os.environ.get("MIN_INVESTMENT_AMOUNT", "10"))

# Fallback used only if get_swaper_min_interest_rate() (reads the cell just
# left of "Swaper" in "Répartition géographique", see that function's
# docstring) fails - default 0 preserves the pre-2026-07-31 behavior (no
# interest-rate filtering at all) instead of silently excluding every loan
# on a read error.
MIN_INTEREST_RATE = float(os.environ.get("SWAPER_MIN_INTEREST_RATE", "0"))



def handle_two_factor(page) -> None:
    """If Swaper prompts for a Google Authenticator code after submitting
    credentials, generate a fresh TOTP code from SWAPER_TOTP_SECRET (the same
    base32 secret that was scanned into the authenticator app) and submit it.

    Verified against the real 2FA screen on 2026-07-06: the code input is
    `input[name='code']`, there's a "Trust this browser and skip 2FA for 30
    days" checkbox (ticked here so the persisted session, see
    STORAGE_STATE_FILE, avoids repeating 2FA on the next scheduled runs), and
    the same "Log In" div is reused to submit the code.
    """
    otp_input = page.locator("input[name='code']")
    try:
        otp_input.wait_for(timeout=8000)
    except PlaywrightTimeoutError:
        return  # no 2FA prompt shown, nothing to do

    if not SWAPER_TOTP_SECRET:
        raise RuntimeError(
            "Swaper is asking for a 2FA code but SWAPER_TOTP_SECRET is not set. "
            "Set it to the base32 secret used to configure Google Authenticator."
        )

    log.info("2FA prompt detected, generating and submitting TOTP code...")
    totp = pyotp.TOTP(SWAPER_TOTP_SECRET)
    code = totp.now()
    human_type(otp_input, code)
    human_pause()

    try:
        page.get_by_text("Trust this browser").click(timeout=3000)
    except PlaywrightTimeoutError:
        log.warning("Could not find the 'trust this browser' checkbox, continuing anyway.")

    # Guard against the 30s TOTP window rolling over while we were typing
    # the code / clicking "trust this browser" - suspected cause of a
    # GitHub Actions failure on 2026-07-09 where the whole 2FA sequence
    # completed without any Playwright error, but the page never left
    # /login (i.e. the code was silently rejected as stale). If a
    # freshly-generated code differs from what's currently filled in,
    # clear the field and retype it right before submitting.
    fresh_code = totp.now()
    if fresh_code != code:
        log.info("TOTP code rolled over to a new 30s window while typing, retyping the fresh code.")
        otp_input.fill("")
        human_type(otp_input, fresh_code)
        human_pause()

    page.locator("div.button.clickable", has_text="Log In").click()


def login(page) -> None:
    """Log in to Swaper using the credentials (and TOTP secret, if 2FA is
    enabled) from env vars.

    Selectors verified against the real login form on 2026-07-06: the email
    and password inputs have stable `name` attributes, but the submit control
    is a styled `<div class="button clickable ... disabled">` (not a real
    `<button>`), which loses its `disabled` class once both fields are filled.
    """
    log.info("Navigating to login page...")
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    human_mouse_wander(page)

    # Dismiss the Cookiebot consent banner if it shows up.
    try:
        page.get_by_role("button", name="Allow all cookies").click(timeout=5000)
    except PlaywrightTimeoutError:
        pass

    # If a previous session was restored (see STORAGE_STATE_FILE) and is still
    # valid, Swaper redirects away from /login immediately - nothing else to do.
    if "/login" not in page.url:
        log.info("Reused a previous session, already logged in at %s", page.url)
        return

    log.info("Filling in credentials...")
    human_type(page.locator("input[name='email']"), SWAPER_EMAIL)
    human_pause()
    human_type(page.locator("input[name='password']"), SWAPER_PASSWORD)
    human_pause()
    page.locator("div.button.clickable", has_text="Log In").click()

    handle_two_factor(page)

    # Wait until we've left the login page (successful redirect to dashboard).
    page.wait_for_url(lambda url: "/login" not in url, timeout=20000)
    log.info("Logged in successfully, current URL: %s", page.url)


def fetch_loans(page) -> dict:
    """Fetch the loans list by navigating to the real /en/loans page and
    capturing its own API call, so the CSRF token (`x-xsrf-token` header,
    derived from a cookie) and `referer` are exactly what the API expects -
    a manually reconstructed fetch() call gets rejected with 403 otherwise.

    Verified against a real account on 2026-07-06: the page issues
    `POST /rest/public/loans` with body
    `{"page": 1, "pageSize": 13, "groups": [], "amountFrom": null,
    "amountTo": null, "status": null}` and returns
    `{accountBalance, totalRecords, page, results}`. This only fetches the
    first page (13 items) - fine for "is anything newly available" style
    monitoring, but increase pageSize / paginate if you need the full list.
    """
    log.info("Navigating to the loans page and capturing the loans API response...")
    with page.expect_response(
        lambda r: r.url.endswith("/rest/public/loans") and r.request.method == "POST"
    ) as response_info:
        page.goto("https://swaper.com/en/loans", wait_until="domcontentloaded")
    response = response_info.value
    if not response.ok:
        raise RuntimeError(f"Loans API returned status {response.status}")
    human_mouse_wander(page)
    return response.json()


def extract_loans(payload) -> list:
    """Normalize the API response into a flat list of loan dicts.

    Verified response shape: {"accountBalance": ..., "totalRecords": ...,
    "page": ..., "results": [{"id": ..., "number": ..., "status": ...,
    "amount": ..., "interestRatePerYear": ..., "term": {...}, ...}, ...]}
    """
    if isinstance(payload, dict):
        items = payload.get("results") or []
    elif isinstance(payload, list):
        items = payload
    else:
        items = []

    return [item for item in items if item.get("id") is not None or item.get("loanId") is not None]


def extract_balance(payload) -> float:
    """Extract the uninvested account balance (EUR) from the API response."""
    if isinstance(payload, dict):
        balance = payload.get("accountBalance")
        if isinstance(balance, (int, float)):
            return float(balance)
    return 0.0


def fetch_loans_for_originator(page, originator_name: str) -> dict:
    """Fetch loans filtered to a single loan originator, without driving
    Swaper's custom JS "Loan originators" multiselect dropdown (a fragile
    custom widget - reverse-engineering its click/toggle behaviour turned
    out to be unreliable). Confirmed instead via real DevTools captures
    (2026-07-25, provided directly by the user) that `POST
    /rest/public/loans`'s body simply takes the originator's exact display
    name as a plain string in its `"groups"` array - e.g.
    `{"groups": ["Wandoo Finance Group"]}` or `{"groups": ["SW Finance"]}`
    - not an opaque id, so no name->id mapping is needed at all.

    Instead of reconstructing the request from scratch (which fetch_loans()'s
    docstring already documented gets rejected with 403 - the x-xsrf-token
    header/cookie and referer must be exactly what the real app computes),
    this intercepts the page's OWN outgoing request (fired by a normal
    `page.goto` to the loans page) via `page.route()` and rewrites only its
    JSON body to add the `groups` filter, then forwards it unmodified
    otherwise - so every header the app itself computed (csrf token,
    referer, cookies, ...) is preserved exactly. Because this is the
    Angular app's own request/response cycle (just with a patched body),
    the app renders the FILTERED results in the page itself afterwards, so
    the visible `tr.loan-row` rows on the page match this originator right
    after this call returns - no separate UI filtering step needed before
    investing.
    """
    def _rewrite_body(route):
        request = route.request
        try:
            body = json.loads(request.post_data or "{}")
        except ValueError:
            body = {}
        body["groups"] = [originator_name]
        route.continue_(post_data=json.dumps(body))

    page.route("**/rest/public/loans", _rewrite_body)
    try:
        with page.expect_response(
            lambda r: r.url.endswith("/rest/public/loans") and r.request.method == "POST"
        ) as response_info:
            page.goto("https://swaper.com/en/loans", wait_until="domcontentloaded")
        response = response_info.value
        if not response.ok:
            raise RuntimeError(f"Loans API returned status {response.status} for originator {originator_name!r}")
        human_mouse_wander(page)
        return response.json()
    finally:
        page.unroute("**/rest/public/loans", _rewrite_body)


def _filter_loans_by_min_interest_rate(loans: list, min_interest_rate: float) -> list:
    """Client-side minimum-interest-rate filter (added 2026-07-31, from
    get_swaper_min_interest_rate() - same Sheet cell convention as
    PeerBerry's/Lendermarket's own minInterestRate). Unlike those two
    platforms, Swaper's `/rest/public/loans` endpoint has no known/verified
    server-side interest-rate filter parameter (only `groups`, used by
    `fetch_loans_for_originator()`, is confirmed) - so this filters the
    already-fetched loans locally on their own `interestRatePerYear` field
    instead of adding an unverified request param. A `min_interest_rate` of
    0 (or falsy) is a no-op, returning `loans` unchanged.
    """
    if not min_interest_rate:
        return loans
    kept = []
    for loan in loans:
        try:
            rate = float(loan.get("interestRatePerYear") or 0)
        except (TypeError, ValueError):
            rate = 0.0
        if rate >= min_interest_rate:
            kept.append(loan)
    return kept


def _split_budget_across_available_originators(available_money: float, originator_loans: dict) -> dict:
    """Split `available_money` EVENLY across the loan originators that
    currently have at least one loan available - per explicit user spec
    (2026-07-25): "si un seul loan on met tout sur lui mais si deux loans
    on divise par 2 sur les deux loans" (1 originator with loans -> gets
    the full balance, 2 -> 50/50 each, etc.). Originators with no loans
    currently available (empty list) get nothing and aren't counted in the
    split.

    `originator_loans`: {originator_name: [loan, ...]} - only originators
    with a non-empty loan list are considered.

    Returns {originator_name: budget} for originators that should be
    invested into this run.
    """
    available_originators = [name for name, loans in originator_loans.items() if loans]
    if not available_originators:
        return {}
    share = round(available_money / len(available_originators), 2)
    return {name: share for name in available_originators}


def _redact_sensitive_headers(headers: dict) -> dict:
    """Return a copy of `headers` with session/auth values (cookies,
    bearer/auth tokens, the CSRF token) replaced by a placeholder -
    everything else (content-type, custom API headers, etc.) needed to
    replicate a call is kept as-is, INCLUDING THE HEADER NAMES of the
    redacted ones (e.g. seeing `x-xsrf-token` is present, just not its live
    value, is exactly what's needed to know a future pure-HTTP bot must
    derive that header from a same-named cookie - see fetch_loans()'s
    docstring). Avoids putting live session credentials into an emailed
    diagnostics attachment; the shape of the request (headers present,
    body) is what matters for building the bot later, not the live token.
    """
    redacted = {}
    for name, value in headers.items():
        if name.lower() in ("cookie", "authorization", "set-cookie", "x-xsrf-token"):
            redacted[name] = "[REDACTED - sensitive session/auth value, not needed to see the request shape]"
        else:
            redacted[name] = value
    return redacted


def _record_api_response(captured: list, response) -> None:
    """`page.on("response", ...)` handler used by run() to passively capture
    Swaper's own `/rest/` API traffic while browsing the loans page - feeds
    the investment summary email's attachment (see
    `send_swaper_investment_summary_email()`), which is meant to carry
    everything needed to later attempt building a pure-HTTP-request-based
    bot (mirroring monitors/lendermarket_monitor.py's `requests.Session`
    approach) instead of driving a real browser: for every captured call,
    both the request (method, full url, ALL header NAMES - values redacted
    for cookies/auth/csrf, see `_redact_sensitive_headers()` - and the raw
    POST body) and the response (status, ALL header NAMES same redaction,
    and the raw body) are kept. Only ever registered AFTER login() has
    fully completed; also explicitly skips anything login/auth-related as
    a second safety layer (belt-and-braces, per the repo's security lesson
    about page.route()/page.on() interceptors and credentials) even though
    that should never happen at this point in the flow - the login/2FA
    request shape itself is deliberately NEVER captured this way (a future
    pure-HTTP bot would still need `login()`/`handle_two_factor()`'s
    already-documented browser-based flow, or its own separate careful
    capture - not silently piggy-backed onto this listener).
    """
    url = response.url
    if "swaper.com" not in url or "/rest/" not in url:
        return
    lower_url = url.lower()
    if any(keyword in lower_url for keyword in ("login", "auth", "password")):
        return
    request = response.request
    entry = {
        "method": request.method,
        "url": url,
        "request_headers": _redact_sensitive_headers(request.headers),
        "request_post_data": request.post_data,
        "status": response.status,
    }
    try:
        entry["response_headers"] = _redact_sensitive_headers(response.headers)
    except Exception:
        entry["response_headers"] = None
    try:
        entry["body"] = response.text()[:20000]
    except Exception:
        entry["body"] = None
    captured.append(entry)


def _compute_swaper_loan_shares(budget: float, loans: list, min_investment: float = MIN_INVESTMENT_AMOUNT) -> dict:
    """Split `budget` (one originator's own share of the account balance,
    see `_split_budget_across_available_originators()`) EQUALLY across
    `loans` (that same originator's currently available loans) - exact same
    algorithm as monitors/lendermarket_monitor.py's `_compute_loan_shares()`,
    per explicit user request 2026-07-25: "je ne veux pas de reste < 10e".

    Two adjustments on top of a plain `budget / len(loans)`:
    - If the equal share would be below `min_investment`, fewer loans are
      funded instead (as many as `budget // min_investment` allows, kept in
      listing order) so every funded loan still gets at least
      `min_investment`.
    - If a loan's own `amount` is smaller than its equal share, that loan is
      capped at what it can actually take and the excess is redistributed
      across the other loans in the same pass (equal share recomputed on
      what's left) - repeated until stable.

    Returns `{loan_id: amount}` for every loan that ends up funded (amount
    rounded to 2 decimals); loans below `min_investment` after all
    adjustments are simply omitted (that leftover stays unspent for this
    originator this run - originators are fully independent of each other,
    no cross-originator redistribution).
    """
    caps = {}
    for loan in loans:
        loan_id = loan.get("id")
        if loan_id is None:
            continue
        try:
            cap = float(loan.get("amount") or 0)
        except (TypeError, ValueError):
            cap = 0.0
        if cap > 0:
            caps[loan_id] = cap

    # Keep insertion (listing) order for deterministic drop/keep decisions below.
    active = list(caps.keys())
    remaining = budget
    shares = {}

    while active:
        if remaining < min_investment:
            break

        equal_share = remaining / len(active)
        if equal_share < min_investment:
            max_active = int(remaining // min_investment)
            if max_active <= 0:
                break
            if max_active < len(active):
                active = active[:max_active]
            continue

        capped_any = False
        for loan_id in list(active):
            if caps[loan_id] < equal_share:
                shares[loan_id] = caps[loan_id]
                remaining -= caps[loan_id]
                active.remove(loan_id)
                capped_any = True
        if capped_any:
            continue

        for loan_id in active:
            shares[loan_id] = round(equal_share, 2)
        break

    return shares


def _invest_available_loans(page, loans: list, shares: dict) -> list:
    """Actually invest into available manual loans, using the exact
    per-loan amounts precomputed by `_compute_swaper_loan_shares()` (loans
    with no entry in `shares`, e.g. below the minimum, are skipped).

    REAL clicks, REAL money (2026-07-25, explicit user decision to make this
    the production invest bot rather than only a safe click-and-abort
    capture - see repo memory). For each loan with a computed share: fills
    its amount input with that exact amount and clicks its "+" icon.
    Nothing here blocks/intercepts the request - the real HTTP call really
    goes to Swaper's server, and is passively captured (request+response,
    not blocked) by the already-registered `_record_api_response()`
    listener, so its outcome can be reviewed in the summary email.

    Each loan's row is targeted by its visible `number` text (NOT always
    `rows.first`) since a loan given only a PARTIAL share (share < loan's
    own full amount, the normal case under equal-split) stays visible on
    the page afterwards rather than disappearing/reordering.

    Never observed a confirmation modal on Swaper yet (unlike PeerBerry,
    which needed one) - but if one appears after a click, stops attempting
    further loans immediately and reports the modal's HTML rather than
    guessing which button to click with real money on the line. Also stops
    on any exception while interacting with a row.

    Returns a list of attempt dicts: {loan_id, loan_number, amount,
    modal_html, error}.
    """
    attempts = []
    for loan in loans:
        loan_id = loan.get("id")
        amount = shares.get(loan_id)
        loan_label = loan.get("number") or loan_id
        if not amount or amount < MIN_INVESTMENT_AMOUNT:
            continue

        row = page.locator("tr.loan-row", has_text=str(loan_label))
        if row.count() == 0:
            log.warning("Could not find the row for loan %s on the page - skipping it.", loan_label)
            continue
        row = row.first

        log.info("Investing %.2f EUR into loan %s (REAL money, real click).", amount, loan_label)
        try:
            amount_input = row.locator(".field.currency input").first
            amount_input.click()
            amount_input.fill(f"{amount:.2f}")
            row.locator(".icon-plus").first.click(timeout=10000)
            page.wait_for_timeout(3000)  # let the real request/response complete
        except Exception:
            log.exception("Exception while attempting to invest in loan %s - stopping.", loan_label)
            attempts.append({"loan_id": loan.get("id"), "loan_number": loan.get("number"), "amount": amount, "error": True})
            break

        modal_html = None
        try:
            modal_html = page.evaluate(
                "() => { const m = document.querySelector('.modal, [class*=modal], [role=dialog]'); "
                "return m && m.offsetParent !== null ? m.outerHTML : null; }"
            )
        except Exception:
            pass

        attempts.append({
            "loan_id": loan.get("id"),
            "loan_number": loan.get("number"),
            "amount": amount,
            "modal_html": modal_html,
        })

        if modal_html:
            log.warning(
                "A confirmation modal appeared after investing in loan %s - stopping here "
                "(unrecognized extra step, needs manual review) rather than guessing which button to click.",
                loan_label,
            )
            break

    return attempts


def run(headless: bool = True) -> None:
    if not SWAPER_EMAIL or not SWAPER_PASSWORD:
        log.error("SWAPER_EMAIL and SWAPER_PASSWORD environment variables are required.")
        sys.exit(1)

    state = load_state(STATE_FILE, DEFAULT_STATE)
    gates = state.setdefault("gates", {})

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
            ],
        )
        storage_state = str(STORAGE_STATE_FILE) if STORAGE_STATE_FILE.exists() else None
        context = browser.new_context(
            storage_state=storage_state,
            locale="en-US",
            timezone_id="Europe/Paris",
            **get_context_options(),
        )
        apply_stealth(context)
        page = context.new_page()

        captured_api_calls = []
        investment_attempts = []

        try:
            login(page)
            # Registered only AFTER login() has fully completed (see
            # _record_api_response()'s docstring) - passively captures the
            # /rest/ API traffic fired while fetch_loans() navigates to the
            # loans page (and later, any real investment calls - see below),
            # for the investment summary email.
            page.on("response", lambda response: _record_api_response(captured_api_calls, response))
            payload = fetch_loans(page)

            # REAL investment attempt (2026-07-25, explicit user decision -
            # see module docstring). Per-originator investing (added later
            # the same day, per explicit user request): rather than
            # investing into the aggregate/unfiltered loan list, only the
            # loan originators flagged with "x" in the Google Sheet (see
            # shared.google_sheet.get_selected_swaper_loan_originators(),
            # mirroring the PeerBerry/Lendermarket sheet convention) are
            # considered, and each is checked individually for CURRENT loan
            # availability via fetch_loans_for_originator() (the site's
            # "Loan originators" filter, driven through the API's "groups"
            # field rather than the fragile custom JS dropdown widget - see
            # that function's docstring). The available balance is then
            # split EVENLY across only the originators that currently have
            # >=1 loan available (see
            # _split_budget_across_available_originators()'s docstring for
            # the exact rule), and _invest_available_loans() is called once
            # per such originator with its own budget cap - reusing the
            # existing real-click investing logic, just scoped to whichever
            # originator's rows are currently visible on the page (each
            # fetch_loans_for_originator() call leaves the page showing that
            # originator's filtered rows only, since it's the real Angular
            # app rendering the real - filtered - API response).
            loans_now = extract_loans(payload)
            balance_now = extract_balance(payload)

            try:
                selected_originators = get_selected_swaper_loan_originators()
            except Exception:
                log.exception("Could not read selected Swaper loan originators from the Google Sheet.")
                selected_originators = []

            # minInterestRate + per-country cap, read from the Sheet once per
            # run (added 2026-07-31, same convention/cell layout as
            # PeerBerry's/Lendermarket's own MIN_INTEREST_RATE/country
            # allocations, see get_swaper_min_interest_rate()/
            # get_swaper_country_allocations()) - both are soft-fail: a read
            # error just falls back to the module default / disables country
            # blocking for this run, rather than aborting.
            min_interest_rate = MIN_INTEREST_RATE
            try:
                min_interest_rate = get_swaper_min_interest_rate()
            except Exception:
                log.exception(
                    "Could not read the Swaper minInterestRate from the Google Sheet, falling back to the default (%s).",
                    MIN_INTEREST_RATE,
                )

            country_allocations = {}
            try:
                country_allocations = get_swaper_country_allocations()
            except Exception:
                log.exception("Could not read the Swaper per-country allocations from the Google Sheet, country blocking is disabled this run.")

            country_threshold_percentage = country_allocations.get("threshold_percentage")
            country_invested = dict(country_allocations.get("country_amounts") or {})
            originator_countries = country_allocations.get("originator_countries") or {}
            country_blocked_originators = []
            relevant_countries = {
                originator_countries[name] for name in selected_originators if name in originator_countries
            }

            def _is_country_blocked(country, total_budget):
                if not country or country_threshold_percentage is None or total_budget <= 0:
                    return False
                return country_invested.get(country, 0.0) >= (country_threshold_percentage / 100.0) * total_budget

            total_budget = balance_now + sum(country_invested.values())

            originator_loans = {}
            if not selected_originators:
                log.info("No Swaper loan originator is flagged with 'x' in the Google Sheet - skipping auto-invest.")
            else:
                # Availability is checked for every selected originator
                # REGARDLESS of balance (added 2026-07-26, explicit user
                # request: "j'ai pas besoin d'attendre d'avoir des sous sur
                # mon compte pour te donner tout ce dont tu auras besoin") -
                # this is what feeds the one-time API-structure diagnostics
                # email below even when there's nothing to actually invest
                # yet. Only the real investing step further below stays
                # gated behind the minimum balance. The minInterestRate is
                # applied client-side here too (see
                # _filter_loans_by_min_interest_rate()'s docstring).
                log.info(
                    "%d loan originator(s) selected in the Google Sheet (%s) - checking current "
                    "availability for each (min interest rate: %s%%).",
                    len(selected_originators), ", ".join(selected_originators), min_interest_rate,
                )
                for name in selected_originators:
                    try:
                        originator_payload = fetch_loans_for_originator(page, name)
                    except Exception:
                        log.exception("Failed to fetch filtered loans for originator %r - skipping it.", name)
                        continue
                    loans_for_name = _filter_loans_by_min_interest_rate(
                        extract_loans(originator_payload), min_interest_rate
                    )
                    if loans_for_name:
                        originator_loans[name] = loans_for_name
                        log.info("Originator %r currently has %d loan(s) available.", name, len(loans_for_name))
                    else:
                        log.info("Originator %r currently has no loans available.", name)

                if balance_now < MIN_INVESTMENT_AMOUNT:
                    log.info(
                        "Balance %.2f EUR is below the minimum (%.2f EUR) - availability was still "
                        "checked above for diagnostics, but skipping the actual auto-invest step.",
                        balance_now, MIN_INVESTMENT_AMOUNT,
                    )
                else:
                    # Per-country cap (added 2026-07-31, mirrors
                    # lendermarket_monitor.py's invest_selected_lenders() -
                    # checked ONCE per run, not continuously re-polled since
                    # this bot allocates budget in a single real-time pass
                    # each time it's triggered): any currently-available
                    # originator whose mapped country is already at/above
                    # `country_threshold_percentage`% of the total Swaper
                    # budget (balance + every country's already-invested
                    # amount) is excluded entirely from this run's budget
                    # split (same treatment as "0 loans available").
                    for name in list(originator_loans.keys()):
                        country = originator_countries.get(name)
                        if _is_country_blocked(country, total_budget):
                            log.info(
                                "Originator %r (country %r) is blocked this run: already at/above the %s%% country cap.",
                                name, country, country_threshold_percentage,
                            )
                            country_blocked_originators.append(name)
                            del originator_loans[name]

                    budgets = _split_budget_across_available_originators(balance_now, originator_loans)
                    for name, budget in budgets.items():
                        log.info("Investing up to %.2f EUR into originator %r's loan(s).", budget, name)
                        try:
                            # Re-fetch (not just re-apply the filter) right
                            # before investing - the discovery loop's loan
                            # list can already be stale by now since Swaper's
                            # manual inventory is extremely transient (a loan
                            # can be grabbed by someone else, or a new one can
                            # appear, within seconds - confirmed 2026-08-01:
                            # a loan seen as available during discovery was
                            # already gone a few seconds later). Using the
                            # fresh list here (instead of the discovery-time
                            # originator_loans[name]) avoids computing shares
                            # for/targeting a row that no longer exists, and
                            # correctly picks up any loan that appeared since.
                            refreshed_payload = fetch_loans_for_originator(page, name)
                        except Exception:
                            log.exception("Failed to re-apply the filter for originator %r before investing - skipping it.", name)
                            continue
                        current_loans = _filter_loans_by_min_interest_rate(
                            extract_loans(refreshed_payload), min_interest_rate
                        )
                        if not current_loans:
                            log.info("Originator %r no longer has any loan available right before investing - skipping.", name)
                            continue
                        shares = _compute_swaper_loan_shares(budget, current_loans)
                        attempts = _invest_available_loans(page, current_loans, shares)
                        country = originator_countries.get(name)
                        for attempt in attempts:
                            attempt["originator"] = name
                            # country_invested is updated after EVERY
                            # attempted amount (not just a confirmed-success
                            # status, which this bot's real clicks don't
                            # expose - see _invest_available_loans()'s
                            # docstring) so multiple originators sharing the
                            # same country within this same run can't
                            # jointly blow past the cap.
                            if country and not attempt.get("error"):
                                country_invested[country] = country_invested.get(country, 0.0) + (attempt.get("amount") or 0.0)
                        investment_attempts.extend(attempts)

                    if investment_attempts:
                        # Refresh the snapshot used for the rest of this run
                        # (notification email etc.) to reflect what's actually
                        # left AFTER investing, not the stale pre-invest numbers.
                        payload = fetch_loans(page)

            # Per-country status snapshot for the summary email (added
            # 2026-07-31, mirrors send_lendermarket_invest_summary_email()'s
            # "=== Seuil par pays ===" section) - built AFTER the invest loop
            # above so it reflects this run's own successful attempts too.
            country_status = {}
            for country in relevant_countries:
                invested = country_invested.get(country, 0.0)
                threshold_amount = (
                    (country_threshold_percentage / 100.0) * total_budget
                    if country_threshold_percentage is not None and total_budget > 0
                    else None
                )
                country_status[country] = {
                    "invested": invested,
                    "threshold_amount": threshold_amount,
                    "blocked": threshold_amount is not None and invested >= threshold_amount,
                }
        except Exception:
            log.exception("Failed to log in or fetch loans.")
            browser.close()
            sys.exit(1)

        # Persist cookies/local storage so the next run can skip login (and
        # 2FA, thanks to the "trust this browser" checkbox) while the session
        # remains valid.
        context.storage_state(path=str(STORAGE_STATE_FILE))
        browser.close()

    loans = extract_loans(payload)
    balance = extract_balance(payload)

    log.info("Balance %s, %d loan(s) available.", "positive" if balance > 0 else "zero/unavailable", len(loans))

    # Real investment summary email (see module docstring/
    # _invest_available_loans()'s docstring) - independent of the passive
    # exploration email above. Sent EVERY time at least one investment was
    # actually attempted this run (not one-time - real money moves every
    # time), so successes/failures/modals are always visible.
    if investment_attempts:
        log.info("Sending Swaper investment summary email (%d attempt(s) this run).", len(investment_attempts))
        send_swaper_investment_summary_email(
            investment_attempts,
            captured_api_calls,
            min_interest_rate=min_interest_rate,
            country_threshold_percentage=country_threshold_percentage,
            country_status=country_status,
            country_blocked=country_blocked_originators,
        )
    elif captured_api_calls and originator_loans and balance >= MIN_INVESTMENT_AMOUNT:
        # Diagnostics email (added 2026-07-26, explicit user request: get
        # the loans-listing/filter API structure right away, without
        # waiting for balance >= MIN_INVESTMENT_AMOUNT and an actual
        # investment attempt). Gated by a simple 24h anti-spam cooldown
        # (added 2026-07-26, same day, follow-up request) instead of a
        # one-time-ever flag, so it doesn't spam every run but still keeps
        # coming back periodically. The real invest call's own structure
        # still isn't known until a real investment actually happens, since
        # it's never captured without real money moving (2026-07-25 decision).
        # ALSO requires `originator_loans` to be non-empty (added 2026-07-26,
        # later follow-up: "je veux juste [ce mail] si ça investit sur des
        # prets") - `captured_api_calls` alone is basically ALWAYS non-empty
        # (constraints/logged-in/loans-listing/history-statistics calls fire
        # every single run regardless of loan availability), so without this
        # extra check the email would fire every 24h even when there's
        # nothing at all to invest in for any selected originator. Now it
        # only fires when at least one selected originator currently HAS
        # loan(s) available (i.e. a real investment attempt would have
        # happened if only the balance had been sufficient).
        # ALSO requires `balance >= MIN_INVESTMENT_AMOUNT` (added 2026-07-26,
        # same follow-up request: "solde >= 10 car c'est 10 mini pour
        # investir et capturer l'appel api") - reaching this `elif` branch
        # already means investment_attempts is empty, so if the balance is
        # still below the minimum a real investment wouldn't have been
        # attempted anyway (see the `balance_now < MIN_INVESTMENT_AMOUNT`
        # skip further up) - no point sending the diagnostics email in that
        # case since it can never capture the real invest call either way.
        # No anti-spam cooldown (removed 2026-07-26, explicit user request:
        # "tu peux enlever l'anti spam pour swaper ?") - sent every run the
        # conditions above hold.
        log.info(
            "Sending Swaper API-structure diagnostics email (%d call(s) captured, no "
            "investment attempted yet this run).",
            len(captured_api_calls),
        )
        send_swaper_api_structure_email(captured_api_calls)

    # TEMPORARY DEBUG: force-send a recap email regardless of balance/new
    # loans, to validate the SMTP pipeline end-to-end. Triggered via the
    # `force_test_email` workflow_dispatch input. Remove once confirmed working.
    force_test_email = os.environ.get("FORCE_TEST_EMAIL", "").lower() == "true"
    if force_test_email:
        log.info("FORCE_TEST_EMAIL is set - sending a forced test recap email.")
        send_swaper_email(balance, loans)

    if balance < 10:
        ensure_schedule("30m", cron_job_id=SWAPER_CRON_JOB_ID, state_file=CRON_SCHEDULE_STATE_FILE)
    else:
        ensure_schedule("2m", cron_job_id=SWAPER_CRON_JOB_ID, state_file=CRON_SCHEDULE_STATE_FILE)

    # Same rule for both monitors (see notification_gate.py): only really
    # "available" when there's money to invest AND at least one loan listed.
    available = balance >= 10 and bool(loans)
    send, was_reset = should_notify(gates, "swaper", available, record=not force_test_email)

    if was_reset:
        log.info("Resetting notification gate (balance < 10 or no loans available).")

    log.info(
        "Notification decision context: balance=%.2f, loans_count=%d, available=%s, force_test_email=%s",
        balance,
        len(loans),
        available,
        force_test_email,
    )

    if send and not force_test_email:
        log.info("Notification decision: SEND (reason=balance >= 10 and loans available and gate open).")
        send_swaper_email(balance, loans)
    elif available:
        if force_test_email:
            log.info("Notification decision: SKIP normal cycle send (reason=force_test_email_already_sent).")
        else:
            log.info("Notification decision: SKIP (reason=already_notified_for_current_cycle).")
    else:
        log.info("Notification decision: SKIP (reason=balance < 10 or no loans available).")

    save_state(STATE_FILE, state)


if __name__ == "__main__":
    # Random startup delay (up to 1 minute) so scheduled runs don't always
    # fire at exactly the same second, making the traffic look less bot-like.
    delay = random.uniform(0, 60)
    log.info("Startup jitter: sleeping for %.1f seconds before starting.", delay)
    time.sleep(delay)

    # Set headless=False locally (e.g. via `python swaper_monitor.py --show`) to
    # watch the browser and debug the login flow if selectors need adjusting.
    run(headless="--show" not in sys.argv)
