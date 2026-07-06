"""Email notification via SMTP."""

import os
import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

log = logging.getLogger("swaper_monitor")

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


def send_email(balance: float, loans: list) -> None:
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
