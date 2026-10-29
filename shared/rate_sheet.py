"""Onglet "taux" du Google Sheet : pour chaque plateforme, la durée de base (émission -> échéance) et le rendement (%)
des prêts / projets, plus l'historique mensuel du taux moyen pondéré et de la durée moyenne pondérée.

Même mise en page que l'onglet "durée" (voir shared.google_sheet.fill_remaining_duration_amounts) :
colonne A = société de prêt / projet, B = durée de base, C = rendement (%), D = montant, E... = mois (mois courant à
gauche). Sur la ligne de chaque secteur (Crowdlending, Crowdlending savings...), la colonne D porte l'horizon de
l'historique ("1 an", "1 an 6 mois", "18 mois"...), modifiable à la main.

Le bloc d'une plateforme contient, pour chaque société de prêt (ou directement sous la ligne de la plateforme quand
elle n'a pas de société de prêt) :
- deux lignes d'historique : "taux moyen pondéré" (moyenne pondérée par montant des rendements) et "durée moyenne
  pondérée" (moyenne pondérée par montant des durées de base), moyennées chaque mois sur l'horizon du secteur
  (voir shared.rate_history) ;
- une ligne par durée de base (et par projet pour une plateforme de projets), triée par durée croissante, avec le
  rendement moyen pondéré par montant (C) et le montant (D).
"""

import logging

from shared import rate_history as history
from shared.google_sheet import (
    GEO_SECTION_BOUNDARY_LABELS,
    _DURATION_AMOUNT_COL,
    _DURATION_AMOUNT_FORMAT,
    _DURATION_FIRST_MONTH_COL,
    _DURATION_MONTH_CELL_FORMAT,
    _DURATION_ORIGINATOR_FORMAT,
    _DURATION_RATE_COL,
    _DURATION_RATE_FORMAT,
    _DURATION_RATE_HEADER,
    _DURATION_ROW_FORMAT,
    _DURATION_SECTION_LABELS,
    _call_with_retry,
    _col_letter,
    _duration_month_columns,
    _duration_own_section,
    _duration_sections,
    get_worksheet_by_name,
)
from shared.remaining_duration import months_to_term
from shared.report_date import get_report_date

logger = logging.getLogger(__name__)

RATE_SHEET_NAME = "taux"
HISTORY_LABELS = ("taux moyen pondéré", "durée moyenne pondérée")
_AVG_RATE_LABEL, _AVG_DURATION_LABEL = HISTORY_LABELS

_BLUE = {"red": 0.6431373, "green": 0.7607843, "blue": 0.95686275}
_GREY = {"red": 0.9372549, "green": 0.9372549, "blue": 0.9372549}
_SOLID = {"style": "SOLID", "width": 1}
_COLUMN_WIDTHS = (453, 142, 142, 76, 81)  # A, B, C, D, E (comme l'onglet "durée")
_SECTION_FORMATS = (
    {"backgroundColor": _BLUE, "borders": {"top": _SOLID, "bottom": _SOLID}, "textFormat": {"fontSize": 11, "bold": True}},
    {"backgroundColor": _BLUE, "borders": {"top": _SOLID, "bottom": _SOLID}, "textFormat": {"fontSize": 11, "bold": True}},
    {"backgroundColor": _BLUE, "borders": {"top": _SOLID, "bottom": _SOLID}, "horizontalAlignment": "RIGHT", "textFormat": {"fontSize": 9, "bold": True}},
    {"backgroundColor": _BLUE, "borders": {"top": _SOLID, "bottom": _SOLID}},
)
_SECTION_MONTH_FORMAT = {
    "backgroundColor": _BLUE, "borders": {"bottom": _SOLID}, "horizontalAlignment": "RIGHT", "textFormat": {"fontSize": 9, "bold": True},
}
_PLATFORM_FORMATS = (
    {"backgroundColor": _GREY, "horizontalAlignment": "LEFT", "textFormat": {"bold": True}},
    {"backgroundColor": _GREY, "horizontalAlignment": "LEFT", "textFormat": {"bold": True}},
    {"backgroundColor": _GREY, "horizontalAlignment": "LEFT", "textFormat": {"bold": True}},
    {"backgroundColor": _GREY, "numberFormat": _DURATION_AMOUNT_FORMAT["numberFormat"], "borders": {"top": _SOLID}},
)
_PLATFORM_MONTH_FORMAT = {"backgroundColor": _GREY, "numberFormat": _DURATION_AMOUNT_FORMAT["numberFormat"], "borders": {"top": {"style": "NONE"}}}
_RATE_MONTH_NUMBER_FORMAT = {"type": "PERCENT", "pattern": "0.00%"}
_DEFAULT_HORIZON = "1 an"


def _term_label(term: tuple) -> str:
    """(mois, jours) -> "1 an 1 mois 1 jour" / "2 ans" / "5 mois" / "0 jour" (les mois sont regroupés en années)."""
    years, months = divmod(abs(term[0]), 12)
    days = abs(term[1])
    parts = []
    if years:
        parts.append(f"{years} an" + ("s" if years > 1 else ""))
    if months:
        parts.append(f"{months} mois")
    if days or not parts:
        parts.append(f"{days} jour" + ("s" if days > 1 else ""))
    return " ".join(parts)


def _range(worksheet, first_row: int, last_row: int, first_col: int = 0, last_col: int | None = None) -> dict:
    """Plage GridRange (lignes 1-based incluses, colonnes 0-based, `last_col` exclu)."""
    grid_range = {"sheetId": worksheet.id, "startRowIndex": first_row - 1, "endRowIndex": last_row, "startColumnIndex": first_col}
    if last_col is not None:
        grid_range["endColumnIndex"] = last_col
    return grid_range


def _grid_size(worksheet) -> tuple:
    """(lignes, colonnes) actuelles de l'onglet, relues sur l'API (gspread ne met pas ses propriétés à jour)."""
    metadata = _call_with_retry(
        worksheet.spreadsheet.fetch_sheet_metadata, {"fields": "sheets(properties(sheetId,gridProperties))"},
    )
    for sheet in metadata.get("sheets", []):
        if sheet["properties"]["sheetId"] == worksheet.id:
            properties = sheet["properties"]["gridProperties"]
            return properties["rowCount"], properties["columnCount"]
    return worksheet.row_count, worksheet.col_count


def _layout_is_ready(grid: list, column_count: int) -> bool:
    sections = _duration_sections(grid)
    if column_count <= _DURATION_FIRST_MONTH_COL - 1 or not sections:
        return False
    for row_idx, text, _ in sections:
        row = grid[row_idx - 1]
        if len(row) <= _DURATION_RATE_COL or row[_DURATION_RATE_COL].strip() != _DURATION_RATE_HEADER or not text:
            return False
    return True


def _ensure_layout(worksheet, grid: list) -> list:
    """Met l'onglet au format de l'onglet "durée" (largeurs de colonnes, lignes de secteur avec les en-têtes
    "Rendement" et l'horizon, lignes de plateforme) s'il ne l'est pas encore ; retourne la grille à jour."""
    _, column_count = _grid_size(worksheet)
    if _layout_is_ready(grid, column_count):
        return grid
    logger.info("Mise en forme de l'onglet '%s' (format de l'onglet 'durée').", RATE_SHEET_NAME)
    requests = []
    if column_count < _DURATION_AMOUNT_COL + 1:
        requests.append({"appendDimension": {"sheetId": worksheet.id, "dimension": "COLUMNS", "length": _DURATION_AMOUNT_COL + 1 - column_count}})
    for col, width in enumerate(_COLUMN_WIDTHS[:_DURATION_AMOUNT_COL + 1]):
        requests.append({"updateDimensionProperties": {
            "range": {"sheetId": worksheet.id, "dimension": "COLUMNS", "startIndex": col, "endIndex": col + 1},
            "properties": {"pixelSize": width}, "fields": "pixelSize",
        }})

    def format_row(row_idx: int, formats: tuple, values: dict) -> None:
        for col, cell_format in enumerate(formats):
            cell = {"userEnteredFormat": cell_format}
            if col in values:
                cell["userEnteredValue"] = {"stringValue": values[col]}
            requests.append({"updateCells": {
                "range": _range(worksheet, row_idx, row_idx, col, col + 1),
                "rows": [{"values": [cell]}],
                "fields": "userEnteredFormat" + (",userEnteredValue" if col in values else ""),
            }})

    for row_idx, text, _ in _duration_sections(grid):
        values = {_DURATION_RATE_COL: _DURATION_RATE_HEADER}
        if not text:
            values[_DURATION_AMOUNT_COL] = _DEFAULT_HORIZON
        format_row(row_idx, _SECTION_FORMATS, values)
    platform_labels = {label.casefold() for label in GEO_SECTION_BOUNDARY_LABELS} - set(_DURATION_SECTION_LABELS) - {"bourse"}
    for row_idx, row in enumerate(grid, 1):
        if row and row[0].strip().casefold() in platform_labels:
            format_row(row_idx, _PLATFORM_FORMATS, {})
    _call_with_retry(worksheet.client.batch_update, worksheet.spreadsheet_id, {"requests": requests})
    return _call_with_retry(worksheet.get_all_values)


def _horizon_months(sections: list, platform_row: int) -> int:
    section = _duration_own_section(sections, platform_row)
    if section and section[2]:
        return section[2]
    if section and section[1]:
        logger.warning(
            "Horizon illisible dans '%s'!D%s (%r) - %s mois utilisés. Attendu : '1 an', '1 an 6 mois', '18 mois'...",
            RATE_SHEET_NAME, section[0], section[1], history.DEFAULT_HORIZON_MONTHS,
        )
    return history.DEFAULT_HORIZON_MONTHS


def fill_rate_amounts(platform: str, rates, platform_level: bool = False, projects: bool = False) -> None:
    """Synchronise le bloc de la plateforme `platform` (ligne dont la colonne A vaut exactement son nom) de l'onglet
    "taux" avec les prêts de `rates` (`shared.remaining_duration.RateWeights`, alimenté par
    `rates.add(..., base_term=(mois, jours))`).

    - société de prêt (nom en colonne A) puis, dessous, ses lignes "taux moyen pondéré" et "durée moyenne pondérée"
      portant, à partir de la colonne E, la moyenne mensuelle (mois courant à gauche, sur l'horizon du secteur lu
      en colonne D de sa ligne) pondérée par montant ; puis une ligne par durée de base (colonne B), avec le
      rendement moyen pondéré par montant (C) et le montant (D). Les sociétés sans prêt en cours gardent leur
      ligne tant que l'historique de l'horizon contient des données pour elles ;
    - `platform_level` : plateforme sans société de prêt (projets, compte unique...) : pas de ligne de société, les
      deux lignes d'historique sont posées sous la ligne de la plateforme, à sa maille ;
    - `projects` (avec `platform_level`) : les clés de `rates` sont des noms de projets, écrits en colonne A de
      chaque ligne de durée.
    Le bloc va jusqu'à la prochaine plateforme/section et est réécrit en entier à chaque exécution."""
    worksheet = get_worksheet_by_name(RATE_SHEET_NAME)
    grid = _call_with_retry(worksheet.get_all_values)
    grid = _ensure_layout(worksheet, grid)

    def cell(row_idx: int, col: int = 0) -> str:
        row = grid[row_idx - 1]
        return row[col].strip() if len(row) > col else ""

    platform_row = next((i for i in range(1, len(grid) + 1) if cell(i).casefold() == platform.casefold()), None)
    if platform_row is None:
        logger.warning("Plateforme '%s' non trouvée dans l'onglet '%s' - rien écrit.", platform, RATE_SHEET_NAME)
        return
    boundaries = {label.casefold() for label in GEO_SECTION_BOUNDARY_LABELS} | {"crowdlending"}
    boundaries.discard(platform.casefold())
    end_row = platform_row + 1
    while end_row <= len(grid) and cell(end_row).casefold() not in boundaries:
        end_row += 1
    current_rows = end_row - platform_row - 1

    today = get_report_date()
    sections = _duration_sections(grid)
    horizon_months = _horizon_months(sections, platform_row)

    # Regroupement des prêts : par société (ou une seule clé None au niveau plateforme), puis par durée de base
    # et par projet : entries[(clé, durée de base, clé projet)] = [montant, somme montant x taux, somme montant avec taux].
    originator_names = {}
    project_names = {"": ""}
    loans_by_key = {}
    entries = {}
    for key, name, term, amount, rate in rates.loans():
        if platform_level:
            group, project_key = None, (key if projects else "")
            project_names.setdefault(project_key, name if projects else "")
        else:
            group, project_key = key, ""
            originator_names.setdefault(key, name)
        loans_by_key.setdefault(group, []).append((term, amount, rate))
        entry = entries.setdefault((group, term, project_key), [0.0, 0.0, 0.0])
        entry[0] += amount
        if rate is not None:
            entry[1] += amount * rate
            entry[2] += amount

    path = history.state_path(platform)
    days, history_names, runs = history.load_history(path)
    if platform_level:
        sums_by_key = {history.PLATFORM_KEY: history.loan_sums(loans_by_key.get(None, []))}
        names_by_key = {history.PLATFORM_KEY: platform}
        originators = {None: platform}
    else:
        sums_by_key = {key: history.loan_sums(loans) for key, loans in loans_by_key.items()}
        names_by_key = dict(originator_names)
        originators = dict(originator_names)
        for key, name in history.tracked_originators(path, today, horizon_months).items():
            originators.setdefault(key, name)

    sums_by_key = {key: sums for key, sums in sums_by_key.items() if sums}
    history.record_day(days, history_names, runs, today, sums_by_key, names_by_key, today, horizon_months)
    history.save_history(path, days, history_names, runs)

    def display(history_key: str) -> str:
        return "ensemble de la plateforme" if history_key == history.PLATFORM_KEY else history_names.get(history_key, history_key)

    for history_key, sums in sums_by_key.items():
        logger.info(
            "[%s] %s - %s enregistré dans %s : taux moyen %s, durée moyenne %.1f mois.", platform, display(history_key), today, path.name,
            "%.2f%%" % (sums[0] / sums[1]) if sums[1] > 0 else "inconnu", sums[2] / sums[3],
        )
    if not sums_by_key:
        logger.info("[%s] aucun prêt avec durée de base le %s : rien d'enregistré dans %s.", platform, today, path.name)

    # Lignes voulues : (type, clé société, clé projet, durée de base, texte A, texte B, montant, rendement %).
    def total_amount(key) -> float:
        return sum(amount for _, amount, _ in loans_by_key.get(key, []))

    ordered_keys = sorted(originators, key=lambda key: (-total_amount(key), str(originators[key]).casefold()))
    desired = []
    for key in ordered_keys:
        history_key = history.PLATFORM_KEY if key is None else key
        if not platform_level:
            desired.append(("orig", history_key, originators[key], None, None))
        desired.append(("rate", history_key, _AVG_RATE_LABEL, None, None))
        desired.append(("duration", history_key, _AVG_DURATION_LABEL, None, None))
        terms = sorted({(term, project_key) for (group, term, project_key) in entries if group == key},
                       key=lambda item: (item[0], project_names[item[1]].casefold()))
        for term, project_key in terms:
            amount, weighted_sum, weighted_base = entries[(key, term, project_key)]
            rate = weighted_sum / weighted_base if weighted_base > 0 else None
            desired.append(("term", history_key, project_names[project_key],             _term_label(term),
                            (round(amount, 2), None if rate is None else round(rate / 100, 6))))

    own_window = history.window_months(today, horizon_months)
    averages = {
        (history_key, month): history.monthly_average(days, history_key, *month)
        for history_key in {row[1] for row in desired} for month in own_window
    }
    for (history_key, month), average in sorted(averages.items(), key=lambda item: (display(item[0][0]), item[0][1])):
        if average is None:
            continue
        samples = history.monthly_samples(days, history_key, *month)
        logger.info(
            "[%s] %s - %s : taux moyen %s, durée moyenne %s (%s jour(s) enregistré(s)).", platform, display(history_key),
            history.month_label(*month), "inconnu" if average[0] is None else "%.2f%%" % average[0],
            "inconnue" if average[1] is None else "%.1f mois" % average[1], len(samples),
        )
    needed = {month for (_, month), average in averages.items() if average is not None}

    in_any_window = set(own_window)
    for _, _, section_months in sections:
        in_any_window.update(history.window_months(today, section_months or history.DEFAULT_HORIZON_MONTHS))
    existing_months = _duration_month_columns(grid, sections)
    final_months = sorted({month for month in existing_months if month in in_any_window} | needed, reverse=True)
    grid_rows, grid_columns = _grid_size(worksheet)

    requests = []
    for index in range(len(existing_months) - 1, -1, -1):
        if existing_months[index] not in final_months:
            col = _DURATION_FIRST_MONTH_COL + index
            logger.info("Suppression de la colonne %s de '%s' (mois hors de l'horizon de tous les secteurs).", _col_letter(col + 1), RATE_SHEET_NAME)
            requests.append({"deleteDimension": {"range": {"sheetId": worksheet.id, "dimension": "COLUMNS", "startIndex": col, "endIndex": col + 1}}})
            grid_columns -= 1
    kept_months = set(existing_months) & set(final_months)
    for index, month in enumerate(final_months):
        if month not in kept_months:
            col = _DURATION_FIRST_MONTH_COL + index
            logger.info("Création de la colonne %s ('%s') dans '%s'.", _col_letter(col + 1), history.month_label(*month), RATE_SHEET_NAME)
            requests.append({"insertDimension": {
                "range": {"sheetId": worksheet.id, "dimension": "COLUMNS", "startIndex": col, "endIndex": col + 1},
                "inheritFromBefore": col >= grid_columns,  # Google exige l'héritage pour une insertion après la dernière colonne
            }})
            grid_columns += 1
    end_column = _DURATION_FIRST_MONTH_COL + len(final_months)

    # Redimensionnement du bloc : lignes en trop supprimées / manquantes insérées en fin de bloc.
    row_shift = len(desired) - current_rows
    if row_shift < 0:
        requests.append({"deleteDimension": {"range": {
            "sheetId": worksheet.id, "dimension": "ROWS", "startIndex": platform_row + len(desired), "endIndex": end_row - 1,
        }}})
    elif row_shift > 0:
        requests.append({"insertDimension": {
            "range": {"sheetId": worksheet.id, "dimension": "ROWS", "startIndex": end_row - 1, "endIndex": end_row - 1 + row_shift},
            "inheritFromBefore": end_row > grid_rows or end_row > len(grid),  # insertion après la dernière ligne : héritage obligatoire
        }})

    def final_row_of(row_idx: int) -> int:
        return row_idx + row_shift if row_idx > platform_row else row_idx

    # En-têtes de mois et formats de colonnes des lignes de secteur / de plateforme.
    for row_idx, _, _ in sections:
        final_row = final_row_of(row_idx)
        if final_months:
            header_cells = [
                {"userEnteredValue": {"stringValue": history.month_label(*month)}, "userEnteredFormat": _SECTION_MONTH_FORMAT}
                for month in final_months
            ]
            requests.append({"updateCells": {
                "range": _range(worksheet, final_row, final_row, _DURATION_FIRST_MONTH_COL, end_column),
                "rows": [{"values": header_cells}], "fields": "userEnteredValue,userEnteredFormat",
            }})
    platform_labels = {label.casefold() for label in GEO_SECTION_BOUNDARY_LABELS} - set(_DURATION_SECTION_LABELS) - {"bourse"}
    if final_months:
        for row_idx, row in enumerate(grid, 1):
            if row and row[0].strip().casefold() in platform_labels:
                final_row = final_row_of(row_idx)
                requests.append({"repeatCell": {
                    "range": _range(worksheet, final_row, final_row, _DURATION_FIRST_MONTH_COL, end_column),
                    "cell": {"userEnteredFormat": _PLATFORM_MONTH_FORMAT},
                    "fields": "userEnteredFormat",
                }})

    # Contenu du bloc (toutes les colonnes de chaque ligne : valeur + format).
    rows = []
    for kind, history_key, text_a, text_b, payload in desired:
        label_format = _DURATION_ORIGINATOR_FORMAT if kind == "orig" else _DURATION_ROW_FORMAT
        cells = [
            {"userEnteredValue": {"stringValue": text_a}, "userEnteredFormat": label_format} if text_a else {"userEnteredFormat": label_format},
            {"userEnteredValue": {"stringValue": text_b}, "userEnteredFormat": _DURATION_ROW_FORMAT} if text_b else {"userEnteredFormat": _DURATION_ROW_FORMAT},
        ]
        rate_cell = {"userEnteredFormat": _DURATION_RATE_FORMAT}
        amount_cell = {"userEnteredFormat": _DURATION_AMOUNT_FORMAT}
        if kind == "term":
            amount, rate = payload
            amount_cell["userEnteredValue"] = {"numberValue": amount}
            if rate is not None:
                rate_cell["userEnteredValue"] = {"numberValue": rate}
        cells += [rate_cell, amount_cell]
        for month in final_months:
            cell_format = dict(_DURATION_MONTH_CELL_FORMAT)
            month_cell = {}
            average = averages.get((history_key, month))
            if kind in ("rate", "duration"):
                value = None if average is None else average[0 if kind == "rate" else 1]
                if kind == "rate":
                    cell_format["numberFormat"] = _RATE_MONTH_NUMBER_FORMAT
                    if value is not None:
                        month_cell["userEnteredValue"] = {"numberValue": round(value / 100, 6)}
                else:
                    cell_format["horizontalAlignment"] = "RIGHT"
                    if value is not None:
                        month_cell["userEnteredValue"] = {"stringValue": _term_label(months_to_term(value))}
            month_cell["userEnteredFormat"] = cell_format
            cells.append(month_cell)
        rows.append({"values": cells})
    if rows:
        requests.append({"updateCells": {
            "range": _range(worksheet, platform_row + 1, platform_row + len(desired), 0, end_column),
            "rows": rows, "fields": "userEnteredValue,userEnteredFormat",
        }})

    # Total de la plateforme (colonne D) : somme des montants du bloc.
    amount_letter = _col_letter(_DURATION_AMOUNT_COL + 1)
    total_value = (
        {"formulaValue": (
            f"=SUM({amount_letter}{platform_row + 1}:INDEX('{RATE_SHEET_NAME}'!{amount_letter}:{amount_letter};"
            f"ROW({amount_letter}{platform_row + len(desired) + 1})-1))"
        )}
        if desired else {"numberValue": 0}
    )
    requests.append({"updateCells": {
        "range": _range(worksheet, platform_row, platform_row, _DURATION_AMOUNT_COL, _DURATION_AMOUNT_COL + 1),
        "rows": [{"values": [{"userEnteredValue": total_value}]}], "fields": "userEnteredValue",
    }})

    _call_with_retry(worksheet.client.batch_update, worksheet.spreadsheet_id, {"requests": requests})
    logger.info(
        "Mise à jour de '%s' terminée pour %s : %s société(s) de prêt, %s ligne(s) de durée de base.",
        RATE_SHEET_NAME, platform, len(originators) if not platform_level else 0,
        sum(1 for row in desired if row[0] == "term"),
    )
