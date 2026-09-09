"""Go & Grow (Bondora's savings product, https://app.goandgrow.eu/en/gogrow)
balance and statement fetcher.

Same overall data flow/Sheet-writing conventions as monefit_diversification.py
("Crowdlending savings" is a single savings product, no per-loan-originator
split), but a fundamentally different LOGIN TECHNIQUE: Go & Grow's Cloudflare
bot-management blocks Playwright/CDP-driven navigation unconditionally (every
request gets an empty `content-length: 0` response), even with a valid,
authenticated session cookie already seeded into a fresh context - confirmed
reproducible across the bundled Chromium, `channel="chrome"` and
`channel="msedge"`. A plain, non-CDP HTTP client (Python's `requests`) is NOT
blocked at all - neither for the initial GET nor for the POST/form-submission
login requests - so this script logs in via a pure-HTTP replay of the site's
Keycloak/OIDC login flow instead of using Playwright, which also happens to
satisfy running unattended in GitHub Actions trivially (no browser/Chromium
install needed at all, just `requests`).

Login flow (Keycloak/OIDC, `response_mode=form_post`), verified end-to-end
against the real account on 2026-07-17:
    1. `GET https://app.goandgrow.eu/en/gogrow` redirects (via
       `sso.bondora.com`) to a real Keycloak login page with a `<form>`
       whose `action` URL and hidden inputs (`session_code`, `execution`,
       `client_id`, `tab_id`, `client_data`, `credentialId`, ...) must be
       replayed verbatim - only `username`/`password` need to be filled in.
    2. POSTing that form returns a page containing a SECOND hidden
       auto-submit `<form>` (OIDC callback: `code`, `iss`, `id_token`,
       `state`, `session_state`, `continue`) targeting
       `https://app.goandgrow.eu/signin-oidc`.
    3. POSTing THAT form lands on the real, authenticated dashboard HTML
       with a full session cookie jar (`AppAuthCookie`, `accessToken`,
       `bsid`, Keycloak session cookies, etc.) - no JS execution needed
       anywhere in this exchange.
No 2FA/TOTP step was observed on this account.

The dashboard/statements HTML pages are just an SPA shell (data is rendered
client-side by a React app loaded via an iframe) with no server-rendered
balance/statement figures - the real data comes from a separate JSON API
discovered by grepping that SPA's JS bundle for the string "api.prd.goandgrow":
    - `GET https://api.prd.goandgrow.eu/investor/api/v2/gogrow` -> a JSON
      array of "goals" (this account currently has exactly one, named
      "Go & Grow" itself, since no custom sub-goal was created), each with
      `ClientValue` (current balance), `TotalDeposits`, `TotalEarnings`,
      `AnnualTargetReturnRate`.
    - `GET https://api.prd.goandgrow.eu/investor/api/v2/statements` -> a
      JSON array of ledger entries: `{"Type": "Deposit"|"Return"|..., ...,
      "Amount": <float>, "Date": <ISO datetime>, "Balance": <float>}`.
      "Return" entries are Go & Grow's daily interest accrual ("Go & Grow
      returns"); "Deposit" AND any "Withdraw"/"Withdrawal"-labelled entry
      (matched case-insensitively by substring, e.g. found live 2026-08-14
      on a real withdrawal) are capital movements, not income - both are
      excluded from every bucket below. IMPORTANT: confirmed 2026-08-14
      there is NO separate "fee"-labelled Type/entry for the flat 1 EUR
      fee Go & Grow charges on every withdrawal - it's silently deducted,
      never itemized in the statement - so "fees" is instead INFERRED as
      `(number of Withdraw*-Type entries this month) * WITHDRAWAL_FEE_EUR`
      and written to the Sheet's "frais" sub-row. No bonus/cashback/
      contest-labelled entry TYPE has been observed yet on this account -
      any entry whose Type is neither Deposit/Return nor Withdraw* is
      treated as bonus/cashback/contest income by default (see
      fetch_current_month_statement_totals() below), so a real one will be
      picked up automatically once it occurs; its exact Type label should
      be double-checked against the log output at that point.
    Both endpoints accept the plain `requests.Session()`'s cookies with no
    extra bearer-token/CORS workaround needed (same as most other
    *_diversification.py's directly-called JSON APIs).

Added 2026-09-09: switched the XIRR Bonus/Frais/Intérêts shares from
isolated counterfactuals (cancel ONE factor, XIRR_real - XIRR_without that
factor) to a proper Shapley-value decomposition (see
shared/xirr_shapley.py's module docstring) - the old method left an
unexplained gap between XIRR and the sum of its "explaining" shares
because XIRR is non-linear in its cashflows (interaction effects between
factors were silently dropped). Shapley shares are additive by
construction: XIRR Bonus + XIRR Frais + XIRR Intérêts now sums back to
XIRR real - XIRR with every factor neutralized (Cash drag/Taxes are
always exactly 0.0 on this platform, see below, so they don't need to be
part of the game - checked at runtime, warns if off by more than 0.0001).
IMPORTANT sign-flip vs every other platform: Go & Grow has NO withholding
tax at all (statements API has no tax breakdown), so "XIRR Taxes" is
hardcoded to 0.0 - but it DOES have a real, distinct platform fee (the
flat withdrawal fee above), so "XIRR Frais" is the one genuinely computed
via Shapley here (the OLD code mislabelled this exact same fee-based
counterfactual as "taxes_xirr_contribution"/"XIRR Taxes/Frais" - now
correctly attributed to Frais only).

Required env vars:
    GOANDGROW_EMAIL, GOANDGROW_PASSWORD  -> Go & Grow account credentials
                                             (falls back to the legacy
                                             GOANDGROW_EMAIL/GOANDGROW_PASSWORD
                                             names if the new ones aren't set)
Optional:
    GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS   -> used to write this month's totals
                                              to the Google Sheet via
                                              fill_current_month_amounts() /
                                              fill_current_month_bonus_breakdown()
                                              (see shared/google_sheet.py)

No session/cookie persistence (unlike the Playwright-based scripts'
`storage_state.json` files) - the pure-HTTP login flow above is lightweight
enough to just run fresh every time, no benefit to caching it.
"""

import os
import sys
import logging
from datetime import date, datetime
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()

from shared.google_sheet import fill_current_month_amounts, fill_current_month_bonus_breakdown, fill_geographic_repartition_amounts
from shared.report_date import get_report_now, is_current_month
from shared.weighted_average import INVESTED_BALANCE_LABEL, NON_INVESTED_BALANCE_LABEL, compute_time_weighted_average
from shared.xirr import compute_xirr
from shared.xirr_shapley import compute_shapley_xirr_shares

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("goandgrow_diversification")

LOGIN_URL = "https://app.goandgrow.eu/en/gogrow"
GOALS_API_URL = "https://api.prd.goandgrow.eu/investor/api/v2/gogrow"
STATEMENTS_API_URL = "https://api.prd.goandgrow.eu/investor/api/v2/statements"
PLATFORM_LABEL = "Go & Grow"
REPORT_TIMEZONE = ZoneInfo("Europe/Paris")

GOANDGROW_EMAIL = os.environ.get("GOANDGROW_EMAIL") or os.environ.get("GOANDGROW_EMAIL")
GOANDGROW_PASSWORD = os.environ.get("GOANDGROW_PASSWORD") or os.environ.get("GOANDGROW_PASSWORD")

# Go & Grow charges a flat 1 EUR fee on every withdrawal - NOT its own
# statement entry/Type (confirmed 2026-08-14: no "fee"-labelled Type exists
# in the statements API), so it can't be read directly and must be inferred
# as (number of withdrawals this month) * WITHDRAWAL_FEE_EUR instead.
WITHDRAWAL_FEE_EUR = 1.0


class _LoginFormParser(HTMLParser):
    """Extracts the first `<form>`'s `action` plus all of its `<input>`
    name/value pairs (including hidden fields like `session_code`,
    `execution`, `credentialId`, `code`, `id_token`, ...) - both login
    steps' forms need to be replayed verbatim, only overriding
    username/password on the first one."""

    def __init__(self):
        super().__init__()
        self.action = None
        self.inputs = {}
        self._in_form = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form" and self.action is None:
            self._in_form = True
            self.action = attrs.get("action")
        elif tag == "input" and self._in_form:
            name = attrs.get("name")
            if name:
                self.inputs[name] = attrs.get("value", "")


def _find_form(html: str):
    parser = _LoginFormParser()
    parser.feed(html)
    if not parser.action:
        raise RuntimeError("No <form> found in the page - the login flow may have changed.")
    return parser.action, parser.inputs


def login(session: requests.Session) -> None:
    """Log in to Go & Grow via a pure-HTTP replay of its Keycloak/OIDC
    `response_mode=form_post` flow - see module docstring for the full
    3-step exchange. No browser/Playwright involved at all."""
    log.info("Starting Go & Grow login (pure HTTP, no browser - see module docstring)...")

    resp = session.get(LOGIN_URL, timeout=30)
    resp.raise_for_status()
    action, inputs = _find_form(resp.text)
    inputs["username"] = GOANDGROW_EMAIL
    inputs["password"] = GOANDGROW_PASSWORD
    log.info("Submitting credentials to the Keycloak login form...")
    resp2 = session.post(action, data=inputs, timeout=30)
    resp2.raise_for_status()

    action2, inputs2 = _find_form(resp2.text)
    log.info("Submitting the OIDC callback form...")
    resp3 = session.post(action2, data=inputs2, timeout=30)
    resp3.raise_for_status()

    if len(resp3.text) < 5000:
        raise RuntimeError(
            f"Login likely failed - unexpectedly short final page "
            f"(url={resp3.url}, length={len(resp3.text)})."
        )
    log.info("Logged in successfully, dashboard length: %d", len(resp3.text))


def fetch_goals(session: requests.Session) -> list:
    """GET the account's Go & Grow goal(s) - a JSON array, each entry
    having at least `ClientValue` (current balance), `TotalDeposits` and
    `TotalEarnings`. Most accounts only have the single default goal
    (named "Go & Grow" itself), but this sums across ALL of them so a
    multi-goal account's total balance is still correct."""
    resp = session.get(GOALS_API_URL, timeout=30, headers={"Accept": "application/json"})
    resp.raise_for_status()
    goals = resp.json() or []
    log.info("Fetched %d Go & Grow goal(s): %r", len(goals), goals)
    return goals


def fetch_total_balance(goals: list) -> float:
    total = sum(float(g.get("ClientValue") or 0.0) for g in goals)
    return round(total, 2)


def fetch_all_statement_entries(session: requests.Session) -> list:
    """Fetch the FULL statement ledger (no date-range param exists on this
    endpoint - see module docstring) once, so both the current-month
    totals AND the since-inception XIRR block below can share the same
    single fetch instead of re-requesting it twice."""
    log.info("Requesting the full statements API...")
    resp = session.get(STATEMENTS_API_URL, timeout=30, headers={"Accept": "application/json"})
    resp.raise_for_status()
    entries = resp.json() or []
    log.info("Fetched %d statement entr(y/ies).", len(entries))
    return entries


def fetch_current_month_statement_totals(session: requests.Session, entries: list | None = None) -> dict:
    """Fetch this calendar month's interest ("Return"-type entries) and
    bonus/cashback/contest totals from the statements API (see module
    docstring), filtering entries by date in Python since this endpoint
    doesn't take a date-range query param (unlike most other platforms'
    equivalents) - it returns the full history, so REPORT_DATE-driven
    "this month" filtering happens locally here. `entries` can be passed in
    (already fetched via fetch_all_statement_entries()) to avoid a second
    HTTP call - fetched fresh if omitted.

    Also returns "closing_balance": each entry's own running `Balance`
    field (see module docstring) as of the LATEST entry dated on or before
    `end_date` - since the full history is already fetched here regardless
    of REPORT_DATE, a month-range backfill run for a PAST month can reuse
    this to fill in that month's real end-of-month total balance (instead
    of only ever having a live current balance available), the same way
    `ClientValue` reflects the live total today - `None` if no entry at or
    before `end_date` was found.
    """
    now = get_report_now(REPORT_TIMEZONE)
    start_date = now.replace(day=1).date()
    end_date = now.date()
    log.info("Filtering statement entries locally for %s to %s...", start_date, end_date)

    if entries is None:
        entries = fetch_all_statement_entries(session)

    interest_received = 0.0
    bonus_cashback_contest = 0.0
    withdrawal_count = 0
    unknown_types = set()
    latest_dt_at_or_before_end = None
    closing_balance = None

    for entry in entries:
        raw_date = entry.get("Date")
        try:
            entry_dt = datetime.fromisoformat(raw_date)
        except (TypeError, ValueError):
            log.warning("Could not parse statement entry date %r - skipping this entry.", raw_date)
            continue
        entry_date = entry_dt.date()

        if entry_date <= end_date and (latest_dt_at_or_before_end is None or entry_dt > latest_dt_at_or_before_end):
            latest_dt_at_or_before_end = entry_dt
            try:
                closing_balance = round(float(entry["Balance"]), 2) if entry.get("Balance") is not None else None
            except (TypeError, ValueError):
                closing_balance = None

        if not (start_date <= entry_date <= end_date):
            continue

        entry_type = (entry.get("Type") or "").strip()
        entry_type_lower = entry_type.lower()
        try:
            amount = float(entry.get("Amount") or 0.0)
        except (TypeError, ValueError):
            amount = 0.0

        if entry_type == "Return":
            interest_received += amount
        elif entry_type == "Deposit":
            continue  # capital movement, not income
        elif "withdraw" in entry_type_lower:
            withdrawal_count += 1
            continue  # capital movement, not income (fee handled separately below)
        else:
            # No bonus/cashback/contest-labelled entry Type has been seen
            # yet on this account (see module docstring) - treat anything
            # else as bonus/cashback/contest income by default.
            bonus_cashback_contest += amount
            unknown_types.add(entry_type)

    if unknown_types:
        log.info(
            "Statement entry Type(s) treated as bonus/cashback/contest this run: %s",
            unknown_types,
        )

    interest_received = round(interest_received, 2)
    bonus_cashback_contest = round(bonus_cashback_contest, 2)
    fees = round(withdrawal_count * WITHDRAWAL_FEE_EUR, 2)
    log.info(
        "This month's (%s to %s) totals: interest_received=%.2f, bonus_cashback_contest=%.2f, withdrawals=%d, fees=%.2f, closing_balance=%s",
        start_date, end_date, interest_received, bonus_cashback_contest, withdrawal_count, fees, closing_balance,
    )
    return {
        "interest_received": interest_received,
        "bonus_cashback_contest": bonus_cashback_contest,
        "fees": fees,
        "closing_balance": closing_balance,
    }


def build_xirr_cashflows(entries: list, end_date: date | None = None) -> dict:
    """Build the since-inception XIRR cashflow list + lifetime bonus/fee/
    interest totals from the REAL per-transaction, per-DATED statements
    ledger (see module docstring) - unlike Iuvo/Lendermarket/Monefit, Go &
    Grow's statements API already gives a genuine dated ledger with a real
    running `Balance` per entry, so this can use REAL cashflow dates (no
    monthly midpoint approximation needed), same tier of accuracy as
    Afranga/Swaper's own XIRR blocks.

    "Deposit"-type entries become NEGATIVE cashflows (money the investor
    put in); any entry whose Type contains "withdraw" becomes a POSITIVE
    cashflow (money returned) MINUS the flat WITHDRAWAL_FEE_EUR fee (the
    fee is silently deducted by Go & Grow, never itemized - see module
    docstring - so it must be subtracted here to keep the reconstructed
    cashflow consistent with what the investor actually received).
    "Return" entries are interest, not an external cashflow (summed into
    `lifetime_interest` instead). Everything else is bonus/cashback/contest
    income (internal, not an external cashflow either).

    `end_date` (added 2026-09-07, for a BACKFILLED past REPORT_DATE month):
    when given, entries dated AFTER it are excluded entirely - needed
    since `entries` is always the account's FULL history (this API has no
    date-range param), so a backfill run's real "today" would otherwise
    leak future-dated entries into a past month's lifetime totals.

    Returns `{"cashflows": [(date, amount), ...], "lifetime_bonus":
    float, "lifetime_fees": float, "lifetime_interest": float,
    "inception_date": date | None}`.
    """
    cashflows = []
    lifetime_bonus = 0.0
    lifetime_interest = 0.0
    withdrawal_count = 0
    inception_date = None

    for entry in entries:
        raw_date = entry.get("Date")
        try:
            entry_date = datetime.fromisoformat(raw_date).date()
        except (TypeError, ValueError):
            continue
        if end_date is not None and entry_date > end_date:
            continue
        if inception_date is None or entry_date < inception_date:
            inception_date = entry_date

        entry_type = (entry.get("Type") or "").strip()
        entry_type_lower = entry_type.lower()
        try:
            amount = float(entry.get("Amount") or 0.0)
        except (TypeError, ValueError):
            amount = 0.0

        if entry_type == "Deposit":
            cashflows.append((entry_date, -amount))
        elif "withdraw" in entry_type_lower:
            withdrawal_count += 1
            cashflows.append((entry_date, abs(amount) - WITHDRAWAL_FEE_EUR))
        elif entry_type == "Return":
            lifetime_interest += amount
        else:
            lifetime_bonus += amount

    return {
        "cashflows": cashflows,
        "lifetime_bonus": round(lifetime_bonus, 2),
        "lifetime_fees": round(withdrawal_count * WITHDRAWAL_FEE_EUR, 2),
        "lifetime_interest": round(lifetime_interest, 2),
        "inception_date": inception_date,
    }


def compute_average_balance(entries: list, start_date: date, end_date: date) -> float:
    """Day-weighted average of the account's single-goal `Balance` over
    [start_date, end_date] (new Sheet row "solde moyen pondéré investi",
    added 2026-09-08) - the ENTIRE balance counts as "invested" here (see
    module docstring: Go & Grow has no separate uninvested/idle-cash
    wallet, unlike Iuvo/Lendermarket), so "non investi" is always 0.0,
    hardcoded, same convention already used for Cash drag above.

    Unlike Iuvo/Lendermarket/Monefit's coarse monthly-midpoint
    approximation, Go & Grow's statements API already gives a REAL running
    `Balance` per dated entry (see build_xirr_cashflows()'s docstring) - so
    this converts that forward-fill balance into `(date, delta)` events
    (same technique as mintos_diversification.py's cash-side conversion)
    and feeds them into the shared, generic
    compute_time_weighted_average() - a true day-weighted average, not an
    approximation."""
    sortable_entries = []
    for entry in entries:
        raw_date = entry.get("Date")
        try:
            entry_date = datetime.fromisoformat(raw_date).date()
        except (TypeError, ValueError):
            continue
        try:
            balance = round(float(entry["Balance"]), 2) if entry.get("Balance") is not None else None
        except (TypeError, ValueError):
            balance = None
        if balance is None:
            continue
        sortable_entries.append((entry_date, balance))
    sortable_entries.sort(key=lambda pair: pair[0])

    events = []
    running_balance = 0.0
    for entry_date, balance in sortable_entries:
        delta = balance - running_balance
        events.append((entry_date, delta))
        running_balance = balance

    return compute_time_weighted_average(events, start_date, end_date, opening_balance=0.0)


def run() -> None:
    if not GOANDGROW_EMAIL or not GOANDGROW_PASSWORD:
        log.error("GOANDGROW_EMAIL and GOANDGROW_PASSWORD environment variables are required.")
        sys.exit(1)

    log.info("Starting Go & Grow diversification run (pure HTTP, no browser).")

    session = requests.Session()
    # Deliberately no browser-like User-Agent spoofing here: besides being
    # unnecessary (Cloudflare doesn't block this pure-HTTP client, see
    # module docstring), a spoofed Chrome UA has been observed to trip up
    # an unrelated LOCAL corporate proxy's obsolete-browser block when
    # testing on that network.
    session.headers.update({
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })

    try:
        login(session)
        goals = fetch_goals(session)
        balance = fetch_total_balance(goals)
    except Exception:
        log.exception("Failed to log in or fetch the Go & Grow balance.")
        sys.exit(1)

    try:
        entries = fetch_all_statement_entries(session)
        statement_totals = fetch_current_month_statement_totals(session, entries=entries)
    except Exception:
        log.exception("Failed to fetch this month's statement totals - defaulting to 0.0.")
        entries = []
        statement_totals = {"interest_received": 0.0, "bonus_cashback_contest": 0.0, "fees": 0.0, "closing_balance": None}

    log.info("Go & Grow balance: %.2f EUR", balance)
    log.info(
        "This month's interest: %.2f EUR, bonus/cashback/contest: %.2f EUR, fees: %.2f EUR",
        statement_totals["interest_received"], statement_totals["bonus_cashback_contest"], statement_totals["fees"],
    )

    current_month = is_current_month()
    today_date = get_report_now(REPORT_TIMEZONE).date()
    closing_balance = statement_totals.get("closing_balance")
    # For the real current month, always use the live goal balance
    # (fetch_total_balance()). For a backfilled past month (a month-range
    # run), the statements API's own running "Balance" as of that month's
    # end IS the real historical total - use it instead of skipping the
    # total entirely, falling back to skip_total only if no entry could be
    # found/parsed for that date.
    total = balance if current_month else (closing_balance if closing_balance is not None else balance)
    skip_total = not current_month and closing_balance is None

    # Day-weighted average invested/non-invested balances (new Sheet rows
    # "solde moyen pondéré investi"/"non investi", added 2026-09-08) - see
    # compute_average_balance()'s docstring: "non investi" is always 0.0,
    # this account has no separate uninvested cash wallet.
    avg_invested_balance = None
    avg_non_invested_balance = None
    if entries:
        month_start_date = today_date.replace(day=1)
        avg_invested_balance = compute_average_balance(entries, month_start_date, today_date)
        avg_non_invested_balance = 0.0
        log.info(
            "Solde moyen pondéré - investi: %.2f EUR (%s to %s), non investi: 0.00 EUR (pas de solde non investi séparé).",
            avg_invested_balance, month_start_date, today_date,
        )

    # Go & Grow's statements API has no gross/net/withholding-tax
    # breakdown - "interest_received" (summed "Return"-type entries) is
    # mapped to both gross_interest_received/net_interest_received,
    # withholding_tax defaults to 0.0. Same standardized dict shape as
    # every other *_diversification.py.
    amounts = {
        "total": total,
        "gross_interest_received": statement_totals["interest_received"],
        "net_interest_received": statement_totals["interest_received"],
        "withholding_tax": 0.0,
        "bonus_cashback_contest": statement_totals["bonus_cashback_contest"],
    }

    fill_current_month_amounts(
        platform=PLATFORM_LABEL,
        amounts=amounts,
        section="Crowdlending savings",
        skip_total=skip_total,
    )

    # Since-inception XIRR (money-weighted return) + the XIRR Bonus/Cash
    # drag/Taxes-Frais/Intérêts pie-chart shares - unlike Iuvo/Lendermarket/
    # Monefit, Go & Grow's statements API already gives a REAL per-
    # transaction dated ledger (see build_xirr_cashflows()'s docstring), so
    # real dates are used directly, no monthly midpoint approximation
    # needed. "Cash drag"/"XIRR Cash drag" are ALWAYS hardcoded 0.0 (a
    # real, verified figure, not a placeholder, regardless of current vs.
    # backfilled month): this account has a single goal whose ENTIRE
    # balance earns Go & Grow's daily return from day one - there is no
    # separate uninvested/idle-cash wallet in this data model (unlike
    # Iuvo's real available_funds vs receivables_p2p split), so there is
    # nothing for a cash-drag calculation to measure.
    #
    # Added 2026-09-07: this block can now ALSO be computed for a
    # BACKFILLED (past) REPORT_DATE month, not just the real current month
    # - unlike most other platforms, this doesn't need a separate
    # reconstruct_outstanding()-style helper at all, since `entries`
    # already is the account's FULL history (no date-range param exists on
    # this API) and each entry carries its own real running `Balance` -
    # the terminal value for a backfilled month is simply `closing_balance`
    # (already computed above by fetch_current_month_statement_totals() as
    # the latest entry's Balance at/before `today_date`, which for a
    # month-range backfill run IS that month's own end date via
    # REPORT_DATE). `build_xirr_cashflows(entries, end_date=today_date)`
    # excludes any entry dated AFTER `today_date` so a backfill run's real
    # "today" doesn't leak future cashflows/bonus/interest into a past
    # month's totals.
    xirr_value = None
    bonus_xirr_contribution = None
    frais_xirr_contribution = None
    interest_xirr_contribution = None
    terminal_value = balance if current_month else closing_balance
    if entries and terminal_value is not None:
        xirr_data = build_xirr_cashflows(entries, end_date=today_date)
        signed_cashflows = list(xirr_data["cashflows"])
        signed_cashflows.append((today_date, terminal_value))

        xirr_value = compute_xirr(signed_cashflows)
        if xirr_value is None:
            log.warning("Could not compute XIRR from %d real cashflow(s) - XIRR row will not be updated.", len(signed_cashflows) - 1)
        else:
            log.info(
                "Computed since-inception XIRR as of %s: %.2f%% (%d real cashflow(s), total value %.2f EUR).",
                today_date, xirr_value * 100, len(signed_cashflows) - 1, terminal_value,
            )

            lifetime_bonus_total = xirr_data["lifetime_bonus"]
            lifetime_fees_total = xirr_data["lifetime_fees"]
            # XIRR Intérêts: counterfactual XIRR share attributable to real
            # interest received since inception ("Return"-type entries, no
            # withholding tax on this platform, so lifetime_interest
            # already IS the net figure).
            lifetime_net_interest = xirr_data["lifetime_interest"]

            # Shapley decomposition (added 2026-09-09, see
            # shared/xirr_shapley.py's module docstring for why): each
            # factor's neutralizing delta below is EXACTLY the same value
            # the old isolated-contribution code used to add/subtract from
            # terminal_value one at a time - only the way the factors are
            # COMBINED changed (all 2**3=8 subsets evaluated jointly, not
            # one factor cancelled in isolation), so the resulting shares
            # are guaranteed to sum back to XIRR real - XIRR with every
            # factor neutralized (efficiency property, checked at runtime
            # via a warning log). Go & Grow has NO withholding tax at all
            # (statements API has no tax breakdown, see module docstring) -
            # "XIRR Taxes" is hardcoded to 0.0 rather than
            # duplicating/inventing a value, and is NOT part of the
            # Shapley game. Cash drag/XIRR Cash drag stay hardcoded 0.0
            # too (no separate uninvested/idle-cash wallet exists here,
            # see module docstring) - also excluded from the game (a
            # constant-zero factor can't change any other share's value).
            factor_deltas = {
                "XIRR Bonus": -lifetime_bonus_total,
                "XIRR Frais": lifetime_fees_total,
                "XIRR Intérêts": -lifetime_net_interest,
            }
            shapley_shares = compute_shapley_xirr_shares(
                signed_cashflows[:-1], today_date, terminal_value, factor_deltas,
                log=log, log_context="Go & Grow",
            )
            bonus_xirr_contribution = shapley_shares.get("XIRR Bonus")
            frais_xirr_contribution = shapley_shares.get("XIRR Frais")
            interest_xirr_contribution = shapley_shares.get("XIRR Intérêts")
            log.info(
                "XIRR Shapley shares (since-inception): %r",
                {k: round(v * 100, 4) for k, v in shapley_shares.items() if v is not None},
            )

    # No bonus/cashback/contest statement entry Type has been observed yet
    # on this account (see module docstring) - everything currently
    # defaults into "prime", same catch-all convention used by
    # monefit_diversification.py until a real one shows up and its exact
    # Type label can be mapped to the right sub-row. "frais" is a real,
    # confirmed fee bucket (see module docstring) - written on the
    # existing "frais" sub-row under the Go & Grow block. "XIRR"/"Cash
    # drag" and the XIRR Bonus/Cash drag/Taxes-Frais/Intérêts pie-chart
    # shares are only included when actually computed. "XIRR Intérêts"
    # (added 2026-09-07) sits right after "XIRR Taxes/Frais" - this pushes
    # the block one row taller than it was verified at 2026-08-14, so
    # `max_rows` is bumped 15 -> 16 to keep the search bounded before the
    # next platform block. IMPORTANT: a "XIRR Intérêts" row must exist in
    # the Go & Grow block on the sheet itself (right after "XIRR
    # Taxes/Frais") for this new value to actually land somewhere - this
    # script fills an existing row by label, it doesn't insert new
    # labelled rows into this block.
    bonus_breakdown = {"prime": statement_totals["bonus_cashback_contest"], "frais": statement_totals["fees"]}
    if xirr_value is not None:
        bonus_breakdown["XIRR"] = xirr_value
        bonus_breakdown["Cash drag"] = 0.0
        bonus_breakdown["XIRR Cash drag"] = 0.0
        bonus_breakdown["XIRR Taxes"] = 0.0
    if bonus_xirr_contribution is not None:
        bonus_breakdown["XIRR Bonus"] = bonus_xirr_contribution
    if frais_xirr_contribution is not None:
        bonus_breakdown["XIRR Frais"] = frais_xirr_contribution
    if interest_xirr_contribution is not None:
        bonus_breakdown["XIRR Intérêts"] = interest_xirr_contribution
    if avg_invested_balance is not None:
        bonus_breakdown[INVESTED_BALANCE_LABEL] = avg_invested_balance
    if avg_non_invested_balance is not None:
        bonus_breakdown[NON_INVESTED_BALANCE_LABEL] = avg_non_invested_balance
    fill_current_month_bonus_breakdown(
        platform=PLATFORM_LABEL,
        breakdown=bonus_breakdown,
        section="Crowdlending savings",
    )

    if current_month:
        fill_geographic_repartition_amounts([{"name": PLATFORM_LABEL, "amount": balance}])


if __name__ == "__main__":
    run()
