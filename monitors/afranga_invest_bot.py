"""Afranga auto-invest bot (primary + secondary market), pure HTTP.

Configuration comes entirely from the "config robots" Google Sheet (see
shared/robot_config.py): "Actif" drives the primary market, "Actif marché
secondaire" (+ rate / remaining months / discount-premium bounds) the
secondary market. "Répartition équivalente" only applies to the primary
market. Caps (per country / per loan, in % of balance + invested) apply to both.

Order of a run: secondary market first (most discounted listing first, then
shortest remaining term), then whatever budget is left on the primary market.

Endpoints (found 2026-10-07 by reading the pages' own JS, all JSON):
    GET  /profile/invest-sm/api/rows   secondary listings, Laravel-style filters
         interest_rate_percent[from|to], discount[from|to] (signed %, negative =
         discount), remaining_term[from|to] (months), originator_id[], limit, page
    GET  /profile/invest/api/rows      primary listings (interest_rate_percent, term)
    POST /profile/cart/add-to-cart     JSON, header X-CSRF-TOKEN (token embedded in
         the market page's `investMarket(JSON.parse(...))` config)
    POST /profile/cart/<id>/destroy    removes a cart line
Review page (`order.review_url`) is only rendered with a non-empty cart; its
confirm endpoint is read from that page at run time. Secondary (verified from the
page's JS + a live probe, 2026-10-08): POST /profile/cart/api/buy-sm (`urls.buySm`),
JSON {"risk_confirmed": bool}, headers Accept: application/json, X-Requested-With:
XMLHttpRequest, X-CSRF-TOKEN (token from the page's `investConfirm` config); answers
{"status": "ok"|"redirect", "redirect": url} or {"status": "risk"} (resend with
risk_confirmed=true). Primary: legacy `pmForm` form POST (not verified).

MODE (env AFRANGA_BOT_MODE, default "probe"):
    dry   - plan only, no HTTP call that changes anything.
    probe - adds the first planned line of each market to the cart, captures the
            review page structure into the diagnostics (emailed), removes the
            line again. Never buys.
    live  - real purchases.

Required env vars: AFRANGA_EMAIL, AFRANGA_PASSWORD, AFRANGA_TOTP_SECRET (if 2FA),
GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS, SMTP_* / EMAIL_TO.
Optional: CRON_JOB_API_KEY, AFRANGA_CRON_JOB_ID -> cron-job.org schedule every 2 min
(balance >= minimum) / once a day (below), cached in a local state file so the API
is only called when the mode changes (see shared/cron_schedule.py).
"""

import json
import logging
import os
import re
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from math import floor, inf
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

from diversification.afranga_diversification import _HEADERS, fetch_uninvested_balance, login
from shared.cron_schedule import apply_startup_jitter, ensure_schedule
from shared.google_sheet import get_geo_platform_snapshot
from shared.notifier import send_afranga_invest_summary_email
from shared.robot_config import Candidate, build_tracker, get_platform_config, plan_allocations
from shared.session_cache import get_or_refresh_session

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("afranga_invest_bot")

BASE = "https://afranga.com"
SECONDARY_PAGE_URL = f"{BASE}/profile/invest-sm"
PRIMARY_PAGE_URL = f"{BASE}/profile/invest"

SESSION_STATE_FILE = Path(__file__).parent / "afranga_invest_session_state.json"
DIAGNOSTICS_FILE = Path(__file__).parent / "afranga_invest_diagnostics.log"
CRON_SCHEDULE_STATE_FILE = Path(__file__).parent / "afranga_cron_schedule_state.json"
AFRANGA_CRON_JOB_ID = os.environ.get("AFRANGA_CRON_JOB_ID")

MODE = os.environ.get("AFRANGA_BOT_MODE", "probe").strip().lower()
MIN_INVESTMENT_AMOUNT = float(os.environ.get("AFRANGA_MIN_INVESTMENT_AMOUNT", "10"))
PAGE_SIZE = 100
MAX_PAGES = 10
# Secondary: a partial purchase must be >= this and leave >= this; smaller listings are bought in full only.
SECONDARY_MIN_LEFT = 100.0
MAX_DIAGNOSTICS_EMAIL_CHARS = 2_000_000

_CONFIG_RE = re.compile(r"investMarket\(JSON\.parse\('(.*?)'\)\)\"", re.S)
_SECRET_RES = [
    re.compile(r'("csrf"\s*:\s*")[^"]+'),
    re.compile(r"(\\u0022csrf\\u0022:\\u0022)[^\\]+"),
    re.compile(r'(name="_token"\s+value=")[^"]+'),
    re.compile(r'("_token"\s*:\s*")[^"]+'),
    re.compile(r'(name="csrf-token"\s+content=")[^"]+'),
]


def _redact(text: str) -> str:
    for pattern in _SECRET_RES:
        text = pattern.sub(r"\1<redacted>", text)
    return text


def _log_diagnostics(tag: str, **fields) -> None:
    entry = {"timestamp": datetime.now(timezone.utc).isoformat(), "tag": tag, **fields}
    try:
        with DIAGNOSTICS_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str, ensure_ascii=False) + "\n")
    except OSError:
        log.exception("Could not write to %s", DIAGNOSTICS_FILE)


def _collect_run_diagnostics(since: datetime) -> str | None:
    if not DIAGNOSTICS_FILE.exists():
        return None
    lines = []
    for line in DIAGNOSTICS_FILE.read_text(encoding="utf-8").splitlines():
        try:
            if datetime.fromisoformat(json.loads(line)["timestamp"]) >= since:
                lines.append(line)
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
    if not lines:
        return None
    text = "\n".join(lines)
    if len(text) > MAX_DIAGNOSTICS_EMAIL_CHARS:
        text = "(truncated, last part only)\n" + text[-MAX_DIAGNOSTICS_EMAIL_CHARS:]
    return text


def _floor2(value: float) -> float:
    return floor(value * 100 + 1e-9) / 100


def _number(text) -> float | None:
    cleaned = re.sub(r"[^\d.\-]", "", str(text or "").replace(",", "."))
    try:
        return float(cleaned)
    except ValueError:
        return None


def _extract_market_config(html: str) -> dict:
    match = _CONFIG_RE.search(html)
    if not match:
        raise RuntimeError("investMarket config not found in the market page.")
    return json.loads(json.loads('"' + match.group(1).replace("\\'", "'") + '"'))


@dataclass
class MarketRows:
    config: dict
    rows: list


def _ajax_headers(csrf: str | None = None) -> dict:
    headers = {**_HEADERS, "Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
    if csrf:
        headers["X-CSRF-TOKEN"] = csrf
    return headers


def fetch_market(session: requests.Session, page_url: str, filters: dict) -> MarketRows:
    """Loads the market page (for its csrf/urls config) then every page of
    `<page>/api/rows` matching `filters` (already in Laravel query form)."""
    r = session.get(page_url, headers=_HEADERS, timeout=20)
    r.raise_for_status()
    config = _extract_market_config(r.text)

    rows = []
    for page in range(1, MAX_PAGES + 1):
        params = {**filters, "limit": str(PAGE_SIZE), "page": str(page)}
        resp = session.get(config["urls"]["rows"], params=params, headers=_ajax_headers(), timeout=20)
        resp.raise_for_status()
        body = resp.json()
        rows.extend(body.get("rows") or [])
        if page >= (body.get("last_page") or 1):
            break
    else:
        log.warning("Hit MAX_PAGES (%d) on %s - listing truncated.", MAX_PAGES, page_url)
    return MarketRows(config, rows)


def _range_filters(out: dict, name: str, low, high) -> None:
    if low is not None:
        out[f"{name}[from]"] = f"{low:g}"
    if high is not None:
        out[f"{name}[to]"] = f"{high:g}"


@dataclass
class Line:
    """One planned purchase (principal `amount`, paid `cost`)."""
    row: dict
    loan_name: str
    amount: float
    cost: float
    market: str


def plan_secondary(rows: list, tracker, budget: float) -> list:
    """Most discounted first (most negative premium), then shortest remaining
    term. A listing under SECONDARY_MIN_LEFT (or `full_exit_only`) is bought in
    full only; otherwise the purchase must be >= SECONDARY_MIN_LEFT and may not
    leave less than SECONDARY_MIN_LEFT behind."""
    config = tracker.config
    candidates = []
    for row in rows:
        loan = config.find_loan((row.get("originator") or {}).get("name"))
        if loan is None:
            continue
        rate = _number(row.get("interest_rate"))
        months = _number(row.get("remaining_term"))
        if loan.secondary_rejection_reason(rate, row.get("premium"), months) is not None:
            continue
        candidates.append((row, loan, row.get("premium") or 0.0, months if months is not None else inf))
    candidates.sort(key=lambda item: (item[2], item[3]))

    plan, remaining = [], budget
    spent_loan: dict = {}
    spent_country: dict = {}
    for row, loan, premium, _months in candidates:
        factor = 1 + premium / 100.0
        max_principal = float(row["max_principal"])
        min_principal = float(row["min_principal"])
        country = tracker.country_of(loan.name)
        ceiling = min(
            max_principal,
            loan.max_amount if loan.max_amount else inf,
            tracker.loan_room(loan.name, spent_loan.get(loan.name, 0.0)),
            tracker.country_room(country, spent_country.get(country, 0.0)),
            remaining / factor,
        )
        amount = _floor2(ceiling)
        min_amount = max(min_principal, loan.min_amount or 0.0)
        if row.get("full_exit_only") or max_principal < SECONDARY_MIN_LEFT:
            if amount + 0.005 < max_principal:
                continue
            amount = max_principal
        else:
            left = max_principal - amount
            if 0 < left < SECONDARY_MIN_LEFT:
                full_cost_ok = _floor2(min(ceiling, max_principal)) + 0.005 >= max_principal
                amount = max_principal if full_cost_ok else _floor2(max_principal - SECONDARY_MIN_LEFT)
            min_amount = max(min_amount, SECONDARY_MIN_LEFT)
        if amount < min_amount or amount <= 0:
            continue
        cost = round(amount * factor + 1e-9, 2)
        if cost > remaining + 1e-9:
            continue
        plan.append(Line(row, loan.name, amount, cost, "secondary"))
        remaining -= cost
        spent_loan[loan.name] = spent_loan.get(loan.name, 0.0) + amount
        if country:
            spent_country[country] = spent_country.get(country, 0.0) + amount
    return plan


def plan_primary(rows: list, tracker, budget: float) -> list:
    candidates, by_id = [], {}
    for row in rows:
        loan = tracker.config.find_loan((row.get("originator") or {}).get("name"))
        if loan is None:
            continue
        candidates.append(Candidate(row["loan_id"], loan.name, float(row["max_principal"]), _number(row.get("interest_rate"))))
        by_id[row["loan_id"]] = row
    allocations = plan_allocations(candidates, tracker, budget, MIN_INVESTMENT_AMOUNT)
    plan = []
    for loan_id, amount in allocations.items():
        row = by_id[loan_id]
        if amount < float(row.get("min_principal") or 0):
            continue
        plan.append(Line(row, (tracker.config.find_loan(row["originator"]["name"])).name, amount, amount, "primary"))
    return plan


def _add_to_cart(session: requests.Session, csrf: str, urls: dict, line: Line) -> tuple:
    if line.market == "secondary":
        row = line.row
        payload = {
            "market": "secondary_market", "type": "buy", "loan_id": row["loan_id"],
            "originator_id": row["originator"]["id"], "investment_id": row["investment_id"],
            "market_secondary_id": row["market_secondary_id"], "premium": row["premium"], "amount": line.amount,
        }
    else:
        payload = {"market": "primary_market", "type": "buy", "loan_id": line.row["loan_id"], "amount": line.amount}
    r = session.post(urls["add"], json=payload, headers=_ajax_headers(csrf), timeout=20)
    try:
        body = r.json()
    except ValueError:
        body = None
    ok = r.ok and isinstance(body, dict) and body.get("success") is not False
    _log_diagnostics("add_to_cart", market=line.market, payload=payload, status=r.status_code, ok=ok,
                     response=_redact(r.text[:5000]))
    return ok, body


def _capture_review_page(session: requests.Session, review_url: str, market: str) -> str:
    """Fetches the confirm page and logs what the buy step needs."""
    r = session.get(review_url, headers=_HEADERS, timeout=20)
    html = r.text
    form = re.search(r"<form[^>]*pmForm[^>]*>.*?</form>", html, re.S)
    confirm = re.search(r"investConfirm\(.{0,3000}", html, re.S)
    _log_diagnostics(
        "review_page", market=market, status=r.status_code, final_url=r.url, length=len(html),
        confirm_config=_redact(confirm.group(0)) if confirm else None,
        pm_form=_redact(form.group(0)[:4000]) if form else None,
        html_head=None if (confirm or form) else _redact(html[:3000]),
    )
    return html


def _confirm(session: requests.Session, review_html: str, market: str, csrf: str) -> bool:
    if market == "secondary":
        match = re.search(r"investConfirm\(JSON\.parse\('(.*?)'\)\)", review_html, re.S)
        config = None
        if match:
            config = json.loads(json.loads('"' + match.group(1).replace("\\'", "'") + '"'))
        buy_url = ((config or {}).get("urls") or {}).get("buySm")
        if not buy_url:
            raise RuntimeError("buySm url not found in the review page (see 'review_page' diagnostics).")
        token = (config or {}).get("csrf") or csrf
        for risk in (False, True):
            r = session.post(buy_url, json={"risk_confirmed": risk}, headers=_ajax_headers(token), timeout=30)
            try:
                body = r.json()
            except ValueError:
                body = {}
            _log_diagnostics("confirm_secondary", risk_confirmed=risk, status=r.status_code, response=_redact(r.text[:5000]))
            if body.get("status") in ("ok", "redirect"):
                if body.get("redirect"):
                    try:
                        done = session.get(body["redirect"], headers=_HEADERS, timeout=20)
                        _log_diagnostics("confirm_done_page", status=done.status_code, final_url=done.url,
                                         html=_redact(done.text[:3000]))
                    except requests.RequestException:
                        log.exception("Could not fetch the post-purchase page.")
                return True
            if body.get("status") != "risk":
                return False
        return False

    form = re.search(r"<form[^>]*pmForm[^>]*>.*?</form>", review_html, re.S)
    if not form:
        raise RuntimeError("pmForm not found in the primary review page (see 'review_page' diagnostics).")
    block = form.group(0)
    action = re.search(r'action="([^"]+)"', block)
    data = {}
    for tag in re.findall(r"<input[^>]*>", block):
        name = re.search(r'name="([^"]+)"', tag)
        if name:
            value = re.search(r'value="([^"]*)"', tag)
            data[name.group(1)] = value.group(1) if value else ""
    r = session.post(action.group(1) if action else PRIMARY_PAGE_URL, data=data, headers=_HEADERS, timeout=30,
                     allow_redirects=True)
    _log_diagnostics("confirm_primary", status=r.status_code, final_url=r.url, fields=sorted(data), response=_redact(r.text[:3000]))
    return r.ok and "/login" not in r.url


def _find_key(node, key):
    """Last value stored under `key` anywhere in a JSON structure."""
    found = None
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key:
                found = v
            sub = _find_key(v, key)
            found = sub if sub is not None else found
    elif isinstance(node, list):
        for item in node:
            sub = _find_key(item, key)
            found = sub if sub is not None else found
    return found


def _remove_from_cart(session: requests.Session, csrf: str, urls: dict, cart_loan_id) -> None:
    if not cart_loan_id:
        log.warning("Cart line id unknown, remove it manually from the Afranga cart.")
        return
    resp = session.post(urls["destroyLine"].replace("__ID__", str(cart_loan_id)), json={},
                        headers=_ajax_headers(csrf), timeout=20)
    _log_diagnostics("cart_cleanup", status=resp.status_code, response=_redact(resp.text[:1000]))


def execute_market(session: requests.Session, market: MarketRows, lines: list, tracker, stats: dict) -> None:
    """dry: nothing. probe: first line only, added then removed, never bought.
    live: add every line, then confirm the whole cart once."""
    if not lines or MODE == "dry":
        return
    csrf, urls = market.config["csrf"], market.config["urls"]
    # A confirm buys the WHOLE cart: lines left by a crashed run / manual use must never be bought blindly.
    order = market.config.get("order") or {}
    stale_ids = [_find_key(line, "cart_loan_id") for line in order.get("lines") or []]
    if order.get("count") and (len(stale_ids) < order["count"] or not all(stale_ids)):
        raise RuntimeError("The Afranga cart is not empty and its lines can't be identified - empty it manually.")
    for cart_loan_id in stale_ids:
        log.warning("Removing stale cart line %s before planning purchases.", cart_loan_id)
        _remove_from_cart(session, csrf, urls, cart_loan_id)
    to_add = lines[:1] if MODE == "probe" else lines
    added = []
    review_url = market.config.get("order", {}).get("review_url")
    for line in to_add:
        stats["attempts"] += 1
        ok, body = _add_to_cart(session, csrf, urls, line)
        if ok:
            added.append((line, _find_key((body or {}).get("line", body), "cart_loan_id")))
            review_url = _find_key(body, "review_url") or review_url
        else:
            stats["failures"] += 1
    if not added:
        return

    try:
        review_html = _capture_review_page(session, review_url, lines[0].market)
        if MODE == "live":
            confirmed = _confirm(session, review_html, lines[0].market, csrf)
    except Exception:
        for _line, cart_loan_id in added:
            _remove_from_cart(session, csrf, urls, cart_loan_id)
        raise

    if MODE == "probe":
        stats["probed"] = True
        for _line, cart_loan_id in added:
            _remove_from_cart(session, csrf, urls, cart_loan_id)
        return

    if confirmed:
        for line, _id in added:
            stats["successes"] += 1
            stats["total_invested"] += line.cost
            stats["balance_after"] -= line.cost
            tracker.add(line.loan_name, line.amount)
            stats["invested"].append(
                {"market": line.market, "loan": line.loan_name, "amount": line.amount, "cost": line.cost,
                 "rate": _number(line.row.get("interest_rate")), "premium": line.row.get("premium")}
            )
    else:
        stats["failures"] += len(added)


def run() -> None:
    run_started_at = datetime.now(timezone.utc)
    apply_startup_jitter(CRON_SCHEDULE_STATE_FILE)
    stats = {
        "mode": MODE, "attempts": 0, "successes": 0, "failures": 0, "total_invested": 0.0,
        "invested": [], "plan": [], "probed": False,
    }
    error = None
    try:
        session = requests.Session()
        balance, _extra = get_or_refresh_session(
            session, SESSION_STATE_FILE,
            fetch_fn=lambda extra: fetch_uninvested_balance(session),
            login_fn=lambda: (login(session), {}),
            platform_name="Afranga",
        )
        # login() returns None, so the balance is fetched by fetch_fn on the fresh session.
        stats["balance_before"] = stats["balance_after"] = balance
        log.info("Afranga mode=%s, uninvested balance: %.2f EUR", MODE, balance)
        # Before any early return, so the schedule always follows the balance.
        ensure_schedule(
            "24h" if balance < MIN_INVESTMENT_AMOUNT else "2m",
            cron_job_id=AFRANGA_CRON_JOB_ID, state_file=CRON_SCHEDULE_STATE_FILE,
        )
        if balance < MIN_INVESTMENT_AMOUNT:
            log.info("Balance below the minimum investment amount - nothing to do.")
            return

        config = get_platform_config("Afranga")
        if not config.active_names() and not config.secondary_names():
            log.info("No Afranga loan is active (primary or secondary) in the config sheet.")
            return
        try:
            geo_snapshot = get_geo_platform_snapshot("Afranga", "Bienprêter")
        except Exception:
            log.exception("Geographic snapshot unavailable - caps computed from the balance only.")
            geo_snapshot = None
        tracker = build_tracker(config, balance, geo_snapshot)

        budget = balance
        if config.secondary_names():
            filters = {}
            _range_filters(filters, "interest_rate_percent", config.lowest_min_rate(secondary=True), None)
            _range_filters(filters, "discount", *config.secondary_bounds("min_premium", "max_premium"))
            _range_filters(filters, "remaining_term", *config.secondary_bounds("min_months", "max_months"))
            market = fetch_market(session, SECONDARY_PAGE_URL, filters)
            lines = plan_secondary(market.rows, tracker, budget)
            log.info("Secondary: %d listing(s) fetched, %d planned.", len(market.rows), len(lines))
            stats["plan"] += [_line_summary(line) for line in lines]
            execute_market(session, market, lines, tracker, stats)
            budget = stats["balance_after"] if MODE == "live" else budget - sum(line.cost for line in lines)

        if config.active_names() and budget >= MIN_INVESTMENT_AMOUNT:
            filters = {}
            _range_filters(filters, "interest_rate_percent", config.lowest_min_rate(), None)
            market = fetch_market(session, PRIMARY_PAGE_URL, filters)
            lines = plan_primary(market.rows, tracker, budget)
            log.info("Primary: %d listing(s) fetched, %d planned.", len(market.rows), len(lines))
            stats["plan"] += [_line_summary(line) for line in lines]
            execute_market(session, market, lines, tracker, stats)
    except Exception as exc:
        error = str(exc)
        _log_diagnostics("run_error", error=error, traceback=traceback.format_exc())
        log.exception("Afranga invest bot failed.")
    finally:
        _log_diagnostics("run_summary", stats=stats, error=error)
        had_problem = bool(error or stats["failures"])
        # dry/probe runs only email on error; diagnostics are attached only when something went wrong.
        if error or (MODE == "live" and (stats["plan"] or stats["attempts"])):
            send_afranga_invest_summary_email(
                stats, error=error, diagnostics_text=_collect_run_diagnostics(run_started_at) if had_problem else None
            )
    if error:
        sys.exit(1)


def _line_summary(line: Line) -> dict:
    row = line.row
    return {
        "market": line.market, "loan": line.loan_name, "amount": line.amount, "cost": line.cost,
        "rate": _number(row.get("interest_rate")), "premium": row.get("premium"),
        "remaining_months": _number(row.get("remaining_term") or row.get("term")),
    }


if __name__ == "__main__":
    run()
