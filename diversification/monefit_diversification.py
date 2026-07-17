"""Monefit (SmartSaver) account balance fetcher.

Same idea as afranga_diversification.py / peerberry_diversification.py /
lendermarket_diversification.py / loanch_diversification.py, but simpler:
Monefit SmartSaver is a single savings product (not a marketplace split
across many loan originators), so there's nothing to group/aggregate - this
just logs into https://smartsaver.monefit.com, reads the account's "Total
Wealth" balance, and hands it to fill_current_month_amounts() (see
google_sheet.py) as a single-entry list using "monefit" as the loan
originator label, mirroring the {"originator", "outstanding"} shape used by
afranga_diversification.py. No email is sent - same as the other
diversification scripts.

REWRITTEN 2026-07-18 to use plain `requests` instead of Playwright (no
browser at all), same technique as bricks_diversification.py /
goandgrow_diversification.py / bienpreter_diversification.py - much faster
in GitHub Actions. Verified live: Monefit's backend (a separate API host,
`api-smartsaver.monefit.com`) has NO Cloudflare/bot-protection at all and
returns the JWT directly in the login response body (no DOM-interception
trick needed like the old Playwright version had to use for the account
summary call).

Login mechanism (verified 2026-07-18): `POST
https://api-smartsaver.monefit.com/v1/auth/login` with JSON body
`{"identificator": <email>, "password": <password>}` (NOT `"email"` - that
field name returns a 401 "Incorrect e-mail or password" even with correct
credentials, `"identificator"` is the field the SPA actually uses) returns
`{"data": {"result": {"mfa": false, "token": "<JWT>", ...}}}` - the JWT is
used as a `Authorization: Bearer <token>` header on every subsequent API
call. `mfa` is `false` on this account (confirmed no 2FA/TOTP step exists,
consistent with the original 2026-07-09 Playwright build's observation).

Balance: `GET https://api-smartsaver.monefit.com/v1/vaults` returns
`{"data": {"total": "...", "totalWealth": "5194.02522602", "mainAccount":
"...", "result": [...per-vault details...]}}` - `totalWealth` matches the
summary endpoint's `closingBalance` (see below) and is the same figure
shown as "Total Wealth" on the summary page.

This month's stats: `GET
https://api-smartsaver.monefit.com/v1/account/summary?dateFrom=<1st of
month>&dateTo=<today>` returns `{"data": {"result": {"openingBalance":
..., "closingBalance": ..., "interestIncome": "0.00000408", "bonus":
"0.00000000", "maturedVaults": "0", ...}}}` - same fields/semantics as the
old Playwright version's `page.expect_response()` capture of this same
endpoint (the SPA calls it itself); "Current month" default window
(1st of the current month through TODAY).

Required env vars:
    MONEFIT_EMAIL, MONEFIT_PASSWORD    -> Monefit SmartSaver account credentials
Optional:
    GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS  -> used to write this month's totals
                                            to the Google Sheet via
                                            fill_current_month_amounts() (see
                                            google_sheet.py)
"""

import os
import sys
import logging
from datetime import date

import requests
from dotenv import load_dotenv

from shared.google_sheet import fill_current_month_amounts, fill_current_month_bonus_breakdown, fill_geographic_repartition_amounts

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("monefit_diversification")

API_BASE = "https://api-smartsaver.monefit.com"
LOGIN_URL = f"{API_BASE}/v1/auth/login"
VAULTS_URL = f"{API_BASE}/v1/vaults"
SUMMARY_URL = f"{API_BASE}/v1/account/summary"
LOAN_ORIGINATOR_LABEL = "monefit"

MONEFIT_EMAIL = os.environ.get("MONEFIT_EMAIL")
MONEFIT_PASSWORD = os.environ.get("MONEFIT_PASSWORD")

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://smartsaver.monefit.com",
    "Referer": "https://smartsaver.monefit.com/en/login",
}


def login(session: requests.Session) -> None:
    """Log in to Monefit SmartSaver using MONEFIT_EMAIL/MONEFIT_PASSWORD via
    a plain HTTP POST (no browser), and set the returned JWT as the
    session's Authorization header for subsequent calls.

    Raises RuntimeError if the account has 2FA enabled (not handled here -
    no 2FA was ever observed on this account, see module docstring) or if
    login otherwise fails.
    """
    log.info("POST %s (submitting credentials)...", LOGIN_URL)
    r = session.post(LOGIN_URL, json={"identificator": MONEFIT_EMAIL, "password": MONEFIT_PASSWORD}, timeout=30)
    log.info("POST login: status=%s", r.status_code)
    r.raise_for_status()

    result = (r.json() or {}).get("data", {}).get("result") or {}
    if result.get("mfa"):
        raise RuntimeError("Monefit is asking for a 2FA/MFA step, which this pure-HTTP script does not handle.")

    token = result.get("token")
    if not token:
        raise RuntimeError(f"Login response did not contain a token: {result!r}")

    session.headers.update({"Authorization": f"Bearer {token}"})
    log.info("Logged in successfully.")


def fetch_balance(session: requests.Session) -> float:
    """Fetch the account's "Total Wealth" balance via the vaults endpoint.
    See module docstring for the verified `totalWealth` field."""
    log.info("GET %s (fetching balance)...", VAULTS_URL)
    r = session.get(VAULTS_URL, timeout=30)
    log.info("GET vaults: status=%s", r.status_code)
    r.raise_for_status()

    result = (r.json() or {}).get("data") or {}
    total_wealth = result.get("totalWealth")
    if total_wealth is None:
        raise RuntimeError(f"Could not find 'totalWealth' in the vaults response: {result!r}")

    try:
        return float(total_wealth)
    except (TypeError, ValueError):
        raise RuntimeError(f"Could not parse 'totalWealth' out of {total_wealth!r}.")


def fetch_current_month_statement_totals(session: requests.Session) -> dict:
    """Fetch this calendar month's "From daily returns" / "Rewards &
    bonuses" / "Matured Vaults" totals via the account/summary endpoint.
    See module docstring for the verified endpoint/fields."""
    today = date.today()
    first = today.replace(day=1)
    params = {"dateFrom": first.isoformat(), "dateTo": today.isoformat()}
    log.info("GET %s (fetching this month's statement totals)...", SUMMARY_URL)
    r = session.get(SUMMARY_URL, params=params, timeout=30)
    log.info("GET account/summary: status=%s", r.status_code)
    r.raise_for_status()

    result = (r.json() or {}).get("data", {}).get("result") or {}
    log.info("Raw account/summary result: %r", result)
    try:
        daily_returns = round(float(result.get("interestIncome") or 0.0), 2)
    except (TypeError, ValueError):
        log.warning("Could not parse 'interestIncome' %r - defaulting to 0.0.", result.get("interestIncome"))
        daily_returns = 0.0
    try:
        rewards_bonuses = round(float(result.get("bonus") or 0.0), 2)
    except (TypeError, ValueError):
        log.warning("Could not parse 'bonus' %r - defaulting to 0.0.", result.get("bonus"))
        rewards_bonuses = 0.0
    try:
        matured_vaults = round(float(result.get("maturedVaults") or 0.0), 2)
    except (TypeError, ValueError):
        log.warning("Could not parse 'maturedVaults' %r - defaulting to 0.0.", result.get("maturedVaults"))
        matured_vaults = 0.0

    log.info(
        "Parsed statement totals: daily_returns=%.2f, rewards_bonuses=%.2f, matured_vaults=%.2f",
        daily_returns, rewards_bonuses, matured_vaults,
    )
    return {
        "daily_returns": daily_returns,
        "rewards_bonuses": rewards_bonuses,
        "matured_vaults": matured_vaults,
    }


def run() -> None:
    if not MONEFIT_EMAIL or not MONEFIT_PASSWORD:
        log.error("MONEFIT_EMAIL and MONEFIT_PASSWORD environment variables are required.")
        sys.exit(1)

    log.info("Starting Monefit diversification run (pure HTTP, no browser).")

    session = requests.Session()
    session.headers.update(_HEADERS)

    try:
        login(session)
        balance = fetch_balance(session)
    except Exception:
        log.exception("Failed to log in or fetch the Monefit balance.")
        sys.exit(1)

    try:
        log.info("Fetching this month's statement totals...")
        statement_totals = fetch_current_month_statement_totals(session)
    except Exception:
        log.exception(
            "Failed to fetch this month's From daily returns/Rewards & bonuses/Matured Vaults - "
            "defaulting all three to 0.0."
        )
        statement_totals = {"daily_returns": 0.0, "rewards_bonuses": 0.0, "matured_vaults": 0.0}

    originators = [{"originator": LOAN_ORIGINATOR_LABEL, "outstanding": balance}]
    log.info("Monefit balance: %.2f EUR", balance)
    log.info(
        "This month's From daily returns: %.2f EUR, Rewards & bonuses: %.2f EUR, Matured Vaults: %.2f EUR",
        statement_totals["daily_returns"], statement_totals["rewards_bonuses"], statement_totals["matured_vaults"],
    )

    # Monefit's account/summary API has no gross/net/withholding-tax
    # breakdown (unlike Afranga/Bienpreter) - "From daily returns" is
    # mapped to both gross_interest_received/net_interest_received since
    # it's the closest equivalent figure on hand, withholding_tax defaults
    # to 0.0. Same standardized dict shape as every other
    # *_diversification.py, plus the platform-specific daily_returns/
    # rewards_bonuses/matured_vaults fields kept alongside it.
    # "rewards_bonuses" (the "Rewards & bonuses" figure) was already
    # fetched but only kept as a platform-specific extra field, never
    # surfaced under the standardized name - dissociated here too via
    # bonus_cashback_contest so it's ready to be written to its own Sheet
    # cell, separate from interest.
    amounts = {
        "total": balance,
        "gross_interest_received": statement_totals["daily_returns"],
        "net_interest_received": statement_totals["daily_returns"],
        "withholding_tax": 0.0,
        "bonus_cashback_contest": statement_totals["rewards_bonuses"],
        "daily_returns": statement_totals["daily_returns"],
        "rewards_bonuses": statement_totals["rewards_bonuses"],
        "matured_vaults": statement_totals["matured_vaults"],
    }
    fill_current_month_amounts(
        platform="Monefit",
        amounts=amounts,
        section="Crowdlending savings"
    )

    # Monefit's "bonus" field ("Rewards & bonuses") maps to "prime" (a
    # referral-style reward) - written to its own dedicated sub-row, never
    # to the "Bonus" row itself (a SUM formula over prime/cashback/
    # concours). Note: Monefit also runs a separate weekly investment-draw
    # ("concours"-style lottery, confirmed via live page text: "5 winners
    # ... picked at random") but the account/summary API has no distinct
    # field for draw winnings - only "prime" is written here, "concours"
    # is left untouched pending a dedicated data source.
    fill_current_month_bonus_breakdown(
        platform="Monefit",
        breakdown={"prime": statement_totals["rewards_bonuses"]},
        section="Crowdlending savings",
    )

    loan_originators = [{"name": LOAN_ORIGINATOR_LABEL, "amount": balance}]

    fill_geographic_repartition_amounts(loan_originators)


if __name__ == "__main__":
    run()
