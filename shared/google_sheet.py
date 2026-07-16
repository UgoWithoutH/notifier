import os
import re
import json
import logging

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from gspread.utils import rowcol_to_a1

from shared.report_date import get_report_date


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

logger = logging.getLogger(__name__)


load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

SPREADSHEET_ID = os.environ["GOOGLE_SHEET_ID"]
GOOGLE_CREDENTIALS = os.environ["GOOGLE_CREDENTIALS"]


def get_google_credentials():
    logger.info("Chargement des credentials Google...")
    return Credentials.from_service_account_info(
        json.loads(GOOGLE_CREDENTIALS),
        scopes=SCOPES,
    )


def get_latest_dashboard_worksheet(spreadsheet_id: str):
    logger.info("Recherche de la dernière feuille Dashboard...")

    credentials = get_google_credentials()
    client = gspread.authorize(credentials)
    spreadsheet = client.open_by_key(spreadsheet_id)

    dashboards = []

    # 1 seul appel API : spreadsheet.worksheets()
    for worksheet in spreadsheet.worksheets():
        title = worksheet.title.strip()
        match = re.match(r"(?i)^dashboard\s*(\d{4})$", title)

        if match:
            year = int(match.group(1))
            logger.info("Feuille Dashboard trouvée : %s", title)
            dashboards.append((year, worksheet))

    if not dashboards:
        logger.error("Aucune feuille Dashboard trouvée.")
        raise RuntimeError("Aucune feuille Dashboard trouvée.")

    dashboards.sort(key=lambda x: x[0], reverse=True)
    worksheet = dashboards[0][1]

    logger.info("Feuille Dashboard sélectionnée : %s", worksheet.title)
    return worksheet


def find_cell_by_value(grid, value: str):
    """Recherche en mémoire (pas d'appel API). Retourne (row, col) 1-based ou None."""
    logger.info("Recherche de la cellule exacte : '%s'", value)

    for row_idx, row in enumerate(grid, start=1):
        for col_idx, cell_value in enumerate(row, start=1):
            if cell_value == value:
                logger.info("Cellule trouvée : %s", rowcol_to_a1(row_idx, col_idx))
                return row_idx, col_idx

    logger.warning("Cellule non trouvée : '%s'", value)
    return None


def find_current_month_cell(grid, row):
    """Recherche en mémoire dans la ligne `row` (1-based). Uses
    get_report_date() (REPORT_DATE env var override, falls back to the
    real current date) instead of a hardcoded date.today() so a manual
    workflow run can target a specific month's column."""
    today = get_report_date()

    month_names = {
        1: "janv.", 2: "févr.", 3: "mars", 4: "avr.",
        5: "mai", 6: "juin", 7: "juil.", 8: "août",
        9: "sept.", 10: "oct.", 11: "nov.", 12: "déc.",
    }

    expected_month = month_names[today.month]
    expected_year = str(today.year)[-2:]

    logger.info("Recherche du mois courant : %s %s", expected_month, expected_year)

    if row - 1 >= len(grid):
        logger.warning("Mois courant introuvable (ligne hors grille).")
        return None

    values = grid[row - 1]
    logger.info("Valeurs de la ligne %s : %s", row, values)

    for col_idx, value in enumerate(values, start=1):
        if not value:
            continue
        value = value.lower().strip()
        if expected_month in value and expected_year in value:
            address = rowcol_to_a1(row, col_idx)
            logger.info("Mois courant trouvé : %s (%s)", address, value)
            return {"row": row, "col": col_idx, "address": address}

    logger.warning("Mois courant introuvable.")
    return None


def find_first_cell_containing_below(grid, start_row, start_col, search_text: str):
    """Recherche en mémoire, colonne `start_col` (1-based), sous `start_row`."""
    logger.info(
        "Recherche de '%s' sous la ligne %s, colonne %s",
        search_text, start_row, start_col
    )

    for row_idx in range(start_row + 1, len(grid) + 1):
        row = grid[row_idx - 1]

        if start_col - 1 >= len(row):
            continue

        value = row[start_col - 1]

        if value and search_text.lower() in value.lower():
            logger.info("Texte trouvé : %s (%s)", rowcol_to_a1(row_idx, start_col), value)
            return row_idx

    logger.warning("Texte '%s' non trouvé.", search_text)
    return None


def fill_current_month_amounts(platform: str, amounts: dict, section: str = "Crowdlending"):
    """
    `section` : libellé de la cellule sous laquelle chercher `platform`
    (ex. "Crowdlending" pour la plupart des plateformes, "Crowdlending
    savings" pour Monefit).
    """
    logger.info("Début mise à jour Google Sheet pour %s (section '%s')", platform, section)

    worksheet = get_latest_dashboard_worksheet(SPREADSHEET_ID)

    # 1 seul appel API pour charger toute la feuille
    grid = worksheet.get_all_values()

    section_pos = find_cell_by_value(grid, section)
    if not section_pos:
        raise RuntimeError(f"La section '{section}' n'a pas été trouvée.")

    section_row, section_col = section_pos

    current_month_cell = find_current_month_cell(grid, section_row)
    if not current_month_cell:
        raise RuntimeError("La colonne du mois courant n'a pas été trouvée.")

    current_month_col = current_month_cell["col"]

    platform_row = find_first_cell_containing_below(
        grid, section_row, section_col, platform
    )
    if not platform_row:
        raise RuntimeError(
            f"La plateforme '{platform}' n'a pas été trouvée sous '{section}'."
        )

    total_amount = amounts.get("total", 0)
    gross_interest_received = amounts.get("gross_interest_received", 0)

    logger.info(
        "Valeurs à écrire : total=%s, intérêts=%s",
        total_amount, gross_interest_received
    )

    # 1 seul appel API pour écrire les 2 valeurs (lignes adjacentes, même colonne)
    start_a1 = rowcol_to_a1(platform_row, current_month_col)
    end_a1 = rowcol_to_a1(platform_row + 1, current_month_col)
    range_name = f"{start_a1}:{end_a1}"

    worksheet.update(
        range_name,
        [[total_amount], [gross_interest_received]],
        value_input_option="USER_ENTERED"
    )

    logger.info("Mise à jour terminée pour %s", platform)

def find_rows_by_texts_below(grid, start_row, start_col, texts: list):
    """
    Cherche plusieurs textes en une seule passe (en mémoire) sous `start_row`,
    dans la colonne `start_col` (1-based). Recherche insensible à la casse,
    par sous-chaîne (comme find_first_cell_containing_below).
    Retourne un dict {texte_original: row_idx} pour les textes trouvés.
    """
    remaining = {t.lower().strip(): t for t in texts}
    found = {}

    for row_idx in range(start_row + 1, len(grid) + 1):
        if not remaining:
            break

        row = grid[row_idx - 1]

        if start_col - 1 >= len(row):
            continue

        value = row[start_col - 1]

        if not value:
            continue

        value_lower = value.lower().strip()

        for key in list(remaining.keys()):
            if key in value_lower:
                original_text = remaining.pop(key)
                found[original_text] = row_idx
                logger.info(
                    "Loan originator trouvé : '%s' -> ligne %s",
                    original_text,
                    row_idx
                )

    if remaining:
        logger.warning(
            "Loan originators non trouvés : %s",
            list(remaining.values())
        )

    return found

def fill_geographic_repartition_amounts(loan_originators: list):
    """
    loan_originators : liste de dicts, ex.
        [{"name": "Bienprêter", "amount": 1000}, {"name": "Lendix", "amount": 500}]

    Cherche la cellule "Répartition géographique", puis pour chaque loan
    originator cherche son nom sous cette cellule, dans la même colonne,
    et écrit le montant dans la cellule juste à droite (colonne + 1).
    """
    logger.info(
        "Début mise à jour Répartition géographique (%s loan originators)",
        len(loan_originators)
    )

    worksheet = get_latest_dashboard_worksheet(SPREADSHEET_ID)

    # 1 seul appel API pour charger toute la feuille
    grid = worksheet.get_all_values()

    geo_pos = find_cell_by_value(grid, "Répartition géographique")

    if not geo_pos:
        raise RuntimeError(
            "La section 'Répartition géographique' n'a pas été trouvée."
        )

    geo_row, geo_col = geo_pos

    names = [lo["name"] for lo in loan_originators]

    # 1 seule passe en mémoire pour trouver toutes les lignes
    rows_by_name = find_rows_by_texts_below(grid, geo_row, geo_col, names)

    missing = [name for name in names if name not in rows_by_name]

    if missing:
        # On ne bloque plus les autres écritures pour autant : on log les
        # loan originators manquants et on continue avec ceux qui ont été
        # trouvés, plutôt que de tout annuler.
        logger.warning(
            "Loan originator(s) non trouvé(s), ignoré(s) : %s", missing
        )

    target_col = geo_col + 1

    updates = []

    for lo in loan_originators:
        row = rows_by_name.get(lo["name"])
        if row is None:
            continue

        amount = lo.get("amount", 0)
        address = rowcol_to_a1(row, target_col)

        updates.append({
            "range": address,
            "values": [[amount]],
        })

        logger.info(
            "Préparation écriture : %s = %s (%s)",
            lo["name"],
            amount,
            address
        )

    if not updates:
        logger.warning("Aucun loan originator trouvé, rien à écrire.")
        return

    worksheet.batch_update(updates, value_input_option="USER_ENTERED")

    logger.info(
        "Mise à jour Répartition géographique terminée (%d trouvé(s), %d manquant(s)).",
        len(updates),
        len(missing),
    )

if __name__ == "__main__":
    fill_current_month_amounts(
        platform="Bienprêter",
        amounts={
            "total": 1000,
            "gross_interest_received": 50
        }
    )