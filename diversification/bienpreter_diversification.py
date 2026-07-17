"""Bienpreter dashboard balance fetcher.

Same family as afranga_diversification.py / monefit_diversification.py etc,
but simpler and different in one key way: Bienpreter is NOT broken down by
loan originator at all here - per the user's request, this just logs into
https://www.bienpreter.com, reads two figures on the dashboard
(https://www.bienpreter.com/u/tableau-de-bord):
  - "solde disponible" (available cash balance not yet invested)
  - "capital à recevoir" (capital still to be repaid on active investments)
sums them, and hands the single total to fill_current_month_amounts() (see
google_sheet.py) - no per-originator dict, just one number, no email sent
either.

REWRITTEN 2026-07-17 to use plain `requests` instead of Playwright (no
browser at all), same technique as bricks_diversification.py /
goandgrow_diversification.py - much faster in GitHub Actions (no Chromium
download/launch). Verified live: Bienpreter is a plain Symfony
(server-rendered) site with NO Cloudflare/bot-protection at all on the
login form or the dashboard/operations pages - a vanilla `requests.Session()`
sails through with zero issues, no cookie-seeding/storage_state workaround
needed like Bricks briefly required.

Login mechanism (Symfony CSRF-protected form, verified 2026-07-17):
`GET https://www.bienpreter.com/connexion` returns an HTML form (name=
"user_login", method="post", NO `action` attribute - posts back to the same
`/connexion` URL) with hidden inputs `_csrf_token` and `user_login[_token]`
(both must be read fresh from that GET and POSTed back verbatim - they're
per-session/per-request Symfony CSRF tokens, not static). POST fields:
`user_login[email]`, `user_login[password]`, `user_login[remember_me]=1`,
plus the two token fields above. A successful login response is a 200
whose final URL (`requests` follows the redirect chain automatically) is
`/u/tableau-de-bord` - the dashboard IS the login response body itself (no
extra navigation needed), so `fetch_balances()` just parses that same
response's HTML directly instead of a second GET. No 2FA/TOTP step exists
on this account (confirmed in the original 2026-07-09 Playwright build
too).

The dashboard's markup was inspected end-to-end against the real account,
so `fetch_balances()` uses precise regex patterns (mirroring the original
Playwright DOM-scraping selectors) rather than a generic heuristic:
- "Capital à recevoir" is a proper `<dl><dt>Capital à recevoir</dt><dd
  class="...">1 220,00 €</dd></dl>` pair - found via a regex anchored on
  the `<dt>` text containing "recevoir", value from the following `<dd>`.
- "Solde disponible" is a `<div class="useroffice-box"><p>Solde
  disponible<br><span class="number big">955,25 €</span></p>...</div>`
  block - found via a regex anchored on the `<p>` text starting with
  "Solde disponible", value from the nested `<span>`.
  Note: this exact balance value also appears elsewhere on the page with
  no nearby label (top nav "Solde : ...", a bare `<span class="number
  big">`) - a generic "scan every currency-looking string and guess by
  nearby keyword" approach does NOT work reliably here (the account-
  summary panel groups several different labeled values - Capital,
  Capital remboursé, Capital à recevoir, Intérêts bruts/nets... - inside
  one shared container, so several candidates' surrounding text
  legitimately contains unrelated keywords). Hence the precise anchored
  regexes above instead.

Also fetches this calendar month's interest received (like every other
*_diversification.py's equivalent) from the "Toutes mes opérations" page
(https://www.bienpreter.com/u/operations) - see
fetch_current_month_interest_totals() below for exactly how gross/net/
withholding tax are obtained (Bienpreter has no single labeled "net
interest" figure anywhere, so this is reconstructed from NET = GROSS - TAX
using two real figures read off the page, not a guessed/configured flat
tax rate). Verified this page is ALSO plain server-rendered HTML reachable
via a normal `session.get(...)` with the same date-range/page query params
used in the original Playwright build - same row markup
(`.transaction__name`/`.transaction__amount`/`.transaction__interests`),
same "one placeholder row on out-of-range pages" pagination quirk.

Required env vars:
    BIENPRETER_EMAIL, BIENPRETER_PASSWORD -> Bienpreter account credentials
Optional:
    GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS    -> used to write this month's totals
                                              to the Google Sheet via
                                              fill_current_month_amounts() (see
                                              google_sheet.py)
"""

import re
import os
import sys
import logging

import requests
from dotenv import load_dotenv

from shared.google_sheet import fill_current_month_amounts, fill_current_month_bonus_breakdown
from shared.report_date import get_report_now

load_dotenv()

from zoneinfo import ZoneInfo

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bienpreter_diversification")

LOGIN_URL = "https://www.bienpreter.com/connexion"
OPERATIONS_URL = "https://www.bienpreter.com/u/operations"
MAX_OPERATIONS_PAGES = 300  # safety cap against an infinite loop if pagination ever misbehaves
# Bienpreter is a French platform; "this month" below means the current
# calendar month up to TODAY (1st of the month through today, NOT the full
# month) - same semantics verified for Swaper's "This Month" / Afranga's
# "Current Month" quick filters. Pinned explicitly rather than relying on
# the executing machine's local clock (e.g. UTC on a CI runner).
REPORT_TIMEZONE = ZoneInfo("Europe/Paris")

BIENPRETER_EMAIL = os.environ.get("BIENPRETER_EMAIL")
BIENPRETER_PASSWORD = os.environ.get("BIENPRETER_PASSWORD")

_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def login(session: requests.Session) -> str:
    """Log in to Bienpreter using BIENPRETER_EMAIL/BIENPRETER_PASSWORD via
    a plain HTTP POST (no browser). Returns the dashboard HTML (the login
    response's body IS the dashboard page - see module docstring).

    Raises RuntimeError if the login form's CSRF tokens can't be found, or
    if the post-login response doesn't land on /u/tableau-de-bord (wrong
    credentials show the same /connexion form again with an error banner).
    """
    log.info("GET %s (fetching login form + CSRF tokens)...", LOGIN_URL)
    r = session.get(LOGIN_URL, timeout=30)
    log.info("GET login page: status=%s", r.status_code)
    r.raise_for_status()

    csrf_match = re.search(r'name="_csrf_token" value="([^"]*)"', r.text)
    token_match = re.search(r'name="user_login\[_token\]" value="([^"]*)"', r.text)
    if not csrf_match or not token_match:
        raise RuntimeError("Could not find Bienpreter login CSRF tokens on the /connexion page.")

    payload = {
        "user_login[email]": BIENPRETER_EMAIL,
        "user_login[password]": BIENPRETER_PASSWORD,
        "user_login[remember_me]": "1",
        "_csrf_token": csrf_match.group(1),
        "user_login[_token]": token_match.group(1),
    }
    log.info("POST %s (submitting credentials)...", LOGIN_URL)
    r2 = session.post(LOGIN_URL, data=payload, timeout=30)
    log.info("POST login: status=%s, final_url=%s", r2.status_code, r2.url)
    r2.raise_for_status()

    if "/u/tableau-de-bord" not in r2.url:
        raise RuntimeError(f"Login did not reach the dashboard (still on {r2.url}) - check credentials.")
    log.info("Logged in successfully.")
    return r2.text


def _parse_amount(text: str):
    """Parse a currency-formatted amount (e.g. "955,25 €", "1 220 €",
    "€1,234.56") into a float, without assuming a fixed locale - whichever
    of ',' or '.' appears last is treated as the decimal separator, the
    other (or repeats of it) as thousands separators."""
    if not text:
        return None
    cleaned = text.replace("\xa0", " ").replace("&nbsp;", " ").strip()
    cleaned = re.sub(r"[^\d.,\s-]", "", cleaned).replace(" ", "")
    if not cleaned:
        return None

    has_comma, has_dot = "," in cleaned, "." in cleaned
    if has_comma and has_dot:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif has_comma:
        last_part = cleaned.rsplit(",", 1)[-1]
        if len(last_part) == 2:
            cleaned = cleaned.replace(",", "", cleaned.count(",") - 1).replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")

    try:
        return float(cleaned)
    except ValueError:
        return None


def _strip_tags(html_fragment: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", "", html_fragment or "")).strip()


def fetch_balances(dashboard_html: str) -> dict:
    """Parse "solde disponible" and "capital à recevoir" out of the
    dashboard HTML (the login() response body), returning both as floats.
    See module docstring for the verified regex anchors."""
    solde_match = re.search(
        r"Solde disponible.*?<span[^>]*>(.*?)</span>", dashboard_html, re.DOTALL | re.IGNORECASE
    )
    recevoir_match = re.search(
        r"<dt>[^<]*[Rr]ecevoir</dt>\s*<dd[^>]*>(.*?)</dd>", dashboard_html, re.DOTALL
    )

    if not solde_match:
        raise RuntimeError("Could not find 'Solde disponible' on the Bienpreter dashboard.")
    if not recevoir_match:
        raise RuntimeError("Could not find 'Capital à recevoir' on the Bienpreter dashboard.")

    raw_solde = _strip_tags(solde_match.group(1))
    raw_recevoir = _strip_tags(recevoir_match.group(1))
    log.info("Raw values found on the dashboard: solde=%r, capital_a_recevoir=%r", raw_solde, raw_recevoir)

    available_balance = _parse_amount(raw_solde)
    capital_to_receive = _parse_amount(raw_recevoir)
    if available_balance is None:
        raise RuntimeError(f"Could not parse 'Solde disponible' out of {raw_solde!r}.")
    if capital_to_receive is None:
        raise RuntimeError(f"Could not parse 'Capital à recevoir' out of {raw_recevoir!r}.")

    return {"available_balance": available_balance, "capital_to_receive": capital_to_receive}


def _fetch_operations_page(session: requests.Session, start_date: str, end_date: str, page_number: int) -> list:
    """Fetch one page of https://www.bienpreter.com/u/operations (plain
    server-rendered HTML, no JSON API) filtered to the given date range,
    and extract each transaction row. See module docstring for the
    verified row markup (`.transaction__name`/`.transaction__amount`/
    `.transaction__interests`)."""
    url = f"{OPERATIONS_URL}?selected-tab=1&startDate={start_date}&endDate={end_date}&page={page_number}"
    log.info("GET operations page %d for %s to %s...", page_number, start_date, end_date)
    r = session.get(url, timeout=30)
    log.info("GET operations page %d: status=%s", page_number, r.status_code)
    r.raise_for_status()

    rows_html = re.findall(r"<tr[^>]*>.*?</tr>", r.text, re.DOTALL)
    rows = []
    for row_html in rows_html:
        name_match = re.search(r'transaction__name">(.*?)</p>', row_html, re.DOTALL)
        amount_match = re.search(r'transaction__amount[^"]*">(.*?)</', row_html, re.DOTALL)
        interest_matches = re.findall(r'transaction__interests[^"]*">(.*?)</', row_html, re.DOTALL)
        rows.append(
            {
                "label": _strip_tags(name_match.group(1)) if name_match else None,
                "amountText": _strip_tags(amount_match.group(1)) if amount_match else None,
                "interestTexts": [_strip_tags(t) for t in interest_matches],
            }
        )
    return rows


def fetch_current_month_interest_totals(session: requests.Session) -> dict:
    """Fetch this calendar month's interest received, split into net/gross/
    withholding tax, from the "Toutes mes opérations" page. See module
    docstring / historical Playwright-version docstring (kept in repo
    memory) for the full reasoning behind reconstructing gross/net from
    `.transaction__interests` + "Prélèvements fiscaux" rows instead of
    `.transaction__amount` directly (Bienpreter loans are "In Fine" -
    capital bundled into a repayment row's total would otherwise
    contaminate the interest figure).

    Paginates via the `page` query param, stopping once a page returns no
    real rows (bounded by MAX_OPERATIONS_PAGES as a safety net; out-of-
    range pages render exactly one placeholder row with no `.transaction__name`,
    filtered out before checking emptiness). Uses REPORT_TIMEZONE
    (Europe/Paris) to decide "this month" (1st of the current month
    through TODAY, not the full month).
    """
    now = get_report_now(REPORT_TIMEZONE)
    start_date = now.replace(day=1).strftime("%Y-%m-%d")
    end_date = now.strftime("%Y-%m-%d")

    gross_interest_received = 0.0
    withholding_tax = 0.0

    for page_number in range(1, MAX_OPERATIONS_PAGES + 1):
        rows = _fetch_operations_page(session, start_date, end_date, page_number)
        rows = [r for r in rows if r.get("label")]
        log.info("Operations page %d: %d real row(s) found.", page_number, len(rows))
        if not rows:
            break

        for row in rows:
            for interest_text in row.get("interestTexts") or []:
                gross_interest_received += _parse_amount(interest_text) or 0.0

            if (row.get("label") or "") == "Prélèvements fiscaux":
                withholding_tax += abs(_parse_amount(row.get("amountText")) or 0.0)
    else:
        log.warning(
            "Hit MAX_OPERATIONS_PAGES (%d) without an empty page - results may be truncated.",
            MAX_OPERATIONS_PAGES,
        )

    net_interest_received = gross_interest_received - withholding_tax
    log.info(
        "Parsed interest totals: gross_interest_received=%.2f, withholding_tax=%.2f, net_interest_received=%.2f",
        gross_interest_received, withholding_tax, net_interest_received,
    )
    return {
        "net_interest_received": net_interest_received,
        "withholding_tax": withholding_tax,
        "gross_interest_received": gross_interest_received,
    }


def run() -> None:
    if not BIENPRETER_EMAIL or not BIENPRETER_PASSWORD:
        log.error("BIENPRETER_EMAIL and BIENPRETER_PASSWORD environment variables are required.")
        sys.exit(1)

    log.info("Starting Bienpreter diversification run (pure HTTP, no browser).")

    session = requests.Session()
    session.headers.update(_HEADERS)

    try:
        dashboard_html = login(session)
        balances = fetch_balances(dashboard_html)
    except Exception:
        log.exception("Failed to log in or fetch Bienpreter balances.")
        sys.exit(1)

    try:
        log.info("Fetching this month's interest totals from the operations page...")
        interest_totals = fetch_current_month_interest_totals(session)
    except Exception:
        log.exception("Failed to fetch this month's interest totals - defaulting all figures to 0.0.")
        interest_totals = {"net_interest_received": 0.0, "withholding_tax": 0.0, "gross_interest_received": 0.0}

    total = balances["available_balance"] + balances["capital_to_receive"]
    interest_totals["total"] = total
    # Verified 2026-07-17 (dumping every distinct .transaction__name label
    # over the last 180 days): no bonus/cashback/contest TRANSACTION shows
    # up in /u/operations (only Dépôt de fonds/Intention de prêt acceptée/
    # Prélèvements fiscaux/Remboursement anticipé partiel-total/
    # Remboursement mensuel/Retrait de fonds). HOWEVER this does NOT rule
    # out a separate referral/parrainage page (/u/parrainage) the way it
    # did for Swaper/Afranga - that page's investigation is BLOCKED by a
    # reproducible Bienpreter-side server error on login ("DÉSOLÉ, IL
    # SEMBLERAIT QUE NOUS AYONS RENCONTRÉ UN PROBLÈME"), not yet resolved.
    # 0.0 here is a PLACEHOLDER pending that investigation, not a verified
    # real value like the other platforms - revisit once the site recovers.
    interest_totals["bonus_cashback_contest"] = 0.0
    log.info(
        "Bienpreter: solde disponible=%.2f EUR + capital à recevoir=%.2f EUR = %.2f EUR",
        balances["available_balance"], balances["capital_to_receive"], total,
    )
    log.info(
        "This month's interest totals: gross_interest_received=%.2f EUR, net_interest_received=%.2f EUR, "
        "withholding_tax=%.2f EUR",
        interest_totals["gross_interest_received"], interest_totals["net_interest_received"], interest_totals["withholding_tax"],
    )

    fill_current_month_amounts(
        platform="Bienprêter",
        amounts=interest_totals
    )

    # Placeholder write (see the comment above bonus_cashback_contest) -
    # writes 0.0 into "prime" for now (a referral bonus, if one is ever
    # confirmed, would be a "prime" - not a cashback/concours). Update the
    # breakdown/category once /u/parrainage can actually be investigated.
    fill_current_month_bonus_breakdown(
        platform="Bienprêter",
        breakdown={"prime": interest_totals["bonus_cashback_contest"]},
    )


if __name__ == "__main__":
    run()
