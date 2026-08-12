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


def send_swaper_investment_summary_email(
    attempts: list,
    captured_api_calls: list,
    min_interest_rate: float | None = None,
    country_threshold_percentage: float | None = None,
    country_status: dict | None = None,
    country_blocked: list | None = None,
    originator_cap_status: dict | None = None,
    originator_blocked: list | None = None,
    error: str | None = None,
) -> None:
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

    Each attempt's own `confirm_api_calls` (added 2026-08-01, per explicit
    user request: "je veux absolument r\u00e9cup\u00e9rer la requ\u00eate api pour
    investir par mail je veux des logs d\u00e9taill\u00e9s") - the real /rest/ call(s)
    fired by clicking the modal's "Confirm" button - are rendered DIRECTLY
    in the email BODY (method/url/status + response body, not just buried
    in the JSON attachment), right under that attempt's own line.

    `min_interest_rate`/`country_threshold_percentage`/`country_status`/
    `country_blocked` (added 2026-07-31, mirrors
    `send_lendermarket_invest_summary_email()`'s equivalent sections): the
    minimum interest rate actually used this run (from
    `shared.google_sheet.get_swaper_min_interest_rate()`), the configured
    per-country cap percentage (from `get_swaper_country_allocations()`,
    None if no threshold cell is set), a per-country
    `{invested, threshold_amount, blocked}` breakdown, and the list of loan
    originators excluded this run because their country already hit the
    cap.

    `originator_cap_status`/`originator_blocked` (added 2026-08-05): the
    SAME kind of cap, but per loan originator instead of per country - a
    percentage of the total budget a single loan originator should never
    exceed, from `shared.google_sheet.get_swaper_originator_caps()`.

    `error` (added 2026-08-01, explicit user request: "si y'a une erreur ou
    autre il faut arrêter le bot et à la fin du run quoi qu'il arrive
    envoyer le mail") - when set, this email is sent EVEN IF `attempts` is
    empty (monitors/swaper_monitor.py's `run()` calls this whenever
    `attempts or error`), with the error message shown prominently at the
    top of the body and in the subject, so a failed run is never silent.
    """
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_TO]):
        log.error(
            "SMTP configuration is incomplete; cannot send email. "
            "Required env vars: SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_TO."
        )
        return

    subject = f"[Swaper] {len(attempts)} investissement(s) r\u00e9el(s) tent\u00e9(s)"
    if error:
        subject += " - ERREUR"
    body_lines = []
    if error:
        body_lines.append(f"\u26a0 ERREUR pendant ce run - le bot s'est arr\u00eat\u00e9 : {error}")
        body_lines.append("")
    body_lines += [
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
            line += " -- ERREUR pendant la requ\u00eate HTTP, voir les logs"
        elif attempt.get("not_approved"):
            line += " -- Swaper indique que l'investissement manuel n'est pas approuv\u00e9 pour ce pr\u00eat, investissement stopp\u00e9"
        elif attempt.get("confirmed"):
            line += " -- investissement confirm\u00e9 (requ\u00eate d'achat r\u00e9ussie)"
        else:
            line += " -- non confirm\u00e9, voir la pi\u00e8ce jointe"
        body_lines.append(line)
        for call in attempt.get("confirm_api_calls") or []:
            body_lines.append(
                f"    -> Requ\u00eate API d'investissement : {call.get('method')} {call.get('url')} "
                f"-> HTTP {call.get('status')}"
            )
            body_lines.append(f"       Corps r\u00e9ponse : {(call.get('body') or '')[:1000]}")
    body_lines.append("")

    if min_interest_rate is not None:
        body_lines.append(f"Taux d'int\u00e9r\u00eat minimum utilis\u00e9 : {min_interest_rate}%")

    if country_status:
        if country_threshold_percentage is not None:
            body_lines.append(f"=== Seuil par pays ({country_threshold_percentage}% du budget total) ===")
        else:
            body_lines.append("=== Seuil par pays (aucun seuil configur\u00e9) ===")
        for country, s in sorted(country_status.items()):
            threshold_amount = s.get("threshold_amount")
            line = f"- {country} : investi {s.get('invested', 0.0):.2f} \u20ac"
            if threshold_amount is not None:
                line += f" / plafond {threshold_amount:.2f} \u20ac"
            if s.get("blocked"):
                line += " (BLOQU\u00c9)"
            body_lines.append(line)
        body_lines.append("")

    if country_blocked:
        body_lines.append(
            "Loan originators bloqu\u00e9s ce run (seuil d'investissement par pays atteint) : "
            + ", ".join(country_blocked)
        )
        body_lines.append("")
    if originator_cap_status:
        body_lines.append("=== Plafond par loan originator (% du budget total) ===")
        for name, s in sorted(originator_cap_status.items()):
            threshold_amount = s.get("threshold_amount")
            line = f"- {name} : investi {s.get('invested', 0.0):.2f} \u20ac / plafond {s.get('max_percentage', 0.0):.2f}%"
            if threshold_amount is not None:
                line += f" ({threshold_amount:.2f} \u20ac)"
            if s.get("blocked"):
                line += " (BLOQU\u00c9)"
            body_lines.append(line)
        body_lines.append("")

    if originator_blocked:
        body_lines.append(
            "Loan originators bloqués ce run (plafond par loan originator atteint) : "
            + ", ".join(originator_blocked)
        )
        body_lines.append("")
    body_lines.append(
        "Le fichier joint contient les vraies requ\u00eates/r\u00e9ponses HTTP /rest/ observ\u00e9es "
        "pendant ce run (m\u00e9thode/URL/toutes les en-t\u00eates - valeurs sensibles redacted - "
        "corps/statut), pour les appels de listing/filtre de pr\u00eats, de v\u00e9rification "
        "d'approbation ET d'achat. V\u00e9rifie le statut de la requ\u00eate d'achat pour confirmer "
        "qu'elle a bien r\u00e9ussi. Depuis le 2026-08-01, tout ce flux (apr\u00e8s connexion) est "
        "fait en pur HTTP (sans navigateur) - seule la connexion/2FA reste bas\u00e9e sur le "
        "navigateur (voir monitors/swaper_monitor.py) et n'est jamais captur\u00e9e ici."
    )
    body = "\n".join(body_lines)

    msg = MIMEMultipart()
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    # `attempts` (see _invest_available_loans()'s docstring for its shape:
    # loan_id/loan_number/amount/confirmed/not_approved/error/
    # confirm_api_calls) is included alongside captured_api_calls so the
    # full per-attempt detail is in the attachment, not just living in the
    # in-memory `attempts` list.
    attachment_payload = {"attempts": attempts, "captured_api_calls": captured_api_calls}
    attachment_text = json.dumps(attachment_payload, indent=2, ensure_ascii=False, default=str)
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

    min_interest_rate = stats.get("min_interest_rate")
    if min_interest_rate is not None:
        body_parts.append(f"Taux d'intérêt minimum utilisé : {min_interest_rate}%")

    lender_stats = stats.get("lender_stats") or {}
    invested_lenders = {
        name: s for name, s in lender_stats.items() if s.get("successes", 0) > 0
    }
    body_parts.append("")
    body_parts.append("=== Investissements (taux et lender) ===")
    if invested_lenders:
        for name, s in sorted(invested_lenders.items()):
            country = s.get("country")
            label = f"{name} ({country})" if country else name
            rates = [
                lo.get("interestRate")
                for lo in (s.get("invested_loans") or [])
                if lo.get("interestRate") is not None
            ]
            rate_str = ", ".join(f"{rate:.2f}%" for rate in rates) if rates else "n/a"
            body_parts.append(
                f"- {label} : {s.get('invested_amount', 0.0):.2f} € @ {rate_str}"
            )
    else:
        body_parts.append("Aucun investissement réussi ce run.")

    country_blocked = stats.get("country_blocked") or []
    if country_blocked:
        body_parts.append("")
        body_parts.append(
            "Lenders bloqués ce run (seuil d'investissement par pays atteint) : "
            + ", ".join(country_blocked)
        )

    originator_blocked = stats.get("originator_blocked") or []
    if originator_blocked:
        body_parts.append("")
        body_parts.append(
            "Lenders bloqués ce run (plafond par lender atteint) : "
            + ", ".join(originator_blocked)
        )

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


def send_diversification_recap_email(amounts: dict, missing_platforms: list | None = None, error: str | None = None) -> None:
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
    """
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_TO]):
        log.error(
            "SMTP configuration is incomplete; cannot send email. "
            "Required env vars: SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_TO."
        )
        return

    total = sum(amounts.values())
    subject = f"[Diversification] Récapitulatif montants non investis - {total:.2f} €"
    if error:
        subject += " - ERREUR"

    body_lines = []
    if error:
        body_lines.append(f"⚠ ERREUR pendant la lecture du récapitulatif : {error}")
        body_lines.append("")
    body_lines.append("Montants non investis (disponibles, pas encore prêtés) par plateforme :")
    body_lines.append("")
    for platform, amount in sorted(amounts.items()):
        body_lines.append(f"- {platform:<15s} : {amount:.2f} €")
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
    except Exception:
        log.exception("Failed to send diversification recap email.")
