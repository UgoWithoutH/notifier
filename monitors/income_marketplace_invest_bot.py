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
         20 loans per page, `{loanCount, pages, loanList}`. Only the interest-rate
         range filter (id 7) is honoured server-side (status / originator
         multiselects are ignored), so those are filtered here. The listing is live
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

MODE (env INCOME_MARKETPLACE_BOT_MODE, default "dry"):
    dry  - plan only, nothing is bought.
    live - real purchases.

Required env vars: INCOME_MARKETPLACE_EMAIL, INCOME_MARKETPLACE_PASSWORD,
INCOME_MARKETPLACE_TOTP_SECRET, GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS, SMTP_* / EMAIL_TO.
Optional: CRON_JOB_API_KEY, INCOME_MARKETPLACE_CRON_JOB_ID -> cron-job.org schedule from the
"Temps planification solde suffisant / insuffisant" columns of the sheet block
(defaults: every 2 min when the balance >= minimum, once a day below).
"""

import json
import logging
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

from diversification.income_marketplace_diversification import API_BASE, INVESTOR_DETAILS_URL, _fetch_json, login
from shared.cron_schedule import DAY_MINUTES, apply_startup_jitter, ensure_schedule_interval
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

MODE = os.environ.get("INCOME_MARKETPLACE_BOT_MODE", "dry").strip().lower()
MIN_INVESTMENT_AMOUNT = float(os.environ.get("INCOME_MARKETPLACE_MIN_INVESTMENT_AMOUNT", "10"))
MAX_PAGES = 60
LIST_MAX_ATTEMPTS = 4
LIST_RETRY_BASE_DELAY_SECONDS = 3
BALANCE_CHECK_ATTEMPTS = 4
BALANCE_CHECK_DELAY_SECONDS = 2
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


def fetch_marketplace_loans(page, token: str, min_rate: float | None) -> list:
    """Every listed loan (deduplicated on `loan_id`), highest interest rate first.
    `min_rate` is a server-side pre-filter (falls back to an unfiltered listing if refused)."""
    filters = [] if min_rate is None else [{"id": RATE_FILTER_ID, "values": [min_rate, RATE_FILTER_MAX]}]
    loans: dict = {}
    page_count = 1
    page_number = 1
    while page_number <= min(page_count, MAX_PAGES):
        body = {
            "filters": filters, "search": "", "minimum_investment": _format_amount(MIN_INVESTMENT_AMOUNT),
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
        for loan in data.get("loanList") or []:
            loan.pop("loan_schedule", None)
            loans.setdefault(loan["loan_id"], loan)
        page_number += 1
    if page_count > MAX_PAGES:
        log.warning("Hit MAX_PAGES (%d) of %d - listing truncated.", MAX_PAGES, page_count)
    return list(loans.values())


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
    # "Exclure prêts extensibles" of the sheet = the site's "Exclude extendable loans" option.
    if config_loan.exclude_extendable and loan.get("loan_extendable_schedule"):
        return False
    return _term_matches(loan, config_loan, today)


def plan_loans(loans: list, tracker, budget: float) -> list:
    """[(loan, originator name in the sheet, amount)] in listing order."""
    candidates, by_ref = [], {}
    today = datetime.now(timezone.utc).date()
    for loan in loans:
        if not _is_investable(loan):
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
    result = _post_json(page, INVESTMENT_CREATE_URL, token, payload)
    _log_diagnostics(
        "investment_create", payload=payload, status=result["status"], response=result["text"][:3000],
        balance_before=balance_before,
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
    disabled, wait_range = begin_run(PLATFORM, INCOME_MARKETPLACE_CRON_JOB_ID, CRON_SCHEDULE_STATE_FILE, 2, DAY_MINUTES)
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
            default_minutes = 2 if sufficient else DAY_MINUTES
            ensure_schedule_interval(
                config.schedule_minutes(sufficient, default_minutes) if config else default_minutes,
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

            loans = fetch_marketplace_loans(page, token, config.lowest_min_rate())
            log.info("%d loan(s) listed, %d with status Current.", len(loans), sum(_is_investable(loan) for loan in loans))

            # Mail (once) when no listed loan belongs to an originator flagged in the sheet.
            state = load_state(STATE_FILE, {"missing_loans": {}})
            seen_names = set()
            today = datetime.now(timezone.utc).date()
            for loan in loans:
                config_loan = config.find_loan(loan.get("loan_originator_name"))
                if config_loan is not None and _is_investable(loan) and _accepted(loan, config_loan, today):
                    seen_names.add(config_loan.name)
            all_names = sorted(config.active_names())
            report_missing_loans(
                PLATFORM, {name: name in seen_names for name in all_names},
                state.setdefault("missing_loans", {}), all_names=all_names, grace_seconds=TRANSIENT_GRACE_SECONDS,
            )
            save_state(STATE_FILE, state)

            plan = plan_loans(loans, tracker, investable)
            stats["plan"] = [_line_summary(*line) for line in plan]
            log.info("%d purchase(s) planned (%.2f EUR).", len(plan), sum(amount for *_rest, amount in plan))
            if MODE != "live":
                return
            for loan, loan_name, amount in plan:
                stats["attempts"] += 1
                ok, invested, balance_after, reason = place_investment(page, token, loan, amount, stats["balance_after"])
                if ok:
                    stats["successes"] += 1
                    stats["total_invested"] += invested
                    tracker.add(loan_name, invested)
                    stats["invested"].append(_line_summary(loan, loan_name, invested))
                else:
                    stats["failures"] += 1
                    stats["failed_attempts"].append({"label": loan_name, "amount": amount, "reason": reason})
                stats["balance_after"] = balance_after
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
