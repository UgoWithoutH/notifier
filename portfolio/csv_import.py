"""Import and normalize Boursobank and Trade Republic portfolio CSV files."""

from __future__ import annotations

import csv
import argparse
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path


SUPPORTED_BROKERS = ("boursobank", "trade_republic")

FIELD_ALIASES = {
    "name": ("nom", "libelle", "instrument", "titre", "produit", "name"),
    "isin": ("isin", "code isin"),
    "quantity": ("quantite", "qte", "nombre", "shares", "quantity"),
    "current_price": ("cours", "prix actuel", "cours actuel", "current price", "price"),
    "market_value": (
        "valorisation",
        "valeur actuelle",
        "montant actuel",
        "market value",
        "position value",
        "value",
    ),
    "average_cost": ("pru", "prix de revient unitaire", "prix moyen", "average cost"),
    "cost_basis": ("prix de revient", "montant investi", "cout total", "cost basis"),
    "unrealized_gain": (
        "plus value latente",
        "plus value",
        "performance eur",
        "gain perte",
        "unrealized gain",
        "profit loss",
    ),
    "unrealized_gain_percent": (
        "plus value latente pourcentage",
        "plus value pourcentage",
        "performance pourcentage",
        "gain perte pourcentage",
        "unrealized gain percent",
        "profit loss percent",
    ),
    "currency": ("devise", "currency"),
}


@dataclass(frozen=True)
class Position:
    broker: str
    name: str
    isin: str
    quantity: float
    current_price: float | None
    market_value: float
    average_cost: float | None
    cost_basis: float | None
    unrealized_gain: float | None
    unrealized_gain_percent: float | None
    currency: str
    source_file: str


def _normalize_header(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(character for character in value if not unicodedata.combining(character))
    value = value.lower().replace("%", " pourcentage ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value).split())


def _parse_number(value: str | None) -> float | None:
    if value is None:
        return None
    cleaned = str(value).replace("\u202f", "").replace("\xa0", "").strip()
    if not cleaned or cleaned in {"-", "--", "n/a", "N/A"}:
        return None
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = re.sub(r"[^0-9,.'+-]", "", cleaned.strip("()"))
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    else:
        cleaned = cleaned.replace("'", "")
    try:
        number = float(cleaned)
    except ValueError:
        return None
    return -number if negative else number


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"Encodage CSV non pris en charge : {path}")


def _column_mapping(fieldnames: list[str]) -> dict[str, str]:
    normalized = {_normalize_header(header): header for header in fieldnames if header}
    mapping = {}
    for field, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            original = normalized.get(_normalize_header(alias))
            if original is not None:
                mapping[field] = original
                break
    return mapping


def read_positions(path: Path, broker: str) -> list[Position]:
    if broker not in SUPPORTED_BROKERS:
        raise ValueError(f"Courtier non pris en charge : {broker}")

    text = _read_text(path)
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=";,\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(text.splitlines(), dialect=dialect)
    if not reader.fieldnames:
        raise ValueError(f"Aucun en-tête trouvé dans {path}")

    columns = _column_mapping(reader.fieldnames)
    missing = [field for field in ("name", "quantity", "market_value") if field not in columns]
    if missing:
        raise ValueError(
            f"Colonnes requises introuvables dans {path.name}: {', '.join(missing)}. "
            f"En-têtes détectés: {', '.join(reader.fieldnames)}"
        )

    positions = []
    for row in reader:
        name = (row.get(columns["name"]) or "").strip()
        quantity = _parse_number(row.get(columns["quantity"]))
        market_value = _parse_number(row.get(columns["market_value"]))
        if not name or quantity is None or market_value is None:
            continue

        current_price = _parse_number(row.get(columns.get("current_price", "")))
        average_cost = _parse_number(row.get(columns.get("average_cost", "")))
        cost_basis = _parse_number(row.get(columns.get("cost_basis", "")))
        unrealized_gain = _parse_number(row.get(columns.get("unrealized_gain", "")))
        unrealized_gain_percent = _parse_number(
            row.get(columns.get("unrealized_gain_percent", ""))
        )

        if current_price is None and quantity:
            current_price = market_value / quantity
        if cost_basis is None and average_cost is not None:
            cost_basis = quantity * average_cost
        if average_cost is None and cost_basis is not None and quantity:
            average_cost = cost_basis / quantity
        if unrealized_gain is None and cost_basis is not None:
            unrealized_gain = market_value - cost_basis
        if unrealized_gain_percent is None and unrealized_gain is not None and cost_basis:
            unrealized_gain_percent = unrealized_gain / cost_basis * 100

        positions.append(
            Position(
                broker=broker,
                name=name,
                isin=(row.get(columns.get("isin", "")) or "").strip(),
                quantity=quantity,
                current_price=current_price,
                market_value=market_value,
                average_cost=average_cost,
                cost_basis=cost_basis,
                unrealized_gain=unrealized_gain,
                unrealized_gain_percent=unrealized_gain_percent,
                currency=(row.get(columns.get("currency", "")) or "EUR").strip() or "EUR",
                source_file=path.name,
            )
        )

    return positions


def read_latest_exports(input_dir: Path) -> list[Position]:
    positions = []
    for broker in SUPPORTED_BROKERS:
        files = list((input_dir / broker).glob("*.csv"))
        if not files:
            continue
        latest = max(files, key=lambda path: path.stat().st_mtime)
        positions.extend(read_positions(latest, broker))
    return positions


SHEET_HEADERS = [
    "Courtier",
    "ETF",
    "ISIN",
    "Quantité",
    "Cours actuel",
    "Valorisation",
    "PRU",
    "Montant investi",
    "Plus-value latente",
    "Plus-value latente %",
    "Devise",
    "Fichier source",
]


def build_sheet_rows(positions: list[Position]) -> list[list[object]]:
    rows: list[list[object]] = [SHEET_HEADERS]
    for position in sorted(positions, key=lambda item: (item.broker, item.name.lower())):
        rows.append(
            [
                position.broker,
                position.name,
                position.isin,
                position.quantity,
                position.current_price,
                position.market_value,
                position.average_cost,
                position.cost_basis,
                position.unrealized_gain,
                position.unrealized_gain_percent,
                position.currency,
                position.source_file,
            ]
        )

    rows.append([])
    for broker in (*SUPPORTED_BROKERS, "TOTAL"):
        selected = positions if broker == "TOTAL" else [item for item in positions if item.broker == broker]
        if not selected:
            continue
        market_value = sum(item.market_value for item in selected)
        known_cost_basis = [item.cost_basis for item in selected if item.cost_basis is not None]
        cost_basis = sum(known_cost_basis) if len(known_cost_basis) == len(selected) else None
        gain = market_value - cost_basis if cost_basis is not None else None
        gain_percent = gain / cost_basis * 100 if gain is not None and cost_basis else None
        rows.append(
            [
                broker,
                "TOTAL",
                "",
                "",
                "",
                market_value,
                "",
                cost_basis,
                gain,
                gain_percent,
                "EUR",
                "",
            ]
        )
    return rows


def sync_google_sheet(positions: list[Position], worksheet_title: str) -> None:
    import gspread

    from shared.google_sheet import (
        SPREADSHEET_ID,
        _call_with_retry,
        get_google_credentials,
    )

    client = gspread.authorize(get_google_credentials())
    spreadsheet = _call_with_retry(client.open_by_key, SPREADSHEET_ID)
    try:
        worksheet = _call_with_retry(spreadsheet.worksheet, worksheet_title)
    except gspread.WorksheetNotFound:
        worksheet = _call_with_retry(
            spreadsheet.add_worksheet,
            title=worksheet_title,
            rows=max(len(positions) + 10, 100),
            cols=len(SHEET_HEADERS),
        )

    rows = build_sheet_rows(positions)
    _call_with_retry(worksheet.clear)
    _call_with_retry(worksheet.update, "A1", rows, value_input_option="USER_ENTERED")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Importe les derniers CSV Boursobank et Trade Republic dans Google Sheets."
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "portfolio_imports",
        help="Dossier contenant boursobank/ et trade_republic/.",
    )
    parser.add_argument("--worksheet", default="Positions ETF", help="Nom de l'onglet cible.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Affiche les données sans écrire dans Google Sheets.",
    )
    args = parser.parse_args()

    positions = read_latest_exports(args.input_dir)
    if not positions:
        parser.error(f"Aucune position trouvée dans {args.input_dir}")

    if args.dry_run:
        for row in build_sheet_rows(positions):
            print(row)
        return

    sync_google_sheet(positions, args.worksheet)
    print(f"{len(positions)} position(s) synchronisée(s) dans l'onglet '{args.worksheet}'.")


if __name__ == "__main__":
    main()