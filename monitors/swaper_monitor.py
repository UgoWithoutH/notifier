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
    SWAPER_LOOP_MAX_HOURS (default 1)      -> safety cutoff (hours) for the
                                               continuous invest loop below -
                                               user-adjustable, stops the loop
                                               without a success after this long
                                               (kept low by default since GitHub
                                               Actions hosted runners hard-cap a
                                               job at 6h anyway - see below)
    SWAPER_LOOP_POLL_INTERVAL_SECONDS (default 1) -> delay between passes
                                               while the invest loop is active -
                                               kept short since Swaper's manual
                                               loan inventory is extremely
                                               transient (grabbed within seconds)
    SWAPER_CRON_JOB_ID                     -> cron-job.org job id, disabled
                                               for the duration of the invest
                                               loop (see shared/cron_schedule.py's
                                               set_job_enabled()) and re-enabled
                                               once the loop stops

REAL auto-invest bot (added 2026-07-25, explicit user decision - real money,
no more click-and-abort safety net): Swaper's manual loan inventory is
extremely transient (a single loan can appear and be grabbed by another
investor within minutes). Once the balance is >= MIN_INVESTMENT_AMOUNT,
run() no longer just does a single discovery+invest pass and waits for the
next externally-triggered run - it loops CONTINUOUSLY inside the same
Playwright session (added 2026-08-01, explicit user request: "dès que solde
>= 10 je voudrais que le bot tourne en boucle sans s'arrêter jusqu'à qu'il
réussisse à investir"), re-checking availability and re-attempting every
SWAPER_LOOP_POLL_INTERVAL_SECONDS, until either an investment is confirmed,
the balance drops back below the minimum (nothing left to invest), or the
SWAPER_LOOP_MAX_HOURS safety cutoff is reached without success. While the
loop is active, the external cron-job.org trigger (SWAPER_CRON_JOB_ID) is
disabled via `shared.cron_schedule.set_job_enabled()` - no point in a
second, overlapping run firing mid-loop - and re-enabled once the loop
stops (success or timeout), even on an unexpected error (done in a
`finally`). NOTE: GitHub Actions hosted runners hard-cap a single job at 6
hours regardless of SWAPER_LOOP_MAX_HOURS or the workflow's own
timeout-minutes - the default was lowered from an initial 24h to 1h for this
reason (a value above ~6h only matters on a self-hosted runner anyway). If
an unexpected error occurs anywhere during login/discovery/investing, the
loop/bot stops immediately (never keeps retrying blindly) and the run's
summary email is STILL sent at the end no matter what (with the error
included in its body) - see `run_error` in `run()`.
For each available loan (in listing
order), it fills the row's amount input with min(money left, loan's own
amount) and clicks its real "+" icon - a REAL click that really reaches
Swaper's server (nothing is blocked/aborted anymore). The real request AND
response of each investment call are passively captured by the already-
registered `_record_api_response()` listener. A summary
(shared.notifier.send_swaper_investment_summary_email()) is emailed every
time at least one investment was attempted (not one-time - real money moves
every time). Swaper DOES show a `#loan-confirmation-slider` confirmation
modal after clicking "+" (first observed 2026-08-01 - the module used to
assume no such modal existed) - `_invest_available_loans()` now clicks its
real "Confirm" button too (explicit user decision 2026-08-01: "le bot doit
investir", real money) instead of stopping there. If the modal's "Confirm"
button can't be found/clicked, or any other unrecognized UI appears,
investing still stops immediately rather than guessing, and the modal's
HTML is included in the summary for manual review.

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

Post-login flow switched to pure HTTP (added 2026-08-01, explicit user
request for speed - "login en playwright et après le reste en pur http"):
`login()` still drives a real Playwright browser (the only step genuinely
gated by Swaper's reCAPTCHA v3, confirmed 2026-07-26 - see repo memory),
but everything afterward (loan-listing/filter polling AND the real invest
calls) now goes through a plain `requests.Session()` seeded with the
login's cookies (`_build_http_session()`), instead of driving the UI via
`page.goto()`/clicks. `fetch_loans()` is now a single function (an HTTP
POST, optionally filtered via `groups`) replacing the old
`fetch_loans()`/`fetch_loans_for_originator()`/`fetch_loans_fast()` trio -
there's no more "slow reload vs fast in-page fetch" distinction since a
plain request is already fast. `_invest_available_loans()` no longer
clicks the amount input/"+" icon/Confirm button - it replicates the 2 real
calls a real investment fires (confirmed from a real successful capture,
2026-08-01): `GET /rest/public/profile/is-manual-investment-approved`
(must return `true`) then `POST /rest/public/loans/{id}/buy` with
`{"amount": <float>}` - there is NO separate "confirm" API call, the
browser's confirmation modal is purely a UI safety step, so these 2 calls
are the whole real flow, not a guess. The browser/context stay open for
the whole run only to persist `storage_state` at the end.

REVERTED (2026-08-01, later same day, real GitHub Actions failure): the
plain `requests.Session()` approach above is now BLOCKED by Cloudflare -
a real run got `403 Forbidden` on the very first `fetch_loans()` call
right after a successful Playwright login, and the SAME 403 recurred
identically after `_relogin_and_rebuild_session()` logged back in again
(login itself succeeded both times - "Reused a previous session, already
logged in" - only the plain-HTTP calls failed). This is the exact same
fingerprint-based Cloudflare bot-fight-mode pattern already documented in
repo memory for Bricks (a naive non-browser client gets blocked even with
valid session cookies, while the identical call through a real automated
Chromium succeeds) - swaper.com is also Cloudflare-fronted (`Server:
cloudflare`, `CF-RAY` present on the 403 responses). FIX: every post-login
call (`fetch_loans()`, the manual-investment-approval check, the buy call)
now goes through `page.evaluate(fetch(...))` - the real browser's own
fetch(), same-origin, cookies attached automatically - instead of a
separate `requests.Session()`. `_build_http_session()`/`_xsrf_headers()`
were replaced by `page.goto()`-driven navigation for the loans listing and
real UI clicks for investing (see `fetch_loans()`/`_invest_available_loans()`);
`login()`
stays exactly as before (still the only step needing a real browser for
its own reCAPTCHA-related reasons). No more session cookie syncing needed
at the end of the run since the browser context IS the source of truth
throughout.
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
from shared.cron_schedule import ensure_schedule, set_job_enabled
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

# Continuous invest loop (added 2026-08-01, explicit user request: "dès que
# solde >= 10 je voudrais que le bot tourne en boucle sans s'arrêter jusqu'à
# qu'il réussisse à investir"). SWAPER_LOOP_MAX_HOURS is the safety cutoff
# (user-adjustable env var, as explicitly requested) - the loop gives up
# after this many hours without a successful investment. Default lowered to
# 1h (was 24h) since GitHub Actions hosted runners hard-cap a single job at
# 6h regardless of this setting anyway - a higher value only matters on a
# self-hosted runner.
SWAPER_LOOP_MAX_HOURS = float(os.environ.get("SWAPER_LOOP_MAX_HOURS", "1"))
# How long to wait between two discovery+invest passes while looping - kept
# short (default 1s) since Swaper's manual loan inventory is extremely
# transient (a loan can be grabbed by someone else within seconds).
SWAPER_LOOP_POLL_INTERVAL_SECONDS = float(os.environ.get("SWAPER_LOOP_POLL_INTERVAL_SECONDS", "1"))

# Fallback used only if get_swaper_min_interest_rate() (reads the cell just
# left of "Swaper" in "Répartition géographique", see that function's
# docstring) fails - default 0 preserves the pre-2026-07-31 behavior (no
# interest-rate filtering at all) instead of silently excluding every loan
# on a read error.
MIN_INTEREST_RATE = float(os.environ.get("SWAPER_MIN_INTEREST_RATE", "0"))

# Loans-listing page navigated to directly (fetch_loans() lets the real
# Angular app fire the API call itself - see that function's docstring for
# why a manually-built request gets 403).
LOANS_PAGE_URL = "https://swaper.com/en/loans"
LOANS_URL = "https://swaper.com/rest/public/loans"

class SwaperSessionExpired(RuntimeError):
    """Raised by the post-login page.evaluate(fetch()) calls
    (fetch_loans()/_invest_available_loans()) when Swaper responds 401/403 -
    distinguished from other failures so run()'s invest loop can tell "the
    login session went stale mid-run" apart from a genuine API/validation
    error, and react by logging back in again (see run()'s
    `_relogin()`/`_with_relogin_retry()`) instead of just giving up for the
    rest of the loop.
    """



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


def _record_http_call(captured: list, method: str, url: str, request_headers: dict, request_body, result: dict) -> None:
    """Builds the same shaped diagnostics entry (redacted request/response
    headers, raw bodies) as before so
    `send_swaper_investment_summary_email()`/`send_swaper_api_structure_email()`
    need no changes - `result` is `{"status": int, "headers": dict, "text": str}`.
    """
    entry = {
        "method": method,
        "url": url,
        "request_headers": _redact_sensitive_headers(dict(request_headers)),
        "request_post_data": request_body,
        "status": result.get("status"),
    }
    try:
        entry["response_headers"] = _redact_sensitive_headers(dict(result.get("headers") or {}))
    except Exception:
        entry["response_headers"] = None
    entry["body"] = (result.get("text") or "")[:20000]
    captured.append(entry)


def fetch_loans(page, captured_api_calls: list, groups: list = None) -> dict:
    """Fetch the loans list by letting Swaper's own Angular SPA fire its
    REAL request (`page.goto()` to the loans page) - NOT a manually
    reconstructed fetch call (confirmed 2026-08-01, later same day: a
    manually-built request gets HTTP 403 regardless of whether it's built
    via `requests` or via `page.evaluate(fetch(...))` - this exact risk was
    already flagged on 2026-07-25 for this same endpoint: "a fully manual
    reconstructed request gets 403/415", but was apparently forgotten when
    the pure-HTTP migration shipped). Verified response shape:
    `{accountBalance, totalRecords, page, results}`.

    `groups` filters to a single loan originator by its exact display name
    (confirmed via real DevTools captures, 2026-07-25 - e.g.
    `["Wandoo Finance Group"]`, not an opaque id): rather than building the
    request from scratch, a `page.route()` interceptor rewrites the SPA's
    own outgoing request BODY to add `"groups": [...]` before letting it
    through unmodified otherwise - every header the real app computed
    itself (x-xsrf-token/referer/cookies/swaper-client) is preserved
    untouched. Every call (filtered or not) is recorded into
    `captured_api_calls`, feeding the investment summary/diagnostics
    emails.
    """
    def _patch_body(route):
        request = route.request
        try:
            body = json.loads(request.post_data or "{}")
        except Exception:
            body = {}
        body["groups"] = groups
        route.continue_(post_data=json.dumps(body))

    if groups:
        page.route("**/rest/public/loans", _patch_body)
    try:
        with page.expect_response(
            lambda r: r.url == LOANS_URL and r.request.method == "POST"
        ) as response_info:
            page.goto(LOANS_PAGE_URL, wait_until="domcontentloaded")
        response = response_info.value
    finally:
        if groups:
            page.unroute("**/rest/public/loans", _patch_body)

    request = response.request
    text = response.text()
    result = {"status": response.status, "headers": dict(response.headers), "text": text}
    _record_http_call(captured_api_calls, "POST", LOANS_URL, dict(request.headers), request.post_data, result)
    if response.status in (401, 403):
        raise SwaperSessionExpired(f"Loans API returned status {response.status} - session likely expired")
    if not response.ok:
        raise RuntimeError(
            f"Loans API returned status {response.status}" + (f" for groups {groups!r}" if groups else "")
        )
    return json.loads(text or "null")


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


# Candidate field names tried (in order) to attribute a loan back to its
# originator when using the combined multi-group fetch below - the real
# field name is unconfirmed (no live loan data available to inspect at the
# time this was written, see repo memory).
_ORIGINATOR_FIELD_CANDIDATES = ("company", "loanOriginator", "originator", "originatorName", "group")


def fetch_loans_by_selected_originators(page, captured_api_calls: list, selected_originators: list, min_interest_rate: float):
    """Fetch ALL selected originators' loans in a SINGLE `fetch_loans()` call
    (`groups` accepts multiple originator names at once, same as Auto-Invest
    EASY's own `defaultGroups` array - confirmed by the user) instead of one
    navigation per originator - cuts the discovery loop from O(N) page
    reloads to O(1).

    Only trusted if every returned loan can be confidently attributed back
    to one of the requested originators via a recognizable field (tried in
    order, see `_ORIGINATOR_FIELD_CANDIDATES`) - returns `None` (the caller
    falls back to the slower, proven one-call-per-originator method) if that
    can't be established, since silently misgrouping or dropping a real loan
    is worse than the wasted time of the slow path. An empty result needs no
    field to trust (nothing to group), so it's always accepted as-is.

    Returns `{originator_name: [loan, ...]}` on success, `None` on failure.
    """
    payload = fetch_loans(page, captured_api_calls, groups=selected_originators)
    loans = _filter_loans_by_min_interest_rate(extract_loans(payload), min_interest_rate)

    grouped = {name: [] for name in selected_originators}
    if not loans:
        return grouped

    field = next((c for c in _ORIGINATOR_FIELD_CANDIDATES if all(c in loan for loan in loans)), None)
    if field is None:
        log.warning("Combined multi-originator fetch: no recognizable originator field on the response - falling back.")
        return None

    lowered_names = {name.lower(): name for name in selected_originators}
    for loan in loans:
        matched_name = lowered_names.get(str(loan.get(field) or "").strip().lower())
        if matched_name is None:
            log.warning(
                "Combined multi-originator fetch: loan %s has unrecognized %r value %r - falling back.",
                loan.get("number") or loan.get("id"), field, loan.get(field),
            )
            return None
        grouped[matched_name].append(loan)

    return grouped


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


def _record_api_response(captured: list, response) -> None:
    """Passively captures every `/rest/` API call fired by real UI clicks
    (registered on `page.on("response", ...)` only AFTER `login()` has fully
    returned - never during credentials/2FA - with a belt-and-braces skip of
    any URL containing login/auth/password, per this repo's documented
    security lesson about interceptors leaking credentials). Never blocks
    anything - purely observational, feeding the investment summary email.
    """
    url = response.url
    if "swaper.com" not in url or "/rest/" not in url:
        return
    lowered = url.lower()
    if "login" in lowered or "auth" in lowered or "password" in lowered:
        return

    request = response.request
    entry = {
        "method": request.method,
        "url": url,
        "request_headers": _redact_sensitive_headers(dict(request.headers)),
        "request_post_data": request.post_data,
        "status": response.status,
    }
    try:
        entry["response_headers"] = _redact_sensitive_headers(dict(response.headers))
    except Exception:
        entry["response_headers"] = None
    try:
        entry["body"] = response.text()[:20000]
    except Exception:
        entry["body"] = None
    captured.append(entry)


def _extract_balance_from_attempts(attempts: list):
    """Reads the post-investment balance directly out of the real `buy`
    call's response body (already captured via `_record_api_response()`,
    shape `{"investment": {...}, "user": {"accountBalance": ...}}`) instead
    of firing an extra `fetch_loans()` navigation just to learn it - saves a
    full page reload after every successful invest pass. Returns `None` if
    no confirmed attempt's buy response can be parsed this way (caller
    falls back to a real refresh).
    """
    for attempt in reversed(attempts):
        if not attempt.get("confirmed"):
            continue
        for call in reversed(attempt.get("confirm_api_calls") or []):
            if call.get("method") != "POST" or "/buy" not in (call.get("url") or ""):
                continue
            try:
                body = json.loads(call.get("body") or "null") or {}
            except Exception:
                continue
            balance = (body.get("user") or {}).get("accountBalance")
            if isinstance(balance, (int, float)):
                return float(balance)
    return None


def _invest_available_loans(page, loans: list, shares: dict, captured_api_calls: list) -> list:
    """Actually invest into available manual loans, using the exact
    per-loan amounts precomputed by `_compute_swaper_loan_shares()` (loans
    with no entry in `shares`, e.g. below the minimum, are skipped).

    REAL clicks, REAL money (2026-07-25, explicit user decision to make this
    the production invest bot - see module docstring). Reverted 2026-08-01
    (later same day, real GitHub Actions failure) from manually-reconstructed
    `page.evaluate(fetch())` calls back to driving the actual UI: a safe
    throwaway test (nonexistent loan_id + a below-minimum amount, so no real
    money could ever move) confirmed Swaper's WAF returns 403 for a manually
    built request to BOTH `is-manual-investment-approved` AND `.../buy`, the
    exact same block already found on `fetch_loans()` - the real click flow
    is the only way these calls actually succeed. Both real requests fire as
    a side effect of the click sequence below and are captured passively by
    the `page.on("response", ...)` listener registered in `run()` (see
    `_record_api_response()`) - this function makes no HTTP calls itself.
    Ported verbatim from the last pre-pure-HTTP commit (`cc26b84`) rather
    than re-guessed, since that version was already live-verified.

    Each loan's row is targeted by its visible `number` (NOT always
    `rows.first`) since a loan given only a PARTIAL share (the normal case
    under equal-split) stays visible afterwards rather than
    disappearing/reordering - skipped (not fatal) if its row can't be found
    at all. Swaper shows a `#loan-confirmation-slider.open` confirmation
    modal after clicking "+"; the modal detection itself uses a generic
    CSS query (modal/dialog/toast/snackbar/popup/confirm classes) rather
    than hardcoding that one ID, since that selector is unverified against
    every possible UI variant - the row's own outerHTML right after the
    click is ALSO always captured as a fallback, so whatever appeared is
    visible in the summary email even if not recognized as a "modal" here.
    Stops attempting further loans as soon as a modal was shown but
    couldn't be confirmed (real money on the line, never guesses which
    button to click) - keeps going to the next loan only if clicking "+"
    never produced any recognizable modal at all.

    Returns a list of attempt dicts: {loan_id, loan_number, amount,
    modal_html, row_html_after_click, confirmed, confirm_api_calls, error}.
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
            try:
                page.locator("#loan-confirmation-slider.open").wait_for(state="visible", timeout=8000)
            except PlaywrightTimeoutError:
                pass
            # The modal can open as an empty shell (no .modal-footer/Confirm
            # button yet) while Swaper runs an async approval check first -
            # real production failure (loan BLS-305654) before this wait was
            # added. Silently gives up after 15s, same as the wait above.
            try:
                page.locator(".modal-footer .button.clickable", has_text="Confirm").wait_for(state="visible", timeout=15000)
            except PlaywrightTimeoutError:
                pass
        except Exception:
            log.exception("Exception while attempting to invest in loan %s - stopping.", loan_label)
            attempts.append({"loan_id": loan_id, "loan_number": loan.get("number"), "amount": amount, "error": True})
            break

        modal_html = None
        try:
            modal_html = page.evaluate(
                "() => { const m = document.querySelector("
                "'.modal, [class*=modal], [role=dialog], [class*=dialog], "
                "[class*=toast], [class*=snackbar], [class*=popup], [class*=confirm]"
                "'); return m && m.offsetParent !== null ? m.outerHTML : null; }"
            )
        except Exception:
            pass

        row_html_after_click = None
        try:
            row_html_after_click = row.evaluate("el => el.outerHTML")
        except Exception:
            pass

        confirmed = False
        confirm_api_calls = []
        if modal_html:
            calls_before_confirm = len(captured_api_calls)
            try:
                confirm_button = page.locator(".modal-footer .button.clickable", has_text="Confirm").first
                confirm_button.click(timeout=10000)
                try:
                    page.locator("#loan-confirmation-slider.open").wait_for(state="hidden", timeout=8000)
                except PlaywrightTimeoutError:
                    pass
                confirmed = True
            except Exception:
                log.exception(
                    "Could not click the Confirm button for loan %s - stopping here rather than guessing.",
                    loan_label,
                )
            confirm_api_calls = captured_api_calls[calls_before_confirm:]
            if confirm_api_calls:
                for call in confirm_api_calls:
                    log.info(
                        "REAL INVEST API CALL for loan %s: %s %s -> HTTP %s | body: %s",
                        loan_label, call.get("method"), call.get("url"), call.get("status"),
                        (call.get("body") or "")[:2000],
                    )
            else:
                log.warning(
                    "Clicked Confirm for loan %s but no new /rest/ API call was captured afterward.",
                    loan_label,
                )

        attempts.append({
            "loan_id": loan_id,
            "loan_number": loan.get("number"),
            "amount": amount,
            "modal_html": modal_html,
            "row_html_after_click": row_html_after_click,
            "confirmed": confirmed,
            "confirm_api_calls": confirm_api_calls,
        })

        if modal_html and not confirmed:
            break

    return attempts


def run(headless: bool = True) -> None:
    if not SWAPER_EMAIL or not SWAPER_PASSWORD:
        log.error("SWAPER_EMAIL and SWAPER_PASSWORD environment variables are required.")
        sys.exit(1)

    state = load_state(STATE_FILE, DEFAULT_STATE)
    gates = state.setdefault("gates", {})

    # Pre-declared with safe defaults (added 2026-08-01, explicit user
    # request: "si y'a une erreur ou autre il faut arrêter le bot et à la
    # fin du run quoi qu'il arrive envoyer le mail") - so that if login()/
    # fetch_loans() or anything else below raises BEFORE these get their
    # real values, the summary-email code after the `with` block still has
    # something safe to work with instead of raising a NameError that would
    # itself skip sending the email.
    run_error = None
    payload = None
    captured_api_calls = []
    investment_attempts = []
    min_interest_rate = MIN_INTEREST_RATE
    country_threshold_percentage = None
    country_status = {}
    country_blocked_originators = []
    originator_loans = {}

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

        try:
            login(page)
            # Registered only AFTER login() fully returns, never during
            # credentials/2FA - see _record_api_response()'s docstring.
            page.on("response", lambda response: _record_api_response(captured_api_calls, response))

            def _relogin() -> None:
                # The browser/page are kept alive for the whole run anyway
                # (see the storage_state persistence at the end), so a mid-run
                # session expiry (explicit user request: "et si la session
                # expire ça refait le login") can be recovered from without
                # restarting the whole process - every call reads cookies
                # fresh from the same page/context, no separate session
                # object to rebuild.
                log.warning("Swaper session appears to have expired mid-run - logging back in again.")
                login(page)

            def _with_relogin_retry(call):
                try:
                    return call()
                except SwaperSessionExpired:
                    _relogin()
                    return call()

            payload = _with_relogin_retry(lambda: fetch_loans(page, captured_api_calls))

            # REAL investment attempt (2026-07-25, explicit user decision -
            # see module docstring). Per-originator investing (added later
            # the same day, per explicit user request): rather than
            # investing into the aggregate/unfiltered loan list, only the
            # loan originators flagged with "x" in the Google Sheet (see
            # shared.google_sheet.get_selected_swaper_loan_originators(),
            # mirroring the PeerBerry/Lendermarket sheet convention) are
            # considered, and each is checked individually for CURRENT loan
            # availability via fetch_loans(page, ..., groups=[name])
            # (the site's "Loan originators" filter, driven through the
            # API's "groups" field - see that function's docstring). The
            # available balance is then split EVENLY across only the
            # originators that currently have >=1 loan available (see
            # _split_budget_across_available_originators()'s docstring for
            # the exact rule), and _invest_available_loans() is called once
            # per such originator with its own budget cap (pure HTTP calls,
            # see that function's docstring for the 2026-08-01 switch off
            # Playwright clicks).
            loans_now = extract_loans(payload)
            balance_now = extract_balance(payload)

            if balance_now < MIN_INVESTMENT_AMOUNT:
                # Stop the whole run right here (2026-08-01, explicit user
                # request) - no Sheet reads, no per-originator availability
                # discovery, no invest loop at all when there isn't even enough
                # to fund a single manual investment. investment_attempts/
                # originator_loans/country_status keep their safe pre-declared
                # defaults, so the summary-email logic below behaves exactly as
                # if this run had nothing to invest.
                log.info(
                    "Balance %.2f EUR is below the minimum (%.2f EUR) at the start of this run - "
                    "stopping here, no discovery or investing this run.",
                    balance_now, MIN_INVESTMENT_AMOUNT,
                )
            else:
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
                    # Continuous invest loop (added 2026-08-01, explicit user
                    # request: "d\u00e8s que solde >= 10 je voudrais que le bot tourne
                    # en boucle sans s'arr\u00eater jusqu'\u00e0 qu'il r\u00e9ussisse \u00e0
                    # investir"). As long as the balance is >= the minimum,
                    # repeat discovery+invest passes - instead of a single pass
                    # per externally-triggered run - until either a real
                    # investment gets confirmed, the balance drops back below
                    # the minimum (nothing left to invest), or
                    # SWAPER_LOOP_MAX_HOURS elapses without success (a
                    # user-adjustable safety cutoff, see that env var's
                    # docstring). The external cron-job.org trigger is disabled
                    # for the loop's duration (no point in a second, overlapping
                    # run firing mid-loop) and re-enabled once it stops, in a
                    # `finally` so it's re-enabled even on an unexpected error.
                    loop_active = balance_now >= MIN_INVESTMENT_AMOUNT
                    loop_deadline = time.monotonic() + SWAPER_LOOP_MAX_HOURS * 3600
                    cron_disabled = False
                    if loop_active:
                        log.info(
                            "Balance %.2f EUR >= minimum - looping continuously (up to %.1fh) until an "
                            "investment succeeds.", balance_now, SWAPER_LOOP_MAX_HOURS,
                        )
                        cron_disabled = set_job_enabled(SWAPER_CRON_JOB_ID, False)

                    try:
                        pass_number = 0
                        while True:
                            pass_number += 1
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
                                "availability for each (min interest rate: %s%%, pass %d).",
                                len(selected_originators), ", ".join(selected_originators), min_interest_rate, pass_number,
                            )
                            originator_loans = {}
                            fast_grouped = None
                            try:
                                fast_grouped = _with_relogin_retry(
                                    lambda: fetch_loans_by_selected_originators(
                                        page, captured_api_calls, selected_originators, min_interest_rate
                                    )
                                )
                            except Exception:
                                log.exception("Combined multi-originator fetch failed - falling back to per-originator fetches.")

                            if fast_grouped is not None:
                                for name, loans_for_name in fast_grouped.items():
                                    if loans_for_name:
                                        originator_loans[name] = loans_for_name
                                        log.info("Originator %r currently has %d loan(s) available (fast combined fetch).", name, len(loans_for_name))
                                    else:
                                        log.info("Originator %r currently has no loans available (fast combined fetch).", name)
                            else:
                                for name in selected_originators:
                                    try:
                                        originator_payload = _with_relogin_retry(
                                            lambda name=name: fetch_loans(page, captured_api_calls, groups=[name])
                                        )
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

                            pass_attempts = []
                            if balance_now < MIN_INVESTMENT_AMOUNT:
                                log.info(
                                    "Balance %.2f EUR is below the minimum (%.2f EUR) - availability was still "
                                    "checked above for diagnostics, but skipping the actual auto-invest step.",
                                    balance_now, MIN_INVESTMENT_AMOUNT,
                                )
                            else:
                                # Per-country cap (added 2026-07-31, mirrors
                                # lendermarket_monitor.py's invest_selected_lenders() -
                                # re-checked on EVERY pass since balance_now/total_budget
                                # can change as this loop invests) - any currently-
                                # available originator whose mapped country is already
                                # at/above `country_threshold_percentage`% of the total
                                # Swaper budget (balance + every country's already-
                                # invested amount) is excluded from this pass's budget
                                # split (same treatment as "0 loans available").
                                total_budget = balance_now + sum(country_invested.values())
                                for name in list(originator_loans.keys()):
                                    country = originator_countries.get(name)
                                    if _is_country_blocked(country, total_budget):
                                        log.info(
                                            "Originator %r (country %r) is blocked this run: already at/above the %s%% country cap.",
                                            name, country, country_threshold_percentage,
                                        )
                                        if name not in country_blocked_originators:
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
                                        # appear, within seconds). Using the fresh list
                                        # here (instead of the discovery-time
                                        # originator_loans[name]) avoids computing shares
                                        # for/targeting a loan that no longer exists, and
                                        # correctly picks up any loan that appeared since.
                                        refreshed_payload = _with_relogin_retry(
                                            lambda name=name: fetch_loans(page, captured_api_calls, groups=[name])
                                        )
                                    except Exception:
                                        log.exception("Failed to re-fetch loans for originator %r before investing - skipping it.", name)
                                        continue
                                    current_loans = _filter_loans_by_min_interest_rate(
                                        extract_loans(refreshed_payload), min_interest_rate
                                    )
                                    if not current_loans:
                                        log.info("Originator %r no longer has any loan available right before investing - skipping.", name)
                                        continue
                                    shares = _compute_swaper_loan_shares(budget, current_loans)
                                    try:
                                        attempts = _with_relogin_retry(
                                            lambda: _invest_available_loans(page, current_loans, shares, captured_api_calls)
                                        )
                                    except Exception:
                                        log.exception("Failed to invest into originator %r's loan(s) - skipping it this pass.", name)
                                        continue
                                    country = originator_countries.get(name)
                                    for attempt in attempts:
                                        attempt["originator"] = name
                                        # country_invested is updated after EVERY
                                        # attempted amount (not just a confirmed
                                        # status) so multiple originators sharing the
                                        # same country can't jointly blow past the cap.
                                        if country and not attempt.get("error"):
                                            country_invested[country] = country_invested.get(country, 0.0) + (attempt.get("amount") or 0.0)
                                    pass_attempts.extend(attempts)

                            investment_attempts.extend(pass_attempts)

                            if pass_attempts:
                                if any(a.get("confirmed") for a in pass_attempts):
                                    # Refresh the balance used for the rest of this run
                                    # (notification email etc.) and this loop's own exit
                                    # checks. The real buy response already embeds the
                                    # post-investment balance (user.accountBalance) - use
                                    # that directly instead of an extra full page reload
                                    # when possible, falling back to a real refresh only
                                    # if that can't be extracted.
                                    fresh_balance = _extract_balance_from_attempts(pass_attempts)
                                    if fresh_balance is not None:
                                        balance_now = fresh_balance
                                        log.info("Balance updated from the buy response: %.2f EUR (skipped an extra fetch).", balance_now)
                                    else:
                                        payload = _with_relogin_retry(lambda: fetch_loans(page, captured_api_calls))
                                        balance_now = extract_balance(payload)
                                # else: nothing confirmed this pass - balance/loans are
                                # unchanged, no refresh needed at all.

                            if not loop_active:
                                break

                            pass_succeeded = any(a.get("confirmed") and not a.get("error") for a in pass_attempts)
                            if pass_succeeded:
                                log.info("Investment confirmed on pass %d - stopping the invest loop.", pass_number)
                                break
                            if balance_now < MIN_INVESTMENT_AMOUNT:
                                log.info("Balance dropped below the minimum - stopping the invest loop (nothing left to invest).")
                                break
                            if time.monotonic() >= loop_deadline:
                                log.warning(
                                    "Reached the %.1fh safety limit without a successful investment - stopping the invest loop.",
                                    SWAPER_LOOP_MAX_HOURS,
                                )
                                break
                            time.sleep(SWAPER_LOOP_POLL_INTERVAL_SECONDS)
                    except Exception as exc:
                        # Unexpected error during the loop itself (not one of the
                        # already-handled per-originator fetch/invest failures
                        # above, which just `continue` past that one originator) -
                        # stop the bot immediately rather than keep looping
                        # blindly (explicit user request, see run_error's usage
                        # below - the summary email is still sent regardless).
                        log.exception("Unexpected error during the invest loop - stopping.")
                        run_error = str(exc)
                    finally:
                        if cron_disabled:
                            set_job_enabled(SWAPER_CRON_JOB_ID, True)

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
        except Exception as exc:
            # Any failure here (login, initial fetch, or anything above not
            # already caught by the invest loop's own try/except) stops the
            # bot immediately - the summary email is still sent afterward no
            # matter what (explicit user request), with this error included.
            log.exception("Failed to log in or fetch loans, or an unexpected error occurred.")
            run_error = str(exc)

        # Persist cookies/local storage so the next run can skip login (and
        # 2FA, thanks to the "trust this browser" checkbox) while the session
        # remains valid - only on a clean run, never after an error (the
        # session may be in a broken/partial state). Every call in this run
        # went through the same Playwright context, so its cookie jar
        # already reflects any mid-run rotation (e.g. XSRF-TOKEN) with no
        # separate syncing needed.
        if run_error is None:
            try:
                context.storage_state(path=str(STORAGE_STATE_FILE))
            except Exception:
                log.exception("Failed to persist storage state.")
        browser.close()

    loans = extract_loans(payload)
    balance = extract_balance(payload)

    log.info("Balance %s, %d loan(s) available.", "positive" if balance > 0 else "zero/unavailable", len(loans))

    # Real investment summary email (see module docstring/
    # _invest_available_loans()'s docstring) - independent of the passive
    # exploration email above. Sent EVERY time at least one investment was
    # actually attempted this run (not one-time - real money moves every
    # time) OR an error occurred (explicit user request: "à la fin du run
    # quoi qu'il arrive envoyer le mail") - so successes/failures/modals/
    # errors are always visible.
    if investment_attempts or run_error:
        log.info(
            "Sending Swaper investment summary email (%d attempt(s) this run, error=%s).",
            len(investment_attempts), run_error,
        )
        send_swaper_investment_summary_email(
            investment_attempts,
            captured_api_calls,
            min_interest_rate=min_interest_rate,
            country_threshold_percentage=country_threshold_percentage,
            country_status=country_status,
            country_blocked=country_blocked_originators,
            error=run_error,
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

    # if balance < 10:
    #     ensure_schedule("30m", cron_job_id=SWAPER_CRON_JOB_ID, state_file=CRON_SCHEDULE_STATE_FILE)
    # else:
    #     ensure_schedule("2m", cron_job_id=SWAPER_CRON_JOB_ID, state_file=CRON_SCHEDULE_STATE_FILE)

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

    if run_error:
        # Deferred from the with-block above so the summary email is always
        # sent first (explicit user request) - only now does this mark the
        # GitHub Actions job itself as failed.
        log.error("Exiting with a failure status this run due to: %s", run_error)
        sys.exit(1)


if __name__ == "__main__":
    # Random startup delay (up to 1 minute) so scheduled runs don't always
    # fire at exactly the same second, making the traffic look less bot-like.
    delay = random.uniform(0, 60)
    log.info("Startup jitter: sleeping for %.1f seconds before starting.", delay)
    time.sleep(delay)

    # Set headless=False locally (e.g. via `python swaper_monitor.py --show`) to
    # watch the browser and debug the login flow if selectors need adjusting.
    run(headless="--show" not in sys.argv)
