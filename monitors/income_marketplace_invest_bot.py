"""Income Marketplace (getincome.com) auto-invest bot.

Configuration comes entirely from the "config robots" Google Sheet (block
"Income Marketplace", see shared/robot_config.py): "Actif" per loan originator,
rate bounds, caps (per country / per loan, in % of balance + invested), minimum
/ maximum amount per loan and "Répartition équivalente". Same planning logic as
the Lendermarket / Afranga bots.

Income Marketplace has no usable plain-HTTP login (Auth0 + TOTP), so the login
reuses the Playwright flow of diversification/income_marketplace_diversification.py
and every API call goes through `page.evaluate(fetch(...))` with the Bearer
accessToken (the session is persisted in STORAGE_STATE_FILE between runs).

Endpoints (verified live 2026-11-12, all JSON, Bearer token):
    GET  /api/investor-details            funds.available = uninvested cash
    POST /api/loan/list/<page>            body {"filters": [{"id": 7, "values": [min, max]}],
                                          "search": "", "sort": ..., "sort_dir": ..., "minimum_investment": ...}
         20 loans per page, `{loanCount, pages, loanList}`. The interest-rate range filter
         (id 7) and the free-text `search` (matches the originator name, verified live
         2026-11-20: "Virtus Lending" -> 3 of 4446 loans, unknown name -> 0) are honoured
         server-side; the other filters (status / originator multiselects) are ignored, so
         those are filtered here. The listing is live
         (loans are taken while paging), so rows are deduplicated on `loan_id`.
    POST /api/investment-create           body [{"loan_ref": <int>, "amount": "<str>"}]
         Verified with a real 10 EUR purchase: HTTP 201 and
           {"overall_status": "success", "overall_message": "...",
            "data": {"<loan_ref>": {"tried_to_invest_amount": 10, "loan_originator_name": "...",
                     "message": "Investment created", "message_i18n": "...investmentCreated",
                     "investment_id": 8833533, "invested_amount": 10}}}
         The cash is debited immediately (investor-details funds.available -10 on the very next
         call). A refused loan is ALSO HTTP 201: {"overall_status": "warning", "data": {"<ref>":
         {"tried_to_invest_amount": 10, "message": "Invalid REF number", "message_i18n":
         "...invalidRef"}}} - no `investment_id` / `invested_amount`, so the verdict comes from
         the per-loan entry of the body, never from the HTTP status.
         The amount is silently CAPPED to the uninvested cash: asking 100000 with 7.71 EUR
         available bought 7.71 (`tried_to_invest_amount` 100000, `invested_amount` 7.71). The
         "keep uninvested" setting is not enforced server-side, so the bot must never ask more
         than its planned budget, and books `invested_amount` (not the requested amount).
         Bodies that cannot be read (network error, unexpected format) fall back to checking
         that the uninvested cash really dropped.
    GET  /api/investments/?page_size=1000 {total, items, pages}; the new purchase shows up there
         right away (`investment_id`, `investment_invest_amount` "10.00", `loan_ref` as a string).

MODE (env INCOME_MARKETPLACE_BOT_MODE, default "live"; the workflow's manual-run
"mode" input can select "dry"):
    live - real purchases.
    dry  - plan only, nothing is bought.

Required env vars: INCOME_MARKETPLACE_EMAIL, INCOME_MARKETPLACE_PASSWORD,
INCOME_MARKETPLACE_TOTP_SECRET, GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS, SMTP_* / EMAIL_TO.
Optional: CRON_JOB_API_KEY, INCOME_MARKETPLACE_CRON_JOB_ID -> cron-job.org schedule from the
"Temps planification solde suffisant / insuffisant" columns of the sheet block
(defaults: every 2 min when the balance >= minimum, once a day below).
"""

import heapq
import itertools
import json
import logging
import math
import os
import sys
import time
import traceback
from calendar import monthrange
from datetime import date, datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from playwright.sync_api import sync_playwright

from diversification.income_marketplace_diversification import (
    API_BASE, INVESTOR_DETAILS_URL, _fetch_json, companies_from_investments, fetch_investments, login,
)
from shared.cron_schedule import apply_startup_jitter, ensure_schedule_interval
from shared.google_sheet import get_geo_platform_snapshot
from shared.missing_loans import TRANSIENT_GRACE_SECONDS, report_missing_loans
from shared.notifier import send_income_marketplace_invest_summary_email
from shared.robot_config import Candidate, begin_run, build_tracker, get_platform_config, plan_allocations
from shared.state import load_state, save_state

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("income_marketplace_invest_bot")

PLATFORM = "Income Marketplace"
LOAN_LIST_URL = f"{API_BASE}/loan/list/"
INVESTMENT_CREATE_URL = f"{API_BASE}/investment-create"
RATE_FILTER_ID = 7
RATE_FILTER_MAX = 100
INVESTABLE_STATUSES = {"current"}

STORAGE_STATE_FILE = Path(__file__).parent / "income_marketplace_invest_storage_state.json"
STATE_FILE = Path(__file__).parent / "income_marketplace_invest_state.json"
DIAGNOSTICS_FILE = Path(__file__).parent / "income_marketplace_invest_diagnostics.log"
CRON_SCHEDULE_STATE_FILE = Path(__file__).parent / "income_marketplace_cron_schedule_state.json"
INCOME_MARKETPLACE_CRON_JOB_ID = os.environ.get("INCOME_MARKETPLACE_CRON_JOB_ID")

MODE = os.environ.get("INCOME_MARKETPLACE_BOT_MODE", "live").strip().lower()
MIN_INVESTMENT_AMOUNT = float(os.environ.get("INCOME_MARKETPLACE_MIN_INVESTMENT_AMOUNT", "10"))
MAX_PAGES = 300
PAGES_PER_BATCH_MAX = 60
LOANS_PER_PAGE = 20
MAX_PASSES = 3
LIST_MAX_ATTEMPTS = 4
LIST_RETRY_BASE_DELAY_SECONDS = 3
BALANCE_CHECK_ATTEMPTS = 4
BALANCE_CHECK_DELAY_SECONDS = 2
NETWORK_ERROR_MAX_ATTEMPTS = 2
MAX_DIAGNOSTICS_EMAIL_CHARS = 2_000_000


def _log_diagnostics(tag: str, **fields) -> None:
    entry = {"timestamp": datetime.now(timezone.utc).isoformat(), "tag": tag, **fields}
    try:
        with DIAGNOSTICS_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str, ensure_ascii=False) + "\n")
    except OSError:
        log.exception("Could not write to %s", DIAGNOSTICS_FILE)


def _collect_run_diagnostics(since: datetime, tags: set[str] | None = None) -> str | None:
    if not DIAGNOSTICS_FILE.exists():
        return None
    lines = []
    for line in DIAGNOSTICS_FILE.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
            if datetime.fromisoformat(entry["timestamp"]) >= since and (tags is None or entry.get("tag") in tags):
                lines.append(line)
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
    if not lines:
        return None
    text = "\n".join(lines)
    if len(text) > MAX_DIAGNOSTICS_EMAIL_CHARS:
        text = "(truncated, last part only)\n" + text[-MAX_DIAGNOSTICS_EMAIL_CHARS:]
    return text


def _format_amount(amount: float) -> str:
    """"10", "10.5", "10.55" (the site's own cart sends the plain number as a string)."""
    return f"{amount:.2f}".rstrip("0").rstrip(".")


def _number(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _post_json(page, url: str, token: str, body) -> dict:
    """POST `body` from the authenticated page. Never raises on a network error
    (status 0) and never retries: for the purchase call a blind retry could buy twice."""
    return page.evaluate(
        """
        async ({url, token, body}) => {
            try {
                const r = await fetch(url, {
                    method: 'POST',
                    headers: {'Accept': 'application/json', 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token},
                    body: JSON.stringify(body)
                });
                return {status: r.status, text: await r.text()};
            } catch (e) {
                return {status: 0, text: String(e)};
            }
        }
        """,
        {"url": url, "token": token, "body": body},
    )


def fetch_cash_balance(page, token: str) -> float:
    funds = _fetch_json(page, INVESTOR_DETAILS_URL, token).get("funds", {})
    return float(funds.get("available") or 0.0)


def iter_marketplace_pages(page, token: str, min_rate: float | None, search: str = ""):
    """Yields the loans of each listing page, highest interest rate first (up to MAX_PAGES pages).
    `min_rate` is a server-side pre-filter (falls back to an unfiltered listing if refused).
    `search` is the server-side free-text filter, which matches the originator name."""
    filters = [] if min_rate is None else [{"id": RATE_FILTER_ID, "values": [min_rate, RATE_FILTER_MAX]}]
    page_count = 1
    page_number = 1
    while page_number <= min(page_count, MAX_PAGES):
        body = {
            "filters": filters, "search": search, "minimum_investment": _format_amount(MIN_INVESTMENT_AMOUNT),
            "sort": "loan_investor_interest_rate", "sort_dir": "desc",
        }
        for attempt in range(1, LIST_MAX_ATTEMPTS + 1):
            result = _post_json(page, f"{LOAN_LIST_URL}{page_number}", token, body)
            if result["status"] == 200:
                break
            log.warning("loan/list page %d: HTTP %s (attempt %d/%d).", page_number, result["status"], attempt, LIST_MAX_ATTEMPTS)
            _log_diagnostics("loan_list_error", page=page_number, attempt=attempt, status=result["status"], response=result["text"][:1000])
            if attempt < LIST_MAX_ATTEMPTS:
                time.sleep(LIST_RETRY_BASE_DELAY_SECONDS * attempt)
        else:
            if filters and page_number == 1:
                log.warning("Rate filter refused by the API - fetching the unfiltered listing.")
                filters = []
                continue
            raise RuntimeError(f"POST {LOAN_LIST_URL}{page_number} returned HTTP {result['status']}: {result['text'][:300]}")
        data = json.loads(result["text"])
        page_count = int(data.get("pages") or 0)
        batch = data.get("loanList") or []
        for loan in batch:
            loan.pop("loan_schedule", None)
        yield batch
        page_number += 1
    if page_count > MAX_PAGES:
        log.warning("Hit MAX_PAGES (%d) of %d - listing truncated.", MAX_PAGES, page_count)


def iter_open_loan_pages(page, token: str, config, names: list):
    """Returns `(pages, readable, drained)`: the listing pages restricted to the originators in
    `names` (one server-side search per originator, with its own minimum rate), merged back in
    descending rate order so the plan still favours the highest rates first; the set of
    originators still being read; and the set whose listing was read to the end (or is empty).
    The caller can remove an originator from `readable` to stop fetching its pages. An
    originator whose search finds nothing has no loan on the marketplace."""
    readable, drained = set(), set()

    def tracked(name, first, pages):
        batch = first
        while True:
            yield batch
            if name not in readable:
                return
            batch = next(pages, None)
            if batch is None:
                readable.discard(name)
                drained.add(name)
                return

    streams = []
    for name in names:
        pages = iter_marketplace_pages(page, token, config.loans[name].min_rate, search=name)
        first = next(pages, [])
        if first:
            readable.add(name)
            streams.append(itertools.chain.from_iterable(tracked(name, first, pages)))
        else:
            drained.add(name)
    rate_desc = lambda loan: -(_number(loan.get("loan_investor_interest_rate")) or 0.0)
    merged = heapq.merge(*streams, key=rate_desc)
    return iter(lambda: list(itertools.islice(merged, LOANS_PER_PAGE)) or None, None), readable, drained


def retire_capped_originators(plan: list, tracker, readable: set) -> None:
    """Removes from `readable` the originators that cannot take another purchase on top of
    `plan` (no room left under their loan or country cap for at least their minimum amount):
    their remaining pages cannot change the plan."""
    spent_loan, spent_country = {}, {}
    for _loan, name, amount in plan:
        spent_loan[name] = spent_loan.get(name, 0.0) + amount
        country = tracker.country_of(name)
        if country:
            spent_country[country] = spent_country.get(country, 0.0) + amount
    for name in list(readable):
        loan = tracker.config.loans[name]
        country = tracker.country_of(name)
        room = min(
            tracker.loan_room(name, spent_loan.get(name, 0.0)),
            tracker.country_room(country, spent_country.get(country, 0.0)),
        )
        if room + 1e-9 < (loan.min_amount if loan.min_amount is not None else MIN_INVESTMENT_AMOUNT):
            log.info("Originator %r is at its cap with this plan - its remaining pages are not read.", name)
            readable.discard(name)


def _pages_needed(unallocated: float, config, names) -> int:
    """Listing pages worth reading to place `unallocated` EUR: at most one loan per minimum
    amount of the sheet (the smallest among `names`), LOANS_PER_PAGE loans per page."""
    smallest = min(
        (config.loans[name].min_amount if config.loans[name].min_amount is not None else MIN_INVESTMENT_AMOUNT)
        for name in names
    ) or MIN_INVESTMENT_AMOUNT
    loans_needed = math.ceil(unallocated / smallest - 1e-9)
    return max(1, min(PAGES_PER_BATCH_MAX, math.ceil(loans_needed / LOANS_PER_PAGE)))


def build_plan(page, token: str, config, tracker, open_names: list, budget: float, excluded=()) -> tuple:
    """`(plan, loans, drained)`: reads only as many listing pages as the budget and the
    sheet's minimum amounts call for, then keeps reading (lower rates) while part of the budget
    stays unallocated and an originator still has room under its caps. The plan is recomputed
    from every loan read so far. `drained`: the originators whose whole listing was read."""
    pages, readable, drained = iter_open_loan_pages(page, token, config, open_names)
    loans: dict = {}
    unallocated = budget
    while True:
        exhausted = read_more_loans(pages, loans, _pages_needed(unallocated, config, open_names))
        plan = plan_loans(list(loans.values()), tracker, budget, excluded)
        unallocated = budget - sum(amount for *_rest, amount in plan)
        log.info(
            "%d loan(s) listed, %d purchase(s) planned, %.2f EUR unallocated.",
            len(loans), len(plan), unallocated,
        )
        if exhausted or unallocated < MIN_INVESTMENT_AMOUNT:
            break
        retire_capped_originators(plan, tracker, readable)
        if not readable:
            break
    return plan, loans, drained


def read_more_loans(pages, loans: dict, page_budget: int) -> bool:
    """Reads up to `page_budget` more listing pages into `loans` (deduplicated on `loan_id`).
    Returns True once the listing is exhausted."""
    for _ in range(page_budget):
        batch = next(pages, None)
        if batch is None:
            return True
        for loan in batch:
            loans.setdefault(loan["loan_id"], loan)
    return False


def _is_investable(loan: dict) -> bool:
    return str(loan.get("loan_status") or "").strip().lower() in INVESTABLE_STATUSES


def _add_months(day: date, months: float) -> date:
    total = day.year * 12 + day.month - 1 + int(months)
    year, month = divmod(total, 12)
    month += 1
    return date(year, month, min(day.day, monthrange(year, month)[1]))


def _term_end(loan: dict) -> date | None:
    """The site's "Remaining Term" filter follows the buyback date (the listing shows e.g.
    "1y 11m" remaining but "5m 28d" to buyback and the filter 0-6m accepts it), else the due date."""
    raw = loan.get("loan_buyback_date") if loan.get("loan_buyback_guarantee") else None
    for value in (raw, loan.get("loan_due_date")):
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            continue
    return None


def _term_matches(loan: dict, config_loan, today: date) -> bool:
    """"Durée restante min / max" (months) of the sheet; empty bound = no bound."""
    low, high = config_loan.min_months, config_loan.max_months
    if low is None and high is None:
        return True
    end = _term_end(loan)
    if end is None:
        return False
    return (low is None or end >= _add_months(today, low)) and (high is None or end <= _add_months(today, high))


def _accepted(loan: dict, config_loan, today: date) -> bool:
    # "Inclure prêts extensibles" of the sheet (unchecked = the site's "Exclude extendable loans" option).
    if not config_loan.include_extendable and loan.get("loan_extendable_schedule"):
        return False
    return _term_matches(loan, config_loan, today)


def plan_loans(loans: list, tracker, budget: float, excluded=()) -> list:
    """[(loan, originator name in the sheet, amount)] in listing order. `excluded`: loan refs
    not to buy (already refused / cut during this run)."""
    candidates, by_ref = [], {}
    today = datetime.now(timezone.utc).date()
    for loan in loans:
        if not _is_investable(loan) or loan.get("loan_ref") in excluded:
            continue
        config_loan = tracker.config.find_loan(loan.get("loan_originator_name"))
        available = _number(loan.get("investable_amount"))
        if config_loan is None or not available or not loan.get("loan_ref"):
            continue
        if not _accepted(loan, config_loan, today):
            continue
        candidates.append(Candidate(loan["loan_ref"], config_loan.name, available, _number(loan.get("loan_investor_interest_rate"))))
        by_ref[loan["loan_ref"]] = (loan, config_loan.name)
    allocations = plan_allocations(candidates, tracker, budget, MIN_INVESTMENT_AMOUNT)
    return [(*by_ref[ref], amount) for ref, amount in allocations.items()]


def _parse_investment_response(result: dict, loan_ref) -> tuple:
    """`(invested_amount, rejection)` from the investment-create body: `(amount, None)` when the
    purchase is confirmed, `(None, message)` when the API refused it, `(None, None)` when the
    body cannot be interpreted (the caller then falls back to the cash balance)."""
    try:
        entry = json.loads(result["text"])["data"][str(loan_ref)]
    except (ValueError, KeyError, TypeError):
        return None, None
    if not isinstance(entry, dict):
        return None, None
    invested = _number(entry.get("invested_amount"))
    if entry.get("investment_id") and invested and invested > 0:
        return invested, None
    return None, f"{entry.get('message')} ({entry.get('message_i18n')})"


def place_investment(page, token: str, loan: dict, amount: float, balance_before: float) -> tuple:
    """Returns `(ok, invested_amount, balance_after, failure_reason)`.

    The API answers HTTP 201 even when the purchase is refused, so the verdict comes from the
    per-loan entry of the body (`investment_id` + `invested_amount`). `invested_amount` can be
    lower than `amount`: the API silently caps the amount to the uninvested cash (it ignores the
    "keep uninvested" setting of the sheet), so `amount` must never exceed the planned budget.
    Without a readable body (network error, unexpected format) the purchase is confirmed from
    the uninvested cash dropping instead."""
    payload = [{"loan_ref": loan["loan_ref"], "amount": _format_amount(amount)}]
    for network_attempt in range(1, NETWORK_ERROR_MAX_ATTEMPTS + 1):
        result = _post_json(page, INVESTMENT_CREATE_URL, token, payload)
        _log_diagnostics(
            "investment_create", payload=payload, status=result["status"], response=result["text"][:3000],
            balance_before=balance_before, network_attempt=network_attempt,
            loan={key: loan.get(key) for key in (
                "loan_ref", "loan_originator_name", "loan_status", "loan_investor_interest_rate", "investable_amount",
                "loan_term_period", "loan_due_date", "loan_buyback_date", "loan_extendable_schedule",
            )},
        )
        invested, rejection = _parse_investment_response(result, loan["loan_ref"])
        if invested:
            if invested < amount - 0.01:
                log.warning("Loan %s: %.2f EUR requested but only %.2f EUR invested.", loan["loan_ref"], amount, invested)
            return True, invested, fetch_cash_balance(page, token), None
        if rejection:
            return False, 0.0, balance_before, f"HTTP {result['status']} : {rejection}"
        balance_after = balance_before
        # status 0 = no response (network): the purchase may still have gone through.
        check_balance = result["status"] == 0 or 200 <= result["status"] < 300
        for attempt in range(BALANCE_CHECK_ATTEMPTS if check_balance else 1):
            if attempt:
                time.sleep(BALANCE_CHECK_DELAY_SECONDS)
            balance_after = fetch_cash_balance(page, token)
            if balance_before - balance_after >= amount - 0.01:
                return True, balance_before - balance_after, balance_after, None
        # Resending is only safe because the cash was verified as not debited (a dropped
        # keep-alive connection fails once, the next request opens a fresh one).
        if result["status"] == 0 and network_attempt < NETWORK_ERROR_MAX_ATTEMPTS:
            log.warning("Loan %s: network error, cash not debited - retrying the purchase.", loan["loan_ref"])
            balance_before = balance_after
            continue
        snippet = " ".join(result["text"][:300].split())
        reason = f"HTTP {result['status']}, solde non débité ({balance_before:.2f} -> {balance_after:.2f} €) : {snippet}"
        return False, 0.0, balance_after, reason


def _line_summary(loan: dict, loan_name: str, amount: float) -> dict:
    return {
        "loan": loan_name, "loan_ref": loan.get("loan_ref"), "amount": amount,
        "rate": _number(loan.get("loan_investor_interest_rate")), "status": loan.get("loan_status"),
        "term": loan.get("loan_term_period"),
    }


def run() -> None:
    disabled, wait_range = begin_run(PLATFORM, INCOME_MARKETPLACE_CRON_JOB_ID, CRON_SCHEDULE_STATE_FILE)
    if disabled:
        return

    run_started_at = datetime.now(timezone.utc)
    apply_startup_jitter(CRON_SCHEDULE_STATE_FILE, wait_range)
    stats = {
        "mode": MODE, "attempts": 0, "successes": 0, "failures": 0, "total_invested": 0.0,
        "invested": [], "plan": [], "failed_attempts": [],
    }
    error = None
    config = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(storage_state=str(STORAGE_STATE_FILE) if STORAGE_STATE_FILE.exists() else None)
            page = context.new_page()
            token = login(page)
            context.storage_state(path=str(STORAGE_STATE_FILE))

            balance = fetch_cash_balance(page, token)
            stats["balance_before"] = stats["balance_after"] = balance
            log.info("%s mode=%s, uninvested balance: %.2f EUR", PLATFORM, MODE, balance)
            # Read before any early return, so the schedule always follows the
            # balance; a read failure only matters once the balance is sufficient.
            config_error = None
            try:
                config = get_platform_config(PLATFORM)
            except Exception as exc:
                config_error = exc
                log.exception("Could not read the %s configuration from the 'config robots' sheet.", PLATFORM)
            investable = config.investable_balance(balance) if config else balance
            sufficient = investable >= MIN_INVESTMENT_AMOUNT
            if config:
                ensure_schedule_interval(
                    config.schedule_minutes(sufficient),
                    cron_job_id=INCOME_MARKETPLACE_CRON_JOB_ID, state_file=CRON_SCHEDULE_STATE_FILE, sufficient=sufficient,
                )
            if not sufficient:
                log.info(
                    "Investable balance %.2f EUR (balance %.2f EUR minus %.2f EUR kept uninvested) is below the minimum investment amount - nothing to do.",
                    investable, balance, config.keep_uninvested if config else 0.0,
                )
                return

            if config is None:
                raise config_error
            if not config.active_names():
                log.info("No %s loan originator is active in the config sheet.", PLATFORM)
                return
            try:
                geo_snapshot = get_geo_platform_snapshot(PLATFORM, "Iuvo")
            except Exception:
                log.exception("Geographic snapshot unavailable - caps computed from the balance only.")
                geo_snapshot = None
            tracker = build_tracker(config, balance, geo_snapshot)
            tracker.sync_live_invested((c["name"], c["amount"]) for c in companies_from_investments(fetch_investments(page, token)))
            open_names = tracker.investable_names(MIN_INVESTMENT_AMOUNT)
            if not open_names:
                log.info("Every active %s loan originator has reached its loan / country cap - nothing to do.", PLATFORM)
                return
            skipped = sorted(set(config.active_names()) - set(open_names))
            if skipped:
                log.info("Loan originator(s) at their cap, not looked for: %s.", ", ".join(skipped))

            excluded: set = set()
            state = load_state(STATE_FILE, {"missing_loans": {}})
            for pass_number in range(1, MAX_PASSES + 1):
                plan, loans, drained = build_plan(page, token, config, tracker, open_names, investable, excluded)
                log.info("%d loan(s) with status Current.", sum(_is_investable(loan) for loan in loans.values()))

                if pass_number == 1:
                    # Mail (once) when no listed loan belongs to an originator flagged in the sheet.
                    # Only originators whose result is settled are reported: seen, or listing read to
                    # the end / empty (an originator whose pages were not all read may still be listed).
                    seen_names = set()
                    today = datetime.now(timezone.utc).date()
                    for loan in loans.values():
                        config_loan = config.find_loan(loan.get("loan_originator_name"))
                        if config_loan is not None and _is_investable(loan) and _accepted(loan, config_loan, today):
                            seen_names.add(config_loan.name)
                    all_names = sorted(open_names)
                    found = {name: True for name in all_names if name in seen_names}
                    found.update({name: False for name in all_names if name in drained and name not in seen_names})
                    if found:
                        report_missing_loans(
                            PLATFORM, found, state.setdefault("missing_loans", {}),
                            all_names=all_names, grace_seconds=TRANSIENT_GRACE_SECONDS,
                        )
                        save_state(STATE_FILE, state)

                stats["plan"] += [_line_summary(*line) for line in plan]
                log.info("%d purchase(s) planned (%.2f EUR).", len(plan), sum(amount for *_rest, amount in plan))
                if MODE != "live" or not plan:
                    break
                shortfall = False
                for loan, loan_name, amount in plan:
                    stats["attempts"] += 1
                    ok, invested, balance_after, reason = place_investment(page, token, loan, amount, stats["balance_after"])
                    if ok:
                        stats["successes"] += 1
                        stats["total_invested"] += invested
                        tracker.add(loan_name, invested)
                        stats["invested"].append(_line_summary(loan, loan_name, invested))
                        if invested < amount - 0.01:
                            shortfall = True
                            excluded.add(loan["loan_ref"])
                    else:
                        stats["failures"] += 1
                        stats["failed_attempts"].append({"label": loan_name, "amount": amount, "reason": reason})
                        shortfall = True
                        excluded.add(loan["loan_ref"])
                    stats["balance_after"] = balance_after

                # A loan taken by someone else or whose amount dropped leaves cash unplaced:
                # read a fresh listing and plan the rest.
                investable = config.investable_balance(stats["balance_after"])
                open_names = tracker.investable_names(MIN_INVESTMENT_AMOUNT)
                if not shortfall or pass_number == MAX_PASSES or investable < MIN_INVESTMENT_AMOUNT or not open_names:
                    break
                log.info("Part of the plan could not be placed - planning the remaining %.2f EUR (pass %d/%d).", investable, pass_number + 1, MAX_PASSES)
    except Exception as exc:
        error = str(exc)
        _log_diagnostics("run_error", error=error, traceback=traceback.format_exc())
        log.exception("%s invest bot failed.", PLATFORM)
    finally:
        _log_diagnostics("run_summary", stats=stats, error=error)
        if error or (MODE == "live" and stats["attempts"]):
            had_problem = bool(error or stats["failures"])
            send_income_marketplace_invest_summary_email(
                stats, error=error,
                diagnostics_text=_collect_run_diagnostics(run_started_at, tags=None if had_problem else {"investment_create"}),
                robot_config=config,
            )
    if error:
        sys.exit(1)


if __name__ == "__main__":
    run()
