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


def _format_lendermarket_loan(loan: dict) -> str:
    """Format a single loan as one line: lender, identifier, available
    amount, and yield (interest rate) - no extra fields and no URL."""
    lender_name = (loan.get("lender") or {}).get("displayName") or "Fournisseur inconnu"
    loan_identifier = loan.get("loanPublicId") or loan.get("uuid")

    amount = loan.get("investableAmount") or loan.get("loanAmount")
    try:
        amount_str = f"{float(amount):.2f} €"
    except (TypeError, ValueError):
        amount_str = "montant n/a"

    rate = loan.get("interestRate")
    try:
        rate_str = f"{float(rate):.2f}%"
    except (TypeError, ValueError):
        rate_str = "n/a"

    return f"{lender_name} - Prêt {loan_identifier} : {amount_str} | rendement {rate_str}"


def send_lendermarket_email(balance: float | None, segments: dict) -> None:
    """Notify about newly available Lendermarket loans.

    `segments` maps a segment key to {"label", "loans"}, where "loans" is
    the raw list of loan dicts (from the public getActiveLoans API)
    currently active in that segment - not just the new one(s) that
    triggered the notification. Each loan is listed individually with its
    own available amount and yield, plus a per-segment total amount.
    """
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_TO]):
        log.error(
            "SMTP configuration is incomplete; cannot send email. "
            "Required env vars: SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_TO."
        )
        return

    total_loans = sum(len(s["loans"]) for s in segments.values())
    labels = ", ".join(s["label"] for s in segments.values())
    subject = f"[Lendermarket] {total_loans} prêt(s) disponible(s) ({labels})"

    body_parts = []
    balance_str = f"{balance:.2f} €" if balance is not None else "indisponible"
    body_parts.append(f"Solde disponible : {balance_str}")
    body_parts.append("")

    for segment in segments.values():
        loans = segment["loans"]
        segment_total = 0.0
        for loan in loans:
            amount = loan.get("investableAmount") or loan.get("loanAmount")
            try:
                segment_total += float(amount)
            except (TypeError, ValueError):
                pass

        body_parts.append(f"{segment['label']} ({len(loans)} prêt(s), montant total {segment_total:.2f} €)")
        body_parts.extend(f"  - {_format_lendermarket_loan(loan)}" for loan in loans)
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
