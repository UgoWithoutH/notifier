"""Email notification via SMTP."""

import json
import os
import smtplib
import logging
from math import isfinite
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
log = logging.getLogger("notifier")

SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
EMAIL_FROM = os.environ.get("EMAIL_FROM", SMTP_USER)
EMAIL_TO = os.environ.get("EMAIL_TO")


def _fmt_bounds(low, high, unit: str) -> str:
    if low is None and high is None:
        return "-"
    low_str = f"{low:g}" if low is not None else ""
    high_str = f"{high:g}" if high is not None else ""
    return f"{low_str}..{high_str}{unit}"


def _robot_config_lines(config) -> list:
    """Body section describing the "config robots" sheet settings used by a run
    (shared.robot_config.PlatformConfig, duck-typed to keep this module free of
    the Google Sheet import). Empty list if `config` is None."""
    if config is None:
        return []
    lines = ["", "=== Config robot utilisée ==="]
    country_pct = getattr(config, "country_max_pct", None)
    lines.append(f"Plafond par pays : {country_pct:g}% du solde" if country_pct is not None else "Plafond par pays : aucun")
    for loan in config.loans.values():
        if not (loan.active or loan.secondary_active):
            continue
        markets = "/".join(m for m, on in (("primaire", loan.active), ("secondaire", loan.secondary_active)) if on)
        parts = [
            f"taux {_fmt_bounds(loan.min_rate, loan.max_rate, '%')}",
            f"montant {_fmt_bounds(loan.min_amount, loan.max_amount, ' €')}",
            f"plafond loan {loan.max_loan_pct:g}%" if loan.max_loan_pct is not None else "plafond loan -",
        ]
        if loan.equal_split:
            parts.append("répartition équivalente")
        if loan.secondary_active:
            parts.append(f"durée restante {_fmt_bounds(loan.min_months, loan.max_months, ' mois')}")
            parts.append(f"décote/prime {_fmt_bounds(loan.min_premium, loan.max_premium, '%')}")
        country = f" ({loan.country})" if loan.country else ""
        lines.append(f"- {loan.name}{country} [{markets}] : " + " | ".join(parts))
    if len(lines) == 3:
        lines.append("Aucun loan actif.")
    return lines


def _send_invest_summary_email(
    platform: str,
    invested: list,
    failures: int = 0,
    error: str | None = None,
    robot_config=None,
    attachment: tuple | None = None,
    failed: list | None = None,
) -> None:
    """Common recap email of the Swaper/Lendermarket/Afranga invest bots
    (same format on purpose, explicit user request 2026-10-21): what was
    invested (originator/loan, amount, interest rate), any error/failure,
    then the "config robot" reminder at the bottom.

    `invested`: list of `{"label": str, "amount": float, "rates": [float]}`.
    `failed`: list of `{"label": str, "amount": float | None, "reason": str}`,
    one per failed attempt, listed under the failure count.
    `attachment`: optional `(filename, text)` with the technical detail of a
    failure, only attached when something went wrong."""
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_TO]):
        log.error(
            "SMTP configuration is incomplete; cannot send email. "
            "Required env vars: SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_TO."
        )
        return

    total = sum(item["amount"] for item in invested)
    if invested:
        subject = f"[{platform} Invest Bot] {len(invested)} investissement(s) - {total:.2f} €"
    else:
        subject = f"[{platform} Invest Bot] Aucun investissement réussi"
    if error or failures:
        subject += " - ERREUR"

    body_parts = []
    if error:
        body_parts += [f"⚠ ERREUR : {error}", ""]
    if failures:
        body_parts.append(f"⚠ {failures} tentative(s) d'investissement échouée(s).")
        for item in failed or []:
            amount = item.get("amount")
            amount_str = f" ({amount:.2f} €)" if amount is not None else ""
            body_parts.append(f"  - {item['label']}{amount_str} : {item.get('reason') or 'raison inconnue'}")
        body_parts.append("")

    if invested:
        body_parts.append("=== Investissements ===")
        unverified_amounts = 0
        for item in invested:
            rates = ", ".join(f"{rate:.2f}%" for rate in item.get("rates") or [])
            amounts = item.get("amounts") or []
            detail = f" ({' + '.join(f'{a:.2f}' for a in amounts)})" if len(amounts) > 1 else ""
            unverified = item.get("unverified_amounts", 0)
            unverified_amounts += unverified
            note = f" (montant demandé pour {unverified} prêt(s), réponse Swaper sans montant exploitable)" if unverified else ""
            body_parts.append(f"- {item['label']} : {item['amount']:.2f} €{detail}{note} @ {rates or 'n/a'}")
        total_note = f" (dont {unverified_amounts} montant(s) demandé(s), non vérifié(s) dans la réponse Swaper)" if unverified_amounts else ""
        body_parts.append(f"Total investi : {total:.2f} €{total_note}")

    body_parts.extend(_robot_config_lines(robot_config))
    if attachment:
        body_parts += ["", "Le détail technique (requêtes/réponses) est joint à cet email."]

    msg = MIMEMultipart()
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg["Subject"] = subject
    msg.attach(MIMEText("\n".join(body_parts), "plain"))
    if attachment:
        filename, text = attachment
        part = MIMEText(text, "plain", "utf-8")
        part.add_header("Content-Disposition", "attachment", filename=filename)
        msg.attach(part)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())
        log.info("%s invest summary email sent to %s.", platform, EMAIL_TO)
    except Exception:
        log.exception("Failed to send %s invest summary email.", platform)


def _swaper_failure_reason(attempt: dict) -> str:
    if attempt.get("error"):
        return "exception pendant la saisie du montant ou le clic sur « + »"
    if attempt.get("not_approved"):
        return "investissement non approuvé par Swaper"
    if not attempt.get("modal_html"):
        return "aucun modal de confirmation détecté après le clic sur « + »"
    if not attempt.get("confirmed"):
        return "bouton « Confirm » introuvable ou non cliquable"
    return "échec inconnu"


def _swaper_actual_amount(attempt: dict) -> float | None:
    """Return Swaper's accepted buy amount, when the successful response provides it."""
    for call in reversed(attempt.get("confirm_api_calls") or []):
        if call.get("method") != "POST" or "/buy" not in (call.get("url") or ""):
            continue
        try:
            status = int(call.get("status"))
            body = json.loads(call.get("body") or "null") or {}
        except (TypeError, ValueError):
            continue
        if not 200 <= status < 300 or not isinstance(body, dict):
            continue
        investment = body.get("investment")
        if not isinstance(investment, dict):
            continue
        raw_amount = investment.get("amount")
        if isinstance(raw_amount, bool):
            continue
        try:
            amount = float(raw_amount)
        except (TypeError, ValueError):
            continue
        if amount > 0 and isfinite(amount):
            return amount
    return None


def send_swaper_investment_summary_email(attempts: list, error: str | None = None, robot_config=None) -> None:
    """Swaper recap, built from the attempts of monitors/swaper_invest_bot.py
    (`_invest_available_loans()`): only confirmed attempts count as
    investments, the others as failures. The attachment (the real
    investment requests/responses, plus the modal/row HTML of each failed
    attempt) is only joined when something failed."""
    invested = {}
    failures = 0
    failed = []
    failed_details = []
    investment_calls = []
    for attempt in attempts:
        loan_ref = attempt.get("loan_number") or attempt.get("loan_id")
        if attempt.get("confirmed") and not attempt.get("error") and not attempt.get("not_approved"):
            name = attempt.get("originator") or f"Prêt {loan_ref}"
            entry = invested.setdefault(name, {"label": name, "amount": 0.0, "rates": [], "amounts": [], "unverified_amounts": 0})
            actual_amount = _swaper_actual_amount(attempt)
            amount = actual_amount if actual_amount is not None else (attempt.get("amount") or 0.0)
            entry["amount"] += amount
            entry["amounts"].append(amount)
            if actual_amount is None:
                entry["unverified_amounts"] += 1
            try:
                entry["rates"].append(float(attempt.get("rate")))
            except (TypeError, ValueError):
                pass
        else:
            failures += 1
            reason = _swaper_failure_reason(attempt)
            label = f"{attempt.get('originator') or 'Swaper'} - prêt {loan_ref}"
            failed.append({"label": label, "amount": attempt.get("amount"), "reason": reason})
            failed_details.append({
                "loan_number": loan_ref,
                "originator": attempt.get("originator"),
                "amount": attempt.get("amount"),
                "reason": reason,
                "modal_html": (attempt.get("modal_html") or "")[:20000] or None,
                "row_html_after_click": (attempt.get("row_html_after_click") or "")[:20000] or None,
            })
        for call in attempt.get("confirm_api_calls") or []:
            url = call.get("url") or ""
            if not any(path in url for path in ("/buy", "is-manual-investment-approved")):
                continue
            investment_calls.append({
                "loan_number": loan_ref,
                "originator": attempt.get("originator"),
                "amount": attempt.get("amount"),
                **call,
            })

    attachment = None
    if failed_details or investment_calls:
        attachment = (
            "swaper_investment_requests.json",
            json.dumps(
                {"failed_attempts": failed_details, "investment_calls": investment_calls},
                indent=2, ensure_ascii=False, default=str,
            ),
        )
    _send_invest_summary_email(
        "Swaper", list(invested.values()), failures=failures, error=error,
        robot_config=robot_config, attachment=attachment, failed=failed,
    )


def send_lendermarket_invest_summary_email(stats: dict, error: str | None = None, diagnostics_text: str | None = None, robot_config=None) -> None:
    """Send the end-of-run recap for monitors/lendermarket_monitor.py's real
    auto-invest step (invest_selected_lenders(), added 2026-07-24). Only
    called when at least one real investment attempt was made this run, OR
    an unexpected error occurred (see that module's run()) - NOT on every
    run, so this frequent scheduled monitor doesn't spam an email every
    cycle when there was simply nothing to invest.

    `stats` is the dict built by invest_selected_lenders(): `balance_before`,
    `balance_after` (running balance decremented by every successful
    investment), `lender_budgets` (per-lender share of the balance, 0.0 for
    a selected lender with no loan available this run), `invest_attempts`,
    `invest_successes`, `invest_failures`, `total_invested`, `lender_stats`
    (per-lender dict with `budget`, `country`, `loans_seen`, `attempts`,
    `successes`, `failures`, `invested_amount`, `invested_loans` - each a
    `{"amount", "interestRate"}` dict, no loan id), `country_blocked`
    (added 2026-07-31: list of lender names excluded this run because their
    country already hit the Google-Sheet-configured per-country cap - see
    get_lendermarket_country_allocations()), `min_interest_rate` (the rate
    actually used this run). Also `originator_blocked` (added 2026-08-05):
    the SAME kind of cap, but per lender instead of per country - see
    get_lendermarket_originator_caps().

    Per explicit user request (2026-08-12), the email body is DELIBERATELY
    kept lean: only the invested lender/country + the invested amount and
    interest rate(s) are shown (no loan id, no per-lender budget/loans-seen
    breakdown, no full per-country/per-lender cap status dump - just the
    short "bloqué" lender-name lists when a cap was actually hit).
    `error`, if set, is a short description of an unexpected exception that
    interrupted the invest step early. Same convention as
    peerberry_invest_bot.py's summary email: the body never includes raw
    request/response detail - `diagnostics_text` (this run's own
    lendermarket_invest_diagnostics.log entries, built by
    lendermarket_monitor._collect_run_invest_diagnostics()), if provided,
    is attached instead as a plain-text .log file, so the full detail
    (including every failed attempt's real request/response) is directly
    available by email.
    """
    invested = []
    for name, s in sorted((stats.get("lender_stats") or {}).items()):
        if s.get("successes", 0) <= 0:
            continue
        country = s.get("country")
        rates = [
            lo.get("interestRate")
            for lo in (s.get("invested_loans") or [])
            if lo.get("interestRate") is not None
        ]
        invested.append({
            "label": f"{name} ({country})" if country else name,
            "amount": s.get("invested_amount", 0.0),
            "rates": rates,
            "amounts": [lo["amount"] for lo in (s.get("invested_loans") or []) if lo.get("amount") is not None],
        })

    attachment = None
    if diagnostics_text:
        attachment = ("lendermarket_invest_bot_diagnostics_this_run.log", diagnostics_text)
    _send_invest_summary_email(
        "Lendermarket", invested, failures=stats.get("invest_failures", 0), error=error,
        robot_config=robot_config, attachment=attachment, failed=stats.get("failed_attempts"),
    )


def send_afranga_invest_summary_email(stats: dict, error: str | None = None, diagnostics_text: str | None = None, robot_config=None) -> None:
    """Recap of monitors/afranga_invest_bot.py (same email format as the
    Swaper/Lendermarket ones). Only real purchases (`stats["invested"]`) are
    listed; dry/probe runs never buy anything, so they only get here on error."""
    grouped = {}
    for item in stats.get("invested") or []:
        entry = grouped.setdefault(item["loan"], {"markets": [], "amount": 0.0, "rates": [], "amounts": []})
        if item["market"] not in entry["markets"]:
            entry["markets"].append(item["market"])
        entry["amount"] += item["amount"]
        entry["amounts"].append(item["amount"])
        if item.get("rate") is not None:
            entry["rates"].append(item["rate"])
    invested = [
        {
            "label": f"[{'/'.join(entry['markets'])}] {loan}",
            "amount": entry["amount"],
            "rates": entry["rates"],
            "amounts": entry["amounts"],
        }
        for loan, entry in grouped.items()
    ]
    attachment = None
    if diagnostics_text:
        attachment = ("afranga_invest_bot_diagnostics_this_run.log", diagnostics_text)
    _send_invest_summary_email(
        "Afranga", invested, failures=stats.get("failures", 0), error=error,
        robot_config=robot_config, attachment=attachment, failed=stats.get("failed_attempts"),
    )


def send_peerberry_email(originators: list) -> None:
    """Send the PeerBerry "distribution by loan originators" recap.

    `originators` is the list built by
    peerberry_monitor.normalize_originators(): one dict per loan originator
    with `originator`, `company`, `iso2`, `amount` (EUR, float) and `part`
    (%, float), already sorted by amount descending.
    """
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_TO]):
        log.error(
            "SMTP configuration is incomplete; cannot send email. "
            "Required env vars: SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_TO."
        )
        return

    total_amount = sum(o["amount"] for o in originators)
    subject = f"[PeerBerry] Répartition par prêteur ({len(originators)} prêteurs, {total_amount:.2f} €)"

    body_parts = [f"Montant total investi : {total_amount:.2f} €", ""]
    for o in originators:
        label = f"{o['originator']} ({o['iso2']})" if o.get("iso2") else o["originator"]
        body_parts.append(f"{label} : {o['amount']:.2f} € ({o['part']:.2f}%)")
    body = "\n".join(body_parts)

    msg = MIMEMultipart()
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())
        log.info("PeerBerry notification email sent to %s.", EMAIL_TO)
    except Exception:
        log.exception("Failed to send PeerBerry notification email.")


def send_peerberry_available_email(available_money: float) -> None:
    """Notify that PeerBerry's "Available for investment" balance is >= 10 EUR.

    Sent every run the condition is met (no notification gate/dedup - by
    design, see peerberry_monitor.py).
    """
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_TO]):
        log.error(
            "SMTP configuration is incomplete; cannot send email. "
            "Required env vars: SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_TO."
        )
        return

    subject = f"[PeerBerry] {available_money:.2f}€ disponible pour investir"
    body = f"Montant disponible pour investir sur PeerBerry : {available_money:.2f} €"

    msg = MIMEMultipart()
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())
        log.info("PeerBerry available-balance notification email sent to %s.", EMAIL_TO)
    except Exception:
        log.exception("Failed to send PeerBerry available-balance notification email.")


def send_peerberry_invest_bot_summary_email(stats: dict, error: str | None = None, diagnostics_text: str | None = None) -> None:
    """Send the end-of-run recap for monitors/peerberry_invest_bot.py.

    `stats` is the dict built by peerberry_invest_bot.run(): `polls`,
    `loans_seen` (count), `invest_attempts`, `invest_successes`,
    `invest_failures`, `total_invested_attempted`, `stuck_events`, `errors`,
    `final_available_money`, `selected_originators`, `min_interest_rate`
    (the real minInterestRate value used this run's `/loans` requests,
    read from the Sheet at startup - added 2026-08-03 so a "0 loans_seen"
    run can be diagnosed directly from this email instead of needing the
    separate GitHub Actions console log),
    `originator_stats` (per-originator dict with `loans_seen`, `attempts`,
    `successes`, `failures`, `invested_amount`, `invested_loans` - there is
    no per-originator budget anymore, investments simply draw from the
    shared `final_available_money`),
    `raw_originators_seen` (every distinct raw `loanOriginator` value
    PeerBerry returned this run, matched or not - lets a mismatch between
    the Sheet selection and PeerBerry's real values be spotted directly
    from the email), `initial_available_money` (balance seen at startup,
    before any investment this run), `total_invested_all_originators`/
    `total_peerberry_budget` (everything invested across EVERY loan
    originator on the account, live from the API, plus the available
    balance - the base the per-country threshold percentage is applied
    to), `country_threshold_percentage`/`country_threshold_amount`
    (per-country investment cap read from the Sheet at startup, see
    shared.google_sheet.get_peerberry_country_allocations()),
    `country_invested_initial`/`country_invested_final`/`blocked_countries`
    (countries that reached that cap during the run - see
    peerberry_invest_bot.py's `_update_blocked_countries()`), and
    `country_details` (one dict per country - `country`, `initial_amount`,
    `final_amount`, `pct_of_budget`, `blocked` - the full per-country debug
    breakdown shown in the email below, to spot a wrong-looking block/
    non-block directly without digging through logs/diagnostics).
    Also `originator_cap_details`/`blocked_originators_cap` (added
    2026-08-05): the SAME kind of cap, but per loan originator instead of
    per country - a percentage of the total budget a single loan
    originator should never exceed, read from
    shared.google_sheet.get_peerberry_originator_caps() (see
    peerberry_invest_bot.py's `_update_blocked_originators()`).
    `error`, if set, is a short description of a fatal error that stopped
    the run early. The email body itself never includes any diagnostic
    request/response detail - `diagnostics_text`, if provided (this run's
    own diagnostics-file entries, built by
    peerberry_invest_bot._collect_run_diagnostics()), is attached instead as
    a plain-text .log file, so the full detail is directly available by
    email without needing to manually pull it out of the GitHub Actions
    cache.
    """
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_TO]):
        log.error(
            "SMTP configuration is incomplete; cannot send email. "
            "Required env vars: SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_TO."
        )
        return

    status = "ÉCHEC" if error else "OK"
    subject = f"[PeerBerry Invest Bot] {status} - {stats.get('invest_successes', 0)} investissement(s) réussi(s)"

    body_parts = [
        f"Statut : {status}" + (f" ({error})" if error else ""),
        f"Sondages effectués : {stats.get('polls', 0)}",
        f"Prêts distincts vus (tous originators confondus) : {stats.get('loans_seen', 0)}",
        f"Tentatives d'investissement : {stats.get('invest_attempts', 0)}",
        f"  - réussies : {stats.get('invest_successes', 0)}",
        f"  - échouées : {stats.get('invest_failures', 0)}",
        f"Montant total tenté : {stats.get('total_invested_attempted', 0.0):.2f} €",
        f"Solde final non investi : {stats.get('final_available_money', 0.0):.2f} €",
        f"Situations bloquées détectées : {stats.get('stuck_events', 0)}",
        f"Erreurs rencontrées : {stats.get('errors', 0)}",
    ]

    min_interest_rate = stats.get("min_interest_rate")
    if min_interest_rate is not None:
        body_parts.append(f"Taux d'intérêt minimum utilisé (minInterestRate) : {min_interest_rate}")

    raw_originators_seen = stats.get("raw_originators_seen") or []
    if raw_originators_seen:
        body_parts.append("")
        body_parts.append("Loan originators bruts vus (renvoyés par l'API, matchés ou non) :")
        body_parts.append("  " + ", ".join(raw_originators_seen))

    country_threshold_percentage = stats.get("country_threshold_percentage")
    if country_threshold_percentage is not None:
        total_peerberry_budget = stats.get("total_peerberry_budget", 0.0)
        body_parts.append("")
        body_parts.append(
            f"Seuil par pays : {country_threshold_percentage:.2f}% du budget total "
            f"({stats.get('total_invested_all_originators', 0.0):.2f} € investis + "
            f"{stats.get('initial_available_money', 0.0):.2f} € disponible = "
            f"{total_peerberry_budget:.2f} €) = {stats.get('country_threshold_amount', 0.0):.2f} € max par pays"
        )

        country_details = stats.get("country_details") or []
        if country_details:
            body_parts.append("")
            body_parts.append("Détail par pays (lu depuis le Sheet au démarrage, puis suivi en direct) :")
            for d in country_details:
                flag = "BLOQUÉ" if d["blocked"] else "ok"
                body_parts.append(
                    f"  - {d['country']:<20s} initial : {d['initial_amount']:>10.2f} € | "
                    f"final : {d['final_amount']:>10.2f} € | "
                    f"{d['pct_of_budget']:>6.2f}% du budget "
                    f"(seuil {country_threshold_percentage:.2f}%) -> {flag}"
                )

        blocked_countries = stats.get("blocked_countries") or []
        body_parts.append("")
        if blocked_countries:
            body_parts.append(f"Pays bloqués ce run ({len(blocked_countries)}) : {', '.join(blocked_countries)}")
        else:
            body_parts.append("Aucun pays bloqué ce run.")

    originator_cap_details = stats.get("originator_cap_details") or []
    if originator_cap_details:
        body_parts.append("")
        body_parts.append("=== Plafond par loan originator (% du budget total) ===")
        for d in originator_cap_details:
            flag = "BLOQUÉ" if d["blocked"] else "ok"
            body_parts.append(
                f"  - {d['originator']:<20s} plafond {d['max_percentage']:.2f}% "
                f"({d['threshold_amount']:.2f} €) | investi : {d['final_amount']:.2f} € "
                f"({d['pct_of_budget']:.2f}%) -> {flag}"
            )
        blocked_originators_cap = stats.get("blocked_originators_cap") or []
        body_parts.append("")
        if blocked_originators_cap:
            body_parts.append(f"Loan originators bloqués ce run (plafond par originator atteint) : {', '.join(blocked_originators_cap)}")
        else:
            body_parts.append("Aucun loan originator bloqué par son propre plafond ce run.")

    originator_stats = stats.get("originator_stats") or {}
    if originator_stats:
        body_parts.append("")
        body_parts.append("=== Détail par loan originator ===")
        for name, s in originator_stats.items():
            body_parts.append("")
            body_parts.append(f"- {name}")
            body_parts.append(
                f"    Investi : {s.get('invested_amount', 0.0):.2f} €"
            )
            body_parts.append(
                f"    Prêts disponibles vus : {s.get('loans_seen', 0)} | "
                f"tentatives : {s.get('attempts', 0)} "
                f"(réussies : {s.get('successes', 0)}, échouées : {s.get('failures', 0)})"
            )
            invested_loans = s.get("invested_loans") or []
            if invested_loans:
                details = ", ".join(f"{lo['loanId']} ({lo['amount']:.2f} €)" for lo in invested_loans)
                body_parts.append(f"    Prêts investis : {details}")
            else:
                body_parts.append("    Prêts investis : aucun")

    if stats.get("invest_attempts", 0) > 0:
        body_parts.append("")
        body_parts.append(
            "Le détail complet (requête/réponse) de chaque tentative d'investissement "
            "est dans le fichier de diagnostics local (non exposé publiquement)."
        )
    if diagnostics_text:
        body_parts.append(
            "Les entrées de diagnostics de ce run sont jointes à cet email "
            "(peerberry_invest_bot_diagnostics_this_run.log)."
        )
    body = "\n".join(body_parts)

    msg = MIMEMultipart()
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    if diagnostics_text:
        attachment = MIMEText(diagnostics_text, "plain", "utf-8")
        attachment.add_header(
            "Content-Disposition", "attachment", filename="peerberry_invest_bot_diagnostics_this_run.log"
        )
        msg.attach(attachment)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())
        log.info("PeerBerry invest bot summary email sent to %s.", EMAIL_TO)
    except Exception:
        log.exception("Failed to send PeerBerry invest bot summary email.")


def send_bienpreter_geo_issues_email(issues: list, error: str | None = None) -> None:
    """Sent by bienpreter_diversification.py's run() ONLY when the
    'Répartition géographique' per-borrower breakdown step
    (fetch_active_loans_by_borrower() + fill_bienpreter_borrower_geo_amounts())
    hit at least one issue this run - never on a fully clean run, to avoid
    spamming an email every time.

    `issues` : list of short strings, each describing one non-fatal problem
    (a project's country couldn't be found/parsed, a borrower has loans in
    more than one country, a resolved country doesn't match any column
    header in the Sheet, ...) - collected across both the fetch side and
    the Sheet-write side.
    `error`, if set, is a short description of an unexpected exception that
    stopped the whole borrower/geo breakdown step early (the rest of the
    Bienprêter run - balances, this month's interest, bonus - is
    unaffected, this step is a soft-fail by design).
    """
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_TO]):
        log.error(
            "SMTP configuration is incomplete; cannot send email. "
            "Required env vars: SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_TO."
        )
        return

    status = "ERREUR" if error else "ATTENTION"
    subject = f"[Bienprêter] {status} - répartition géographique par emprunteur"

    body_parts = []
    if error:
        body_parts.append(f"Une erreur a interrompu la mise à jour de la répartition géographique : {error}")
        body_parts.append("")
    if issues:
        body_parts.append(f"{len(issues)} problème(s) rencontré(s) :")
        body_parts.extend(f"  - {issue}" for issue in issues)
    body = "\n".join(body_parts)

    msg = MIMEMultipart()
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())
        log.info("Bienprêter geographic breakdown issues email sent to %s.", EMAIL_TO)
    except Exception:
        log.exception("Failed to send Bienprêter geographic breakdown issues email.")


def send_diversification_recap_email(amounts: dict, missing_platforms: list | None = None, error: str | None = None) -> bool:
    """Sent once at the very end of the ".github/workflows/diversification.yml"
    GitHub Actions workflow (a dedicated final job that runs after every
    platform job, `if: always()`), summarizing the "non investi" (uninvested
    cash) amount of every platform of that workflow, re-read straight from
    the "Répartition géographique" section of the Sheet via
    shared.google_sheet.get_geographic_repartition_uninvested_amounts()
    (added 2026-08-10, per explicit user request).

    `amounts` : {platform: float}, already-known uninvested amounts.
    `missing_platforms` : platforms whose "non investi" row couldn't be
    read (row not found, or cell empty/non-parsable) - listed separately
    so a silent gap is never mistaken for a real 0.00 €.
    `error` : set if reading the Sheet itself failed entirely; the email is
    still sent (with just the error) so a broken recap step is never silent.

    Platforms with a 0.00 € amount are left out of the email. Returns True
    if the email was sent.
    """
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_TO]):
        log.error(
            "SMTP configuration is incomplete; cannot send email. "
            "Required env vars: SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_TO."
        )
        return False

    amounts = {p: a for p, a in amounts.items() if round(a, 2) != 0}
    total = sum(amounts.values())
    subject = f"Récapitulatif montants non investis - {total:.2f} €"
    if error:
        subject += " - ERREUR"

    body_lines = []
    if error:
        body_lines.append(f"⚠ ERREUR pendant la lecture du récapitulatif : {error}")
        body_lines.append("")
    if amounts:
        body_lines.append("Montants non investis (disponibles, pas encore prêtés) par plateforme :")
        body_lines.append("")
        for platform, amount in sorted(amounts.items()):
            body_lines.append(f"- {platform:<15s} : {amount:.2f} €")
    else:
        body_lines.append("Aucun montant non investi sur les plateformes.")
    body_lines.append("")
    body_lines.append(f"Total : {total:.2f} €")

    if missing_platforms:
        body_lines.append("")
        body_lines.append(f"Non disponible ce run : {', '.join(sorted(missing_platforms))}")

    body = "\n".join(body_lines)

    msg = MIMEMultipart()
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())
        log.info("Diversification recap email sent to %s.", EMAIL_TO)
        return True
    except Exception:
        log.exception("Failed to send diversification recap email.")
        return False


def send_new_bilans_email(new_bilans: list) -> None:
    """Sent by scripts/sync_bilans_financiers.py when new bilan rows were added
    to the "notes bilans" sheet. Each item: dict with platform, owner (lending
    company, or the platform itself), exercice, note. Grouped by platform."""
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_TO]):
        log.error(
            "SMTP configuration is incomplete; cannot send email. "
            "Required env vars: SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_TO."
        )
        return

    subject = f"[Bilans] {len(new_bilans)} nouveau(x) bilan(s) financier(s) ajouté(s)"
    body_lines = ["Nouveaux bilans ajoutés dans l'onglet \"notes bilans\" :", ""]
    by_platform: dict[str, list] = {}
    for b in new_bilans:
        by_platform.setdefault(b.get("platform") or "Autre", []).append(b)
    for platform, items in by_platform.items():
        body_lines.append(f"{platform} ({len(items)}) :")
        for b in items:
            note = f"{b['note']}/5" if b.get("note") not in ("", None) else "n/a"
            body_lines.append(f"  - {b['owner']} - exercice {b['exercice']} : note {note}")
        body_lines.append("")
    body = "\n".join(body_lines)

    msg = MIMEMultipart()
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())
        log.info("New bilans email sent to %s.", EMAIL_TO)
    except Exception:
        log.exception("Failed to send new bilans email.")


def send_bilans_sync_issues_email(issues: list, error: str | None = None) -> None:
    """Sent by scripts/sync_bilans_financiers.py when a platform/company of the
    "notes bilans" sheet cannot be found on the site, or when the sync crashes."""
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_TO]):
        log.error(
            "SMTP configuration is incomplete; cannot send email. "
            "Required env vars: SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_TO."
        )
        return

    if error:
        subject = "[Bilans][ERREUR] Échec de la synchronisation des bilans financiers"
    else:
        subject = f"[Bilans][ALERTE] {len(issues)} élément(s) introuvable(s) sur le site"
    body_lines = []
    if error:
        body_lines += ["La synchronisation a échoué :", error, ""]
    if issues:
        body_lines.append("Éléments de l'onglet \"notes bilans\" introuvables sur liberte-financiere.net :")
        body_lines += [f"- {i}" for i in issues]
    body = "\n".join(body_lines)

    msg = MIMEMultipart()
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())
        log.info("Bilans sync issues email sent to %s.", EMAIL_TO)
    except Exception:
        log.exception("Failed to send bilans sync issues email.")


def send_missing_loans_email(platform: str, names: list, detail: str | None = None) -> None:
    """Sent by the invest bots (Lendermarket, Swaper, Afranga) when a company
    flagged active in the "config robots" sheet is never found on the platform
    (unknown/misspelled name). Once per company (see shared/missing_loans.py)."""
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_TO]):
        log.error(
            "SMTP configuration is incomplete; cannot send email. "
            "Required env vars: SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_TO."
        )
        return

    subject = f"[{platform} Invest Bot] ATTENTION - société introuvable : {', '.join(names)}"
    body_lines = [
        f"Aucun prêt n'a jamais été trouvé sur {platform} pour {len(names)} société(s) active(s) dans l'onglet \"config robots\" :",
        *(f"  - {name}" for name in names),
        "",
        "Vérifier que le nom saisi dans la sheet correspond bien au nom de la société affiché sur la plateforme.",
        "Ce mail n'est envoyé qu'une fois par société ; elle est ensuite suivie normalement dès qu'elle est trouvée.",
    ]
    if detail:
        body_lines += ["", detail]

    msg = MIMEMultipart()
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg["Subject"] = subject
    msg.attach(MIMEText("\n".join(body_lines), "plain"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())
        log.info("Missing loans email sent to %s for %s.", EMAIL_TO, platform)
    except Exception:
        log.exception("Failed to send the missing loans email.")