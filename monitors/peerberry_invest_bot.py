"""PeerBerry automated investment bot (v1).

`workflow_dispatch`-only bot, externally triggered via cron-job.org, that
repeatedly polls PeerBerry's filtered loan listing (same filters as
https://peerberry.com/en/client/invest?sort=-loanId&groupGuarantee=1&loanOriginators=4,12,23,30,33,36,39,41,43,45,47,48,49,50,51,52,53,54,55,56,57,58,59,62,63,64,65,66,67,68,69,70,71,72,73,74,75,76,77,78&minInterestRate=8.5&maxRemainingTerm=185&minRemainingTerm=1,
confirmed with the user 2026-07-30 - critically this DROPS `hideInvested`
entirely (the previous 2026-07-29 filter set included `hideInvested=true`,
which the user confirmed was NOT actually part of their real site filter
after a run saw 0 matching loans across 22272 polls/~75 minutes with this
param present) - see `build_loans_params()`'s own docstring for the
2026-07-29 filter's own history)
and tries to invest available funds into any newly-appeared/still-available
matching loan, as fast as possible (~0.2s polling by default), before other investors
grab it. Within each poll, loans are attempted starting from the END of the
listing (i.e. reversed vs. PeerBerry's own "-loanId" sort/UI order), since
human investors using the real website naturally start clicking from the
top of their screen - loans further down are less likely to already be
contested. On a failed attempt, the bot does NOT wait for the next poll: it
immediately re-fetches the balance and loan listing and keeps trying right
away with fresh data.

Reuses monitors.peerberry_monitor.login()/_HEADERS/PEERBERRY_EMAIL/
PEERBERRY_PASSWORD (same dependency direction as every other module that
piggybacks on the monitor - see that module's docstring) rather than
duplicating the auth flow.

PeerBerry's access_token is a short-lived JWT (login() is only ever called
ONCE at startup) - a real run on 2026-07-23 showed it expiring mid-run,
after which EVERY poll got HTTP 401 for the rest of that run (1621/5167
polls errored, all in one unbroken tail once the token expired, 0
investments possible). Fixed via `_call_with_reauth()`: fetch_loans()/
fetch_available_money() calls are wrapped so a 401 triggers exactly one
re-login() (fresh token) + one retry before being treated as a real error;
attempt_investment()'s POST does the same inline (re-login + retry once on
a 401 response) before counting it as a failed investment attempt.

Endpoints used, all pure HTTP (`requests`, no browser needed), discovered
via one-off, read-only/network-intercepted Playwright explorations on
2026-07-22 (see repo memory for full details):
  - `GET /v2/investor/profile` -> `publicId` (a UUID), needed to build the
    loans-listing URL below (NOT the same as `accountId`, also present in
    that response).
  - `GET /v1/{publicId}/loans` with `hideInvested`/`groupGuarantee`/
    `minInterestRate`/`maxRemainingTerm`/`minRemainingTerm` filters (see
    `build_loans_params()`) -> `{"data": [...], "total": N, ...}`. Only the
    field names are known (from the response's own `sort` mapping):
    `loanId`, `countryId`, `loanOriginator`, `issuedDate`,
    `termTypeTitle`/`termType`, `interestRate`, `term`, `availableToInvest`.
    NO server-side `loanOriginators[]` id filter is sent anymore (removed
    for good 2026-07-31, after being removed 2026-07-28, restored
    2026-07-29, then proven to break again - see `build_loans_params()`'s
    own docstring for the full story) - originator selection happens
    100% client-side by name.
  - `GET /v1/investor/overview` -> `availableMoney` (same as
    peerberry_monitor.fetch_available_money()) - re-fetched periodically to
    track remaining funds across investment attempts within a single run.
  - `POST /v1/loans/{loanId}` with `{"amount": "<string>"}` (see
    `attempt_investment()`) - the real invest submission call, CONFIRMED via
    a network-interception exploration (clicked Invest -> confirmed "Yes" on
    the "Assignment Agreement" popup -> the resulting request was
    intercepted and aborted before ever reaching the server, so no real
    money was spent). This is NOT the previously-guessed
    `/v1/investor/loans/{loanId}/invest` shape.

Which originator actually gets INVESTED IN (and how much budget it gets)
is driven 100% by the Google Sheet - there is no server-side id filter
anymore, so a newly-selected Sheet originator is always visible to the bot
(subject only to pagination, see the `total > pageSize` log warning).
shared.google_sheet.get_selected_peerberry_loan_originators() finds the
"Répartition géographique" cell, then the "Peerberry" cell below it (same
column) - every row between "Peerberry" and the next "Swaper" cell (both
excluded) is a PeerBerry loan originator, and any of those rows whose cell
one column to the LEFT equals "x" (case-insensitive) is selected. A loan
whose `loanOriginator` doesn't match any selected name (see
`_match_selected_originator()`) is skipped entirely.

There is NO per-originator budget split anymore (removed 2026-07-31, see
git history for the previous equal-split-in-MIN_INVESTMENT_AMOUNT-blocks
design) - a matching, non-blocked (see the per-country cap below) loan
simply draws `min(available_money, loan's own availableToInvest)`, same
shared pot for every selected originator. The only investment caps left
are the overall `available_money`/MIN_INVESTMENT_AMOUNT floor and the
per-country threshold described next.

Per-country investment cap: no single country may end up holding more than
`country_threshold_percentage`% of the TOTAL PeerBerry budget (everything
currently invested across EVERY loan originator on the account, summed
live via `fetch_originator_invested_amounts()` at startup - not just the
selected ones - plus the available/not-yet-invested balance). Both the
percentage AND each country's currently-invested amount are read ONCE from
the Google Sheet at startup, by
`shared.google_sheet.get_peerberry_country_allocations()`:
`threshold_percentage` is the value in the cell 2 columns to the left of
"Peerberry" on ITS OWN row (one column further left than the
`minInterestRate` cell read by `get_peerberry_min_interest_rate()` - that
cell is otherwise used as the "x" selection flag for the loan-originator
rows below, but is free on the "Peerberry" row itself); `country_amounts`
is read directly off that same "Peerberry" row, one value per country
column (found dynamically as every non-empty column header on the
"Répartition géographique" row); `originator_countries` (loan originator
name -> country name) is derived from each loan-originator sub-row's own
SINGLE non-empty country column (every PeerBerry loan originator only
ever operates in one country). Once a country's tracked invested total
reaches/exceeds its threshold, EVERY loan for that country - among
selected originators only, see `_match_selected_originator()` - gets
skipped (0 EUR) for the REST of the run, even if a later resync would
show a lower figure (sticky, see `_update_blocked_countries()`). The
Google Sheet itself is never read again after startup: per-country totals
are kept up to date purely from the live API - immediately on this bot's
own successful investments, and resynced from scratch every
`EXTERNAL_INVESTMENT_CHECK_INTERVAL_SECONDS` via a periodic
`fetch_originator_invested_amounts()` call (folding in anything external
too, e.g. PeerBerry's own "Auto-Invest EASY" scheme or a real human) - see
`_group_invested_by_country()`. If the Sheet's threshold percentage cell
is empty (or the Sheet read fails outright), country blocking is disabled
for that run (soft-fail, same as `MIN_INTEREST_RATE` above) rather than
aborting it.

If the available balance seen at startup is already below
MIN_INVESTMENT_AMOUNT, `run()` stops right away (no polling at all, not
even reading the Google Sheet) instead of burning the whole
DURATION_SECONDS window for nothing - this is a normal/expected state
(e.g. right after a previous run already invested everything), NOT an
error: the usual summary email is still sent (0 polls, 0 attempts, exit
code 0), just so this is visible/confirmed rather than silent. The same
early-stop applies mid-run: if the balance drops below
MIN_INVESTMENT_AMOUNT at any point (e.g. it just got fully invested),
the poll loop exits right after that
poll instead of continuing to burn the rest of DURATION_SECONDS with no
possible investment left to make.

Diagnostics (full request/response detail for every real investment
attempt, every error case - including the real exception message AND
traceback, not just a generic label - AND a final "run_summary" entry with
the complete end-of-run stats dict) are written ONLY to DIAGNOSTICS_FILE,
never to stdout/the GitHub Actions console log, and persisted across runs
via `actions/cache` (NOT `actions/upload-artifact`) in the workflow - see
.github/workflows/peerberry-invest-bot.yml and repo memory
(2026-07-22 "Design decisions confirmed with user") for why cache (not a
public-repo-visible artifact) is the safe choice here: this repo has no
`pull_request`-triggered workflow anywhere, so there's no fork/outsider-
controllable code path that could ever read the cache. Since there's no
practical way to browse `actions/cache` content by hand (unlike
artifacts, there's no "Download" button), `run()` also collects THIS
run's own diagnostics entries (`_collect_run_diagnostics()`, filtered by
timestamp) and attaches them as a `.log` file directly on the end-of-run
summary email - so the full detail is available by email without ever
needing to touch the cache. The loan listing looking "stuck"
(unchanged/empty for STUCK_AFTER_SECONDS) is tallied in
`stats["stuck_events"]` (shown in the summary email) AND, since 2026-07-28,
also gets a LIGHTWEIGHT `"stuck"` diagnostics entry (poll number, total,
request duration, how long it's been stuck) written to DIAGNOSTICS_FILE -
NOT the full loan listing response, just enough to prove the request kept
succeeding (200 OK, genuinely empty/unchanged) throughout a long
"nothing matches" stretch, rather than a silent hang/broken loop. (Earlier
versions of this module wrote nothing at all for "stuck" - removed after a
real run showed 119 stuck_events with a totally empty diagnostics file,
leaving no way to confirm the polls were actually running vs. silently
broken.) Conversely, whenever `GET
/v1/{publicId}/loans` DOES return >=1 loan (whether or not it ends up
matching a selected originator), the full raw response (every loan
object, plus the request's own round-trip duration) is written to
DIAGNOSTICS_FILE as a `"loans_found"` entry - this is the proof that the
request itself works and lets a real loan's exact field values (and how
long the call took) be inspected after the fact, e.g. to check whether the
`selected_originators`/`minInterestRate`/`maxRemainingTerm` filters actually
match what's expected, or whether request latency is why a fast-moving
loan was missed. `poll_error`/`balance_refresh_error` entries also now
include `request_duration_seconds` for the same latency-diagnosis reason.

Required env vars:
    PEERBERRY_EMAIL, PEERBERRY_PASSWORD    -> PeerBerry account credentials
    SMTP_HOST, SMTP_USER, SMTP_PASSWORD,   -> outgoing mail server (end-of-run
    EMAIL_TO                                  summary email)
    GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS     -> Google Sheet holding the
                                               "Répartition géographique"
                                               PeerBerry loan originator
                                               selection (see above)
Optional:
    PEERBERRY_TOTP_SECRET     -> base32 TOTP secret, needed if 2FA is enabled
    SMTP_PORT (default 587), EMAIL_FROM (default SMTP_USER)
    DURATION_SECONDS (default "5m")        -> how long this run polls before
                                               stopping on its own. Human-
                                               friendly duration format (see
                                               `_parse_duration_seconds()`):
                                               "1h20" (1h 20m), "1h20m30s",
                                               "45m", "90s", "2h", or a plain
                                               number of seconds ("300").
    POLL_INTERVAL_SECONDS (default 0.2)    -> delay between polls
    MIN_INVESTMENT_AMOUNT (default 10)     -> skip investing below this
                                               remaining balance/loan amount
    STUCK_AFTER_SECONDS (default 45)       -> log diagnostics if the loan
                                               listing's total/ids are
                                               unchanged for this long
    FAILED_LOAN_COOLDOWN_SECONDS (default 3) -> after a failed investment
                                               attempt on a loan, skip
                                               re-attempting that same loan
                                               for this long (other loans
                                               are unaffected) - kept short
                                               since the market moves fast
                                               and a re-opened loan
                                               shouldn't be missed for long
    EXTERNAL_INVESTMENT_CHECK_INTERVAL_SECONDS (default 60) -> how often to
                                               check whether SOMETHING ELSE
                                               (PeerBerry's own "Auto-Invest
                                               EASY" scheme, or a real human,
                                               active on the same account
                                               outside this bot) invested in
                                               a selected originator, and
                                               shrink that originator's own
                                               remaining budget accordingly -
                                               see `fetch_originator_invested_amounts()`
"""

import os
import re
import sys
import json
import time
import logging
import traceback
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

from monitors.peerberry_monitor import login, PEERBERRY_EMAIL, PEERBERRY_PASSWORD, _HEADERS, API_BASE
from shared.notifier import send_peerberry_invest_bot_summary_email
from shared.google_sheet import (
    get_selected_peerberry_loan_originators,
    get_peerberry_min_interest_rate,
    get_peerberry_country_allocations,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("peerberry_invest_bot")

PROFILE_API_URL = f"{API_BASE}/v2/investor/profile"
OVERVIEW_API_URL = f"{API_BASE}/v1/investor/overview"
# Per-loan-originator invested-amount breakdown - same endpoint already
# verified live in diversification/peerberry_diversification.py. Reused here
# (NOT the unverified /en/client/my-investments page, which can't be tested
# locally - the Michelin proxy blocks the login POST for any local live
# check, see repo memory) to detect investments made by something OTHER than
# this bot (PeerBerry's own "Auto-Invest EASY" scheme, or a real human)
# during a run - see fetch_originator_invested_amounts().
ORIGINATORS_DISTRIBUTION_API_URL = f"{API_BASE}/v1/investor/overview/originators"

# UNUSED as of 2026-07-31 - see build_loans_params()'s docstring for why
# the server-side loanOriginators[] id filter was removed for good (twice
# now proven to silently exclude all loans for a real, currently-selected
# Sheet originator, causing 0 loans_seen for 75+ minutes despite normal
# market activity). Kept only for historical reference.
LOAN_ORIGINATORS = [
    4, 12, 23, 30, 33, 36, 39, 41, 43, 45, 47, 48, 49, 50, 51, 52, 53, 54,
    55, 56, 57, 58, 59, 62, 63, 64, 65, 66, 67, 68, 69, 70, 71, 72, 73, 74,
    75, 76, 77, 78,
]

# Overridden at startup in run() from the Sheet cell just left of "Peerberry"
# (shared.google_sheet.get_peerberry_min_interest_rate()) - this literal is
# only the fallback used if that read fails (soft-fail, not fatal, unlike
# the loan originator selection).
MIN_INTEREST_RATE = 8.5

_DURATION_SHORTHAND_RE = re.compile(r"^(?P<hours>\d+)h(?P<minutes>\d+)$")
_DURATION_UNITS_RE = re.compile(
    r"^(?:(?P<hours>\d+)h)?(?:(?P<minutes>\d+)m)?(?:(?P<seconds>\d+(?:\.\d+)?)s)?$"
)


def _parse_duration_seconds(value: str) -> float:
    """Parse a human-friendly duration into seconds. Accepts:
    - a plain number of seconds ("300"), for backwards compatibility;
    - explicit unit(s) in any combination, in h/m/s order ("1h20m30s",
      "45m", "90s", "2h");
    - the shorthand "<hours>h<minutes>" with no unit letter after the
      minutes ("1h20" == 1 hour 20 minutes), since that's the format the
      user actually wants to type.
    Raises ValueError if `value` doesn't match any of the above."""
    value = str(value).strip()
    if not value:
        raise ValueError("empty duration")

    try:
        return float(value)
    except ValueError:
        pass

    match = _DURATION_SHORTHAND_RE.match(value)
    if match:
        return int(match.group("hours")) * 3600 + int(match.group("minutes")) * 60

    match = _DURATION_UNITS_RE.match(value)
    if match and any(match.groups()):
        hours = int(match.group("hours") or 0)
        minutes = int(match.group("minutes") or 0)
        seconds = float(match.group("seconds") or 0)
        return hours * 3600 + minutes * 60 + seconds

    raise ValueError(f"Unrecognized duration format: {value!r} (try e.g. '1h20', '45m', '90s', or a plain number of seconds)")


DURATION_SECONDS = _parse_duration_seconds(os.environ.get("DURATION_SECONDS", "5m"))
# Lowered from 0.5s to 0.2s on 2026-07-29: real request round-trips observed
# in DIAGNOSTICS_FILE are ~0.13s, so 0.2s still leaves a bit of headroom
# while polling noticeably faster (~5 polls/s instead of ~2/s).
POLL_INTERVAL_SECONDS = float(os.environ.get("POLL_INTERVAL_SECONDS", "0.2"))
MIN_INVESTMENT_AMOUNT = float(os.environ.get("MIN_INVESTMENT_AMOUNT", "10"))
STUCK_AFTER_SECONDS = float(os.environ.get("STUCK_AFTER_SECONDS", "45"))
# Re-fetch the real "available for investment" balance from the server every
# this-many polls, to correct any drift from our locally-tracked running
# total after (attempted) investments - avoids hammering the overview
# endpoint on every single ~1s poll.
REFRESH_BALANCE_EVERY_N_POLLS = max(1, int(30 / max(POLL_INTERVAL_SECONDS, 0.1)))
# After a failed investment attempt on a loan (e.g. someone else grabbed it
# first, or it's no longer actually available despite still appearing in
# the listing due to server-side eventual consistency), skip re-attempting
# that exact loan for this long before trying it again - avoids wasting
# attempts hammering a loan that's likely already gone while other loans in
# the same poll/next polls could be invested in instead.
FAILED_LOAN_COOLDOWN_SECONDS = float(os.environ.get("FAILED_LOAN_COOLDOWN_SECONDS", "3"))
# How often to check for external investments (see ORIGINATORS_DISTRIBUTION_API_URL
# above) - a real GET, so not free to call every ~0.2s poll like the loan
# listing itself; every 60s is frequent enough to keep an active external
# bot's budget-eating in check without adding meaningful load.
EXTERNAL_INVESTMENT_CHECK_INTERVAL_SECONDS = float(os.environ.get("EXTERNAL_INVESTMENT_CHECK_INTERVAL_SECONDS", "60"))
# Max time to wait for any single HTTP call (connect+read) before giving up.
# Matching loans can disappear within seconds, so a slow/hanging request
# eating the old 8s default could waste most (or all) of a loan's whole
# availability window while doing nothing else - lowered so the bot fails
# fast and moves on (poll loop / next attempt) instead of blocking. Real
# request_duration_seconds logged in diagnostics has consistently been well
# under 1s in practice, so 4s still leaves comfortable headroom for normal
# latency/slow-response spikes without tying up the bot for 8s on a stall.
HTTP_REQUEST_TIMEOUT_SECONDS = float(os.environ.get("HTTP_REQUEST_TIMEOUT_SECONDS", "4"))

DIAGNOSTICS_FILE = Path(__file__).parent / "peerberry_invest_bot_diagnostics.log"


def _log_diagnostics(tag: str, **fields) -> None:
    """Append one JSON line of full diagnostic detail to DIAGNOSTICS_FILE
    (never printed to stdout/the console log - see module docstring)."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tag": tag,
        **fields,
    }
    try:
        with DIAGNOSTICS_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str, ensure_ascii=False) + "\n")
    except OSError:
        log.exception("Could not write to diagnostics file %s", DIAGNOSTICS_FILE)
    log.info("Diagnostics captured (tag=%s) - see %s for full detail.", tag, DIAGNOSTICS_FILE.name)


# Cap how much diagnostics text gets attached to the summary email, in case
# an unusually busy run (many invest attempts, each with a full request/
# response logged) produces an oversized attachment.
MAX_DIAGNOSTICS_EMAIL_CHARS = 2_000_000


def _collect_run_diagnostics(since: datetime) -> str | None:
    """Read DIAGNOSTICS_FILE and return only the JSON lines written at/after
    `since` (this run's own entries, since DIAGNOSTICS_FILE accumulates
    history across every past run too) - so the full request/response
    detail can be attached directly to the summary email, instead of the
    user having to manually dig it out of the GitHub Actions cache. Returns
    None if the file doesn't exist or this run added nothing to it."""
    if not DIAGNOSTICS_FILE.exists():
        return None
    lines = []
    try:
        with DIAGNOSTICS_FILE.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    entry_time = datetime.fromisoformat(entry["timestamp"])
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
                if entry_time >= since:
                    lines.append(line)
    except OSError:
        log.exception("Could not read diagnostics file %s for the summary email attachment.", DIAGNOSTICS_FILE)
        return None
    if not lines:
        return None
    text = "\n".join(lines)
    if len(text) > MAX_DIAGNOSTICS_EMAIL_CHARS:
        text = text[-MAX_DIAGNOSTICS_EMAIL_CHARS:]
        text = "(truncated, showing the last part only)\n" + text
    return text


def build_loans_params() -> dict:
    """Send only the generic, sheet-independent filters - NO
    `loanOriginators[]` server-side id filter anymore. Which originators
    actually count is decided 100% client-side, by NAME, against the
    Google-Sheet-driven selection (`selected_originators`, matched via
    `_match_selected_originator()`).

    History: an earlier version had a hardcoded 39-ID `loanOriginators[]`
    list that was completely disconnected from the Sheet-driven originator
    SELECTION - a real run then showed 0 loans_seen across ~90 minutes
    while the user was manually seeing/investing on the site at the same
    time, so the id-based filter was removed 2026-07-28 to stop that silent
    structural exclusion. It was RESTORED 2026-07-29 (a fresh 40-id list,
    claimed to be verified against the user's own live filter panel).

    REMOVED AGAIN 2026-07-31: a real run (22413 polls, ~75 minutes, right
    after the 2026-07-30 `hideInvested` fix) again showed `total=0` on
    EVERY single poll for all 33 currently-selected Sheet originators (0
    errors, request durations normal ~0.12-0.2s) - the exact same symptom
    as the 2026-07-28 bug, strongly indicating the 2026-07-29 40-id list
    does not actually cover all 33 names currently selected in the Sheet.
    Since there is no reliable way to keep a hardcoded id list in sync with
    an ever-changing Sheet selection, the server-side id filter is REMOVED
    for good this time - only sheet-independent constraints
    (`groupGuarantee`/`minInterestRate`/`maxRemainingTerm`/`minRemainingTerm`/
    `sort`/`pageSize`) are sent, and `_match_selected_originator()` alone
    decides which loans matter. The `LOAN_ORIGINATORS` constant is now
    unused dead code, kept only for historical reference - do not re-add a
    `loanOriginators[]` param to this function without a real, freshly
    re-verified id list AND a plan to keep it in sync, given this has now
    silently broken the bot twice.

    UPDATED 2026-07-30: `hideInvested` REMOVED entirely (was `"true"`), per
    the user's fresh reference URL - a run with `hideInvested=true` still
    active saw `total=0` on every single one of 22272 polls (~75 minutes),
    confirming that param no longer (if it ever did) matches the user's
    real site filter panel.

    UPDATED 2026-07-30 (later same day): `minInterestRate` now comes from
    `MIN_INTEREST_RATE` (read once at startup in run() from the Sheet cell
    just left of "Peerberry" - see get_peerberry_min_interest_rate()),
    instead of a hardcoded 8.5, so the user can change it from the Sheet
    without a code edit."""
    return {
        "sort": "-loanId",
        "groupGuarantee": "true",
        "minInterestRate": MIN_INTEREST_RATE,
        "maxRemainingTerm": 185,
        "minRemainingTerm": 1,
        "offset": 0,
        "pageSize": 40,
    }


def fetch_public_id(session: requests.Session) -> str:
    r = session.get(PROFILE_API_URL, headers=_HEADERS, timeout=HTTP_REQUEST_TIMEOUT_SECONDS)
    r.raise_for_status()
    public_id = (r.json() or {}).get("publicId")
    if not public_id:
        raise RuntimeError("PeerBerry profile response did not contain a publicId.")
    return public_id


def fetch_available_money(session: requests.Session) -> float:
    r = session.get(OVERVIEW_API_URL, headers=_HEADERS, timeout=HTTP_REQUEST_TIMEOUT_SECONDS)
    r.raise_for_status()
    try:
        return float((r.json() or {}).get("availableMoney"))
    except (TypeError, ValueError):
        return 0.0


def fetch_originator_invested_amounts(session: requests.Session) -> dict:
    """Fetch the raw {originator_name: total_invested_amount} breakdown from
    the same overview/originators endpoint already verified live by
    diversification/peerberry_diversification.py. Used to detect investments
    made by something OTHER than this bot (PeerBerry's own "Auto-Invest EASY"
    scheme, or a real human, active on the same account) during a run: any
    increase in a selected originator's total that isn't explained by this
    bot's own successful investments is, by definition, external - see the
    periodic check in run()."""
    r = session.get(ORIGINATORS_DISTRIBUTION_API_URL, headers=_HEADERS, timeout=HTTP_REQUEST_TIMEOUT_SECONDS)
    r.raise_for_status()
    amounts = {}
    for entry in r.json() or []:
        name = entry.get("originator")
        if not name:
            continue
        try:
            amounts[name] = float(entry.get("amount") or 0)
        except (TypeError, ValueError):
            amounts[name] = 0.0
    return amounts


def fetch_loans(session: requests.Session, public_id: str) -> dict:
    r = session.get(
        f"{API_BASE}/v1/{public_id}/loans",
        headers=_HEADERS,
        params=build_loans_params(),
        timeout=HTTP_REQUEST_TIMEOUT_SECONDS,
    )
    r.raise_for_status()
    return r.json() or {}


def _call_with_reauth(session: requests.Session, func, *args, **kwargs):
    """Call an authenticated API function (fetch_loans/fetch_available_money/
    ...); if it fails with HTTP 401, PeerBerry's access_token (a short-lived
    JWT, see module/monitor docstrings - observed expiring well before a
    long poll run finishes, e.g. a real run on 2026-07-23 got 401 on every
    single poll for the last ~11 minutes of an ~11.5 minute run) has
    expired or been invalidated - re-login once to get a fresh token and
    retry the call exactly once before giving up. Any other exception (or a
    second 401 after the retry) propagates normally to the caller's own
    error handling."""
    try:
        return func(session, *args, **kwargs)
    except requests.exceptions.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 401:
            log.warning("Got 401 Unauthorized calling %s - access token likely expired, re-authenticating and retrying once.", getattr(func, "__name__", func))
            login(session)
            return func(session, *args, **kwargs)
        raise


def _format_amount(amount: float) -> str:
    """Mimic the string PeerBerry's own number-input sends: whole numbers
    with no decimals ("10"), fractional ones trimmed of trailing zeros
    ("140.91"), matching the confirmed real request payload shape."""
    rounded = round(float(amount), 2)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.2f}".rstrip("0").rstrip(".")


def _match_selected_originator(loan_originator_value, selected_originators: list):
    """Match a loan's raw `loanOriginator` field (exact shape unconfirmed -
    could be a name or an id, see module docstring) against the loan
    originator names selected in the Google Sheet. Tries an exact
    case-insensitive match first, then a case-insensitive substring match
    in either direction (in case PeerBerry's value is a longer/shorter
    variant of the sheet's name). Returns the matching selected name (as
    written in the sheet), or None if none matched."""
    if loan_originator_value is None:
        return None
    value = str(loan_originator_value).strip().lower()
    if not value:
        return None
    for name in selected_originators:
        if name.strip().lower() == value:
            return name
    for name in selected_originators:
        name_lower = name.strip().lower()
        if name_lower in value or value in name_lower:
            return name
    return None



def _group_invested_by_country(raw_amounts: dict, originator_countries: dict) -> dict:
    """Group PeerBerry's raw {originator_name: invested_amount} API payload
    (see fetch_originator_invested_amounts()) into {country_name:
    total_amount}, matching each raw originator name against
    `originator_countries` (originator name -> country name, read ONCE
    from the Google Sheet at startup by
    shared.google_sheet.get_peerberry_country_allocations()) the same
    fuzzy way as `_match_selected_originator()`. This lets run()'s
    per-country invested totals be resynced directly from the live API at
    any point during the run WITHOUT ever re-reading the Sheet again - see
    the country-threshold blocking logic in run()."""
    originator_names = list(originator_countries.keys())
    totals = {}
    for raw_name, amount in raw_amounts.items():
        matched_name = _match_selected_originator(raw_name, originator_names)
        if matched_name is None:
            continue
        country = originator_countries[matched_name]
        totals[country] = totals.get(country, 0.0) + amount
    return totals


def _update_blocked_countries(country_invested: dict, threshold_amount, blocked_countries: set) -> list:
    """Add to `blocked_countries` (in place) any country in
    `country_invested` whose total has reached/exceeded `threshold_amount`
    - once a country is blocked it stays blocked for the rest of the run
    (sticky), matching the user's requirement that a country hitting its
    cap can no longer be invested in "durant le run" even if a later
    resync happens to show a lower figure. Returns the list of newly-
    blocked country names (for logging), or [] if none / no threshold
    configured (`threshold_amount` is None, i.e. the Sheet's threshold
    percentage cell was empty - country blocking disabled for this run)."""
    if threshold_amount is None:
        return []
    newly_blocked = []
    for country, amount in country_invested.items():
        if country in blocked_countries:
            continue
        if amount >= threshold_amount:
            blocked_countries.add(country)
            newly_blocked.append(country)
    return newly_blocked


def attempt_investment(session: requests.Session, loan: dict, amount: float) -> bool:
    """Real invest submission call, CONFIRMED on 2026-07-22 via a one-off
    Playwright network-interception exploration (click "Invest" -> confirm
    "Yes" on the "Assignment Agreement" popup -> intercepted+aborted the
    resulting request before it reached the server, so no real money was
    ever spent). Endpoint is `POST /v1/loans/{loanId}` with
    `{"amount": "<string>"}` - NOT the previously-guessed
    `/v1/investor/loans/{loanId}/invest`. See module docstring and repo
    memory for full details.
    Always logs the full request+response to DIAGNOSTICS_FILE, whether it
    succeeds or fails, so any remaining edge cases can be diagnosed."""
    loan_id = loan.get("loanId")
    url = f"{API_BASE}/v1/loans/{loan_id}"
    payload = {"amount": _format_amount(amount)}
    try:
        r = session.post(url, json=payload, headers=_HEADERS, timeout=HTTP_REQUEST_TIMEOUT_SECONDS)
        if r.status_code == 401:
            # Same expired-access-token situation as _call_with_reauth
            # handles for the read-only endpoints - re-login once and retry
            # this POST before treating it as a real investment failure.
            log.warning("Investment attempt for loan %s got 401 Unauthorized - re-authenticating and retrying once.", loan_id)
            login(session)
            r = session.post(url, json=payload, headers=_HEADERS, timeout=HTTP_REQUEST_TIMEOUT_SECONDS)
    except Exception as exc:
        _log_diagnostics(
            "invest_attempt_exception",
            loan=loan,
            url=url,
            payload=payload,
            error=str(exc),
            traceback=traceback.format_exc(),
        )
        log.exception("Investment attempt raised an exception for loan %s.", loan_id)
        return False

    _log_diagnostics(
        "invest_attempt",
        loan=loan,
        url=url,
        payload=payload,
        status=r.status_code,
        response_headers=dict(r.headers),
        response_body=r.text[:5000],
    )
    if r.ok:
        log.info("Investment attempt for loan %s (%.2f EUR) returned status=%s.", loan_id, amount, r.status_code)
        return True
    log.warning(
        "Investment attempt for loan %s (%.2f EUR) FAILED status=%s - full request/response saved to diagnostics.",
        loan_id,
        amount,
        r.status_code,
    )
    return False


def run() -> None:
    if not PEERBERRY_EMAIL or not PEERBERRY_PASSWORD:
        log.error("PEERBERRY_EMAIL and PEERBERRY_PASSWORD environment variables are required.")
        sys.exit(1)

    run_started_at = datetime.now(timezone.utc)

    log.info(
        "Starting PeerBerry invest bot: duration=%.0fs poll_interval=%.1fs min_investment=%.2f stuck_after=%.0fs",
        DURATION_SECONDS,
        POLL_INTERVAL_SECONDS,
        MIN_INVESTMENT_AMOUNT,
        STUCK_AFTER_SECONDS,
    )

    stats = {
        "polls": 0,
        "loans_seen": set(),
        "invest_attempts": 0,
        "invest_successes": 0,
        "invest_failures": 0,
        "total_invested_attempted": 0.0,
        "stuck_events": 0,
        "errors": 0,
        # Every distinct raw `loanOriginator` value returned by PeerBerry
        # this run, matched or not - lets a future "why didn't originator X
        # match" question be answered directly from the summary email
        # instead of digging through DIAGNOSTICS_FILE's loans_found entries.
        "raw_originators_seen": set(),
    }

    session = requests.Session()
    try:
        login(session)
        public_id = fetch_public_id(session)
        available_money = fetch_available_money(session)
    except Exception as exc:
        log.exception("Failed to log in or fetch initial account info.")
        _log_diagnostics("startup_error", step="login_or_initial_fetch", error=str(exc), traceback=traceback.format_exc())
        stats["errors"] += 1
        send_peerberry_invest_bot_summary_email(
            stats,
            error=f"Échec de connexion / récupération initiale du compte : {exc}",
            diagnostics_text=_collect_run_diagnostics(run_started_at),
        )
        sys.exit(1)

    log.info("Solde disponible au début du run : %.2f EUR", available_money)
    stats["initial_available_money"] = available_money

    if available_money < MIN_INVESTMENT_AMOUNT:
        # Not a failure - just nothing to do this run (a common, expected
        # state, e.g. right after a previous run already invested
        # everything) - stop right away instead of polling for the full
        # DURATION_SECONDS for no reason, but still send the usual summary
        # email so this is visible/confirmed rather than silent.
        log.info(
            "Available balance (%.2f EUR) is below the minimum investment amount (%.2f EUR) - nothing to do, stopping without polling.",
            available_money, MIN_INVESTMENT_AMOUNT,
        )
        _log_diagnostics("insufficient_balance", available_money=available_money, min_investment_amount=MIN_INVESTMENT_AMOUNT)
        stats["final_available_money"] = available_money
        send_peerberry_invest_bot_summary_email(
            stats,
            diagnostics_text=_collect_run_diagnostics(run_started_at),
        )
        return

    try:
        selected_originators = get_selected_peerberry_loan_originators()
    except Exception as exc:
        log.exception("Failed to read selected loan originators from the Google Sheet.")
        _log_diagnostics("startup_error", step="google_sheet_selection", error=str(exc), traceback=traceback.format_exc())
        stats["errors"] += 1
        send_peerberry_invest_bot_summary_email(
            stats,
            error=f"Échec de lecture des loan originators sélectionnés (Google Sheet) : {exc}",
            diagnostics_text=_collect_run_diagnostics(run_started_at),
        )
        sys.exit(1)

    global MIN_INTEREST_RATE
    try:
        MIN_INTEREST_RATE = get_peerberry_min_interest_rate()
    except Exception as exc:
        log.warning("Could not read minInterestRate from the Google Sheet, keeping the fallback %.2f: %s", MIN_INTEREST_RATE, exc)
        _log_diagnostics("min_interest_rate_read_error", error=str(exc), traceback=traceback.format_exc(), fallback=MIN_INTEREST_RATE)

    # Per-country investment cap (soft-fail: an error here disables country
    # blocking for this run rather than aborting it, same reasoning as
    # MIN_INTEREST_RATE above - see shared.google_sheet.get_peerberry_country_allocations()).
    try:
        country_data = get_peerberry_country_allocations()
    except Exception as exc:
        log.warning("Could not read PeerBerry country allocations from the Google Sheet - country threshold blocking disabled for this run: %s", exc)
        _log_diagnostics("country_allocations_read_error", error=str(exc), traceback=traceback.format_exc())
        country_data = {"threshold_percentage": None, "country_amounts": {}, "originator_countries": {}}

    country_threshold_percentage = country_data.get("threshold_percentage")
    # Running per-country invested total - starts from the Google Sheet
    # snapshot (as requested), then kept up to date for the rest of the run
    # purely from the live API (own successful investments update it
    # immediately, a periodic resync folds in anything external) - see
    # EXTERNAL_INVESTMENT_CHECK_INTERVAL_SECONDS below. The Sheet itself is
    # never read again after this point.
    country_invested = dict(country_data.get("country_amounts") or {})
    originator_countries = country_data.get("originator_countries") or {}
    # Countries that have reached/exceeded the threshold - sticky for the
    # rest of the run (see _update_blocked_countries()).
    blocked_countries: set = set()

    if not selected_originators:
        log.error("No PeerBerry loan originator selected in the Google Sheet (column -1 == 'x'), nothing to invest in.")
        _log_diagnostics("startup_error", error="no selected loan originators")
        stats["errors"] += 1
        send_peerberry_invest_bot_summary_email(
            stats,
            error="Aucun loan originator PeerBerry sélectionné dans le Google Sheet.",
            diagnostics_text=_collect_run_diagnostics(run_started_at),
        )
        sys.exit(1)

    stats["selected_originators"] = selected_originators

    # Per-originator detail for the end-of-run email: loans seen, attempts,
    # successes/failures, and exactly which loans got invested in for how
    # much. There is no per-originator budget anymore (removed 2026-07-31 -
    # the only investment cap left is the per-country threshold below) -
    # investments simply draw from the shared `available_money`.
    originator_stats = {
        name: {
            "loans_seen": set(),
            "attempts": 0,
            "successes": 0,
            "failures": 0,
            "invested_amount": 0.0,
            "invested_loans": [],
        }
        for name in selected_originators
    }

    # Baseline invested-per-originator snapshot, used only to compute the
    # TOTAL PeerBerry budget below (there's no more per-originator external-
    # investment budget adjustment - see EXTERNAL_INVESTMENT_CHECK_INTERVAL_SECONDS
    # below, which now only resyncs per-country totals) - soft-fail (empty
    # dict) rather than fatal, since this is a nice-to-have, not required
    # for the bot to run at all.
    try:
        initial_raw_invested = fetch_originator_invested_amounts(session)
    except Exception as exc:
        log.warning("Could not fetch the initial invested-per-originator snapshot: %s", exc)
        initial_raw_invested = {}

    # Country threshold amount = threshold_percentage% of the TOTAL PeerBerry
    # budget (everything currently invested across every loan originator,
    # summed live from the API - not just the selected ones - plus the
    # available/not-yet-invested balance) - computed once at startup and
    # kept fixed for the rest of the run.
    total_invested_all_originators = sum(initial_raw_invested.values()) if initial_raw_invested else 0.0
    total_peerberry_budget = total_invested_all_originators + available_money
    stats["total_invested_all_originators"] = total_invested_all_originators
    stats["total_peerberry_budget"] = total_peerberry_budget
    if country_threshold_percentage is not None:
        country_threshold_amount = total_peerberry_budget * country_threshold_percentage / 100.0
        log.info(
            "Seuil par pays PeerBerry : %.2f%% de %.2f EUR (investi %.2f + disponible %.2f) = %.2f EUR max par pays.",
            country_threshold_percentage, total_peerberry_budget, total_invested_all_originators, available_money, country_threshold_amount,
        )
    else:
        country_threshold_amount = None
        log.info("Aucun pourcentage de seuil par pays PeerBerry configuré - blocage par pays désactivé pour ce run.")

    initially_blocked = _update_blocked_countries(country_invested, country_threshold_amount, blocked_countries)
    for country in initially_blocked:
        log.warning(
            "Pays '%s' déjà au-dessus du seuil dès le démarrage (%.2f EUR >= %.2f EUR) - bloqué pour tout ce run.",
            country, country_invested.get(country, 0.0), country_threshold_amount,
        )

    log.info(
        "publicId=%s available_money=%.2f EUR selected_originators=%s",
        public_id, available_money, selected_originators,
    )
    # Logged once (not per-poll) so it's easy to confirm exactly what's being
    # sent to PeerBerry - NO loanOriginators[] id filter anymore (see
    # build_loans_params()'s docstring) - every loan in the response is
    # matched against `selected_originators` above, by name, client-side, to
    # decide actual investment targets. ALSO written to DIAGNOSTICS_FILE (not
    # just the console log) so this is visible in the emailed diagnostics
    # attachment too, without needing the separate GitHub Actions console
    # log - added 2026-08-03 after a run with 21989 polls/0 loans_seen had no
    # way to confirm what minInterestRate/country threshold were actually in
    # effect that run.
    loans_params = build_loans_params()
    log.info("Loans listing query params: %s", loans_params)
    _log_diagnostics(
        "run_params",
        loans_params=loans_params,
        selected_originators=selected_originators,
        country_threshold_percentage=country_threshold_percentage,
    )

    start = time.monotonic()
    last_loan_signature = None
    last_change_at = start
    run_error = None
    # loan_id -> monotonic() timestamp of its last failed investment attempt.
    recently_failed: dict = {}
    # Raw loanOriginator values already console-logged as "unmatched" this
    # run, so the same value isn't logged on every single poll.
    logged_unmatched_originators: set = set()
    # Country names already console-logged as "blocked" this run, so the
    # same country isn't logged again on every single poll once blocked.
    logged_blocked_countries: set = set()
    # (loan_id, reason) pairs already console-logged as "skipped" this run,
    # so the same loan+reason isn't logged again on every single poll it
    # keeps reappearing in the listing - reasons: "cooldown" (recent failed
    # attempt), "balance_too_low" (overall available_money too low),
    # "amount_too_low" (this specific loan's own investable amount is below
    # MIN_INVESTMENT_AMOUNT).
    logged_skip_reasons: set = set()
    last_external_check_at = start

    try:
        while time.monotonic() - start < DURATION_SECONDS:
            poll_start = time.monotonic()
            stats["polls"] += 1

            if stats["polls"] % REFRESH_BALANCE_EVERY_N_POLLS == 0:
                previous_available_money = available_money
                balance_refresh_started_at = time.monotonic()
                try:
                    available_money = _call_with_reauth(session, fetch_available_money)
                    if available_money != previous_available_money:
                        log.info("Solde disponible actualisé : %.2f EUR -> %.2f EUR.", previous_available_money, available_money)
                except Exception as exc:
                    stats["errors"] += 1
                    _log_diagnostics(
                        "balance_refresh_error",
                        error=str(exc),
                        traceback=traceback.format_exc(),
                        request_duration_seconds=round(time.monotonic() - balance_refresh_started_at, 3),
                    )
                    log.exception("Failed to refresh available balance, keeping last known value (%.2f EUR).", available_money)

            if time.monotonic() - last_external_check_at >= EXTERNAL_INVESTMENT_CHECK_INTERVAL_SECONDS:
                last_external_check_at = time.monotonic()
                try:
                    # Resync per-country invested totals directly from the
                    # live API (folds in BOTH this bot's own successful
                    # investments and anything external, e.g. PeerBerry's
                    # own "Auto-Invest EASY" scheme or a real human) - NEVER
                    # re-reads the Google Sheet, only the originator->country
                    # mapping read once at startup.
                    raw_invested = _call_with_reauth(session, fetch_originator_invested_amounts)
                    if originator_countries:
                        fresh_country_totals = _group_invested_by_country(raw_invested, originator_countries)
                        for country, amount in fresh_country_totals.items():
                            country_invested[country] = amount
                        newly_blocked = _update_blocked_countries(country_invested, country_threshold_amount, blocked_countries)
                        for country in newly_blocked:
                            log.warning(
                                "Pays '%s' vient d'atteindre le seuil (%.2f EUR >= %.2f EUR) - bloqué pour le reste du run.",
                                country, country_invested.get(country, 0.0), country_threshold_amount,
                            )
                except Exception as exc:
                    stats["errors"] += 1
                    _log_diagnostics("external_investment_check_error", error=str(exc), traceback=traceback.format_exc())
                    log.exception("Failed to check for external investments, will retry at the next interval.")

            fetch_started_at = time.monotonic()
            try:
                body = _call_with_reauth(session, fetch_loans, public_id)
            except Exception as exc:
                stats["errors"] += 1
                _log_diagnostics(
                    "poll_error",
                    error=str(exc),
                    traceback=traceback.format_exc(),
                    request_duration_seconds=round(time.monotonic() - fetch_started_at, 3),
                )
                log.exception("Failed to fetch the loans listing, will retry next poll.")
                time.sleep(POLL_INTERVAL_SECONDS)
                continue
            fetch_duration_seconds = round(time.monotonic() - fetch_started_at, 3)

            data = body.get("data") or []
            if data:
                # Only logged when the API actually returned >=1 loan (never
                # on a legitimately-empty response, to keep the diagnostics
                # file from being bloated with useless entries) - this is
                # the proof that the request itself works and shows exactly
                # what PeerBerry returned (loanId/loanOriginator/interestRate/
                # availableToInvest/...) so it can be checked against what
                # was expected (matching originators, real-time availability,
                # etc.), including how long the request itself took.
                _log_diagnostics(
                    "loans_found",
                    poll=stats["polls"],
                    total=body.get("total"),
                    request_duration_seconds=fetch_duration_seconds,
                    loans=data,
                )
            loan_ids = tuple(sorted(loan.get("loanId") for loan in data))
            signature = (body.get("total"), loan_ids)

            if signature != last_loan_signature:
                # Only logged when the signature actually changes (not every
                # poll) to avoid spamming the console. `total` can now be
                # much larger since there's no server-side loanOriginators[]
                # filter narrowing it down anymore - meaning loans further
                # down the list (older loanIds) than the 40 newest returned
                # here are invisible to this poll. Flagged here so a future
                # "why didn't it see loan X" question can start from "was
                # pagination the reason" instead of guessing.
                total_now = body.get("total") or 0
                if isinstance(total_now, (int, float)) and total_now > 40:
                    log.info("Loan listing total=%s exceeds pageSize=40 - only the 40 newest loanIds are visible this poll.", total_now)
                if data:
                    log.info(
                        "%d prêt(s) reçu(s) de l'API (poll=%d, total=%s) : %s",
                        len(data), stats["polls"], body.get("total"),
                        [(loan.get("loanId"), loan.get("loanOriginator")) for loan in data],
                    )
                last_loan_signature = signature
                last_change_at = time.monotonic()
            elif time.monotonic() - last_change_at >= STUCK_AFTER_SECONDS:
                # Counted for the summary email (how much of the run saw no
                # matching loans/change) - this is normal/expected (the
                # market is simply empty right now, not a bug). A LIGHTWEIGHT
                # diagnostics entry is also written here (just total/poll/
                # request duration, NOT the full loan array - re-added
                # 2026-07-28 after a real run had 119 stuck_events but a
                # totally empty diagnostics file, leaving no way to confirm
                # the polls were actually succeeding vs. silently broken) -
                # this proves the request itself keeps succeeding (200 OK,
                # `total: 0`/unchanged) throughout a long "nothing matches"
                # stretch, without bloating the file with repeated empty
                # loan arrays the way the old full-response dump did.
                _log_diagnostics(
                    "stuck",
                    poll=stats["polls"],
                    total=body.get("total"),
                    request_duration_seconds=fetch_duration_seconds,
                    stuck_for_seconds=round(time.monotonic() - last_change_at, 1),
                )
                stats["stuck_events"] += 1
                # Reset so we only count once per stuck period, not every poll.
                last_change_at = time.monotonic()

            # Attempt starting from the END of the listing (i.e. reversed
            # vs. PeerBerry's own "-loanId" sort order shown in the UI):
            # human investors using the real website naturally start
            # clicking from the top of their screen, so loans further down
            # the list are less likely to already be contested by the time
            # we get to them.
            loans_to_try = list(reversed(data))
            while loans_to_try:
                loan = loans_to_try.pop(0)
                loan_id = loan.get("loanId")
                stats["loans_seen"].add(loan_id)
                raw_originator = loan.get("loanOriginator")
                stats["raw_originators_seen"].add(raw_originator)

                matched_originator = _match_selected_originator(raw_originator, selected_originators)
                if matched_originator is None:
                    if raw_originator not in logged_unmatched_originators:
                        logged_unmatched_originators.add(raw_originator)
                        log.info("Loan originator '%s' (loanId=%s) does not match any selected Sheet originator - skipping.", raw_originator, loan_id)
                    continue

                originator_stats[matched_originator]["loans_seen"].add(loan_id)

                loan_country = originator_countries.get(matched_originator)
                if loan_country and loan_country in blocked_countries:
                    if loan_country not in logged_blocked_countries:
                        logged_blocked_countries.add(loan_country)
                        log.info(
                            "Pays '%s' (originator '%s', loanId=%s) a atteint/dépassé le seuil - tous les prêts de ce pays sont bloqués pour le reste du run.",
                            loan_country, matched_originator, loan_id,
                        )
                    continue

                failed_at = recently_failed.get(loan_id)
                if failed_at is not None and time.monotonic() - failed_at < FAILED_LOAN_COOLDOWN_SECONDS:
                    if (loan_id, "cooldown") not in logged_skip_reasons:
                        logged_skip_reasons.add((loan_id, "cooldown"))
                        log.info(
                            "Loan %s (originator '%s') en cooldown après un échec récent - ignoré pendant %.0fs.",
                            loan_id, matched_originator, FAILED_LOAN_COOLDOWN_SECONDS,
                        )
                    continue

                if available_money < MIN_INVESTMENT_AMOUNT:
                    if (loan_id, "balance_too_low") not in logged_skip_reasons:
                        logged_skip_reasons.add((loan_id, "balance_too_low"))
                        log.info("Remaining balance %.2f EUR is below the minimum (%.2f EUR), skipping loan %s.", available_money, MIN_INVESTMENT_AMOUNT, loan_id)
                    continue

                try:
                    loan_available = float(loan.get("availableToInvest"))
                except (TypeError, ValueError):
                    loan_available = 0.0
                amount = min(available_money, loan_available)
                if amount < MIN_INVESTMENT_AMOUNT:
                    if (loan_id, "amount_too_low") not in logged_skip_reasons:
                        logged_skip_reasons.add((loan_id, "amount_too_low"))
                        log.info(
                            "Loan %s (originator '%s') availableToInvest=%.2f -> montant investissable %.2f EUR sous le minimum (%.2f EUR) - ignoré.",
                            loan_id, matched_originator, loan_available, amount, MIN_INVESTMENT_AMOUNT,
                        )
                    continue

                log.info("Matching loan found: loanId=%s originator=%s availableToInvest=%.2f -> attempting %.2f EUR.", loan_id, matched_originator, loan_available, amount)
                stats["invest_attempts"] += 1
                originator_stats[matched_originator]["attempts"] += 1
                success = attempt_investment(session, loan, amount)
                stats["total_invested_attempted"] += amount
                if success:
                    stats["invest_successes"] += 1
                    originator_stats[matched_originator]["successes"] += 1
                    originator_stats[matched_originator]["invested_amount"] += amount
                    originator_stats[matched_originator]["invested_loans"].append({"loanId": loan_id, "amount": amount})
                    available_money -= amount
                    log.info("Solde disponible actualisé après investissement : %.2f EUR (investi %.2f EUR dans le prêt %s).", available_money, amount, loan_id)
                    if loan_country:
                        country_invested[loan_country] = country_invested.get(loan_country, 0.0) + amount
                        newly_blocked = _update_blocked_countries(country_invested, country_threshold_amount, blocked_countries)
                        for country in newly_blocked:
                            log.warning(
                                "Pays '%s' vient d'atteindre le seuil (%.2f EUR >= %.2f EUR) suite à cet investissement - bloqué pour le reste du run.",
                                country, country_invested.get(country, 0.0), country_threshold_amount,
                            )
                    continue

                stats["invest_failures"] += 1
                originator_stats[matched_originator]["failures"] += 1
                recently_failed[loan_id] = time.monotonic()
                # Don't wait for the next ~1s poll: a failure usually means
                # the market just moved (amounts/listing are now stale), so
                # immediately refresh the real balance + loan listing and
                # keep trying right away with the fresh (still reversed)
                # list, instead of continuing to work off stale data.
                try:
                    previous_available_money = available_money
                    balance_refresh_started_at = time.monotonic()
                    available_money = _call_with_reauth(session, fetch_available_money)
                    if available_money != previous_available_money:
                        log.info("Solde disponible actualisé après tentative : %.2f EUR -> %.2f EUR.", previous_available_money, available_money)
                except Exception as exc:
                    stats["errors"] += 1
                    _log_diagnostics(
                        "balance_refresh_error",
                        context="post_failure_refresh",
                        error=str(exc),
                        traceback=traceback.format_exc(),
                        request_duration_seconds=round(time.monotonic() - balance_refresh_started_at, 3),
                    )
                    log.exception("Failed to refresh balance after a failed attempt, keeping last known value (%.2f EUR).", available_money)
                try:
                    refresh_started_at = time.monotonic()
                    body = _call_with_reauth(session, fetch_loans, public_id)
                    data = body.get("data") or []
                    if data:
                        _log_diagnostics(
                            "loans_found",
                            context="post_failure_refresh",
                            poll=stats["polls"],
                            total=body.get("total"),
                            request_duration_seconds=round(time.monotonic() - refresh_started_at, 3),
                            loans=data,
                        )
                    loans_to_try = list(reversed(data))
                except Exception as exc:
                    stats["errors"] += 1
                    _log_diagnostics(
                        "poll_error",
                        context="post_failure_refresh",
                        error=str(exc),
                        traceback=traceback.format_exc(),
                        request_duration_seconds=round(time.monotonic() - refresh_started_at, 3),
                    )
                    log.exception("Failed to refresh the loans listing after a failed attempt, continuing with the remaining stale list.")

            if available_money < MIN_INVESTMENT_AMOUNT:
                # Same reasoning as the startup check: once the balance
                # drops below the minimum, no further investment is
                # possible for the rest of the run - stop polling right
                # away instead of wasting the remaining DURATION_SECONDS.
                # Not an error: the run simply exits the loop normally,
                # run_summary/the email still get sent as usual just below.
                log.info(
                    "Available balance (%.2f EUR) dropped below the minimum investment amount (%.2f EUR) - stopping the poll loop early.",
                    available_money, MIN_INVESTMENT_AMOUNT,
                )
                _log_diagnostics("balance_exhausted", available_money=available_money, min_investment_amount=MIN_INVESTMENT_AMOUNT, poll=stats["polls"])
                break

            elapsed_poll = time.monotonic() - poll_start
            remaining_sleep = POLL_INTERVAL_SECONDS - elapsed_poll
            if remaining_sleep > 0:
                time.sleep(remaining_sleep)
    except Exception as exc:
        run_error = str(exc)
        stats["errors"] += 1
        _log_diagnostics("run_error", error=run_error, traceback=traceback.format_exc())
        log.exception("Unhandled error during the poll loop.")

    stats["loans_seen"] = len(stats["loans_seen"])
    stats["raw_originators_seen"] = sorted(str(v) for v in stats["raw_originators_seen"])
    stats["final_available_money"] = available_money
    # Visible in the summary email now (was only ever console-logged before
    # 2026-08-03) - lets a "0 loans_seen" run be diagnosed directly from the
    # email: was the rate too high, or was the market genuinely empty?
    stats["min_interest_rate"] = MIN_INTEREST_RATE
    stats["country_threshold_percentage"] = country_threshold_percentage
    stats["country_threshold_amount"] = country_threshold_amount
    stats["country_invested_initial"] = dict(country_data.get("country_amounts") or {})
    stats["country_invested_final"] = dict(country_invested)
    stats["blocked_countries"] = sorted(blocked_countries)
    # Full per-country debug detail for the summary email: exactly what was
    # read from the Sheet at startup, what it ended at, and how that
    # compares to the threshold - so a wrong-looking block/non-block can be
    # diagnosed directly from the email without digging through logs.
    country_details = []
    for country in sorted(country_invested.keys()):
        final_amount = country_invested.get(country, 0.0)
        pct_of_budget = (final_amount / total_peerberry_budget * 100.0) if total_peerberry_budget else 0.0
        country_details.append({
            "country": country,
            "initial_amount": stats["country_invested_initial"].get(country, 0.0),
            "final_amount": final_amount,
            "pct_of_budget": pct_of_budget,
            "blocked": country in blocked_countries,
        })
    stats["country_details"] = country_details
    for name, s in originator_stats.items():
        s["loans_seen"] = len(s["loans_seen"])
    stats["originator_stats"] = originator_stats
    # Persisted to the cached diagnostics file too (not just emailed/console-
    # logged), so the full end-of-run stats for every past run remain
    # available to inspect later even without the email.
    _log_diagnostics("run_summary", stats=stats, run_error=run_error)
    log.info("Run finished: %s", stats)
    send_peerberry_invest_bot_summary_email(
        stats,
        error=run_error,
        diagnostics_text=_collect_run_diagnostics(run_started_at),
    )

    if run_error:
        sys.exit(1)


if __name__ == "__main__":
    run()
