"""Email notification via SMTP."""

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


def send_swaper_invest_exploration_email(loans_count: int, diagnostics_text: str) -> None:
    """Send the Swaper "invest-structure exploration" diagnostics email.

    Sent alongside send_swaper_email() (same run, same "loans became
    available" trigger) whenever swaper_monitor.py's exploration capture
    produced something - see that module's docstring/`capture_invest_
    exploration()` for what's collected: the raw loans-listing API
    response, any other `/rest/` API calls observed while on the loans
    page (method/url/status/truncated body), and a truncated HTML dump of
    the loans page. NO invest/confirm button is ever clicked to gather
    this (same real-money safety boundary as documented in repo memory for
    the PeerBerry exploration) - purely passive capture of an authenticated,
    already-logged-in Playwright session. `diagnostics_text` (a JSON
    string) is attached as a `.json` file; the email body itself never
    contains the raw detail, only a short explanation of what to do with
    the attachment (send it back so the real invest HTTP request/HTML
    structure can be figured out).
    """
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_TO]):
        log.error(
            "SMTP configuration is incomplete; cannot send email. "
            "Required env vars: SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_TO."
        )
        return

    subject = f"[Swaper] Diagnostic structure d'investissement ({loans_count} prêt(s) dispo)"
    body = (
        f"{loans_count} pr\u00eat(s) manuel(s) sont disponibles sur Swaper.\n\n"
        "Le fichier joint contient : la r\u00e9ponse brute de l'API de listing des "
        "pr\u00eats, les autres appels HTTP /rest/ observ\u00e9s pendant la navigation "
        "sur la page, et un extrait du HTML de la page des pr\u00eats. Aucun clic "
        "d'investissement/confirmation n'a \u00e9t\u00e9 effectu\u00e9 (aucun risque "
        "d'argent r\u00e9el) - c'est juste de la capture passive.\n\n"
        "Renvoie ce fichier pour permettre de comprendre la structure HTML/API "
        "n\u00e9cessaire pour investir automatiquement sur Swaper."
    )

    msg = MIMEMultipart()
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    attachment = MIMEText(diagnostics_text, "plain", "utf-8")
    attachment.add_header(
        "Content-Disposition", "attachment", filename="swaper_invest_exploration_diagnostics.json"
    )
    msg.attach(attachment)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())
        log.info("Swaper invest-exploration diagnostics email sent to %s.", EMAIL_TO)
    except Exception:
        log.exception("Failed to send Swaper invest-exploration diagnostics email.")


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
    `redistributions` (stuck-budget reallocations that happened mid-run).
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
