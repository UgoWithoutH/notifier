import os
import re
import json

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials


load_dotenv()

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
]

SPREADSHEET_ID = os.environ["GOOGLE_SHEET_ID"]
GOOGLE_CREDENTIALS = os.environ["GOOGLE_CREDENTIALS"]


def get_google_credentials():
    return Credentials.from_service_account_info(
        json.loads(GOOGLE_CREDENTIALS),
        scopes=SCOPES,
    )


def get_latest_dashboard_worksheet(spreadsheet_id: str):
    credentials = get_google_credentials()

    client = gspread.authorize(credentials)

    spreadsheet = client.open_by_key(spreadsheet_id)

    dashboards = []

    for worksheet in spreadsheet.worksheets():
        title = worksheet.title.strip()

        match = re.match(r"(?i)^dashboard\s*(\d{4})$", title)

        if match:
            dashboards.append(
                (
                    int(match.group(1)),
                    worksheet,
                )
            )

    if not dashboards:
        raise RuntimeError("Aucune feuille Dashboard trouvée.")

    dashboards.sort(key=lambda x: x[0], reverse=True)

    return dashboards[0][1]


if __name__ == "__main__":
    ws = get_latest_dashboard_worksheet(SPREADSHEET_ID)
    print("Feuille trouvée :", ws.title)