"""Write the CGI PEE contributions (own payment + employer match) into the 'dashboard 2026' tab.

Reads portfolio_imports/esalia/transactions.json (see `esalia_fetch --transactions`).
Months of the sheet year fill the monthly columns; earlier contributions are added as a constant
to the "depuis le début" rows, since the tab has no earlier columns.
The employer match goes in the negative-fees row so the block cost equals what you paid yourself.
"""

from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path

from gspread.utils import rowcol_to_a1

LEDGER = Path(__file__).resolve().parents[1] / "portfolio_imports" / "esalia" / "transactions.json"
WORKSHEET = "dashboard 2026"
YEAR = 2026
MONTHS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]
SECTION = "PEE"
BLOCK = "CGI"
FUND_MARKER = "CGI"


def load_ledger(path: Path) -> tuple[dict, tuple[str, float]]:
    """({'YYYY-MM': {parts, own, employer}}, (date, latest unit value)) from the ledger."""
    ledger = json.loads(path.read_text(encoding="utf-8"))
    months: dict = collections.defaultdict(lambda: {"parts": 0.0, "own": 0.0, "employer": 0.0})
    latest = ("", 0.0)
    for transaction in ledger.values():
        if transaction.get("statut") != "COMPTABILISEE":
            continue
        movements = [
            m
            for m in transaction["_detail"]["_embedded"]["mouvements"]
            if m["sens"] == "SOUSCRIPTION"
            and FUND_MARKER in m["_embedded"]["support_investissement"]["titre"].upper()
        ]
        if not movements:
            continue
        target = (transaction.get("abondement_net") or {}).get("montant", 0.0)
        amount = lambda m: m["echeance"]["montant"]["montant"]  # noqa: E731
        employer = min(movements, key=lambda m: abs(amount(m) - target))
        bucket = months[transaction["date_comptabilisation"][:7]]
        for movement in movements:
            bucket["parts"] += movement["echeance"]["nb_parts"]
            bucket["employer" if movement is employer else "own"] += amount(movement)
            vl = movement["_embedded"]["support_investissement"]["valeur_liquidative"]
            latest = max(latest, (vl["date_valeur"][:10], vl["valeur_part"]["montant"]))
    return months, latest


def build_updates(grid: list[list[str]], months: dict, latest: tuple[str, float], comma: bool) -> tuple[list, list]:
    def label(row: int) -> str:
        values = grid[row - 1] if row - 1 < len(grid) else []
        return values[1].strip() if len(values) > 1 else ""

    def find(text: str, start: int, end: int) -> int | None:
        return next((r for r in range(start, end + 1) if label(r) == text), None)

    section = find(SECTION, 1, len(grid))
    header = find(BLOCK, section, len(grid)) if section else None
    if not header:
        raise SystemExit(f"Bloc '{BLOCK}' introuvable sous '{SECTION}'.")
    stop = min(header + 20, len(grid))
    rows = {
        name: find(name, header + 1, stop)
        for name in ("nombre", "valo unitaire", "frais / abondement (négatif)", "valo totale", "Rendements % 🎯",
                     "montant € depuis le début", "nombre depuis le début")
    }
    if None in rows.values():
        raise SystemExit(f"Bloc '{BLOCK}' : lignes introuvables {[k for k, v in rows.items() if v is None]}.")
    month_cols = {value.strip().lower(): index + 1 for index, value in enumerate(grid[section - 1]) if value.strip()}

    def literal(value: float) -> str:
        text = f"{value:.4f}"
        return text.replace(".", ",") if comma else text

    def is_empty(row: int, column: int) -> bool:
        values = grid[row - 1] if row - 1 < len(grid) else []
        return column - 1 >= len(values) or values[column - 1].strip() == ""

    updates, notes = [(rows["valo unitaire"], 3, latest[1], f"VL actuelle ({latest[0]})")], []
    earlier = {"parts": 0.0, "own": 0.0}
    for month, data in sorted(months.items()):
        year, number = int(month[:4]), int(month[5:])
        if year < YEAR:
            earlier["parts"] += data["parts"]
            earlier["own"] += data["own"]
            continue
        column = month_cols.get(f"{MONTHS[number - 1]} {str(year)[-2:]}")
        if year != YEAR or column is None:
            notes.append(f"{month} : colonne absente de l'onglet, ignoré.")
            continue
        letter = rowcol_to_a1(1, column)[:-1]
        updates += [
            (rows["nombre"], column, round(data["parts"], 4), f"{month} : nombre"),
            (rows["valo unitaire"], column, round((data["own"] + data["employer"]) / data["parts"], 4), f"{month} : VL"),
            (rows["frais / abondement (négatif)"], column, -round(data["employer"], 2), f"{month} : abondement"),
        ]
        if is_empty(rows["valo totale"], column):
            updates.append((rows["valo totale"], column, f"={letter}{rows['nombre']}*$C${rows['valo unitaire']}", f"{month} : valo totale"))
        if is_empty(rows["Rendements % 🎯"], column):
            updates.append(
                (rows["Rendements % 🎯"], column,
                 f"=IFERROR(({letter}{rows['valo totale']}-{letter}{header})/{letter}{header};0)", f"{month} : rendement")
            )
    updates += [
        (rows["montant € depuis le début"], 3, f"=C{header}+{literal(earlier['own'])}", f"avant {YEAR} : versements perso"),
        (rows["nombre depuis le début"], 3, f"=C{rows['nombre']}+{literal(earlier['parts'])}", f"avant {YEAR} : parts"),
    ]
    return updates, notes


def main() -> None:
    parser = argparse.ArgumentParser(description="Remplit le bloc PEE CGI du Sheet depuis le ledger Esalia.")
    parser.add_argument("--dry-run", action="store_true", help="Affiche sans écrire dans le Sheet.")
    parser.add_argument("--ledger", type=Path, default=LEDGER)
    args = parser.parse_args()

    from shared.google_sheet import _call_with_retry, get_worksheet_by_name

    worksheet = get_worksheet_by_name(WORKSHEET)
    grid = _call_with_retry(worksheet.get_all_values)
    months, latest = load_ledger(args.ledger)
    comma = not str(getattr(worksheet.spreadsheet, "locale", "en_US")).startswith("en")
    updates, notes = build_updates(grid, months, latest, comma)
    for row, column, value, what in updates:
        print(f"{rowcol_to_a1(row, column).ljust(6)} <- {value}   ({what})")
    for note in notes:
        print("NOTE :", note)
    if args.dry_run:
        print(f"{len(updates)} cellule(s) à écrire (simulation).")
        return
    _call_with_retry(
        worksheet.batch_update,
        [{"range": rowcol_to_a1(r, c), "values": [[v]]} for r, c, v, _ in updates],
        value_input_option="USER_ENTERED",
    )
    print(f"{len(updates)} cellule(s) écrite(s) dans '{WORKSHEET}'.")


if __name__ == "__main__":
    main()
