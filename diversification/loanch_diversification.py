"""Loanch portfolio diversification (by loan originator) fetcher.

Logs into loanch.com (email/password + a 6-box Google Authenticator TOTP
code, same idea as Swaper/Lendermarket/PeerBerry) and fetches every active
investment via the site's own "Mes investissements"
(https://loanch.com/fr/dashboard/investments) API, then groups them by loan
originator and sums the currently remaining (not yet repaid) principal of
every active investment per originator. It also fetches this calendar
month's "Total des interets payes" / "Total des recompenses" from the
"Releve de compte" (https://loanch.com/fr/dashboard/statement) API - see
fetch_current_month_statement_totals() below. No email is sent - the
amounts are just logged and handed to fill_current_month_amounts() (see
google_sheet.py) so they can be filled into a Google Sheet,
mirroring afranga_diversification.py / peerberry_diversification.py /
lendermarket_diversification.py.

API verified against the real account on 2026-07-09, in two steps:

1. `GET https://api.loanch.com/api/v1/investments?closed=false&ordering=-invested_date&page=N&page_size=100`
   (same list endpoint/filter the dashboard's own React app uses, fetched
   here via `credentials: 'include'` - no CORS/bearer-token issues, unlike
   PeerBerry) -> `{"count", "total_pages", "next", "previous", "results": [...]}`,
   one entry per still-active investment:
   `{"id": "...", "originator_name": "Tambadana", "amount": "146.46", "closed": false, ...}`.
   Paginates via `next`/`total_pages` defensively, even though
   `page_size=100` fit all of them in one page at the time of writing.

2. Importantly, this list's `amount` field is the ORIGINALLY invested
   amount for that investment - installment loans partially repay
   principal over time without the investment being marked `closed` until
   the very last installment, so summing `amount` over-counts the actually
   still-invested capital (verified: 656.79 EUR that way vs. 529.32 EUR
   shown as `total_invested` on `GET https://api.loanch.com/api/v1/dashboard`,
   the account's ground truth). The correct currently-outstanding amount is
   the `principal_left` field, only exposed on the per-investment detail
   endpoint `GET https://api.loanch.com/api/v1/investments/<id>` (no
   trailing slash - one extra request per active investment, confirmed to
   reproduce the 529.32 EUR total exactly when summed).

Quirk worth knowing about if this script ever starts failing 2FA silently:
the machine this was developed on had an unsynced system clock (~55s
ahead of Google's `Date` response header - `w32tm /query /status` showed
"Non synchronisÃ©" / "Local CMOS Clock", and `w32tm /resync` needs admin
rights this account doesn't have) - large enough to push generated TOTP
codes outside Loanch's acceptance window and get every code rejected as
"incorrect", even though the secret itself was correct. `handle_two_factor()`
below works around this instead of requiring the OS clock to be fixed: it
re-measures the drift against the server's own `Date` response header right
before generating the code (not once at the start of `login()`, since
several seconds of human-like typing delay happen in between and the drift
is close enough to a 30s TOTP step boundary that staying fresh matters).

Required env vars:
    LOANCH_EMAIL, LOANCH_PASSWORD      -> Loanch account credentials
Optional:
    LOANCH_TOTP_SECRET                 -> base32 secret used to set up
                                           Google Authenticator, needed if
                                           2FA is enabled on the account
    GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS -> used to write this month's totals to
                                           the Google Sheet via
                                           fill_current_month_amounts() (see
                                           google_sheet.py)
"""

import calendar
import os
import sys
import logging
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pyotp
from dotenv import load_dotenv

from shared.google_sheet import fill_current_month_amounts, fill_geographic_repartition_amounts

load_dotenv()

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from shared.browser_stealth import get_context_options, apply_stealth, human_pause, human_mouse_wander, human_type

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("loanch_diversification")

LOGIN_URL = "https://loanch.com/fr/login"
INVESTMENTS_PAGE_URL = "https://loanch.com/fr/dashboard/investments"
INVESTMENTS_API_URL = "https://api.loanch.com/api/v1/investments"
STATEMENT_API_URL = "https://api.loanch.com/api/v1/statement-report"
STORAGE_STATE_FILE = Path(__file__).parent / "loanch_diversification_storage_state.json"
PAGE_SIZE = 100
# Loanch is a French platform and its "Ce mois-ci" filter means the current
# calendar month in French local time - pin the timezone explicitly instead
# of relying on the executing machine's local clock (e.g. UTC on a CI
# runner), which would compute the wrong month boundary around midnight.
REPORT_TIMEZONE = ZoneInfo("Europe/Paris")

LOANCH_EMAIL = os.environ.get("LOANCH_EMAIL")
LOANCH_PASSWORD = os.environ.get("LOANCH_PASSWORD")
LOANCH_TOTP_SECRET = os.environ.get("LOANCH_TOTP_SECRET")


def dismiss_overlays(page) -> None:
    """Dismiss the cookie consent banner and the (occasional) "get your â‚¬20
    bonus" promo modal if either shows up. Verified on 2026-07-09: the
    cookie banner's "Tout accepter" button needs a forced click (a
    subsequent overlay from the promo modal intercepts a plain click), and
    the promo modal itself has a "Fermer" button.
    """
    try:
        page.locator("button:has-text('Tout accepter')").last.click(timeout=5000, force=True)
    except PlaywrightTimeoutError:
        pass  # cookie banner never appeared, nothing to do
    page.wait_for_timeout(500)

    try:
        page.get_by_role("button", name="Fermer").click(timeout=3000)
    except PlaywrightTimeoutError:
        pass  # promo modal never appeared, nothing to do


def _measure_clock_offset(page) -> float:
    """Return (server time - local time) in seconds, measured from the
    `Date` response header of a plain request to the login page. See the
    module docstring for why this matters: the dev machine's system clock
    was unsynced (~55s off), which is enough to get every generated TOTP
    code rejected unless corrected for.
    """
    try:
        resp = page.request.get(LOGIN_URL)
        date_header = resp.headers.get("date")
        if not date_header:
            return 0.0
        server_time = parsedate_to_datetime(date_header)
        return (server_time - datetime.now(timezone.utc)).total_seconds()
    except Exception:
        return 0.0  # best-effort only - fall back to the local clock as-is


def handle_two_factor(page) -> None:
    """If Loanch prompts for a TOTP code after submitting credentials,
    generate one from LOANCH_TOTP_SECRET (clock-offset corrected, see
    module docstring) and fill it in.

    Verified against the real 2FA screen on 2026-07-09: 6 separate
    unlabeled text inputs under an "Autorisation" heading; filling the last
    one auto-submits the form (no explicit "verify" button to click).
    """
    boxes = page.locator("input[type='text']")
    try:
        boxes.first.wait_for(timeout=8000)
    except PlaywrightTimeoutError:
        return  # no 2FA prompt shown, nothing to do

    if boxes.count() != 6:
        return  # not the 2FA screen we expect, don't misfire into some other text input

    if not LOANCH_TOTP_SECRET:
        raise RuntimeError(
            "Loanch is asking for a 2FA code but LOANCH_TOTP_SECRET is not set. "
            "Set it to the base32 secret used to configure Google Authenticator."
        )

    log.info("2FA prompt detected, generating and submitting TOTP code...")
    clock_offset = _measure_clock_offset(page)
    corrected_time = datetime.now(timezone.utc) + timedelta(seconds=clock_offset)
    code = pyotp.TOTP(LOANCH_TOTP_SECRET).at(corrected_time)

    for i, digit in enumerate(code):
        human_type(boxes.nth(i), digit)
    human_pause()

    page.wait_for_timeout(1500)
    error_text = page.locator("text=Le code que vous avez saisi est incorrect")
    if error_text.count() > 0:
        raise RuntimeError("Loanch rejected the TOTP code (invalid/expired).")


def login(page) -> None:
    """Log in to Loanch using LOANCH_EMAIL/PASSWORD (and LOANCH_TOTP_SECRET
    if 2FA is enabled).

    Selectors verified against the real login form on 2026-07-09: an
    `input[type='email']` / `input[type='password']` pair (no name/id
    attributes) and a "Connexion" button. Like Afranga, the 2FA step (if
    shown) stays on the same /login URL - only the panel's content changes
    to an "Autorisation" heading - so the "already logged in" check below
    only makes sense right after the initial navigation.
    """
    log.info("Navigating to login page...")
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    dismiss_overlays(page)
    human_mouse_wander(page)

    # If a previous session was restored (see STORAGE_STATE_FILE) and is
    # still valid, Loanch redirects away from /login immediately - nothing
    # else to do.
    page.wait_for_timeout(1000)
    if page.url.rstrip("/") != LOGIN_URL.rstrip("/"):
        log.info("Reused a previous session, already logged in at %s", page.url)
        return

    log.info("Filling in credentials...")
    human_type(page.locator("input[type='email']"), LOANCH_EMAIL)
    human_pause()
    human_type(page.locator("input[type='password']"), LOANCH_PASSWORD)
    human_pause()
    page.get_by_role("button", name="Connexion").click()

    handle_two_factor(page)

    for _ in range(40):
        if page.url.rstrip("/") != LOGIN_URL.rstrip("/"):
            break
        page.wait_for_timeout(500)
    else:
        raise RuntimeError(f"Still on the login page after submitting credentials/2FA: {page.url}")
    log.info("Logged in successfully, current URL: %s", page.url)


def fetch_investments(page) -> list:
    """Fetch every still-active (closed=false) investment across all pages
    of the investments API, then fetch each one's detail endpoint to get
    its `principal_left` (see module docstring for why the list endpoint's
    `amount` field alone isn't enough - it's the original invested amount,
    not what's still outstanding on partially-repaid installment loans)."""
    investments = []
    page_number = 1
    while True:
        log.info("Requesting investments API page %d...", page_number)
        result = page.evaluate(
            """
            async ([url, pageNumber, pageSize]) => {
                const params = new URLSearchParams({
                    closed: 'false', ordering: '-invested_date', page: String(pageNumber), page_size: String(pageSize),
                });
                const res = await fetch(`${url}?${params.toString()}`, { credentials: 'include' });
                return { ok: res.ok, status: res.status, body: await res.json().catch(() => null) };
            }
            """,
            [INVESTMENTS_API_URL, page_number, PAGE_SIZE],
        )
        log.info("Investments API page %d response: ok=%s status=%s", page_number, result.get("ok"), result.get("status"))
        if not result.get("ok"):
            raise RuntimeError(f"Investments API returned status {result.get('status')} on page {page_number}")

        body = result.get("body") or {}
        page_investments = body.get("results") or []
        investments.extend(page_investments)
        log.info("Page %d: %d investment(s) found (running total: %d).", page_number, len(page_investments), len(investments))

        if not body.get("next") or page_number >= (body.get("total_pages") or 1):
            break
        page_number += 1

    log.info("Fetching principal_left detail for %d active investment(s)...", len(investments))
    for i, inv in enumerate(investments, start=1):
        detail_result = page.evaluate(
            """
            async ([url, id]) => {
                const res = await fetch(`${url}/${id}`, { credentials: 'include' });
                return { ok: res.ok, status: res.status, body: await res.json().catch(() => null) };
            }
            """,
            [INVESTMENTS_API_URL, inv["id"]],
        )
        if not detail_result.get("ok"):
            raise RuntimeError(f"Investment detail API returned status {detail_result.get('status')} for {inv['id']}")
        inv["principal_left"] = (detail_result.get("body") or {}).get("principal_left")
        if i % 20 == 0 or i == len(investments):
            log.info("Fetched detail for %d/%d investment(s) so far.", i, len(investments))

    return investments


def aggregate_by_originator(investments: list) -> list:
    """Group active investments by loan originator and sum each one's
    remaining principal (principal_left) - one entry per originator, sorted
    by amount descending."""
    totals = {}
    for inv in investments:
        originator = inv.get("originator_name") or "Unknown"
        try:
            amount = float(inv.get("principal_left"))
        except (TypeError, ValueError):
            amount = 0.0
        totals[originator] = totals.get(originator, 0.0) + amount

    originators = [{"originator": name, "amount": amount} for name, amount in totals.items()]
    originators.sort(key=lambda o: o["amount"], reverse=True)
    return originators


def fetch_current_month_statement_totals(page) -> dict:
    """Fetch this calendar month's "Total des interets payes" and "Total
    des recompenses", as shown on
    https://loanch.com/fr/dashboard/statement, via the same
    `statement-report` API the page's own "Ce mois-ci" quick filter uses.

    Verified against the real account on 2026-07-10, in two ways:

    1. Network capture while clicking "Ce mois-ci" on the statement page
       showed it requests
       `GET .../statement-report?start_date=2026-07-01&end_date=2026-07-31`
       - i.e. the entire calendar month (first day to last day), not
       "start of month to today" as one might assume - so this reproduces
       that exact range instead.
    2. The response's `total_interest` (1.59) and `total_bonus` (0) fields
       matched the "Total des interets payes" / "Total des recompenses"
       figures shown on the page exactly for July 2026.

    Uses REPORT_TIMEZONE (Europe/Paris) rather than the executing machine's
    local clock to decide what "this month" means, so this stays correct
    regardless of where/when (e.g. a UTC CI runner around midnight) this
    script actually runs.
    """
    now = datetime.now(REPORT_TIMEZONE)
    start_date = now.replace(day=1).strftime("%Y-%m-%d")
    last_day_of_month = calendar.monthrange(now.year, now.month)[1]
    end_date = now.replace(day=last_day_of_month).strftime("%Y-%m-%d")
    log.info("Requesting statement-report API for %s to %s...", start_date, end_date)

    result = page.evaluate(
        """
        async ([url, startDate, endDate]) => {
            const params = new URLSearchParams({ start_date: startDate, end_date: endDate });
            const res = await fetch(`${url}?${params.toString()}`, { credentials: 'include' });
            return { ok: res.ok, status: res.status, body: await res.json().catch(() => null) };
        }
        """,
        [STATEMENT_API_URL, start_date, end_date],
    )
    log.info("Statement-report API response: ok=%s status=%s", result.get("ok"), result.get("status"))
    if not result.get("ok"):
        raise RuntimeError(f"Statement report API returned status {result.get('status')}")

    body = result.get("body") or {}
    log.info("Raw statement-report body: %r", body)
    try:
        interest_paid = float(body.get("total_interest") or 0.0)
    except (TypeError, ValueError):
        log.warning("Could not parse 'total_interest' %r - defaulting to 0.0.", body.get("total_interest"))
        interest_paid = 0.0
    try:
        rewards = float(body.get("total_bonus") or 0.0)
    except (TypeError, ValueError):
        log.warning("Could not parse 'total_bonus' %r - defaulting to 0.0.", body.get("total_bonus"))
        rewards = 0.0

    log.info("Parsed statement totals: interest_paid=%.2f, rewards=%.2f", interest_paid, rewards)
    return {
        "interest_paid": interest_paid,
        "rewards": rewards,
    }


def run(headless: bool = True) -> None:
    if not LOANCH_EMAIL or not LOANCH_PASSWORD:
        log.error("LOANCH_EMAIL and LOANCH_PASSWORD environment variables are required.")
        sys.exit(1)

    log.info("Starting Loanch diversification run (headless=%s, storage_state_exists=%s).", headless, STORAGE_STATE_FILE.exists())

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        storage_state = str(STORAGE_STATE_FILE) if STORAGE_STATE_FILE.exists() else None
        context = browser.new_context(
            storage_state=storage_state,
            locale="fr-FR",
            **get_context_options(),
        )
        apply_stealth(context, languages="['fr-FR', 'fr']")
        page = context.new_page()

        try:
            login(page)
            page.goto(INVESTMENTS_PAGE_URL, wait_until="domcontentloaded")
            investments = fetch_investments(page)
        except Exception:
            log.exception("Failed to log in or fetch Loanch investments.")
            browser.close()
            sys.exit(1)

        try:
            log.info("Fetching this month's statement totals...")
            statement_totals = fetch_current_month_statement_totals(page)
        except Exception:
            log.exception("Failed to fetch this month's interest paid/rewards - defaulting both to 0.0.")
            statement_totals = {"interest_paid": 0.0, "rewards": 0.0}

        # Persist cookies/local storage so the next run can skip login (and
        # 2FA) while the session remains valid.
        context.storage_state(path=str(STORAGE_STATE_FILE))
        browser.close()

    originators = aggregate_by_originator(investments)
    log.info("Fetched %d active investment(s) across %d loan originator(s).", len(investments), len(originators))
    for o in originators:
        log.info("  %s: %.2f EUR", o["originator"], o["amount"])

    log.info(
        "This month's statement totals: interest_paid=%.2f EUR, rewards=%.2f EUR",
        statement_totals["interest_paid"], statement_totals["rewards"],
    )

    # Loanch's statement-report API has no gross/net/withholding-tax
    # breakdown (unlike Afranga/Bienpreter) - interest_paid is mapped to
    # both gross_interest_received/net_interest_received since it's the
    # only real figure on hand, withholding_tax defaults to 0.0. Same
    # standardized dict shape as every other *_diversification.py, plus the
    # platform-specific interest_paid/rewards fields kept alongside it.
    amounts = {
        "total": sum(o["amount"] for o in originators),
        "gross_interest_received": statement_totals["interest_paid"],
        "net_interest_received": statement_totals["interest_paid"],
        "withholding_tax": 0.0,
        "interest_paid": statement_totals["interest_paid"],
        "rewards": statement_totals["rewards"],
    }
    fill_current_month_amounts(
        platform="Loanch",
        amounts=amounts
    )

    loan_originators = [
        {"name": o["originator"], "amount": o["amount"]}
        for o in originators
    ]

    fill_geographic_repartition_amounts(loan_originators)


if __name__ == "__main__":
    # Set headless=False locally (e.g. via `python loanch_diversification.py --show`)
    # to watch the browser and debug the login flow if selectors need adjusting.
    run(headless="--show" not in sys.argv)
