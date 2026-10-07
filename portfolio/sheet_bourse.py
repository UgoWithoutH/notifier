"""Write Trade Republic purchases and prices into the Bourse blocks of the 'dashboard 2026' tab.

Reads portfolio_imports/trade_republic/transactions.json (history) and the latest
trade_republic_*.csv (positions). Only months that have trades are written, so reruns are idempotent.
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import re
from datetime import date
from pathlib import Path

from gspread.utils import rowcol_to_a1

BASE = Path(__file__).resolve().parents[1] / "portfolio_imports" / "trade_republic"
WORKSHEET = "dashboard 2026"
YEAR = 2026
MONTHS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]

# ISIN -> (section, block title in column B of the Sheet)
BLOCKS = {
    "IE00B4ND3602": ("CTO", "Physical Gold"),
    "IE000U58J0M1": ("CTO", "Global Clean Energy"),
    "IE0007Y8Y157": ("CTO", "Quantum Computing"),
    "FR0013412020": ("PEA", "MSCI Emerging Markets ESG"),
    "FR0011871110": ("PEA", "NASDAQ 100"),
    "IE0002XZSHO1": ("PEA", "MSCI World"),
}


def _num(text: str) -> float:
    return float(text.replace("\xa0", "").replace(" ", "").replace("€", "").replace(",", "."))


def load_trades(ledger_path: Path) -> dict:
    """{(isin, 'YYYY-MM', side): [shares, amount, fees]} from the ledger."""
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    trades: dict = collections.defaultdict(lambda: [0.0, 0.0, 0.0])
    for entry in ledger.values():
        item = entry["item"]
        if item.get("eventType") not in ("TRADING_TRADE_EXECUTED", "TRADING_SAVINGSPLAN_EXECUTED"):
            continue
        match = re.search(r"logos/([A-Z]{2}[A-Z0-9]{10})/", str(item.get("icon")))
        summary = next(
            (s for s in (entry.get("detail") or {}).get("sections", []) if s.get("title") == "Synthèse"), None
        )
        if not match or not summary:
            print(f"Transaction ignorée (format inattendu) : {item.get('title')} {item.get('timestamp', '')[:10]}")
            continue
        info = {d["title"]: (d.get("detail") or {}).get("text") for d in summary["data"]}
        shares_text, price_text = info["Transaction"].split("×")
        shares, price = _num(shares_text), _num(price_text)
        fees = 0.0 if info.get("Frais") in (None, "Gratuit") else _num(info["Frais"])
        side = "sell" if "vente" in str(item.get("subtitle", "")).lower() else "buy"
        bucket = trades[(match.group(1), item["timestamp"][:7], side)]
        bucket[0] += shares
        bucket[1] += shares * price
        bucket[2] += fees
    return trades


def load_positions(directory: Path) -> dict:
    """{isin: (quantity, price)} from the latest positions CSV."""
    latest = sorted(directory.glob("trade_republic_*.csv"))[-1]
    with latest.open(encoding="utf-8-sig") as handle:
        return {r["ISIN"]: (float(r["Quantité"]), float(r["Cours"])) for r in csv.DictReader(handle, delimiter=";")}


def load_initial(path: Path, trades: dict, positions: dict, create: bool) -> dict:
    """Shares held but absent from the history (transfers), booked once on the month of the first run.

    The file is editable: set the real month, price and shares, rerun, and the Sheet follows.
    """
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    initial = {}
    for isin, (held, price) in positions.items():
        explained = sum(
            (shares if side == "buy" else -shares)
            for (t_isin, _month, side), (shares, _amount, _fees) in trades.items()
            if t_isin == isin
        )
        if held - explained > 1e-6:
            initial[isin] = {"month": f"{date.today():%Y-%m}", "shares": round(held - explained, 6), "price": price}
    if create and initial:
        path.write_text(json.dumps(initial, ensure_ascii=False, indent=1), encoding="utf-8")
    return initial


def apply_initial(trades: dict, initial: dict) -> None:
    for isin, entry in initial.items():
        bucket = trades[(isin, entry["month"], "buy")]
        bucket[0] += entry["shares"]
        bucket[1] += entry["shares"] * entry["price"]


def build_updates(grid: list[list[str]], trades: dict, positions: dict, blocks: dict = BLOCKS) -> tuple[list, list]:
    def label(row: int) -> str:
        values = grid[row - 1] if row - 1 < len(grid) else []
        return values[1].strip() if len(values) > 1 else ""

    def find(text: str, start: int, end: int) -> int | None:
        return next((r for r in range(start, end + 1) if label(r) == text), None)

    bourse = find("Bourse", 1, len(grid))
    pea = find("PEA", bourse, len(grid))
    cto = find("CTO", pea, len(grid))
    end = find("Total achat", cto, len(grid))
    if not all((bourse, pea, cto, end)):
        raise SystemExit("Structure Bourse introuvable (Bourse / PEA / CTO / Total achat).")
    sections = {"PEA": (pea + 1, cto - 1), "CTO": (cto + 1, end - 1)}

    def other_section(title: str) -> tuple[int, int] | None:
        """Rows of a section whose title sits in column B and whose end is the next icon row in column A."""
        first = find(title, 1, len(grid))
        if not first:
            return None
        next_header = next(
            (r for r in range(first + 1, len(grid) + 1) if grid[r - 1] and grid[r - 1][0].strip()), len(grid) + 1
        )
        return first + 1, next_header - 1

    month_cols = {
        value.strip().lower(): index + 1 for index, value in enumerate(grid[bourse - 1]) if value.strip()
    }
    updates, notes = [], []
    for isin, (section, title) in blocks.items():
        if section not in sections:
            found = other_section(section)
            if found is None:
                continue
            sections[section] = found
        start, stop = sections[section]
        header = find(title, start, stop)
        if header is None:
            notes.append(f"Bloc '{title}' ({section}) introuvable dans le Sheet : ignoré.")
            continue
        rows = {}
        for wanted in ("nombre", "valo unitaire", "frais", "nombre vente", "valo unitaire vente"):
            rows[wanted] = find(wanted, header + 1, min(header + 20, stop))
        if None in rows.values():
            notes.append(f"Bloc '{title}' : lignes de saisie introuvables.")
            continue
        valuation_row = find("valo totale", header + 1, header + 4)
        return_row = find("Rendements % 🎯", header + 1, min(header + 20, stop))

        def is_empty(row: int, column: int) -> bool:
            values = grid[row - 1] if row - 1 < len(grid) else []
            return column - 1 >= len(values) or values[column - 1].strip() == ""

        if positions.get(isin, (0, None))[1] is not None:
            updates.append((rows["valo unitaire"], 3, positions[isin][1], f"{title} : cours actuel"))
        explained = 0.0
        for (t_isin, month, side), (shares, amount, fees) in sorted(trades.items()):
            if t_isin != isin:
                continue
            explained += shares if side == "buy" else -shares
            year, month_number = int(month[:4]), int(month[5:])
            column = month_cols.get(f"{MONTHS[month_number - 1]} {str(year)[-2:]}")
            if year != YEAR or column is None:
                notes.append(f"{title} {month} : colonne absente de l'onglet {WORKSHEET}, ignoré.")
                continue
            price = round(amount / shares, 4)
            if side == "buy":
                updates += [
                    (rows["nombre"], column, round(shares, 6), f"{title} {month} : nombre"),
                    (rows["valo unitaire"], column, price, f"{title} {month} : prix"),
                    (rows["frais"], column, round(fees, 2), f"{title} {month} : frais"),
                ]
                letter = rowcol_to_a1(1, column)[:-1]
                # Monthly return compares that month's purchase valued at today's price with its cost.
                if valuation_row and is_empty(valuation_row, column):
                    updates.append(
                        (valuation_row, column, f"={letter}{rows['nombre']}*$C${rows['valo unitaire']}", f"{title} {month} : valo totale")
                    )
                if return_row and valuation_row and is_empty(return_row, column):
                    updates.append(
                        (return_row, column, f"=IFERROR(({letter}{valuation_row}-{letter}{header})/{letter}{header};0)", f"{title} {month} : rendement")
                    )
            else:
                updates += [
                    (rows["nombre vente"], column, round(shares, 6), f"{title} {month} : nombre vente"),
                    (rows["valo unitaire vente"], column, price, f"{title} {month} : prix vente"),
                ]
        held = positions.get(isin, (0.0, 0.0))[0]
        if abs(held - explained) > 1e-6:
            notes.append(f"{title} : {held - explained:g} part(s) détenue(s) sans achat dans l'historique (transfert ?).")
    return updates, notes


def report_and_write(worksheet, updates: list, notes: list, dry_run: bool) -> None:
    from shared.google_sheet import _call_with_retry

    for row, column, value, what in updates:
        print(f"{rowcol_to_a1(row, column).ljust(6)} <- {value}   ({what})")
    for note in notes:
        print("NOTE :", note)
    if dry_run:
        print(f"{len(updates)} cellule(s) à écrire (simulation).")
        return
    _call_with_retry(
        worksheet.batch_update,
        [{"range": rowcol_to_a1(r, c), "values": [[v]]} for r, c, v, _ in updates],
        value_input_option="USER_ENTERED",
    )
    print(f"{len(updates)} cellule(s) écrite(s) dans '{WORKSHEET}'.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Remplit les blocs Bourse du Sheet depuis Trade Republic.")
    parser.add_argument("--dry-run", action="store_true", help="Affiche sans écrire dans le Sheet.")
    parser.add_argument("--base-dir", type=Path, default=BASE)
    args = parser.parse_args()

    from shared.google_sheet import _call_with_retry, get_worksheet_by_name

    worksheet = get_worksheet_by_name(WORKSHEET)
    grid = _call_with_retry(worksheet.get_all_values)
    trades = load_trades(args.base_dir / "transactions.json")
    positions = load_positions(args.base_dir)
    initial = load_initial(args.base_dir / "initial_positions.json", trades, positions, create=not args.dry_run)
    apply_initial(trades, initial)
    for isin, entry in initial.items():
        print(f"Position initiale {isin} : {entry['shares']:g} part(s) sur {entry['month']} à {entry['price']} (modifiable dans initial_positions.json)")
    updates, notes = build_updates(grid, trades, positions)
    report_and_write(worksheet, updates, notes, args.dry_run)


if __name__ == "__main__":
    main()
