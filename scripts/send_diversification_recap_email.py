"""Sends one recap email listing every platform's "non investi" (uninvested
cash) amount at the very end of the diversification GitHub Actions
workflow (see .github/workflows/diversification.yml's final "recap" job,
added 2026-08-10 per explicit user request: "à la fin du workflow
diversification avec github j'aimerais recevoir un mail récapitulatif des
montants non investi des plateformes du workflow").

Re-reads the Sheet's "Répartition géographique" section (rather than
threading each platform's already-fetched balance across separate GitHub
Actions jobs, which don't share Python state) via
shared.google_sheet.get_geographic_repartition_uninvested_amounts() - one
extra Sheet read at the end of the run, after every platform job has
already written its own "non investi" row.

Only lists the platforms actually part of this workflow: Mintos and Lande
are manual-login-only (run separately via run_manual_platform.ps1), and
Monefit/Go & Grow have no "non investi" row at all (their whole balance is
already "invested" - see fill_geographic_repartition_uninvested_amount()'s
docstring in shared/google_sheet.py).

Required env vars: GOOGLE_SHEET_ID, GOOGLE_CREDENTIALS, SMTP_HOST,
SMTP_USER, SMTP_PASSWORD, EMAIL_TO (SMTP_PORT/EMAIL_FROM optional, see
shared/notifier.py).
"""

import json
import logging
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from shared.google_sheet import get_geographic_repartition_uninvested_amounts
from shared.notifier import send_diversification_recap_email

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("send_diversification_recap_email")

# Amounts of the last recap email actually sent, persisted across workflow
# runs via actions/cache (see diversification.yml's recap_email job).
STATE_PATH = Path(__file__).resolve().parent.parent / "diversification" / "uninvested_recap_state.json"

WORKFLOW_PLATFORMS = [
    "Swaper", "Iuvo", "Afranga", "Bienprêter", "Lendermarket",
    "Loanch", "Peerberry", "Bricks", "Nectaro", "Debitum", "Income Marketplace",
]


def _load_previous_amounts():
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return {p: round(float(a), 2) for p, a in data.items()}
    except FileNotFoundError:
        return None
    except Exception:
        log.warning("Unreadable recap state file, treating as first run.", exc_info=True)
        return None


def run():
    try:
        amounts = get_geographic_repartition_uninvested_amounts(WORKFLOW_PLATFORMS)
    except Exception as exc:
        log.exception("Failed to read uninvested amounts from the Sheet.")
        send_diversification_recap_email({}, error=str(exc))
        return

    missing = [p for p in WORKFLOW_PLATFORMS if p not in amounts]
    if missing:
        log.warning("No 'non investi' amount found for: %s", ", ".join(missing))

    current = {p: round(a, 2) for p, a in amounts.items()}
    previous = _load_previous_amounts()
    if previous is not None and previous == current:
        log.info("No uninvested amount changed since the last recap email; not sending.")
        return

    if send_diversification_recap_email(amounts, missing_platforms=missing):
        STATE_PATH.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    run()
