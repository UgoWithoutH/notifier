from datetime import date
import os
import re
import json
import logging

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from gspread.utils import rowcol_to_a1


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

logger = logging.getLogger(__name__)


load_dotenv()

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
]

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

    for worksheet in spreadsheet.worksheets():
        title = worksheet.title.strip()

        match = re.match(r"(?i)^dashboard\s*(\d{4})$", title)

        if match:
            year = int(match.group(1))

            logger.info(
                "Feuille Dashboard trouvée : %s",
                title
            )

            dashboards.append(
                (
                    year,
                    worksheet,
                )
            )

    if not dashboards:
        logger.error("Aucune feuille Dashboard trouvée.")
        raise RuntimeError(
            "Aucune feuille Dashboard trouvée."
        )

    dashboards.sort(
        key=lambda x: x[0],
        reverse=True
    )

    worksheet = dashboards[0][1]

    logger.info(
        "Feuille Dashboard sélectionnée : %s",
        worksheet.title
    )

    return worksheet


def find_cell_by_value(worksheet, value: str):
    logger.info(
        "Recherche de la cellule exacte : '%s'",
        value
    )

    cells = worksheet.findall(value)

    for cell in cells:
        if worksheet.cell(cell.row, cell.col).value == value:
            logger.info(
                "Cellule trouvée : %s",
                cell.address
            )

            return cell

    logger.warning(
        "Cellule non trouvée : '%s'",
        value
    )

    return None


def find_current_month_cell(worksheet, row):
    today = date.today()

    month_names = {
        1: "janv.",
        2: "févr.",
        3: "mars",
        4: "avr.",
        5: "mai",
        6: "juin",
        7: "juil.",
        8: "août",
        9: "sept.",
        10: "oct.",
        11: "nov.",
        12: "déc.",
    }

    expected_month = month_names[today.month]
    expected_year = str(today.year)[-2:]

    logger.info(
        "Recherche du mois courant : %s %s",
        expected_month,
        expected_year
    )

    values = worksheet.row_values(row)

    logger.info(
        "Valeurs de la ligne %s : %s",
        row,
        values
    )

    for col_idx, value in enumerate(values, start=1):

        if not value:
            continue

        value = value.lower().strip()

        if (
            expected_month in value
            and expected_year in value
        ):
            address = rowcol_to_a1(row, col_idx)

            logger.info(
                "Mois courant trouvé : %s (%s)",
                address,
                value
            )

            return {
                "row": row,
                "col": col_idx,
                "address": address,
            }

    logger.warning(
        "Mois courant introuvable."
    )

    return None


def find_first_cell_containing_below(
    worksheet,
    start_cell,
    search_text: str
):
    logger.info(
        "Recherche de '%s' sous la cellule %s",
        search_text,
        start_cell.address
    )

    for row in range(
        start_cell.row + 1,
        worksheet.row_count + 1
    ):
        value = worksheet.cell(
            row,
            start_cell.col
        ).value

        if value and search_text.lower() in value.lower():

            cell = worksheet.cell(
                row,
                start_cell.col
            )

            logger.info(
                "Texte trouvé : %s (%s)",
                cell.address,
                cell.value
            )

            return cell

    logger.warning(
        "Texte '%s' non trouvé.",
        search_text
    )

    return None


def fill_current_month_amounts(
    platform: str,
    amounts: dict
):
    logger.info(
        "Début mise à jour Google Sheet pour %s",
        platform
    )

    worksheet = get_latest_dashboard_worksheet(
        SPREADSHEET_ID
    )

    crowdlending_cell = find_cell_by_value(
        worksheet,
        "Crowdlending"
    )

    if not crowdlending_cell:
        raise RuntimeError(
            "La section 'Crowdlending' n'a pas été trouvée."
        )

    current_month_cell = find_current_month_cell(
        worksheet,
        crowdlending_cell.row
    )

    if not current_month_cell:
        raise RuntimeError(
            "La colonne du mois courant n'a pas été trouvée."
        )

    current_month_col = current_month_cell["col"]

    platform_cell = find_first_cell_containing_below(
        worksheet,
        crowdlending_cell,
        platform
    )

    if not platform_cell:
        raise RuntimeError(
            f"La plateforme '{platform}' n'a pas été trouvée sous Crowdlending."
        )

    total_amount = amounts.get(
        "total",
        0
    )

    gross_interest_received = amounts.get(
        "gross_interest_received",
        0
    )

    logger.info(
        "Valeurs à écrire : total=%s, intérêts=%s",
        total_amount,
        gross_interest_received
    )

    worksheet.update_cell(
        platform_cell.row,
        current_month_col,
        total_amount
    )

    worksheet.update_cell(
        platform_cell.row + 1,
        current_month_col,
        gross_interest_received
    )

    logger.info(
        "Mise à jour terminée pour %s",
        platform
    )


if __name__ == "__main__":

    fill_current_month_amounts(
        platform="Bienprêter",
        amounts={
            "total": 1000,
            "gross_interest_received": 50
        }
    )