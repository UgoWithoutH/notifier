"""Email notification via SMTP."""

import json
import os
import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
log = logging.getLogger("notifier")

SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
EMAIL_FROM = os.environ.get("EMAIL_FROM", SMTP_USER)
EMAIL_TO = os.environ.get("EMAIL_TO")


def _format_loan(loan: dict) -> str:
    """Format a single loan as one line: identifier, available amount, and
    yield (interest rate) - the only 3 things asked for, no extra fields
    and no URL."""
    number = loan.get("number") or loan.get("id")

    amount = loan.get("amount")
    try:
        amount_str = f"{float(amount):.2f} €"
    except (TypeError, ValueError):
        amount_str = "montant n/a"

    interest = loan.get("interestRatePerYear")
    try:
        interest_str = f"{float(interest):.2f}%"
    except (TypeError, ValueError):
        interest_str = "n/a"

    return f"Prêt {number} : {amount_str} | rendement {interest_str}"


def send_swaper_email(balance: float, loans: list) -> None:
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_TO]):
        log.error(
            "SMTP configuration is incomplete; cannot send email. "
            "Required env vars: SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_TO."
        )
        return

    total_amount = 0.0
    for loan in loans:
        try:
            total_amount += float(loan.get("amount"))
        except (TypeError, ValueError):
            pass

    subject = f"[Swaper] {balance:.2f}€ dispo + {len(loans)} prêt(s) manuel(s) disponible(s)"
    body_parts = [
        f"Solde non investi : {balance:.2f} €",
        f"Montant total disponible : {total_amount:.2f} €",
        "",
    ]
    body_parts.extend(_format_loan(loan) for loan in loans)
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
        log.info("Notification email sent to %s.", EMAIL_TO)
    except Exception:
        log.exception("Failed to send notification email.")


def send_swaper_investment_summary_email(attempts: list, captured_api_calls: list) -> None:
    """Send a summary of REAL Swaper investments made this run via the
    manual "+" button (see monitors.swaper_monitor._invest_available_loans()
    - explicit user decision 2026-07-25 to make this the actual production
    invest bot: real money, no click-and-abort safety net anymore).

    Sent EVERY time at least one investment was attempted this run (not
    one-time - real money moves every time, so it should always be
    visible), listing each attempt (loan number/id, amount, whether an
    unrecognized confirmation modal appeared, whether an error occurred).
    Attaches the raw captured `/rest/` API calls (added 2026-07-25, same
    day, explicit user request: "envoie bien tout ce dont tu auras besoin
    pour après essayer de faire en full http request le bot") as a `.json`
    file - for every loans-listing/filter/invest call observed this run:
    method, full url, ALL header NAMES for both request and response
    (values redacted for cookies/auth/csrf - see `_redact_sensitive_headers()`
    - so the shape of what's required is visible without leaking a live,
    short-lived session token), the raw request POST body, the response
    status and body. Together with `monitors/swaper_monitor.py`'s already-
    documented `login()`/`handle_two_factor()` flow (NOT captured this way,
    deliberately, to never risk logging a plaintext password/2FA code),
    this should carry everything needed to later attempt reproducing the
    loans-listing/filter/invest calls as plain HTTP requests (mirroring
    monitors/lendermarket_monitor.py's `requests.Session`-based bot),
    instead of driving a real browser - also useful right now, to confirm
    each investment attempt actually succeeded (status code/body).
    """
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_TO]):
        log.error(
            "SMTP configuration is incomplete; cannot send email. "
            "Required env vars: SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_TO."
        )
        return

    subject = f"[Swaper] {len(attempts)} investissement(s) r\u00e9el(s) tent\u00e9(s)"
    body_lines = [
        f"{len(attempts)} investissement(s) r\u00e9el(s) tent\u00e9(s) automatiquement sur Swaper "
        "(bouton '+' manuel, argent r\u00e9el) :",
        "",
    ]
    for attempt in attempts:
        label = attempt.get("loan_number") or attempt.get("loan_id")
        originator = attempt.get("originator")
        prefix = f"[{originator}] " if originator else ""
        line = f"- {prefix}Pr\u00eat {label} : {attempt.get('amount'):.2f} \u20ac"
        if attempt.get("error"):
            line += " -- ERREUR pendant le clic, voir les logs"
        elif attempt.get("modal_html"):
            line += " -- une fen\u00eatre de confirmation inattendue est apparue, investissement stopp\u00e9 ensuite (voir la pi\u00e8ce jointe)"
        body_lines.append(line)
    body_lines.append("")
    body_lines.append(
        "Le fichier joint contient les vraies requ\u00eates/r\u00e9ponses HTTP /rest/ observ\u00e9es "
        "pendant ce run (m\u00e9thode/URL/toutes les en-t\u00eates - valeurs sensibles redacted - "
        "corps/statut), pour les appels de listing/filtre de pr\u00eats ET d'investissement. "
        "V\u00e9rifie le statut de la requ\u00eate d'investissement pour confirmer qu'elle a bien "
        "r\u00e9ussi. Objectif secondaire : accumuler de quoi tenter, plus tard, de reproduire "
        "ces appels en pur HTTP (sans navigateur) - la connexion/2FA reste elle bas\u00e9e sur "
        "le navigateur (voir monitors/swaper_monitor.py) et n'est jamais captur\u00e9e ici."
    )
    body = "\n".join(body_lines)

    msg = MIMEMultipart()
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    attachment_text = json.dumps(captured_api_calls, indent=2, ensure_ascii=False, default=str)
    attachment = MIMEText(attachment_text, "plain", "utf-8")
    attachment.add_header(
        "Content-Disposition", "attachment", filename="swaper_investment_api_calls.json"
    )
    msg.attach(attachment)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())
        log.info("Swaper investment summary email sent to %s.", EMAIL_TO)
    except Exception:
        log.exception("Failed to send Swaper investment summary email.")


def send_swaper_api_structure_email(captured_api_calls: list) -> None:
    """One-time-ever diagnostics email (added 2026-07-26, explicit user
    request: "j'ai pas besoin d'attendre d'avoir des sous sur mon compte
    pour te donner tout ce dont tu auras besoin") - sends the loans-
    listing/per-originator-filter `/rest/` API call structure captured by
    monitors/swaper_monitor.py's `_record_api_response()` EVEN WHEN the
    account balance is below MIN_INVESTMENT_AMOUNT and no real investment
    was attempted this run, so the pure-HTTP-migration groundwork doesn't
    have to wait for money to be on the account. Same redaction rules as
    `send_swaper_investment_summary_email()` (see `_redact_sensitive_
    headers()`): header NAMES are kept, sensitive values (cookies/auth/csrf)
    are not. Does NOT include the real invest-call structure - that one
    genuinely requires a real investment to happen (real money moving) to
    ever be observed, per the 2026-07-25 decision to drop click-and-abort
    captures; once that happens, `send_swaper_investment_summary_email()`
    takes over (see monitors/swaper_monitor.py's DEFAULT_STATE comment -
    this email is only ever sent once, and skipped entirely if a real
    investment summary email has already been sent instead).
    """
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_TO]):
        log.error(
            "SMTP configuration is incomplete; cannot send email. "
            "Required env vars: SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_TO."
        )
        return

    subject = "[Swaper] Structure des appels API (listing/filtre pr\u00eats) - aucun argent engag\u00e9"
    body = (
        "Aucun investissement r\u00e9el n'a encore \u00e9t\u00e9 tent\u00e9 (solde probablement < "
        "10 \u20ac, ou aucun pr\u00eat disponible pour l'instant) - voici quand m\u00eame, sans "
        "attendre, la structure des appels API observ\u00e9s pour lister/filtrer les pr\u00eats "
        "par lender (m\u00e9thode/URL/toutes les en-t\u00eates - valeurs sensibles redacted - "
        "corps/statut), en pi\u00e8ce jointe JSON.\n\n"
        "La structure de l'appel d'investissement r\u00e9el (clic sur le '+') n'y est pas "
        "encore - elle ne peut \u00eatre observ\u00e9e que lors d'un vrai investissement (argent "
        "r\u00e9el). Un mail de r\u00e9sum\u00e9 d'investissement plus complet sera envoy\u00e9 "
        "automatiquement d\u00e8s que \u00e7a arrivera, et remplacera celui-ci.\n\n"
        "Objectif : accumuler de quoi tenter, plus tard, de reproduire ces appels en pur "
        "HTTP (sans navigateur) - la connexion/2FA reste elle bas\u00e9e sur le navigateur "
        "(voir monitors/swaper_monitor.py) et n'est jamais captur\u00e9e ici."
    )

    msg = MIMEMultipart()
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    attachment_text = json.dumps(captured_api_calls, indent=2, ensure_ascii=False, default=str)
    attachment = MIMEText(attachment_text, "plain", "utf-8")
    attachment.add_header(
        "Content-Disposition", "attachment", filename="swaper_api_structure.json"
    )
    msg.attach(attachment)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())
        log.info("Swaper API-structure diagnostics email sent to %s.", EMAIL_TO)
    except Exception:
        log.exception("Failed to send Swaper API-structure diagnostics email.")


def _format_lendermarket_lender(lender_stats: dict) -> str:
    """Format one lender's aggregate: loan count, total investable amount,
    and yield (min-max interest rate range) - no extra fields and no URL."""
    lender = lender_stats["lender"]
    count = lender_stats["count"]
    total_amount = lender_stats["total_amount"]
    min_rate = lender_stats["min_rate"]
    max_rate = lender_stats["max_rate"]

    if min_rate is None:
        rate_str = "n/a"
    elif min_rate == max_rate:
        rate_str = f"{min_rate:.2f}%"
    else:
        rate_str = f"{min_rate:.2f}%–{max_rate:.2f}%"

    return f"{lender} : {count} prêt(s), montant total {total_amount:.2f} € | rendement {rate_str}"


def send_lendermarket_email(balance: float | None, segments: dict) -> None:
    """Notify about newly available Lendermarket loans.

    `segments` maps a segment key to {"label", "lenders"}, where "lenders"
    is the per-lender aggregate list built by
    lendermarket_monitor.aggregate_by_lender() (loan count, total
    investable amount, min/max interest rate) for every loan currently
    active in that segment - not just the new one(s) that triggered the
    notification.
    """
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_TO]):
        log.error(
            "SMTP configuration is incomplete; cannot send email. "
            "Required env vars: SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_TO."
        )
        return

    total_loans = sum(sum(l["count"] for l in s["lenders"]) for s in segments.values())
    labels = ", ".join(s["label"] for s in segments.values())
    subject = f"[Lendermarket] {total_loans} prêt(s) disponible(s) ({labels})"

    body_parts = []
    balance_str = f"{balance:.2f} €" if balance is not None else "indisponible"
    body_parts.append(f"Solde disponible : {balance_str}")
    body_parts.append("")

    for segment in segments.values():
        segment_count = sum(l["count"] for l in segment["lenders"])
        body_parts.append(f"{segment['label']} ({segment_count} prêt(s))")
        body_parts.extend(f"  - {_format_lendermarket_lender(lender)}" for lender in segment["lenders"])
        body_parts.append("")
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
        log.info("Lendermarket notification email sent to %s.", EMAIL_TO)
    except Exception:
        log.exception("Failed to send Lendermarket notification email.")



def send_lendermarket_invest_summary_email(stats: dict, error: str | None = None, diagnostics_text: str | None = None) -> None:
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
    (per-lender dict with `budget`, `loans_seen`, `attempts`, `successes`,
    `failures`, `invested_amount`, `invested_loans`).
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
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_TO]):
        log.error(
            "SMTP configuration is incomplete; cannot send email. "
            "Required env vars: SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_TO."
        )
        return

    status = "ÉCHEC" if error else "OK"
    subject = f"[Lendermarket Invest Bot] {status} - {stats.get('invest_successes', 0)} investissement(s) réussi(s)"

    balance_before = stats.get("balance_before")
    balance_after = stats.get("balance_after")
    body_parts = [f"Statut : {status}" + (f" ({error})" if error else "")]
    if balance_before is not None:
        body_parts.append(f"Solde avant : {balance_before:.2f} €")
    if balance_after is not None:
        body_parts.append(f"Solde après : {balance_after:.2f} €")
    body_parts += [
        f"Tentatives d'investissement : {stats.get('invest_attempts', 0)}",
        f"  - réussies : {stats.get('invest_successes', 0)}",
        f"  - échouées : {stats.get('invest_failures', 0)}",
        f"Montant total investi : {stats.get('total_invested', 0.0):.2f} €",
    ]

    lender_stats = stats.get("lender_stats") or {}
    if lender_stats:
        body_parts.append("")
        body_parts.append("=== Détail par lender ===")
        for name, s in lender_stats.items():
            body_parts.append("")
            body_parts.append(f"- {name}")
            body_parts.append(
                f"    Budget (part du solde) : {s.get('budget', 0.0):.2f} € | "
                f"investi : {s.get('invested_amount', 0.0):.2f} €"
            )
            body_parts.append(
                f"    Prêts disponibles vus : {s.get('loans_seen', 0)} | "
                f"tentatives : {s.get('attempts', 0)} "
                f"(réussies : {s.get('successes', 0)}, échouées : {s.get('failures', 0)})"
            )
            invested_loans = s.get("invested_loans") or []
            if invested_loans:
                details = ", ".join(f"{lo['loanUuid']} ({lo['amount']:.2f} €)" for lo in invested_loans)
                body_parts.append(f"    Prêts investis : {details}")
            else:
                body_parts.append("    Prêts investis : aucun")

    if stats.get("invest_failures", 0) > 0 or error:
        body_parts.append("")
        body_parts.append(
            "Le détail complet (requête/réponse) de chaque échec, et de toute "
            "erreur inattendue, est joint à cet email."
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
            "Content-Disposition", "attachment", filename="lendermarket_invest_bot_diagnostics_this_run.log"
        )
        msg.attach(attachment)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())
        log.info("Lendermarket invest bot summary email sent to %s.", EMAIL_TO)
    except Exception:
        log.exception("Failed to send Lendermarket invest bot summary email.")


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
    `final_available_money`, `selected_originators`, `originator_budgets`
    (initial per-originator budget), `final_originator_budgets`,
    `originator_stats` (per-originator dict with `loans_seen`, `attempts`,
    `successes`, `failures`, `invested_amount`, `invested_loans`),
    `redistributions` (stuck-budget reallocations that happened mid-run),
    `raw_originators_seen` (every distinct raw `loanOriginator` value
    PeerBerry returned this run, matched or not - lets a mismatch between
    the Sheet selection and PeerBerry's real values be spotted directly
    from the email).
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

    raw_originators_seen = stats.get("raw_originators_seen") or []
    if raw_originators_seen:
        body_parts.append("")
        body_parts.append("Loan originators bruts vus (renvoyés par l'API, matchés ou non) :")
        body_parts.append("  " + ", ".join(raw_originators_seen))

    redistributions = stats.get("redistributions") or []
    if redistributions:
        body_parts.append("")
        body_parts.append(f"Reliquats redistribués en cours de run ({len(redistributions)}) :")
        for r in redistributions:
            body_parts.append(f"  - {r['amount']:.2f} € : '{r['from']}' -> '{r['to']}'")

    originator_stats = stats.get("originator_stats") or {}
    if originator_stats:
        initial_budgets = stats.get("originator_budgets") or {}
        final_budgets = stats.get("final_originator_budgets") or {}
        body_parts.append("")
        body_parts.append("=== Détail par loan originator ===")
        for name, s in originator_stats.items():
            body_parts.append("")
            body_parts.append(f"- {name}")
            body_parts.append(
                f"    Budget initial : {initial_budgets.get(name, 0.0):.2f} € | "
                f"restant : {final_budgets.get(name, 0.0):.2f} € | "
                f"investi : {s.get('invested_amount', 0.0):.2f} €"
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
