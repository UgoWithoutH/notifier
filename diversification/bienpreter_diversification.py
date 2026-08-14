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

ADDED 2026-07-31: unlike the "Crowdlending" section above (single aggregate
figure), the "Répartition géographique" section's Bienprêter block IS
broken down - one row per BORROWER (emprunteur, e.g. "EXCAVAN"), with a
country-column matrix (one column per country, shared across every
platform's block on that sheet). `fetch_active_loans_by_borrower()` fetches
every currently active ("en cours") loan from
https://www.bienpreter.com/u/mes-prets (paginated), sums the invested
amount per borrower (a borrower can have several concurrent loans/
contracts), and resolves each loan's country via its project page
(https://www.bienpreter.com/projets/{id}, "Localisation" field, e.g.
"Madrid - Espagne" -> "Espagne" - see _country_from_location() for the
French-domestic-postal-code special case). The result feeds
shared.google_sheet.fill_bienpreter_borrower_geo_amounts(), which writes
each borrower's amount into the matching country column, inserts a new row
for any borrower not already listed (right after the block's last existing
borrower - explicitly re-styled to match sibling borrower rows, since an
inserted row inherits the next platform header's bold/left-aligned style
otherwise), DELETES any row whose borrower no longer has an active loan,
and rewrites every remaining row's "total" column (one column right of the
borrower name) as a live "=SOMME(...)" formula summing that row's country
columns - it deliberately never touches the "Bienprêter" row's OWN total
column (kept manual, per explicit user request).

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
same "one placeholder row on out-of-range pages" pagination quirk. Also
sums a real "Bonus" row type (confirmed live 2026-08-14 - a genuine
transaction label, not a placeholder) into `bonus_total`, replacing the
old hardcoded 0.0 placeholder that predated this discovery.

Added 2026-08-14: since-inception XIRR (money-weighted return) plus this
month's Cash drag and the XIRR Bonus / XIRR Cash drag / XIRR Taxes/Frais
pie-chart shares, mirroring swaper_diversification.py's/
afranga_diversification.py's own XIRR blocks (see those modules'
docstrings for the full methodology) - see fetch_all_operations()/
get_cached_operations()/compute_average_idle_cash() below. IMPORTANT
DIFFERENCE from Swaper/Afranga: Bienpreter's own operations table already
carries a real per-row "Solde indicatif" running balance right after
every transaction (`.transaction__balance`, verified live to match the
dashboard's own "Solde disponible" exactly for the most recent row) - so
Cash drag's day-by-day idle-cash reconstruction here just REPLAYS those
real balance snapshots instead of reconstructing a running total from
signed per-type deltas the way Swaper/Afranga have to (neither of those
platforms expose a real per-transaction balance). Every operation type's
amount here is ALSO already signed correctly by the site itself (deposits
positive, investments/withdrawals/withholding tax negative, etc. -
verified live across all 10 distinct transaction types this account has
ever had) - no direction-class/type-to-sign mapping needed either, unlike
Afranga's Details table. "Dépôt de fonds"/"Retrait de fonds" are the only
real EXTERNAL cashflows (XIRR); "Vente de prêt" (loan resold on the
secondary market) and "Rétractation de l'intention de prêt" (a loan
commitment reversed) are internal reallocations, same treatment as
Swaper's INVESTMENT/REPAYMENT/BUYBACK types - never treated as XIRR
cashflows, but DO count for the day-by-day balance replay (their real
"Solde indicatif" is used as-is).

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
import html
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

from shared.google_sheet import (
    fill_current_month_amounts,
    fill_current_month_bonus_breakdown,
    fill_bienpreter_borrower_geo_amounts,
    fill_geographic_repartition_uninvested_amount,
)
from shared.report_date import get_report_now, is_current_month
from shared.notifier import send_bienpreter_geo_issues_email
from shared.state import load_state, save_state
from shared.xirr import compute_xirr

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

# Cache of every /u/operations row ever fetched (see get_cached_operations()
# below) - same incremental-fetch idea as afranga_diversification.py's
# XIRR_CASHFLOWS_STATE_FILE, avoids re-fetching the account's entire
# history (77+ pages) on every run.
XIRR_CASHFLOWS_STATE_FILE = Path(__file__).parent / "bienpreter_xirr_cashflows_state.json"
XIRR_CASHFLOWS_STATE_DEFAULT = {"rows": [], "last_fetched_date": None}
# XIRR is a since-inception money-weighted return (not per-month) - this
# start date is early enough to cover any real account's full history.
XIRR_HISTORY_START_DATE = date(2000, 1, 1)

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


ACTIVE_LOANS_URL = "https://www.bienpreter.com/u/mes-prets"
# Fixed filter params matching the user's own reference URL (status[]=3 =
# "en cours"/active loans, not fully repaid/sold ones) - only `page` is
# overridden per request below.
ACTIVE_LOANS_BASE_PARAMS = {
    "magicSearch": "",
    "year": "",
    "status[]": "3",
    "litigation": "",
    "investmentFrom": "all",
    "bpflexEligible": "",
    "sellStatus": "",
    "orderBy": "default",
    "orderType": "ASC",
    "join": "",
}
MAX_ACTIVE_LOANS_PAGES = 20  # safety net against an infinite loop
LOAN_ROW_REGEX = re.compile(r'<tr[^>]*class="bp-tr-main"[^>]*>(.*?)</tr>', re.DOTALL)
LOAN_PROJECT_BORROWER_REGEX = re.compile(
    r'<a\s+href="/projets/(\d+)"[^>]*>.*?</a>\s*<br>\s*([^<]+?)\s*</p>', re.DOTALL
)
LOAN_AMOUNT_REGEX = re.compile(r'contract__amount[^"]*">\s*([^<]+?)\s*</td>', re.DOTALL)
PROJECT_LOCATION_REGEX = re.compile(r"<dt>\s*Localisation\s*</dt>\s*<dd[^>]*>(.*?)</dd>", re.DOTALL)
# "<p class=\"text-center\">\n    24\n    r\u00e9sultats au total\n  </p>" - used to bound
# pagination reliably instead of "stop on an empty page", since out-of-
# range pages on this site don't reliably come back empty (same bug
# already documented for the /u/operations pagination in this file's
# module docstring/repo memory - out-of-range pages can echo stray rows
# instead of a clean empty result set).
TOTAL_RESULTS_REGEX = re.compile(r"(\d+)\s*r\u00e9sultats au total", re.IGNORECASE)


def _parse_active_loans_rows(page_html: str) -> list:
    """Parses one page of https://www.bienpreter.com/u/mes-prets into a
    list of {"project_id", "borrower", "amount"} dicts - see module
    docstring section on active-loans fetching for the verified row
    markup (`<tr class="bp-tr-main">`, project link + borrower name in
    `.contract__project__name`, amount in `.contract__amount`)."""
    rows = []
    for row_html in LOAN_ROW_REGEX.findall(page_html):
        project_match = LOAN_PROJECT_BORROWER_REGEX.search(row_html)
        amount_match = LOAN_AMOUNT_REGEX.search(row_html)
        if not project_match or not amount_match:
            continue

        borrower = html.unescape(_strip_tags(project_match.group(2)))
        amount = _parse_amount(amount_match.group(1))
        if not borrower or amount is None:
            continue

        rows.append({"project_id": project_match.group(1), "borrower": borrower, "amount": amount})
    return rows


def fetch_active_loans(session: requests.Session):
    """Fetches every currently active ("en cours") Bienprêter loan from
    https://www.bienpreter.com/u/mes-prets, paginating via the `page`
    query param until a page returns no loan rows (bounded by
    MAX_ACTIVE_LOANS_PAGES as a safety net). Returns (loans, issues):
    - loans : flat list of {"project_id", "borrower", "amount"} dicts, one
      per loan/contract (a single borrower can appear multiple times, once
      per contract).
    - issues : list of short strings describing a pagination problem (hit
      the page safety net, or collected more/fewer rows than the site's
      own "X résultats au total" - both signal the result may be
      incomplete) - feeds shared.notifier.send_bienpreter_geo_issues_email()."""
    loans = []
    issues = []
    expected_total = None
    for page_number in range(1, MAX_ACTIVE_LOANS_PAGES + 1):
        params = dict(ACTIVE_LOANS_BASE_PARAMS, page=str(page_number))
        log.info("GET active loans page %d...", page_number)
        r = session.get(ACTIVE_LOANS_URL, params=params, timeout=30)
        log.info("GET active loans page %d: status=%s", page_number, r.status_code)
        r.raise_for_status()

        if expected_total is None:
            total_match = TOTAL_RESULTS_REGEX.search(r.text)
            if total_match:
                expected_total = int(total_match.group(1))
                log.info("Active loans: %d résultat(s) au total (from page 1).", expected_total)

        rows = _parse_active_loans_rows(r.text)
        log.info("Active loans page %d: %d loan(s) found.", page_number, len(rows))
        if not rows:
            break
        loans.extend(rows)
        if expected_total is not None and len(loans) >= expected_total:
            break
    else:
        message = (
            f"Pagination des prêts actifs interrompue après {MAX_ACTIVE_LOANS_PAGES} pages sans "
            "atteindre le total attendu - les résultats sont peut-être incomplets."
        )
        log.warning(message)
        issues.append(message)

    if expected_total is not None and len(loans) != expected_total:
        message = (
            f"Prêts actifs : {expected_total} résultat(s) au total attendu(s) mais {len(loans)} "
            "collecté(s) - seuls les premiers ont été conservés (une page hors limite a peut-être "
            "renvoyé des lignes erronées/dupliquées au lieu d'être vide)."
        )
        log.warning(message)
        issues.append(message)
        loans = loans[:expected_total]

    log.info("Total active loans found: %d", len(loans))
    return loans, issues


def _country_from_location(location: str):
    """Parses the project page's 'Localisation' text into just the
    country name. Two formats observed on real projects: foreign ones are
    'City - Country' (e.g. 'Madrid - Espagne', 'Bucarest - Roumanie',
    even 'Montpellier - France'), domestic (French) ones instead start
    with a postal code and have NO country at all - either just
    '13007 Marseille'/'92800 PUTEAUX', OR (confusingly) STILL a dash
    before a city name, e.g. '83330 - Le Castellet' (NOT a 'city -
    country' pair despite the dash). So: if it starts with a postal code,
    it's always France regardless of any dash; otherwise take the text
    after the last ' - ' if present, else the whole string as a fallback.
    """
    location = location.strip()
    if re.match(r"^\d{4,5}\b", location):
        return "France"
    if " - " in location:
        return location.rsplit(" - ", 1)[-1].strip()
    return location or None


def fetch_project_country(session: requests.Session, project_id: str):
    """Fetches https://www.bienpreter.com/projets/{project_id} and reads
    the '<dt>Localisation</dt><dd>...</dd>' pair, returning just the
    country part via _country_from_location(). Returns None if the
    location can't be found/parsed."""
    url = f"https://www.bienpreter.com/projets/{project_id}"
    log.info("GET project page %s (for country)...", project_id)
    r = session.get(url, timeout=30)
    log.info("GET project page %s: status=%s", project_id, r.status_code)
    r.raise_for_status()

    match = PROJECT_LOCATION_REGEX.search(r.text)
    if not match:
        log.warning("Could not find 'Localisation' on project page %s.", project_id)
        return None

    location = _strip_tags(match.group(1))
    country = _country_from_location(location)
    log.info("Project %s location=%r -> country=%r", project_id, location, country)
    return country


def fetch_active_loans_by_borrower(session: requests.Session):
    """Fetches every active loan (fetch_active_loans()) and its project's
    country (fetch_project_country(), cached per project_id since several
    contracts can point at the same project), then groups/sums by
    borrower name. Returns (borrowers, issues):
    - borrowers : {borrower_name: {"amount": float, "country": str|None}} -
      feeds fill_bienpreter_borrower_geo_amounts() in shared/google_sheet.py.
    - issues : list of short strings, one per country that couldn't be
      found/fetched or per borrower with loans in more than one country -
      feeds shared.notifier.send_bienpreter_geo_issues_email() (per
      explicit user request: any missing country or error here should be
      emailed, not just logged).
    """
    loans, issues = fetch_active_loans(session)

    project_country_cache = {}
    borrowers = {}

    for loan in loans:
        project_id = loan["project_id"]
        name = loan["borrower"]
        if project_id not in project_country_cache:
            try:
                country = fetch_project_country(session, project_id)
                if country is None:
                    issues.append(
                        f"Pays introuvable sur la page du projet {project_id} (emprunteur '{name}')."
                    )
                project_country_cache[project_id] = country
            except Exception as exc:
                log.exception("Failed to fetch country for project %s - leaving it unknown.", project_id)
                issues.append(
                    f"Erreur en récupérant le pays du projet {project_id} (emprunteur '{name}') : {exc}"
                )
                project_country_cache[project_id] = None

        country = project_country_cache[project_id]
        entry = borrowers.setdefault(name, {"amount": 0.0, "country": None})
        entry["amount"] += loan["amount"]
        if country and not entry["country"]:
            entry["country"] = country
        elif country and entry["country"] and entry["country"] != country:
            message = (
                f"Emprunteur '{name}' a des prêts dans plusieurs pays "
                f"({entry['country']} vs {country}) - seul le premier trouvé est conservé."
            )
            log.warning(message)
            issues.append(message)

    log.info("Active loans grouped by borrower: %d borrower(s) found.", len(borrowers))
    return borrowers, issues


def _fetch_operations_page(session: requests.Session, start_date: str, end_date: str, page_number: int) -> list:
    """Fetch one page of https://www.bienpreter.com/u/operations (plain
    server-rendered HTML, no JSON API) filtered to the given date range,
    and extract each transaction row. See module docstring for the
    verified row markup (`.transaction__name`/`.transaction__amount`/
    `.transaction__interests`).

    Also parses (added 2026-08-14, needed for XIRR/Cash drag below):
    - "date": the row's own transaction date ("YYYY-MM-DD"), read from the
      `.transaction__date` cell's `title` attribute (e.g.
      "14/08/26 13:32", `%d/%m/%y %H:%M`) rather than its visible
      "14/08/26" text - same value, just avoids a second regex+strip.
    - "balance": the real "Solde indicatif" running balance right AFTER
      this transaction (`.transaction__balance` cell) - verified live to
      match the dashboard's own "Solde disponible" exactly for the most
      recent row, so this can be replayed directly instead of
      reconstructing a balance from summed per-type deltas.
    """
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
        date_match = re.search(r'transaction__date"\s+title="([^"]+)"', row_html, re.DOTALL)
        balance_match = re.search(r'transaction__balance">(.*?)</', row_html, re.DOTALL)

        row_date = None
        if date_match:
            try:
                row_date = datetime.strptime(date_match.group(1).strip(), "%d/%m/%y %H:%M").strftime("%Y-%m-%d")
            except ValueError:
                row_date = None

        rows.append(
            {
                "label": _strip_tags(name_match.group(1)) if name_match else None,
                "amountText": _strip_tags(amount_match.group(1)) if amount_match else None,
                "interestTexts": [_strip_tags(t) for t in interest_matches],
                "date": row_date,
                "balance": _parse_amount(_strip_tags(balance_match.group(1))) if balance_match else None,
            }
        )
    return rows


def fetch_all_operations(session: requests.Session, start_date: str, end_date: str) -> list:
    """Fetch EVERY /u/operations row within [start_date, end_date]
    ("YYYY-MM-DD" strings), paginating via the `page` query param until a
    page returns no real rows (bounded by MAX_OPERATIONS_PAGES as a safety
    net) - the shared pagination loop behind both
    fetch_current_month_interest_totals() (this month only) and
    get_cached_operations() (full/incremental history, for XIRR/Cash drag).
    """
    all_rows = []
    for page_number in range(1, MAX_OPERATIONS_PAGES + 1):
        rows = _fetch_operations_page(session, start_date, end_date, page_number)
        rows = [r for r in rows if r.get("label")]
        log.info("Operations page %d (%s to %s): %d real row(s) found.", page_number, start_date, end_date, len(rows))
        if not rows:
            break
        all_rows.extend(rows)
    else:
        log.warning(
            "Hit MAX_OPERATIONS_PAGES (%d) without an empty page (%s to %s) - results may be truncated.",
            MAX_OPERATIONS_PAGES, start_date, end_date,
        )
    return all_rows


def get_cached_operations(session: requests.Session, end_date: date) -> list:
    """Return every /u/operations row since account inception through
    `end_date`, fetching from the site only the rows NOT already cached
    locally (in XIRR_CASHFLOWS_STATE_FILE) - same incremental-fetch idea
    as afranga_diversification.get_cached_account_details()/
    swaper_diversification.get_cached_account_cashflows() (see those
    docstrings for the full rationale). Re-fetches starting from the
    cached `last_fetched_date` itself (not the day after) so a row booked
    on that same day, added after the previous run already fetched it,
    isn't missed - duplicates are dropped by deduplicating on
    (date, label, amountText, balance): the real "balance" (a precise
    running total) makes an accidental collision between two genuinely
    different transactions extremely unlikely.
    """
    state = load_state(XIRR_CASHFLOWS_STATE_FILE, XIRR_CASHFLOWS_STATE_DEFAULT)
    cached_rows = state.get("rows", [])
    start_date = (
        datetime.strptime(state["last_fetched_date"], "%Y-%m-%d").date()
        if state.get("last_fetched_date") else XIRR_HISTORY_START_DATE
    )

    log.info(
        "Found %d cached operation row(s) (last fetched up to %s) - fetching only %s to %s...",
        len(cached_rows), state.get("last_fetched_date"), start_date, end_date,
    )
    new_rows = fetch_all_operations(session, start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d"))

    seen = set()
    merged = []
    for row in cached_rows + new_rows:
        key = (row.get("date"), row.get("label"), row.get("amountText"), row.get("balance"))
        if key in seen:
            continue
        seen.add(key)
        merged.append(row)

    save_state(XIRR_CASHFLOWS_STATE_FILE, {"rows": merged, "last_fetched_date": end_date.strftime("%Y-%m-%d")})
    log.info("Operations cache now holds %d row(s) (was %d before this run).", len(merged), len(cached_rows))
    return merged


def compute_average_idle_cash(rows: list, start_date: str, end_date: str) -> float:
    """Day-weighted average uninvested-cash balance across [start_date,
    end_date] ("YYYY-MM-DD" strings). Unlike
    swaper_diversification.compute_average_idle_cash()/
    afranga_diversification.compute_average_idle_cash() (which both
    reconstruct a running balance from summed signed per-type deltas,
    since neither platform exposes a real per-transaction balance),
    Bienpreter's own operations rows already carry the REAL "Solde
    indicatif" balance right after every transaction (see
    _fetch_operations_page()'s "balance" field) - so this just replays
    those real snapshots day-by-day instead of reconstructing anything.
    `rows` should be the FULL history (or at least back to before
    `start_date`) so the balance carried INTO `start_date` is accurate -
    passing only rows already restricted to [start_date, end_date] would
    wrongly start the average from 0.0. Returns 0.0 if no dated/balanced
    row is available at all (e.g. before the account's very first
    transaction).
    """
    dated_balances = sorted(
        ((r["date"], r["balance"]) for r in rows if r.get("date") and r.get("balance") is not None),
        key=lambda t: t[0],
    )
    if not dated_balances:
        return 0.0

    try:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        end = datetime.strptime(end_date, "%Y-%m-%d").date()
    except ValueError:
        return 0.0

    balance_by_day = {}
    for day_str, balance in dated_balances:
        balance_by_day[day_str] = balance  # ascending order -> last write = latest same-day transaction

    running_balance = 0.0  # before the account's very first transaction, balance is genuinely 0
    start_str = start.strftime("%Y-%m-%d")
    for day_str, balance in dated_balances:
        if day_str >= start_str:
            break
        running_balance = balance

    total_balance = 0.0
    day_count = 0
    current = start
    while current <= end:
        key = current.strftime("%Y-%m-%d")
        if key in balance_by_day:
            running_balance = balance_by_day[key]
        total_balance += running_balance
        day_count += 1
        current += timedelta(days=1)

    return total_balance / day_count if day_count else 0.0


def fetch_current_month_interest_totals(session: requests.Session) -> dict:
    """Fetch this calendar month's interest received, split into net/gross/
    withholding tax (plus this month's real "Bonus" total, see below), from
    the "Toutes mes op\u00e9rations" page. See module docstring / historical
    Playwright-version docstring (kept in repo memory) for the full
    reasoning behind reconstructing gross/net from `.transaction__interests`
    + "Pr\u00e9l\u00e8vements fiscaux" rows instead of `.transaction__amount` directly
    (Bienpreter loans are "In Fine" - capital bundled into a repayment
    row's total would otherwise contaminate the interest figure).

    Uses REPORT_TIMEZONE (Europe/Paris) to decide "this month" (1st of the
    current month through TODAY, not the full month).
    """
    now = get_report_now(REPORT_TIMEZONE)
    start_date = now.replace(day=1).strftime("%Y-%m-%d")
    end_date = now.strftime("%Y-%m-%d")

    rows = fetch_all_operations(session, start_date, end_date)

    gross_interest_received = 0.0
    withholding_tax = 0.0
    bonus_total = 0.0
    for row in rows:
        for interest_text in row.get("interestTexts") or []:
            gross_interest_received += _parse_amount(interest_text) or 0.0

        label = row.get("label") or ""
        if label == "Pr\u00e9l\u00e8vements fiscaux":
            withholding_tax += abs(_parse_amount(row.get("amountText")) or 0.0)
        elif label == "Bonus":
            bonus_total += abs(_parse_amount(row.get("amountText")) or 0.0)

    net_interest_received = gross_interest_received - withholding_tax
    log.info(
        "Parsed interest totals: gross_interest_received=%.2f, withholding_tax=%.2f, "
        "net_interest_received=%.2f, bonus_total=%.2f",
        gross_interest_received, withholding_tax, net_interest_received, bonus_total,
    )
    return {
        "net_interest_received": net_interest_received,
        "withholding_tax": withholding_tax,
        "gross_interest_received": gross_interest_received,
        "bonus_total": bonus_total,
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
        interest_totals = {
            "net_interest_received": 0.0, "withholding_tax": 0.0,
            "gross_interest_received": 0.0, "bonus_total": 0.0,
        }

    total = balances["available_balance"] + balances["capital_to_receive"]
    interest_totals["total"] = total
    # UPDATED 2026-08-14: the 2026-07-17 "no bonus/cashback/contest
    # TRANSACTION shows up in /u/operations" finding turned out to be
    # stale - a real "Bonus" transaction type DOES exist on this account
    # now (confirmed live 2026-08-14, 3 lifetime rows of 500/100/10 EUR) -
    # bonus_total (this month's real sum, from fetch_current_month_interest_totals())
    # replaces the old hardcoded 0.0 placeholder.
    interest_totals["bonus_cashback_contest"] = interest_totals.get("bonus_total", 0.0)
    log.info(
        "Bienpreter: solde disponible=%.2f EUR + capital à recevoir=%.2f EUR = %.2f EUR",
        balances["available_balance"], balances["capital_to_receive"], total,
    )
    log.info(
        "This month's interest totals: gross_interest_received=%.2f EUR, net_interest_received=%.2f EUR, "
        "withholding_tax=%.2f EUR, bonus_total=%.2f EUR",
        interest_totals["gross_interest_received"], interest_totals["net_interest_received"],
        interest_totals["withholding_tax"], interest_totals["bonus_total"],
    )

    # XIRR (like "total" elsewhere in this repo) is a LIVE-only snapshot
    # metric (needs TODAY's real total account value as its final
    # cashflow) - it can't be meaningfully backfilled for a past REPORT_DATE
    # month, so it's only ever computed/written for the real current month.
    current_month = is_current_month()

    # Since-inception XIRR (money-weighted return) + this month's/lifetime
    # Cash drag + the XIRR Bonus/Cash drag/Taxes-Frais pie-chart shares -
    # mirrors swaper_diversification.py's/afranga_diversification.py's own
    # XIRR blocks (see those modules' docstrings for the full methodology;
    # see THIS module's docstring for the Bienpreter-specific differences -
    # a real per-transaction "Solde indicatif" balance is already on every
    # /u/operations row, so no delta-reconstruction is needed here).
    today_date = get_report_now(REPORT_TIMEZONE).date()
    xirr_value = None
    bonus_xirr_contribution = None
    cash_drag_value = None
    cash_drag_xirr_contribution = None
    taxes_xirr_contribution = None

    all_operations = None
    if current_month:
        try:
            log.info("Fetching the since-inception operations history (cached where possible) for XIRR/Cash drag...")
            all_operations = get_cached_operations(session, today_date)
        except Exception:
            log.exception("Failed to fetch the operations history - XIRR/Cash drag will not be updated.")
            all_operations = None

    total_invested = balances["capital_to_receive"]
    if current_month and all_operations:
        total_account_value = total  # solde disponible + capital à recevoir, same "as if withdrawn today" value used elsewhere in this repo

        signed_cashflows = []
        for row in all_operations:
            if not row.get("date"):
                continue
            row_date = datetime.strptime(row["date"], "%Y-%m-%d").date()
            amount = abs(_parse_amount(row.get("amountText")) or 0.0)
            if row["label"] == "Dépôt de fonds":
                signed_cashflows.append((row_date, -amount))
            elif row["label"] == "Retrait de fonds":
                signed_cashflows.append((row_date, amount))
        signed_cashflows.sort(key=lambda t: t[0])
        signed_cashflows.append((today_date, total_account_value))

        xirr_value = compute_xirr(signed_cashflows)
        if xirr_value is None:
            log.warning("Could not compute XIRR from %d cashflow(s) - XIRR row will not be updated.", len(signed_cashflows) - 1)
        else:
            log.info(
                "Computed since-inception XIRR: %.2f%% (%d deposit/withdrawal cashflow(s), current total value %.2f EUR).",
                xirr_value * 100, len(signed_cashflows) - 1, total_account_value,
            )

            lifetime_bonus_total = sum(
                abs(_parse_amount(r.get("amountText")) or 0.0) for r in all_operations if r["label"] == "Bonus"
            )
            bonus_rows = [r for r in all_operations if r["label"] == "Bonus"]
            log.info(
                "Bonus: %d transaction(s) trouvée(s), total lifetime = %.2f EUR (dates: %s).",
                len(bonus_rows), lifetime_bonus_total,
                [r.get("date") for r in bonus_rows],
            )
            if lifetime_bonus_total:
                cashflows_without_bonus = signed_cashflows[:-1] + [(today_date, total_account_value - lifetime_bonus_total)]
                log.info(
                    "XIRR Bonus - valeur du compte réelle=%.2f EUR, valeur contre-factuelle (sans bonus)=%.2f EUR (delta=%.2f EUR, soit %.1f%% de la valeur totale).",
                    total_account_value, total_account_value - lifetime_bonus_total, lifetime_bonus_total,
                    100 * lifetime_bonus_total / total_account_value if total_account_value else 0,
                )
                xirr_without_bonus = compute_xirr(cashflows_without_bonus)
                log.info("XIRR contre-factuel sans bonus = %s", f"{xirr_without_bonus*100:.2f}%" if xirr_without_bonus is not None else "None (non calculable)")
                if xirr_without_bonus is not None:
                    bonus_xirr_contribution = xirr_value - xirr_without_bonus
                    log.info("Bonus's own share of XIRR: %.2f points.", bonus_xirr_contribution * 100)
                    log.info(
                        "-> XIRR réel=%.2f%%, XIRR sans bonus=%.2f%%, écart=%.2f points, sur %.2f an(s) depuis le premier dépôt.",
                        xirr_value * 100, xirr_without_bonus * 100, bonus_xirr_contribution * 100,
                        (today_date - min(d for d, a in signed_cashflows[:-1] if a < 0)).days / 365.25,
                    )
            else:
                bonus_xirr_contribution = 0.0

            lifetime_withholding_tax = sum(
                abs(_parse_amount(r.get("amountText")) or 0.0) for r in all_operations if r["label"] == "Prélèvements fiscaux"
            )
            if lifetime_withholding_tax:
                cashflows_with_taxes_cancelled = signed_cashflows[:-1] + [(today_date, total_account_value + lifetime_withholding_tax)]
                xirr_with_taxes_cancelled = compute_xirr(cashflows_with_taxes_cancelled)
                if xirr_with_taxes_cancelled is not None:
                    taxes_xirr_contribution = xirr_value - xirr_with_taxes_cancelled
                    log.info(
                        "XIRR share - taxes/frais: %.4f points (lifetime withholding tax %.2f EUR).",
                        taxes_xirr_contribution * 100, lifetime_withholding_tax,
                    )
            else:
                taxes_xirr_contribution = 0.0

            if total_invested > 0:
                month_start_str = today_date.replace(day=1).strftime("%Y-%m-%d")
                today_str = today_date.strftime("%Y-%m-%d")
                avg_idle_cash = compute_average_idle_cash(all_operations, month_start_str, today_str)
                cash_weight = avg_idle_cash / (avg_idle_cash + total_invested)
                monthly_yield_rate = interest_totals["gross_interest_received"] / total_invested
                cash_drag_value = cash_weight * monthly_yield_rate
                log.info(
                    "Computed Cash drag: %.2f%% (avg idle cash %.2f EUR, cash weight %.2f%%, monthly yield %.2f%%).",
                    cash_drag_value * 100, avg_idle_cash, cash_weight * 100, monthly_yield_rate * 100,
                )

                deposit_dates = [r["date"] for r in all_operations if r.get("date") and r["label"] == "Dépôt de fonds"]
                if deposit_dates:
                    since_inception_date = datetime.strptime(min(deposit_dates), "%Y-%m-%d").date()
                    years_elapsed = max((today_date - since_inception_date).days / 365.25, 1 / 365.25)
                    lifetime_gross_interest = sum(
                        _parse_amount(t) or 0.0 for r in all_operations for t in (r.get("interestTexts") or [])
                    )
                    avg_idle_cash_lifetime = compute_average_idle_cash(
                        all_operations, since_inception_date.strftime("%Y-%m-%d"), today_str
                    )
                    cash_weight_lifetime = avg_idle_cash_lifetime / (avg_idle_cash_lifetime + total_invested)
                    lifetime_yield_rate = lifetime_gross_interest / total_invested
                    cash_drag_lifetime_total = cash_weight_lifetime * lifetime_yield_rate
                    missed_earnings = cash_drag_lifetime_total * (avg_idle_cash_lifetime + total_invested)
                    cashflows_with_cash_invested = signed_cashflows[:-1] + [
                        (today_date, total_account_value + missed_earnings)
                    ]
                    xirr_with_cash_invested = compute_xirr(cashflows_with_cash_invested)
                    if xirr_with_cash_invested is not None:
                        cash_drag_xirr_contribution = xirr_value - xirr_with_cash_invested
                        log.info(
                            "XIRR share - cash drag: %.4f points (since-inception, %.2f years, missed earnings ~%.2f EUR).",
                            cash_drag_xirr_contribution * 100, years_elapsed, missed_earnings,
                        )

    # "total" = solde disponible + capital à recevoir, both scraped from
    # LIVE-only dashboard widgets with no date param and no historical/
    # closing-balance equivalent found anywhere on the site (2026-08-06
    # investigation) - skip it for a backfilled month.
    fill_current_month_amounts(
        platform="Bienprêter",
        amounts=interest_totals,
        skip_total=not current_month,
    )

    # "prime" now gets the real "Bonus" transaction total (see above -
    # replaces the old placeholder). "prélèvements" (withholding tax on
    # interest, real figure - see fetch_current_month_interest_totals())
    # is a separate sub-row in the same block, right before "Rendements %"
    # - verified live 2026-08-05. "Cash drag"/"XIRR"/"XIRR Bonus"/
    # "XIRR Cash drag"/"XIRR Taxes/Frais" (added 2026-08-14, only included
    # when actually computed) sit right after "concours" - verified live
    # at platform_row+9 through +13, `max_rows=14` keeps the search bounded
    # before the next platform block ("Hive5", platform_row+16).
    bonus_breakdown = {
        "prime": interest_totals["bonus_cashback_contest"],
        "prélèvements": interest_totals["withholding_tax"],
    }
    if cash_drag_value is not None:
        bonus_breakdown["Cash drag"] = cash_drag_value
    if xirr_value is not None:
        bonus_breakdown["XIRR"] = xirr_value
    if bonus_xirr_contribution is not None:
        bonus_breakdown["XIRR Bonus"] = bonus_xirr_contribution
    if cash_drag_xirr_contribution is not None:
        bonus_breakdown["XIRR Cash drag"] = cash_drag_xirr_contribution
    if taxes_xirr_contribution is not None:
        bonus_breakdown["XIRR Taxes/Frais"] = taxes_xirr_contribution
    fill_current_month_bonus_breakdown(
        platform="Bienprêter",
        breakdown=bonus_breakdown,
        max_rows=14,
    )

    # "Répartition géographique" per-borrower breakdown (added 2026-07-31,
    # per explicit user request): does NOT touch the "Bienprêter" row's own
    # total (kept manual) - only the borrower sub-rows below it, one per
    # active loan's company, amount placed under its loan's country column.
    # Any missing/ambiguous country or unexpected error is emailed (per
    # explicit user request), not just logged - never blocks the rest of
    # the run (Crowdlending section writes already happened above).
    geo_issues = []
    geo_error = None
    if current_month:
        try:
            log.info("Fetching active loans grouped by borrower (for the geographic breakdown)...")
            borrowers, fetch_issues = fetch_active_loans_by_borrower(session)
            geo_issues.extend(fetch_issues)
            geo_issues.extend(fill_bienpreter_borrower_geo_amounts(borrowers))
        except Exception as exc:
            log.exception("Failed to update the Bienprêter geographic breakdown by borrower.")
            geo_error = str(exc)

        # "non investi" row (added 2026-08-10): the same "solde disponible"
        # already scraped above for the Crowdlending section's "total".
        try:
            fill_geographic_repartition_uninvested_amount("Bienprêter", balances["available_balance"])
        except Exception as exc:
            log.exception("Failed to update Bienprêter's 'non investi' row.")
            geo_error = geo_error or str(exc)

    if geo_issues or geo_error:
        send_bienpreter_geo_issues_email(geo_issues, error=geo_error)


if __name__ == "__main__":
    run()
