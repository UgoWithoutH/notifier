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
    loan_id = loan.get("id")
    number = loan.get("number")
    amount = loan.get("amount")
    interest = loan.get("interestRatePerYear")
    status = loan.get("status")
    term = loan.get("term") or {}
    term_str = f"{term.get('value')} {term.get('unit')}" if term else None

    parts = [f"Loan #{loan_id}"]
    if number:
        parts.append(number)
    if amount is not None:
        parts.append(f"amount: {amount}")
    if interest is not None:
        parts.append(f"interest: {interest}%")
    if term_str:
        parts.append(f"term: {term_str}")
    if status:
        parts.append(f"status: {status}")
    return " | ".join(parts)


def send_swaper_email(balance: float, loans: list) -> None:
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_TO]):
        log.error(
            "SMTP configuration is incomplete; cannot send email. "
            "Required env vars: SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_TO."
        )
        return

    subject = f"[Swaper] {balance:.2f}€ dispo + {len(loans)} prêt(s) manuel(s) disponible(s)"
    header = f"Solde non investi : {balance:.2f} €\n\n"
    body = header + "\n".join(_format_loan(loan) for loan in loans)

    msg = MIMEMultipart()
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())
        log.info("Notification email sent to %s.", EMAIL_TO)
    except Exception:
        log.exception("Failed to send notification email.")


def _format_lendermarket_lender(lender_stats: dict) -> str:
    lender = lender_stats["lender"]
    count = lender_stats["count"]
    total_amount = lender_stats["total_amount"]
    min_rate = lender_stats["min_rate"]
    max_rate = lender_stats["max_rate"]

    if min_rate is None:
        rate_str = "taux: n/a"
    elif min_rate == max_rate:
        rate_str = f"taux: {min_rate:.2f}%"
    else:
        rate_str = f"taux: {min_rate:.2f}%–{max_rate:.2f}%"

    return f"{lender} : {count} prêt(s), montant total {total_amount:.2f} € | {rate_str}"


def send_lendermarket_email(balance: float | None, segments: dict) -> None:
    """Notify about newly available Lendermarket loans.

    `segments` maps a segment key to {"label", "page_url", "lenders"}, where
    "lenders" is the per-lender aggregate list built by
    lendermarket_monitor.aggregate_by_lender() (count, total investable
    amount, min/max interest rate) for every loan currently active in that
    segment - not just the new one(s) that triggered the notification.
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
        body_parts.append(f"{segment['label']} ({segment_count} prêt(s)) - {segment['page_url']}")
        body_parts.extend(f"  - {_format_lendermarket_lender(lender)}" for lender in segment["lenders"])
        body_parts.append("")
    body = "\n".join(body_parts)

    msg = MIMEMultipart()
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())
        log.info("Lendermarket notification email sent to %s.", EMAIL_TO)
    except Exception:
        log.exception("Failed to send Lendermarket notification email.")
