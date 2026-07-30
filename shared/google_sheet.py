import os
import re
import json
import logging
import time

import gspread
import requests
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

# Retry tuning for transient Google Sheets API quota errors (HTTP 429,
# e.g. "Quota exceeded for quota metric 'Read requests' ... per minute").
# Rather than letting the whole diversification run fail/exit(1) on a
# transient rate limit, every gspread network call in this module goes
# through _call_with_retry(), which waits (exponential backoff) and
# retries instead of raising immediately.
API_RATE_LIMIT_MAX_RETRIES = 5
API_RATE_LIMIT_INITIAL_WAIT_SECONDS = 30


def _is_rate_limit_error(exc: Exception) -> bool:
    """True if `exc` looks like a Google Sheets API 429 quota-exceeded error."""
    if isinstance(exc, gspread.exceptions.APIError):
        try:
            status_code = exc.response.status_code
        except AttributeError:
            status_code = None
        if status_code == 429:
            return True
        # Belt-and-braces: also match on the error body text, in case a
        # future gspread version doesn't set .response the same way.
        if "429" in str(exc) or "Quota exceeded" in str(exc) or "RESOURCE_EXHAUSTED" in str(exc):
            return True
    return False


def _is_transient_network_error(exc: Exception) -> bool:
    """True if `exc` looks like a transient (non-HTTP-status) network glitch
    - e.g. a connection reset / aborted TLS handshake while calling Google's
    API - rather than a real application-level error. gspread's requests-
    based transport raises these as requests.exceptions.ConnectionError
    (which wraps urllib3's ProtocolError/ConnectionResetError). Seen in a
    real GitHub Actions run: 'Connection aborted.',
    ConnectionResetError(104, 'Connection reset by peer').
    """
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return True
    return False


def _call_with_retry(func, *args, **kwargs):
    """Calls func(*args, **kwargs), retrying with exponential backoff
    (30s, 60s, 120s, 240s, 480s by default) whenever it fails with a
    Google Sheets API 429 "quota exceeded" error OR a transient network
    error (connection reset/aborted, timeout), instead of letting it
    propagate and fail the whole run. Any other exception (or a
    retryable error that persists after all retries) is re-raised as-is.
    """
    wait_seconds = API_RATE_LIMIT_INITIAL_WAIT_SECONDS

    for attempt in range(1, API_RATE_LIMIT_MAX_RETRIES + 2):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            retryable = _is_rate_limit_error(exc) or _is_transient_network_error(exc)
            if not retryable or attempt > API_RATE_LIMIT_MAX_RETRIES:
                raise
            logger.warning(
                "Erreur Google Sheets API transitoire (tentative %s/%s) : %s. "
                "Attente de %ss avant nouvelle tentative...",
                attempt, API_RATE_LIMIT_MAX_RETRIES, exc, wait_seconds
            )
            time.sleep(wait_seconds)
            wait_seconds *= 2


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
    spreadsheet = _call_with_retry(client.open_by_key, spreadsheet_id)

    dashboards = []

    # 1 seul appel API : spreadsheet.worksheets()
    for worksheet in _call_with_retry(spreadsheet.worksheets):
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
    grid = _call_with_retry(worksheet.get_all_values)

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

    _call_with_retry(
        worksheet.update,
        range_name,
        [[total_amount], [gross_interest_received]],
        value_input_option="USER_ENTERED"
    )

    logger.info("Mise à jour terminée pour %s", platform)


def fill_current_month_amounts_with_labels(
    platform: str, total, labeled_amounts: dict, section: str = "Crowdlending", max_rows: int = 6
):
    """Like fill_current_month_amounts(), but for a platform whose block has
    been split into several individually-labeled sub-rows instead of a
    single merged row directly below the platform (fill_current_month_amounts()
    always assumes THAT shape - it would silently write into the wrong row
    otherwise). Writes `total` directly onto the platform's own row, then
    writes each `labeled_amounts` entry (label -> amount) to its own
    dedicated sub-row found below the platform's row, using the same
    label-matching mechanism as fill_current_month_bonus_breakdown()/
    find_rows_by_texts_below() (case-insensitive substring, bounded to
    `max_rows` rows below the platform so it can never bleed into the next
    platform's block).

    Added for Mintos (2026-07-29): its block was split from a single
    "intérêts brut" row into "en cours prêts" / "en cours obligations" /
    "intérêts brut prêts" / "intérêts brut obligations".
    """
    logger.info("Début mise à jour Google Sheet (par labels) pour %s (section '%s')", platform, section)

    worksheet = get_latest_dashboard_worksheet(SPREADSHEET_ID)

    grid = _call_with_retry(worksheet.get_all_values)

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

    updates = [{"range": rowcol_to_a1(platform_row, current_month_col), "values": [[total]]}]
    logger.info("Préparation écriture : %s / total = %s", platform, total)

    labels = list(labeled_amounts.keys())
    rows_by_label = find_rows_by_texts_below(
        grid, platform_row, section_col, labels, max_rows=max_rows
    )

    missing = [label for label in labels if label not in rows_by_label]
    if missing:
        logger.warning(
            "Ligne(s) non trouvée(s) pour %s (ignorée(s), pas de valeur écrite) : %s",
            platform, missing
        )

    for label, row in rows_by_label.items():
        amount = labeled_amounts[label]
        address = rowcol_to_a1(row, current_month_col)
        updates.append({"range": address, "values": [[amount]]})
        logger.info("Préparation écriture : %s / %s = %s (%s)", platform, label, amount, address)

    _call_with_retry(worksheet.batch_update, updates, value_input_option="USER_ENTERED")

    logger.info("Mise à jour terminée pour %s (par labels)", platform)


def fill_current_month_bonus_breakdown(platform: str, breakdown: dict, section: str = "Crowdlending"):
    """Write this month's bonus/cashback/contest figures to their own
    dedicated sub-rows under a platform's block, instead of the merged
    "Bonus" row (which is a SUM formula over those sub-rows in the Sheet
    itself - deliberately never written to here).

    `breakdown` : dict mapping the exact sub-row label (case-insensitive,
    substring-matched, same convention as find_rows_by_texts_below) to the
    amount to write, e.g. {"prime": 12.3} or {"cashback": 5.0} or, for
    Bricks' differently-labelled block, {"parrainages": 1.0, "soldes
    boostés": 2.0}. Only the labels present in `breakdown` are looked up/
    written - a platform whose bonus feature maps to a single category
    (the common case) only ever touches that one row, leaving the other
    sibling rows (and "Bonus" itself) untouched.

    The search for each label is bounded to the 6 rows directly below the
    platform's own row (covers "intérêts brut" / "Bonus" / up to 3 category
    rows / "Rendements %" in every verified block layout) so it can never
    cross into the next platform's block below and misattribute a value
    (e.g. writing into a different platform's "cashback" row just because
    this platform doesn't have one).
    """
    logger.info("Début mise à jour de la répartition bonus/cashback/concours pour %s (section '%s')", platform, section)

    worksheet = get_latest_dashboard_worksheet(SPREADSHEET_ID)

    # 1 seul appel API pour charger toute la feuille
    grid = _call_with_retry(worksheet.get_all_values)

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

    labels = list(breakdown.keys())
    rows_by_label = find_rows_by_texts_below(
        grid, platform_row, section_col, labels, max_rows=6
    )

    missing = [label for label in labels if label not in rows_by_label]
    if missing:
        logger.warning(
            "Ligne(s) non trouvée(s) pour %s (ignorée(s), pas de valeur écrite) : %s",
            platform, missing
        )

    updates = []
    for label, row in rows_by_label.items():
        amount = breakdown.get(label, 0)
        address = rowcol_to_a1(row, current_month_col)
        updates.append({"range": address, "values": [[amount]]})
        logger.info("Préparation écriture : %s / %s = %s (%s)", platform, label, amount, address)

    if not updates:
        logger.warning("Aucune ligne trouvée pour %s, rien à écrire.", platform)
        return

    _call_with_retry(worksheet.batch_update, updates, value_input_option="USER_ENTERED")

    logger.info("Mise à jour de la répartition bonus/cashback/concours terminée pour %s.", platform)


def find_rows_by_texts_below(grid, start_row, start_col, texts: list, max_rows: int = None):
    """
    Cherche plusieurs textes en une seule passe (en mémoire) sous `start_row`,
    dans la colonne `start_col` (1-based). Recherche insensible à la casse,
    par sous-chaîne (comme find_first_cell_containing_below).
    Retourne un dict {texte_original: row_idx} pour les textes trouvés.

    `max_rows` : si fourni, borne la recherche aux `max_rows` lignes situées
    juste sous `start_row` (pour ne jamais déborder sur un bloc suivant qui
    contiendrait par coïncidence un texte similaire plus bas dans la
    feuille). Sans borne (comportement historique), la recherche continue
    jusqu'à la fin de la feuille.
    """
    remaining = {t.lower().strip(): t for t in texts}
    found = {}

    last_row = len(grid) if max_rows is None else min(len(grid), start_row + max_rows)

    for row_idx in range(start_row + 1, last_row + 1):
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

# Labels susceptibles de marquer la fin du bloc de sociétés de prêt d'une
# plateforme sous "Répartition géographique" - soit la ligne d'une AUTRE
# plateforme, soit un en-tête de sous-section (ex. "Crowdlending savings").
# Utilisé par fill_geographic_repartition_amounts() (paramètre `platform`)
# pour détecter automatiquement la fin du bloc d'une plateforme sans que
# chaque appelant ait besoin de préciser explicitement la borne de fin.
# Layout réel vérifié le 2026-07-30 (ordre constaté : Afranga, Iuvo,
# Lendermarket, Loanch, Mintos, Peerberry, Swaper, puis "Crowdlending
# savings" [Monefit/Go & Grow], puis "Crowdlending agricole" [Lande]) -
# mais la recherche ci-dessous ne dépend pas de cet ordre précis : elle
# prend simplement la première de ces étiquettes trouvée sous la ligne de
# la plateforme donnée, quelle que soit sa position dans cette liste.
GEO_SECTION_BOUNDARY_LABELS = [
    "Afranga", "Iuvo", "Lendermarket", "Loanch", "Mintos", "Peerberry",
    "Swaper", "Monefit", "Go & Grow", "Lande",
    "Crowdlending savings", "Crowdlending agricole", "Bourse",
]


def _find_geo_block_end_row(grid, geo_row: int, geo_col: int, platform_row: int, platform: str) -> int:
    """Retourne la ligne (1-based) qui marque la fin du bloc de sociétés de
    prêt du `platform` donné (première ligne strictement en dessous de
    `platform_row` qui n'en fait plus partie) : soit la première ligne
    correspondant à une autre étiquette de GEO_SECTION_BOUNDARY_LABELS,
    soit la 2e ligne vide consécutive (nom vide dans la colonne géo), soit
    la fin de la feuille si rien de tout ça n'est trouvé.
    """
    candidate_rows = []

    for label in GEO_SECTION_BOUNDARY_LABELS:
        if label == platform:
            continue
        row = find_first_cell_containing_below(grid, platform_row, geo_col, label)
        if row and row > platform_row:
            candidate_rows.append(row)

    blank_streak = 0
    for row_idx in range(platform_row + 1, len(grid) + 1):
        row = grid[row_idx - 1]
        name = row[geo_col - 1].strip() if geo_col - 1 < len(row) else ""
        if name:
            blank_streak = 0
            continue
        blank_streak += 1
        if blank_streak >= 2:
            candidate_rows.append(row_idx - 1)
            break

    if not candidate_rows:
        return len(grid) + 1

    return min(candidate_rows)


def _zero_fill_missing_geo_rows(grid, geo_row: int, geo_col: int, target_col: int, platform: str, written_names) -> list:
    """Pour le bloc de sociétés de prêt du `platform` donné sous
    'Répartition géographique', prépare une écriture de 0 pour chaque
    ligne ayant un nom non vide qui n'est PAS dans `written_names` (une
    société de prêt déjà listée dans le tableau mais sans investissement
    actuel ce mois-ci) - pour éviter de laisser une ancienne valeur
    périmée d'un mois précédent au lieu d'un 0 explicite.
    """
    platform_row = find_first_cell_containing_below(grid, geo_row, geo_col, platform)
    if not platform_row:
        logger.warning(
            "Zero-fill 'Répartition géographique' ignoré : la plateforme "
            "'%s' n'a pas été trouvée sous 'Répartition géographique'.",
            platform,
        )
        return []

    end_row = _find_geo_block_end_row(grid, geo_row, geo_col, platform_row, platform)

    updates = []
    for row_idx in range(platform_row + 1, end_row):
        row = grid[row_idx - 1]
        name = row[geo_col - 1].strip() if geo_col - 1 < len(row) else ""
        if not name or name in written_names:
            continue

        address = rowcol_to_a1(row_idx, target_col)
        updates.append({
            "range": address,
            "values": [[0]],
        })
        logger.info(
            "Zero-fill '%s' : '%s' n'a pas d'investissement actuel -> %s = 0",
            platform, name, address
        )

    return updates


def fill_geographic_repartition_amounts(loan_originators: list, platform: str | None = None):
    """
    loan_originators : liste de dicts, ex.
        [{"name": "Bienprêter", "amount": 1000}, {"name": "Lendix", "amount": 500}]

    Cherche la cellule "Répartition géographique", puis pour chaque loan
    originator cherche son nom sous cette cellule, dans la même colonne,
    et écrit le montant dans la cellule juste à droite (colonne + 1).

    `platform` (optionnel) : nom de la plateforme (tel qu'écrit dans la
    feuille, ex. "Peerberry") dont `loan_originators` est le relevé complet
    des sociétés de prêt actuellement investies. Si fourni, toute société
    de prêt déjà listée dans le bloc de cette plateforme mais absente de
    `loan_originators` (= plus aucun investissement actuel dessus) reçoit
    un 0 explicite, au lieu de garder sa dernière valeur écrite (qui
    pourrait dater d'un mois précédent où il y avait encore un
    investissement).
    """
    logger.info(
        "Début mise à jour Répartition géographique (%s loan originators)",
        len(loan_originators)
    )

    worksheet = get_latest_dashboard_worksheet(SPREADSHEET_ID)

    # 1 seul appel API pour charger toute la feuille
    grid = _call_with_retry(worksheet.get_all_values)

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

    zero_fill_count = 0
    if platform:
        zero_updates = _zero_fill_missing_geo_rows(grid, geo_row, geo_col, target_col, platform, set(names))
        zero_fill_count = len(zero_updates)
        updates.extend(zero_updates)

    if not updates:
        logger.warning("Aucun loan originator trouvé, rien à écrire.")
        return

    _call_with_retry(worksheet.batch_update, updates, value_input_option="USER_ENTERED")

    logger.info(
        "Mise à jour Répartition géographique terminée (%d trouvé(s), %d manquant(s), %d mis à 0).",
        len(updates) - zero_fill_count,
        len(missing),
        zero_fill_count,
    )


def _find_x_flag_left_of(row, name_col: int, max_lookback: int = 3) -> bool:
    """Retourne True si l'une des cellules jusqu'à `max_lookback` colonnes à
    gauche de la colonne (1-based) `name_col` vaut exactement "x"
    (insensible à la casse). Recherche de la plus proche à la plus
    éloignée (name_col-1, name_col-2, ...) plutôt qu'un simple
    row[name_col-2], pour rester robuste si une colonne visuelle
    supplémentaire (ex. un taux d'intérêt de référence) est un jour
    insérée entre le flag "x" et le nom du loan originator - repéré le
    2026-07-30 sur le bloc Peerberry, qui a gagné une colonne "taux
    d'intérêt" entre le flag et le nom (flag décalé de -2 à -3), alors que
    les blocs Swaper/Lendermarket n'ont pas cette colonne (flag toujours à
    -2) - cette fonction gère les deux cas sans distinction par plateforme.
    """
    for offset in range(1, max_lookback + 1):
        idx = name_col - 1 - offset
        if idx < 0:
            break
        if idx < len(row) and row[idx].strip().lower() == "x":
            return True
    return False


def get_selected_peerberry_loan_originators() -> list:
    """
    Cherche la cellule "Répartition géographique", puis la cellule
    "Peerberry" en dessous (même colonne) : les lignes entre "Peerberry" et
    la cellule "Swaper" suivante (exclues toutes les deux) sont les loan
    originators du bloc PeerBerry. Pour chacune de ces lignes ayant un nom
    de loan non vide dans la colonne "Répartition géographique", si la
    cellule juste à gauche (colonne - 1) vaut "x" (insensible à la casse),
    ce loan originator est sélectionné.

    Retourne la liste des noms de loan originators sélectionnés (tels
    qu'écrits dans la feuille, dans l'ordre des lignes).
    """
    logger.info("Recherche des loan originators PeerBerry sélectionnés (colonne -1 = 'x')")

    worksheet = get_latest_dashboard_worksheet(SPREADSHEET_ID)

    # 1 seul appel API pour charger toute la feuille
    grid = _call_with_retry(worksheet.get_all_values)

    geo_pos = find_cell_by_value(grid, "Répartition géographique")
    if not geo_pos:
        raise RuntimeError(
            "La section 'Répartition géographique' n'a pas été trouvée."
        )

    geo_row, geo_col = geo_pos

    if geo_col < 2:
        raise RuntimeError(
            "Impossible de lire la colonne à gauche des loans : "
            "'Répartition géographique' est dans la première colonne."
        )

    peerberry_row = find_first_cell_containing_below(grid, geo_row, geo_col, "Peerberry")
    if not peerberry_row:
        raise RuntimeError(
            "La cellule 'Peerberry' n'a pas été trouvée sous 'Répartition géographique'."
        )

    swaper_row = find_first_cell_containing_below(grid, peerberry_row, geo_col, "Swaper")
    if not swaper_row:
        raise RuntimeError(
            "La cellule 'Swaper' n'a pas été trouvée sous 'Peerberry' "
            "(elle délimite la fin du bloc PeerBerry)."
        )

    selected = []
    for row_idx in range(peerberry_row + 1, swaper_row):
        row = grid[row_idx - 1]

        name = row[geo_col - 1].strip() if geo_col - 1 < len(row) else ""
        if not name:
            continue

        if _find_x_flag_left_of(row, geo_col):
            selected.append(name)
            logger.info("Loan originator PeerBerry sélectionné : '%s' (ligne %s)", name, row_idx)

    logger.info("Loan originators PeerBerry sélectionnés : %s", selected)
    return selected


def get_peerberry_min_interest_rate() -> float:
    """
    Cherche la cellule "Répartition géographique", puis la ligne "Peerberry"
    en dessous (même colonne), et lit la valeur numérique (format français,
    virgule décimale, ex. "8,5") dans la cellule juste à gauche du nom
    "Peerberry" sur CETTE ligne (pas les lignes des loan originators
    en dessous, qui ont chacune leur propre valeur dans la même colonne
    visuelle - non utilisée ici). Ajoutée le 2026-07-30 pour piloter
    `minInterestRate` de peerberry_invest_bot.py depuis la feuille au lieu
    d'une valeur codée en dur.
    """
    logger.info("Lecture du minInterestRate PeerBerry depuis la cellule à gauche de 'Peerberry'")

    worksheet = get_latest_dashboard_worksheet(SPREADSHEET_ID)

    grid = _call_with_retry(worksheet.get_all_values)

    geo_pos = find_cell_by_value(grid, "Répartition géographique")
    if not geo_pos:
        raise RuntimeError(
            "La section 'Répartition géographique' n'a pas été trouvée."
        )

    geo_row, geo_col = geo_pos

    if geo_col < 2:
        raise RuntimeError(
            "Impossible de lire la colonne à gauche de 'Peerberry' : "
            "'Répartition géographique' est dans la première colonne."
        )

    peerberry_row = find_first_cell_containing_below(grid, geo_row, geo_col, "Peerberry")
    if not peerberry_row:
        raise RuntimeError(
            "La cellule 'Peerberry' n'a pas été trouvée sous 'Répartition géographique'."
        )

    row = grid[peerberry_row - 1]
    raw = row[geo_col - 2].strip() if geo_col - 2 < len(row) else ""
    if not raw:
        raise RuntimeError(
            "La cellule à gauche de 'Peerberry' est vide - impossible d'en tirer un minInterestRate."
        )

    value = float(raw.replace("\u202f", "").replace(" ", "").replace(",", "."))
    logger.info("minInterestRate PeerBerry lu dans la feuille : %s", value)
    return value


def get_selected_lendermarket_lenders() -> list:
    """
    Cherche la cellule "Répartition géographique", puis la cellule
    "Lendermarket" en dessous (même colonne) : les lignes entre
    "Lendermarket" et la cellule "Loanch" suivante (exclues toutes les
    deux) sont les lenders du bloc Lendermarket. Pour chacune de ces
    lignes ayant un nom de loan non vide dans la colonne "Répartition
    géographique", si la cellule juste à gauche (colonne - 1) vaut "x"
    (insensible à la casse), ce lender est sélectionné.

    Même logique exacte que get_selected_peerberry_loan_originators(), pour
    monitors/lendermarket_monitor.py's invest-structure exploration capture
    (ajoutée le 2026-07-23).

    Retourne la liste des noms de lenders sélectionnés (tels qu'écrits dans
    la feuille, dans l'ordre des lignes).
    """
    logger.info("Recherche des lenders Lendermarket sélectionnés (colonne -1 = 'x')")

    worksheet = get_latest_dashboard_worksheet(SPREADSHEET_ID)

    # 1 seul appel API pour charger toute la feuille
    grid = _call_with_retry(worksheet.get_all_values)

    geo_pos = find_cell_by_value(grid, "Répartition géographique")
    if not geo_pos:
        raise RuntimeError(
            "La section 'Répartition géographique' n'a pas été trouvée."
        )

    geo_row, geo_col = geo_pos

    if geo_col < 2:
        raise RuntimeError(
            "Impossible de lire la colonne à gauche des loans : "
            "'Répartition géographique' est dans la première colonne."
        )

    lendermarket_row = find_first_cell_containing_below(grid, geo_row, geo_col, "Lendermarket")
    if not lendermarket_row:
        raise RuntimeError(
            "La cellule 'Lendermarket' n'a pas été trouvée sous 'Répartition géographique'."
        )

    loanch_row = find_first_cell_containing_below(grid, lendermarket_row, geo_col, "Loanch")
    if not loanch_row:
        raise RuntimeError(
            "La cellule 'Loanch' n'a pas été trouvée sous 'Lendermarket' "
            "(elle délimite la fin du bloc Lendermarket)."
        )

    selected = []
    for row_idx in range(lendermarket_row + 1, loanch_row):
        row = grid[row_idx - 1]

        name = row[geo_col - 1].strip() if geo_col - 1 < len(row) else ""
        if not name:
            continue

        if _find_x_flag_left_of(row, geo_col):
            selected.append(name)
            logger.info("Lender Lendermarket sélectionné : '%s' (ligne %s)", name, row_idx)

    logger.info("Lenders Lendermarket sélectionnés : %s", selected)
    return selected


def get_selected_swaper_loan_originators() -> list:
    """
    Cherche la cellule "Répartition géographique", puis la cellule
    "Swaper" en dessous (même colonne) : les lignes entre "Swaper" et la
    cellule "Crowdlending savings" suivante (exclues toutes les deux) sont
    les loan originators du bloc Swaper. Pour chacune de ces lignes ayant
    un nom de loan non vide dans la colonne "Répartition géographique", si
    la cellule juste à gauche (colonne - 1) vaut "x" (insensible à la
    casse), ce loan originator est sélectionné.

    Même logique exacte que get_selected_peerberry_loan_originators() /
    get_selected_lendermarket_lenders(), pour
    monitors/swaper_monitor.py's per-originator auto-invest (ajouté le
    2026-07-25) : les noms retournés ici sont utilisés tels quels comme
    valeur du filtre "Loan originators" de swaper.com (confirmé via
    DevTools que l'API `/rest/public/loans` accepte directement le nom
    affiché dans son champ `"groups"`, ex. `"groups": ["Wandoo Finance
    Group"]` - pas un id opaque).

    Retourne la liste des noms de loan originators sélectionnés (tels
    qu'écrits dans la feuille, dans l'ordre des lignes).
    """
    logger.info("Recherche des loan originators Swaper sélectionnés (colonne -1 = 'x')")

    worksheet = get_latest_dashboard_worksheet(SPREADSHEET_ID)

    # 1 seul appel API pour charger toute la feuille
    grid = _call_with_retry(worksheet.get_all_values)

    geo_pos = find_cell_by_value(grid, "Répartition géographique")
    if not geo_pos:
        raise RuntimeError(
            "La section 'Répartition géographique' n'a pas été trouvée."
        )

    geo_row, geo_col = geo_pos

    if geo_col < 2:
        raise RuntimeError(
            "Impossible de lire la colonne à gauche des loans : "
            "'Répartition géographique' est dans la première colonne."
        )

    swaper_row = find_first_cell_containing_below(grid, geo_row, geo_col, "Swaper")
    if not swaper_row:
        raise RuntimeError(
            "La cellule 'Swaper' n'a pas été trouvée sous 'Répartition géographique'."
        )

    crowdlending_row = find_first_cell_containing_below(grid, swaper_row, geo_col, "Crowdlending savings")
    if not crowdlending_row:
        raise RuntimeError(
            "La cellule 'Crowdlending savings' n'a pas été trouvée sous 'Swaper' "
            "(elle délimite la fin du bloc Swaper)."
        )

    selected = []
    for row_idx in range(swaper_row + 1, crowdlending_row):
        row = grid[row_idx - 1]

        name = row[geo_col - 1].strip() if geo_col - 1 < len(row) else ""
        if not name:
            continue

        if _find_x_flag_left_of(row, geo_col):
            selected.append(name)
            logger.info("Loan originator Swaper sélectionné : '%s' (ligne %s)", name, row_idx)

    logger.info("Loan originators Swaper sélectionnés : %s", selected)
    return selected

if __name__ == "__main__":
    fill_current_month_amounts(
        platform="Bienprêter",
        amounts={
            "total": 1000,
            "gross_interest_received": 50
        }
    )