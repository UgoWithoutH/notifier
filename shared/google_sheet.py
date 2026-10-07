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


def _is_service_unavailable_error(exc: Exception) -> bool:
    """True if `exc` looks like a transient Google Sheets API 503 "The
    service is currently unavailable" error - a temporary server-side
    outage/overload, unrelated to our own request rate (unlike the 429
    quota case above). Seen in a real GitHub Actions run:
    `gspread.exceptions.APIError: [503]: The service is currently
    unavailable.` from `client.open_by_key()`.
    """
    if isinstance(exc, gspread.exceptions.APIError):
        try:
            status_code = exc.response.status_code
        except AttributeError:
            status_code = None
        if status_code == 503:
            return True
        if "503" in str(exc) or "currently unavailable" in str(exc):
            return True
    return False


def _call_with_retry(func, *args, **kwargs):
    """Calls func(*args, **kwargs), retrying with exponential backoff
    (30s, 60s, 120s, 240s, 480s by default) whenever it fails with a
    Google Sheets API 429 "quota exceeded" error, a 503 "service
    currently unavailable" error, OR a transient network error
    (connection reset/aborted, timeout), instead of letting it propagate
    and fail the whole run. Any other exception (or a retryable error
    that persists after all retries) is re-raised as-is.
    """
    wait_seconds = API_RATE_LIMIT_INITIAL_WAIT_SECONDS

    for attempt in range(1, API_RATE_LIMIT_MAX_RETRIES + 2):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            retryable = (
                _is_rate_limit_error(exc)
                or _is_transient_network_error(exc)
                or _is_service_unavailable_error(exc)
            )
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
    
# Per-process cache of resolved worksheets, keyed by the name passed to
# get_worksheet_by_name() - some tabs (e.g. "Répartition géographique") are
# looked up repeatedly within a single run (once per read/write function),
# and their real title never changes mid-run, so re-resolving (and
# re-logging the same case-insensitive-fallback message) every single time
# is pure waste. Cleared automatically on each new process invocation.
_worksheet_cache: dict = {}


def get_worksheet_by_name(sheet_name: str):
    """
    Retourne la feuille Google Sheets dont le nom correspond à `sheet_name`.

    Utilise SPREADSHEET_ID et les credentials configurés dans les variables
    d'environnement. Essaie d'abord une correspondance EXACTE (rapide, un
    seul appel API) ; si aucune feuille ne porte exactement ce nom, retente
    en comparant chaque feuille existante de façon insensible à la
    casse/aux espaces (ex. "répartition géographique" ou "Répartition
    Géographique" matchent aussi "Répartition géographique") - le nom réel
    d'un onglet peut différer légèrement de la constante utilisée dans le
    code (casse, espaces superflus).

    Le résultat est mis en cache (par nom de feuille) pour le reste du
    processus courant, pour éviter de refaire cette recherche (et de
    réafficher le même message de repli insensible à la casse) à chaque
    appel quand plusieurs fonctions lisent/écrivent la même feuille au
    cours d'un même run.

    Lève `gspread.exceptions.WorksheetNotFound` si aucune feuille ne
    correspond, même après ce fallback insensible à la casse.
    """
    if sheet_name in _worksheet_cache:
        return _worksheet_cache[sheet_name]

    logger.info("Recherche de la feuille Google Sheets : '%s'", sheet_name)

    credentials = get_google_credentials()
    client = gspread.authorize(credentials)

    spreadsheet = _call_with_retry(
        client.open_by_key,
        SPREADSHEET_ID
    )

    try:
        worksheet = _call_with_retry(
            spreadsheet.worksheet,
            sheet_name
        )
    except gspread.exceptions.WorksheetNotFound:
        target = sheet_name.strip().casefold()
        worksheets = _call_with_retry(spreadsheet.worksheets)
        worksheet = next(
            (ws for ws in worksheets if ws.title.strip().casefold() == target),
            None,
        )
        if worksheet is None:
            raise
        logger.info(
            "Aucune feuille nommée exactement '%s' - correspondance insensible "
            "à la casse trouvée : '%s'.", sheet_name, worksheet.title,
        )

    logger.info("Feuille sélectionnée : '%s'", worksheet.title)

    _worksheet_cache[sheet_name] = worksheet
    return worksheet


def get_latest_dashboard_worksheet(spreadsheet_id: str):
    """Picks the "Dashboard <année>" worksheet matching get_report_date()'s
    year (REPORT_DATE env var override, falls back to the real current
    date) - e.g. a backfill run with a 2025 REPORT_DATE targets "Dashboard
    2025", not whichever "Dashboard <year>" sheet happens to be newest. If
    no sheet matches that exact year (e.g. the user hasn't created it yet),
    falls back to the single most recent "Dashboard <year>" sheet found,
    with a warning - same behavior as before this function became
    year-aware."""
    target_year = get_report_date().year
    logger.info("Recherche de la feuille Dashboard pour l'année %s...", target_year)

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

    for year, worksheet in dashboards:
        if year == target_year:
            logger.info("Feuille Dashboard sélectionnée : %s", worksheet.title)
            return worksheet

    worksheet = dashboards[0][1]
    logger.warning(
        "Aucune feuille 'Dashboard %s' trouvée - utilisation de la plus récente à la place : %s",
        target_year, worksheet.title
    )
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


def _parse_french_amount(raw: str):
    """Parse un montant/pourcentage au format français d'une cellule
    (ex. '4 719,62 €', '0,00 €', '10,5', avec U+202F comme séparateur de
    milliers) en float. Retourne None si `raw` est vide/non-parsable -
    utilisé par get_peerberry_country_allocations() pour lire à la fois le
    pourcentage de seuil par pays et les montants déjà investis par pays."""
    if not raw:
        return None
    cleaned = raw.replace("\u202f", "").replace(" ", "").replace("€", "").replace(",", ".").strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
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


def fill_current_month_amounts(
    platform: str, amounts: dict, section: str = "Crowdlending", skip_total: bool = False
):
    """
    `section` : libellé de la cellule sous laquelle chercher `platform`
    (ex. "Crowdlending" pour la plupart des plateformes, "Crowdlending
    savings" pour Monefit).

    `skip_total` : si True, n'écrit PAS `amounts["total"]` sur la ligne de
    la plateforme - utilisé pour un backfill d'un mois passé (via
    REPORT_DATE) : `total` reflète le solde/montant investi ACTUEL de la
    plateforme, pas une vraie donnée historique de ce mois-là (seul
    `gross_interest_received`, calculé sur une vraie plage de dates, a un
    sens pour un mois passé).
    """
    logger.info("Début mise à jour Google Sheet pour %s (section '%s')", platform, section)

    worksheet = get_latest_dashboard_worksheet(SPREADSHEET_ID)

    # 1 seul appel API pour charger toute la feuille
    grid = _call_with_retry(worksheet.get_all_values)

    section_pos = find_cell_by_value(grid, section)
    if not section_pos:
        logger.warning("La section '%s' n'a pas été trouvée (feuille sans doute plus ancienne) - rien écrit pour %s.", section, platform)
        return

    section_row, section_col = section_pos

    current_month_cell = find_current_month_cell(grid, section_row)
    if not current_month_cell:
        logger.warning("La colonne du mois courant n'a pas été trouvée - rien écrit pour %s.", platform)
        return

    current_month_col = current_month_cell["col"]

    platform_row = find_first_cell_containing_below(
        grid, section_row, section_col, platform
    )
    if not platform_row:
        logger.warning("La plateforme '%s' n'a pas été trouvée sous '%s' - rien écrit.", platform, section)
        return

    total_amount = amounts.get("total", 0)
    gross_interest_received = amounts.get("gross_interest_received", 0)

    # "intérêts brut" used to always sit right below the platform's own
    # row (platform_row + 1) - BUG FIXÉ 2026-09-08: since 2 new rows
    # ("solde moyen pondéré investi"/"non investi") were inserted between
    # the platform's row and "intérêts brut" on the live Sheet, that fixed
    # offset now silently writes the interest figure into the wrong row.
    # Find it by label instead (falls back to the old fixed offset with a
    # warning if not found, e.g. an older Dashboard sheet without the new
    # rows). Bounded dynamically (stops at the next platform's own row,
    # see _platform_block_max_rows()) rather than a hardcoded row count.
    interest_row = find_rows_by_texts_below(
        grid, platform_row, section_col, ["intérêts brut"],
        max_rows=_platform_block_max_rows(grid, section_col, platform_row, platform),
    ).get("intérêts brut")
    if interest_row is None:
        logger.warning(
            "Ligne 'intérêts brut' non trouvée sous '%s' par recherche de libellé - "
            "utilisation de l'ancien comportement (ligne juste sous la plateforme).",
            platform,
        )
        interest_row = platform_row + 1

    if skip_total:
        address = rowcol_to_a1(interest_row, current_month_col)
        logger.info(
            "skip_total=True (mois non courant) : écriture uniquement des intérêts = %s (%s), total ignoré",
            gross_interest_received, address,
        )
        _call_with_retry(
            worksheet.update,
            address,
            [[gross_interest_received]],
            value_input_option="USER_ENTERED"
        )
        logger.info("Mise à jour terminée pour %s (%s écrit, total ignoré)", platform, address)
        return

    total_a1 = rowcol_to_a1(platform_row, current_month_col)
    interest_a1 = rowcol_to_a1(interest_row, current_month_col)

    logger.info(
        "Préparation écriture : %s / total = %s (%s), intérêts = %s (%s)",
        platform, total_amount, total_a1, gross_interest_received, interest_a1,
    )

    _call_with_retry(
        worksheet.batch_update,
        [
            {"range": total_a1, "values": [[total_amount]]},
            {"range": interest_a1, "values": [[gross_interest_received]]},
        ],
        value_input_option="USER_ENTERED"
    )

    logger.info("Mise à jour terminée pour %s (total %s, intérêts %s)", platform, total_a1, interest_a1)


def fill_current_month_amounts_with_labels(
    platform: str, total, labeled_amounts: dict, section: str = "Crowdlending",
    skip_total: bool = False,
):
    """Like fill_current_month_amounts(), but for a platform whose block has
    been split into several individually-labeled sub-rows instead of a
    single merged row directly below the platform (fill_current_month_amounts()
    always assumes THAT shape - it would silently write into the wrong row
    otherwise). Writes `total` directly onto the platform's own row, then
    writes each `labeled_amounts` entry (label -> amount) to its own
    dedicated sub-row found below the platform's row, using the same
    label-matching mechanism as fill_current_month_bonus_breakdown()/
    find_rows_by_texts_below() (case-insensitive substring, bounded
    dynamically via _platform_block_max_rows() so it stops at whichever
    comes first: the label being found, or the next platform's own row -
    no more hardcoded `max_rows` to keep in sync by hand).

    Added for Mintos (2026-07-29): its block was split from a single
    "intérêts brut" row into "en cours prêts" / "en cours obligations" /
    "intérêts brut prêts" / "intérêts brut obligations".
    """
    logger.info("Début mise à jour Google Sheet (par labels) pour %s (section '%s')", platform, section)

    worksheet = get_latest_dashboard_worksheet(SPREADSHEET_ID)

    grid = _call_with_retry(worksheet.get_all_values)

    section_pos = find_cell_by_value(grid, section)
    if not section_pos:
        logger.warning("La section '%s' n'a pas été trouvée (feuille sans doute plus ancienne) - rien écrit pour %s.", section, platform)
        return

    section_row, section_col = section_pos

    current_month_cell = find_current_month_cell(grid, section_row)
    if not current_month_cell:
        logger.warning("La colonne du mois courant n'a pas été trouvée - rien écrit pour %s.", platform)
        return

    current_month_col = current_month_cell["col"]

    platform_row = find_first_cell_containing_below(
        grid, section_row, section_col, platform
    )
    if not platform_row:
        logger.warning("La plateforme '%s' n'a pas été trouvée sous '%s' - rien écrit.", platform, section)
        return

    if skip_total:
        updates = []
        logger.info("skip_total=True (mois non courant) : total non écrit pour %s", platform)
    else:
        updates = [{"range": rowcol_to_a1(platform_row, current_month_col), "values": [[total]]}]
        logger.info("Préparation écriture : %s / total = %s", platform, total)

    labels = list(labeled_amounts.keys())
    rows_by_label = find_rows_by_texts_below(
        grid, platform_row, section_col, labels,
        max_rows=_platform_block_max_rows(grid, section_col, platform_row, platform),
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

    if not updates:
        logger.info("Rien à écrire pour %s (par labels) - aucune mise à jour envoyée", platform)
        return

    _call_with_retry(worksheet.batch_update, updates, value_input_option="USER_ENTERED")

    logger.info("Mise à jour terminée pour %s (par labels)", platform)


def fill_current_month_bonus_breakdown(platform: str, breakdown: dict, section: str = "Crowdlending"):
    """Write this month's bonus/cashback/contest figures (now a single
    "Bonus" row per platform) and the other labelled sub-rows (XIRR, Cash
    drag, ...) under a platform's block.

    `breakdown` : dict mapping the exact sub-row label (case-insensitive,
    substring-matched, same convention as find_rows_by_texts_below) to the
    amount to write, e.g. {"Bonus": 12.3}. The "Bonus" label is only
    accepted on a row whose text is exactly "Bonus" (never "XIRR Bonus"
    or similar). Only the labels present in `breakdown` are looked up/
    written.

    No more hardcoded `max_rows`: the search below the platform's own row
    is bounded dynamically via _platform_block_max_rows(), so it stops at
    whichever comes first - the label being found, or the next platform's
    own row (GEO_SECTION_BOUNDARY_LABELS) - instead of a per-platform
    magic number that needed bumping by hand every time a row was
    inserted (a recurring source of bugs, see the block layout comments
    this replaced in each *_diversification.py caller).
    """
    logger.info("Début mise à jour de la répartition bonus/cashback/concours pour %s (section '%s')", platform, section)

    worksheet = get_latest_dashboard_worksheet(SPREADSHEET_ID)

    # 1 seul appel API pour charger toute la feuille
    grid = _call_with_retry(worksheet.get_all_values)

    section_pos = find_cell_by_value(grid, section)
    if not section_pos:
        logger.warning("La section '%s' n'a pas été trouvée (feuille sans doute plus ancienne) - rien écrit pour %s.", section, platform)
        return

    section_row, section_col = section_pos

    current_month_cell = find_current_month_cell(grid, section_row)
    if not current_month_cell:
        logger.warning("La colonne du mois courant n'a pas été trouvée - rien écrit pour %s.", platform)
        return

    current_month_col = current_month_cell["col"]

    platform_row = find_first_cell_containing_below(
        grid, section_row, section_col, platform
    )
    if not platform_row:
        logger.warning("La plateforme '%s' n'a pas été trouvée sous '%s' - rien écrit.", platform, section)
        return

    labels = list(breakdown.keys())
    rows_by_label = find_rows_by_texts_below(
        grid, platform_row, section_col, labels,
        max_rows=_platform_block_max_rows(grid, section_col, platform_row, platform),
    )

    missing = [label for label in labels if label not in rows_by_label]
    for label in list(rows_by_label):
        cell_text = (grid[rows_by_label[label] - 1][section_col - 1] or "").strip().lower()
        if label.strip().lower() == "bonus" and cell_text != "bonus":
            logger.warning("Ligne 'Bonus' exacte non trouvée pour %s (trouvé '%s') - ignorée.", platform, cell_text)
            del rows_by_label[label]
            missing.append(label)
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

        # Une ligne n'est attribuée qu'à UNE clé : l'égalité exacte d'abord, sinon la clé
        # la plus longue (ex. "cash drag brut %" ne doit pas aussi capter "cash drag brut").
        # "bonus" ne doit jamais capter une ligne comme "intérêts brut avec bonus" (la vraie ligne vient plus bas).
        candidates = [key for key in remaining if key in value_lower and (key != "bonus" or value_lower == "bonus")]
        if not candidates:
            continue
        key = value_lower if value_lower in candidates else max(candidates, key=len)
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
# BUG FIXÉ 2026-08-01 : "Bienprêter" manquait de cette liste - son bloc
# (une ligne plateforme + jusqu'à 19 lignes emprunteur) se trouve entre
# Afranga et Iuvo, donc _find_geo_block_end_row() pour Afranga ne
# reconnaissait aucune ligne du bloc Bienprêter comme frontière et
# continuait jusqu'à "Iuvo" - le zero-fill d'Afranga avalait alors TOUT le
# bloc Bienprêter (sa propre ligne D + chaque emprunteur) en écrivant 0
# partout, aucun de ces noms ne correspondant jamais à un loan originator
# Afranga. Reproduit et confirmé en lecture seule (sans écriture réelle)
# via _zero_fill_missing_geo_rows().
GEO_SECTION_BOUNDARY_LABELS = [
    "Afranga", "Bienprêter", "Iuvo", "Lendermarket", "Loanch", "Mintos", "Peerberry",
    "Swaper", "Monefit", "Go & Grow", "Lande", "Bricks", "Nectaro", "Debitum",
    "Income Marketplace",
    "Crowdlending savings", "Crowdlending agricole", "Crowdfunding immobilier", "Bourse",
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


def _platform_block_max_rows(grid, col: int, platform_row: int, platform: str) -> int:
    """Dynamic replacement for a hardcoded `max_rows` value passed to
    find_rows_by_texts_below(): reuses _find_geo_block_end_row()'s boundary
    detection (stop at the next platform's own row, per
    GEO_SECTION_BOUNDARY_LABELS, or 2 consecutive blank cells, or end of
    sheet) so a label search below a platform's row in the "Crowdlending"
    section can never bleed into the next platform's block, without a
    per-platform magic number that needed bumping by hand every time a row
    was inserted."""
    end_row = _find_geo_block_end_row(grid, platform_row, col, platform_row, platform)
    return end_row - platform_row - 1


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
        # La ligne "non investi" (ajoutée 2026-08-10) n'est pas une société
        # de prêt - elle est alimentée séparément par
        # fill_geographic_repartition_uninvested_amount(), jamais mise à 0
        # ici (elle ne fait pas partie de `written_names` puisque ce n'est
        # pas un loan originator).
        if not name or name in written_names or name.lower() == "non investi":
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


def fill_geographic_repartition_uninvested_amount(platform: str, amount):
    """Écrit `amount` (le solde non investi/disponible de `platform`,
    c'est-à-dire l'argent déposé mais pas encore prêté) sur la ligne
    "non investi" trouvée juste sous la ligne de `platform`, dans la
    section "Répartition géographique" (colonne total, geo_col + 1 - même
    colonne que fill_geographic_repartition_amounts()).

    Ajoutée 2026-08-10 : l'utilisateur a inséré une ligne "non investi"
    sous chaque plateforme de cette section (sauf Monefit et Go & Grow,
    qui n'ont pas de bloc de sociétés de prêt/pays et ne sont donc jamais
    passées à cette fonction). Recherche bornée à 3 lignes sous la ligne
    de la plateforme (le layout réel a toujours "non investi" en tout
    premier, juste en dessous) pour ne jamais confondre avec une société
    de prêt qui porterait un nom similaire plus bas dans le bloc.
    """
    logger.info("Début mise à jour 'non investi' pour %s (%s)", platform, amount)

    worksheet = get_worksheet_by_name("Répartition géographique")
    grid = _call_with_retry(worksheet.get_all_values)

    geo_pos = find_cell_by_value(grid, "Répartition géographique")
    if not geo_pos:
        logger.warning("Section 'Répartition géographique' non trouvée - 'non investi' non écrit pour %s.", platform)
        return
    geo_row, geo_col = geo_pos

    platform_row = find_first_cell_containing_below(grid, geo_row, geo_col, platform)
    if not platform_row:
        logger.warning("Plateforme '%s' non trouvée sous 'Répartition géographique' - 'non investi' non écrit.", platform)
        return

    uninvested_row = find_rows_by_texts_below(
        grid, platform_row, geo_col, ["non investi"],
        max_rows=_platform_block_max_rows(grid, geo_col, platform_row, platform),
    ).get("non investi")
    if not uninvested_row:
        logger.warning(
            "Ligne 'non investi' non trouvée sous '%s' - rien n'a été écrit.", platform
        )
        return

    address = rowcol_to_a1(uninvested_row, geo_col + 1)
    _call_with_retry(worksheet.update, address, [[amount]], value_input_option="USER_ENTERED")
    logger.info("Mise à jour 'non investi' terminée pour %s : %s = %s", platform, address, amount)


def get_geographic_repartition_uninvested_amounts(platforms: list) -> dict:
    """Relit (une seule lecture de la feuille) la ligne "non investi" déjà
    écrite par fill_geographic_repartition_uninvested_amount() pour chaque
    plateforme de `platforms`, sous "Répartition géographique" - utilisé par
    le mail récapitulatif de fin de workflow GitHub (send_diversification_
    recap_email.py) pour lister les montants non investis de toutes les
    plateformes déjà passées ce run.

    Retourne {platform: float} pour chaque plateforme dont la ligne "non
    investi" a été trouvée et parsée avec succès ; les plateformes absentes
    (ligne non trouvée, ou cellule vide/non-parsable) sont simplement omises
    du dict.
    """
    worksheet = get_worksheet_by_name("Répartition géographique")
    grid = _call_with_retry(worksheet.get_all_values)

    geo_pos = find_cell_by_value(grid, "Répartition géographique")
    if not geo_pos:
        raise RuntimeError("La section 'Répartition géographique' n'a pas été trouvée.")
    geo_row, geo_col = geo_pos

    amounts = {}
    for platform in platforms:
        platform_row = find_first_cell_containing_below(grid, geo_row, geo_col, platform)
        if not platform_row:
            logger.warning("Plateforme '%s' non trouvée sous 'Répartition géographique'.", platform)
            continue

        uninvested_row = find_rows_by_texts_below(
            grid, platform_row, geo_col, ["non investi"],
            max_rows=_platform_block_max_rows(grid, geo_col, platform_row, platform),
        ).get("non investi")
        if not uninvested_row:
            logger.warning("Ligne 'non investi' non trouvée sous '%s'.", platform)
            continue

        row = grid[uninvested_row - 1]
        raw = row[geo_col] if geo_col < len(row) else ""
        amount = _parse_french_amount(raw)
        if amount is None:
            logger.warning("Montant 'non investi' de '%s' vide/non-parsable (%r).", platform, raw)
            continue

        amounts[platform] = amount

    return amounts


def _insert_missing_geo_rows(worksheet, grid, geo_row, geo_col, target_col, platform: str, missing_loan_originators: list) -> int:
    """Pour le bloc de sociétés de prêt du `platform` donné sous
    'Répartition géographique', INSÈRE une nouvelle ligne (nom + montant, à
    la toute fin du bloc de cette plateforme, juste avant la ligne de la
    plateforme suivante) pour chaque loan originator de
    `missing_loan_originators` (des dicts {"name", "amount"}) qui n'a pas de
    ligne existante dans le bloc - ajoutée 2026-09-08, même principe que
    l'insertion de nouvel emprunteur de fill_bienpreter_borrower_geo_amounts()
    mais pour le cas générique (une seule colonne total, pas de matrice
    pays x emprunteur). Le nom de la ligne insérée est explicitement
    formaté (aligné à droite, police taille 9, non gras) pour matcher le
    style des lignes voisines, une ligne insérée via insert_rows() hérite
    sinon par défaut du style de la ligne de plateforme suivante (gras,
    aligné à gauche).

    Retourne le nombre de lignes réellement insérées (0 si
    `missing_loan_originators` est vide ou si la ligne de `platform` elle-même
    est introuvable).
    """
    if not missing_loan_originators:
        return 0

    platform_row = find_first_cell_containing_below(grid, geo_row, geo_col, platform)
    if not platform_row:
        logger.warning(
            "Insertion de ligne(s) manquante(s) ignorée : la plateforme '%s' n'a pas été trouvée sous 'Répartition géographique'.",
            platform,
        )
        return 0

    end_row = _find_geo_block_end_row(grid, geo_row, geo_col, platform_row, platform)

    name_format = {
        "horizontalAlignment": "RIGHT",
        "textFormat": {"fontSize": 9, "bold": False},
    }
    name_cells_to_restyle = []

    insert_row = end_row
    for lo in missing_loan_originators:
        name = lo["name"]
        amount = lo.get("amount", 0)
        row_length = max(geo_col, target_col)
        row_values = [""] * row_length
        row_values[geo_col - 1] = name
        row_values[target_col - 1] = amount

        logger.info(
            "Insertion d'une nouvelle ligne '%s' (plateforme '%s') à la ligne %s, montant=%s",
            name, platform, insert_row, amount,
        )
        _call_with_retry(worksheet.insert_rows, [row_values], insert_row, value_input_option="USER_ENTERED")
        name_cells_to_restyle.append(rowcol_to_a1(insert_row, geo_col))
        insert_row += 1

    if name_cells_to_restyle:
        _call_with_retry(worksheet.format, name_cells_to_restyle, name_format)

    return len(missing_loan_originators)


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
    investissement). Réciproquement (ajouté 2026-09-08), toute société de
    prêt de `loan_originators` qui n'a AUCUNE ligne existante dans ce bloc
    (nouveau loan originator jamais vu) est INSÉRÉE en nouvelle ligne, tout
    à la fin du bloc de cette plateforme (juste avant la ligne de la
    plateforme suivante) - voir _insert_missing_geo_rows().
    """
    logger.info(
        "Début mise à jour Répartition géographique (%s loan originators)",
        len(loan_originators)
    )

    worksheet = get_worksheet_by_name("Répartition géographique")

    # 1 seul appel API pour charger toute la feuille
    grid = _call_with_retry(worksheet.get_all_values)

    geo_pos = find_cell_by_value(grid, "Répartition géographique")

    if not geo_pos:
        logger.warning(
            "La section 'Répartition géographique' n'a pas été trouvée (feuille sans doute plus ancienne) - rien écrit."
        )
        return

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

    if updates:
        _call_with_retry(worksheet.batch_update, updates, value_input_option="USER_ENTERED")

    inserted_count = 0
    if platform and missing:
        missing_loan_originators = [lo for lo in loan_originators if lo["name"] in missing]
        inserted_count = _insert_missing_geo_rows(
            worksheet, grid, geo_row, geo_col, target_col, platform, missing_loan_originators
        )

    if not updates and not inserted_count:
        logger.warning("Aucun loan originator trouvé, rien à écrire.")
        return

    logger.info(
        "Mise à jour Répartition géographique terminée (%d trouvé(s), %d manquant(s), %d mis à 0, %d ajouté(s) en nouvelle ligne).",
        len(updates) - zero_fill_count,
        len(missing),
        zero_fill_count,
        inserted_count,
    )


def _col_letter(col_idx: int) -> str:
    """Retourne juste la partie lettre(s) d'une colonne 1-based (ex. 5 -> 'E')."""
    return re.sub(r"\d+", "", rowcol_to_a1(1, col_idx))


def _normalize_borrower_name(name: str) -> str:
    return " ".join(name.strip().lower().split())


LANDE_STATUS_ORDER = ("current", "5-30", "31-60", "60", "default")
LANDE_STATUS_HEADER_LABELS = {
    "5-30": "5-30 jours de retard",
    "31-60": "31-60 jours de retard",
    "60": "60+ jours de retard",
    "default": "en défaut",
}
# Style relevé sur les lignes d'en-tête existantes de la feuille (identique à la ligne "non investi").
STATUS_HEADER_STYLE = {
    "horizontalAlignment": "LEFT",
    "textFormat": {"fontFamily": "Arial", "fontSize": 9, "italic": True, "bold": False},
    "backgroundColor": {"red": 0.9372549, "green": 0.9372549, "blue": 0.9372549},
}


def _lande_status_of_header(name: str):
    """Statut Lande correspondant à une ligne d'en-tête de section, ou None."""
    n = name.strip().casefold()
    if "défaut" in n or "defaut" in n:
        return "default"
    if "60+" in n or "+60" in n:
        return "60"
    if "31-60" in n:
        return "31-60"
    if "5-30" in n:
        return "5-30"
    return None


def fill_platform_loan_geo_amounts(
    platform: str, loan_amounts: dict, loan_statuses, status_order: tuple,
    status_header_labels: dict, header_status_of,
) -> list:
    """Met à jour les lignes de prêts/projets d'une plateforme (Lande, Bricks...)
    dans la matrice pays de "Répartition géographique". ``loan_amounts`` est
    indexé par identifiant de prêt, puis par pays : ``{loan_id: {country: remaining_amount}}``.
    ``status_order`` (le 1er = statut sain, sans en-tête), ``status_header_labels`` et
    ``header_status_of(nom_ligne)`` décrivent les lignes d'en-tête de statut propres à la plateforme.
    Les prêts absents du relevé actif sont supprimés; la ligne plateforme est
    réécrite en formules de somme de ses sous-lignes (comme Bienprêter) et la
    ligne ``non investi`` reste gérée par sa fonction dédiée.
    ``loan_statuses`` (``{loan_id: "current"|"5-30"|"31-60"|"60"|"default"}``)
    range chaque prêt sous la ligne d'en-tête de son statut ("5-30 jours de
    retard", "31-60 jours de retard", "60+ jours de retard", "en défaut", dans
    cet ordre sous "non investi") ; les prêts sains restent au-dessus du premier
    en-tête. Un en-tête est créé s'il a au moins un prêt, supprimé sinon.
    """
    logger.info("Début mise à jour Répartition géographique / %s (%d prêt(s))", platform, len(loan_amounts))
    issues = []
    worksheet = get_worksheet_by_name("Répartition géographique")
    grid = _call_with_retry(worksheet.get_all_values)

    geo_pos = find_cell_by_value(grid, "Répartition géographique")
    if not geo_pos:
        message = f"Section 'Répartition géographique' non trouvée - lignes de prêts {platform} non mises à jour."
        logger.warning(message)
        return [message]
    geo_row, geo_col = geo_pos

    platform_row = find_first_cell_containing_below(grid, geo_row, geo_col, platform)
    if not platform_row:
        message = f"Ligne '{platform}' non trouvée sous 'Répartition géographique'."
        logger.warning(message)
        return [message]

    end_row = _find_geo_block_end_row(grid, geo_row, geo_col, platform_row, platform)
    header_row = grid[geo_row - 1]
    country_columns = {
        header_row[col_idx - 1].strip().casefold(): col_idx
        for col_idx in range(geo_col + 2, len(header_row) + 1)
        if header_row[col_idx - 1].strip()
    }
    if not country_columns:
        raise RuntimeError(f"Aucune colonne pays trouvée pour la répartition géographique {platform}.")

    first_country_letter = _col_letter(min(country_columns.values()))
    target_col = geo_col + 1
    normalized_amounts = {
        _normalize_borrower_name(str(loan_id)): countries
        for loan_id, countries in loan_amounts.items()
    }

    # Ligne plateforme = sommes de ses sous-lignes (comme Bienprêter) ; la borne basse suit les insertions/suppressions.
    platform_updates = []
    for col_idx in [target_col, *country_columns.values()]:
        letter = _col_letter(col_idx)
        platform_updates.append({
            "range": rowcol_to_a1(platform_row, col_idx),
            "values": [[f"=SOMME({letter}{platform_row + 1}:INDEX({letter}:{letter};ROW({letter}{end_row})-1))"]],
        })
    statuses = {_normalize_borrower_name(str(loan_id)): status for loan_id, status in (loan_statuses or {}).items()}
    status_headers = {}  # statut -> numéro de ligne de l'en-tête de section
    for row_idx in range(platform_row + 1, end_row):
        row = grid[row_idx - 1]
        name = row[geo_col - 1].strip() if geo_col - 1 < len(row) else ""
        header_status = header_status_of(name) if name else None
        if header_status and header_status not in status_headers:
            status_headers[header_status] = row_idx
    header_status_by_row = {row_idx: status for status, row_idx in status_headers.items()}

    def section_of(normalized_id: str):
        """Statut de l'en-tête sous lequel ranger le prêt (None = prêt sain, avant le premier en-tête)."""
        status = statuses.get(normalized_id)
        return status if status in status_header_labels else None

    needed_headers = {section_of(normalized) for normalized in normalized_amounts} - {None}

    rows_to_delete = []
    existing_rows = {}
    current_section = None
    for row_idx in range(platform_row + 1, end_row):
        row = grid[row_idx - 1]
        name = row[geo_col - 1].strip() if geo_col - 1 < len(row) else ""
        if row_idx in header_status_by_row:
            current_section = header_status_by_row[row_idx]
            if current_section not in needed_headers:
                rows_to_delete.append(row_idx)
            continue
        if not name or name.casefold() == "non investi":
            continue
        normalized = _normalize_borrower_name(name)
        if normalized not in normalized_amounts or section_of(normalized) != current_section:
            rows_to_delete.append(row_idx)
            continue
        existing_rows.setdefault(normalized, row_idx)

    updates = list(platform_updates)
    rows_to_restyle = []
    pending_new_loans = []
    for loan_id, country_amounts in loan_amounts.items():
        normalized_id = _normalize_borrower_name(str(loan_id))
        resolved_amounts = {}
        for country, amount in country_amounts.items():
            country = (country or "").strip()
            country_col = country_columns.get(country.casefold()) if country else None
            if country_col is None:
                message = f"Pays {platform} '{country or 'inconnu'}' introuvable pour le prêt {loan_id}; montant non écrit."
                logger.warning(message)
                issues.append(message)
                continue
            resolved_amounts[country_col] = resolved_amounts.get(country_col, 0.0) + amount

        row_idx = existing_rows.get(normalized_id)
        if row_idx is None:
            pending_new_loans.append((str(loan_id), resolved_amounts, section_of(normalized_id)))
            continue

        rows_to_restyle.append(rowcol_to_a1(row_idx, geo_col))
        row = grid[row_idx - 1]
        filled_country_cols = {
            col_idx for col_idx in country_columns.values()
            if col_idx - 1 < len(row) and row[col_idx - 1].strip()
        }
        for country_col in filled_country_cols | set(resolved_amounts):
            address = rowcol_to_a1(row_idx, country_col)
            updates.append({"range": address, "values": [[resolved_amounts.get(country_col, 0.0)]]})
        formula = f"=SOMME({first_country_letter}{row_idx}:{row_idx})"
        updates.append({"range": rowcol_to_a1(row_idx, target_col), "values": [[formula]]})

    if updates:
        _call_with_retry(worksheet.batch_update, updates, value_input_option="USER_ENTERED")

    name_style = {"horizontalAlignment": "RIGHT", "textFormat": {"fontSize": 9, "bold": False, "italic": False}}
    if rows_to_restyle:
        _call_with_retry(worksheet.format, rows_to_restyle, name_style)

    # Suppressions avant insertions : les index des lignes à supprimer sont ceux de la grille lue.
    for row_idx in sorted(rows_to_delete, reverse=True):
        logger.info("Suppression de la ligne %s obsolète %s.", platform, row_idx)
        _call_with_retry(worksheet.delete_rows, row_idx, row_idx)
        for status, header_row_idx in list(status_headers.items()):
            if header_row_idx == row_idx:
                del status_headers[status]
            elif header_row_idx > row_idx:
                status_headers[status] = header_row_idx - 1
        end_row -= 1

    # En-têtes manquants créés dans l'ordre canonique, chacun juste avant l'en-tête suivant existant (ou en fin de bloc).
    created_headers = 0
    for status in status_order[1:]:
        if status not in needed_headers or status in status_headers:
            continue
        rank = status_order.index(status)
        later_headers = [r for s, r in status_headers.items() if status_order.index(s) > rank]
        insert_row = min(later_headers) if later_headers else end_row
        row_values = [""] * geo_col
        row_values[geo_col - 1] = status_header_labels[status]
        _call_with_retry(
            worksheet.insert_rows, [row_values], insert_row,
            value_input_option="USER_ENTERED", inherit_from_before=True,
        )
        _call_with_retry(worksheet.format, rowcol_to_a1(insert_row, geo_col), STATUS_HEADER_STYLE)
        for other_status, header_row_idx in status_headers.items():
            if header_row_idx >= insert_row:
                status_headers[other_status] = header_row_idx + 1
        status_headers[status] = insert_row
        end_row += 1
        created_headers += 1
        logger.info("Ajout de la ligne %s '%s' en %s.", platform, status_header_labels[status], insert_row)

    def section_rank(section):
        return -1 if section is None else status_order.index(section)

    new_rows_to_restyle = []
    for loan_id, resolved_amounts, section in sorted(pending_new_loans, key=lambda loan: section_rank(loan[2])):
        section_start = status_headers[section] if section else platform_row
        later_headers = [r for r in status_headers.values() if r > section_start]
        insert_row = min(later_headers) if later_headers else end_row
        row_length = max([geo_col, target_col] + list(country_columns.values()))
        row_values = [""] * row_length
        row_values[geo_col - 1] = loan_id
        row_values[target_col - 1] = f"=SOMME({first_country_letter}{insert_row}:{insert_row})"
        for country_col, amount in resolved_amounts.items():
            row_values[country_col - 1] = amount
        # Hérite du format de la ligne au-dessus, jamais de celle de la plateforme suivante (jaune).
        _call_with_retry(
            worksheet.insert_rows, [row_values], insert_row,
            value_input_option="USER_ENTERED", inherit_from_before=True,
        )
        new_rows_to_restyle.append(rowcol_to_a1(insert_row, geo_col))
        for status, header_row_idx in status_headers.items():
            if header_row_idx >= insert_row:
                status_headers[status] = header_row_idx + 1
        end_row += 1

    if new_rows_to_restyle:
        _call_with_retry(worksheet.format, new_rows_to_restyle, name_style)

    logger.info(
        "Mise à jour géographique %s terminée (%d existant(s), %d ajouté(s), %d supprimé(s), %d en-tête(s) créé(s)).",
        platform, len(existing_rows), len(pending_new_loans), len(rows_to_delete), created_headers,
    )
    return issues


def fill_lande_loan_geo_amounts(loan_amounts: dict, loan_statuses=None) -> list:
    """Lignes de prêts Lande : voir fill_platform_loan_geo_amounts()."""
    return fill_platform_loan_geo_amounts(
        "Lande", loan_amounts, loan_statuses, LANDE_STATUS_ORDER, LANDE_STATUS_HEADER_LABELS, _lande_status_of_header,
    )


BRICKS_STATUS_ORDER = ("current", "delay", "default")
BRICKS_STATUS_HEADER_LABELS = {"delay": "en retard", "default": "en défaut"}


def _bricks_status_of_header(name: str):
    """Statut Bricks correspondant à une ligne d'en-tête de section, ou None."""
    n = name.strip().casefold()
    if "défaut" in n or "defaut" in n:
        return "default"
    if "retard" in n:
        return "delay"
    return None


def fill_bricks_project_geo_amounts(project_amounts: dict, project_statuses=None) -> list:
    """Lignes de projets Bricks : voir fill_platform_loan_geo_amounts().
    ``project_amounts`` = ``{nom_projet: {pays: capital restant}}``,
    ``project_statuses`` = ``{nom_projet: "current"|"delay"|"default"}``.
    La ligne "Bricks" devient la somme de ses sous-lignes ("non investi" incluse) : l'en-tête de
    section "Crowdfunding immobilier" est donc réécrit en ``=C<ligne Bricks>`` pour ne rien compter deux fois.
    """
    issues = fill_platform_loan_geo_amounts(
        "Bricks", project_amounts, project_statuses, BRICKS_STATUS_ORDER, BRICKS_STATUS_HEADER_LABELS, _bricks_status_of_header,
    )
    worksheet = get_worksheet_by_name("Répartition géographique")
    grid = _call_with_retry(worksheet.get_all_values)
    geo_pos = find_cell_by_value(grid, "Répartition géographique")
    section_row = find_first_cell_containing_below(grid, geo_pos[0], geo_pos[1], "Crowdfunding immobilier") if geo_pos else None
    platform_row = find_first_cell_containing_below(grid, geo_pos[0], geo_pos[1], "Bricks") if geo_pos else None
    if section_row and platform_row and section_row < platform_row:
        letter = _col_letter(geo_pos[1] + 1)
        _call_with_retry(
            worksheet.update, rowcol_to_a1(section_row, geo_pos[1] + 1), [[f"={letter}{platform_row}"]],
            value_input_option="USER_ENTERED",
        )
    return issues


def fill_bienpreter_borrower_geo_amounts(borrowers: dict):
    """
    borrowers : {nom_emprunteur: {nom_pays: montant}} - les prêts Bienprêter
    actuellement en cours (statut "en remboursement"), regroupés/sommés par
    (emprunteur, pays) (ex. "EXCAVAN": {"Espagne": 300.0}). Un emprunteur
    ayant des prêts dans PLUSIEURS pays a simplement plusieurs clés - son
    montant est alors réparti sur les colonnes pays correspondantes de sa
    ligne, une par pays (FIXÉ 2026-09-08 : l'ancien comportement ne gardait
    que le premier pays trouvé et jetait silencieusement le reste,
    faussant le total réel - cas réel "ROMRADIATOARE", Roumanie + Pays Bas).

    Contrairement à fill_geographic_repartition_amounts() (un seul montant
    juste à droite du nom), le bloc "Bienprêter" de "Répartition
    géographique" est une VRAIE matrice pays x emprunteur : une colonne par
    pays (en-têtes sur la ligne "Répartition géographique" elle-même, à
    partir de geo_col+2 - la colonne juste à droite du nom, geo_col+1, est
    une colonne "total" séparée, sans en-tête pays). Pour chaque emprunteur :
    - Si une ligne du bloc porte déjà ce nom (recherche insensible à la
      casse), le montant de CHAQUE pays est écrit dans la colonne
      correspondante sur cette ligne ; toute colonne pays déjà remplie sur
      cette ligne mais absente du relevé actuel (le prêt dans ce pays n'est
      plus en cours) est remise à 0, per explicit user request - le montant
      ne doit pas rester affiché s'il n'existe plus réellement.
    - Sinon, une nouvelle ligne est INSÉRÉE juste avant la ligne de la
      plateforme suivante (donc juste après le dernier emprunteur du bloc
      Bienprêter), avec le nom + le(s) montant(s) dans la/les bonne(s)
      colonne(s) pays - son nom est explicitement formaté (aligné à droite,
      police taille 9, non gras) pour matcher le style des autres lignes
      emprunteur, car une ligne insérée hérite par défaut du style de la
      ligne d'en-tête de plateforme suivante (gras, aligné à gauche), pas
      de ses voisines.
    - Toute ligne du bloc dont le nom n'apparaît PLUS dans `borrowers` (plus
      aucun prêt en cours pour cet emprunteur, dans AUCUN pays) est
      SUPPRIMÉE.

    Par ailleurs, la colonne "total" (geo_col+1, juste à droite du nom) de
    TOUTES les lignes du bloc restantes (pas seulement celles mises à jour
    ce run) est réécrite comme une formule vivante
    "=SOMME(<1re colonne pays><ligne>:<ligne>)" (ex. "=SOMME(E395:395)") -
    cette colonne ne contenait auparavant qu'un "0,00 €" statique qui ne
    reflétait jamais les montants par pays déjà présents sur la ligne.

    IMPORTANT : la ligne "Bienprêter" elle-même (son propre total en
    geo_col+1) n'est JAMAIS touchée ici - explicitement exclue à la
    demande de l'utilisateur (ce solde est maintenu manuellement).

    Retourne une liste de courtes descriptions de problèmes rencontrés
    (pays introuvable comme en-tête de colonne, pays inconnu pour un
    emprunteur) - liste vide si tout s'est bien passé. Utilisée par
    bienpreter_diversification.py pour décider d'envoyer un email
    d'alerte (ne lève jamais d'exception pour ces cas-là, seulement pour
    une vraie erreur bloquante, ex. section/plateforme introuvable).
    """
    logger.info(
        "Début mise à jour Répartition géographique / Bienprêter (%d emprunteur(s))",
        len(borrowers),
    )

    issues = []

    worksheet = get_worksheet_by_name("Répartition géographique")
    grid = _call_with_retry(worksheet.get_all_values)

    geo_pos = find_cell_by_value(grid, "Répartition géographique")
    if not geo_pos:
        message = "La section 'Répartition géographique' n'a pas été trouvée (feuille sans doute plus ancienne) - répartition par emprunteur non écrite."
        logger.warning(message)
        issues.append(message)
        return issues
    geo_row, geo_col = geo_pos

    bienpreter_row = find_first_cell_containing_below(grid, geo_row, geo_col, "Bienprêter")
    if not bienpreter_row:
        message = "La cellule 'Bienprêter' n'a pas été trouvée sous 'Répartition géographique' - répartition par emprunteur non écrite."
        logger.warning(message)
        issues.append(message)
        return issues

    end_row = _find_geo_block_end_row(grid, geo_row, geo_col, bienpreter_row, "Bienprêter")

    header_row = grid[geo_row - 1]
    country_columns = {
        header_row[col_idx - 1].strip().lower(): col_idx
        for col_idx in range(geo_col + 1, len(header_row) + 1)
        if header_row[col_idx - 1].strip()
    }
    if not country_columns:
        raise RuntimeError("Aucune colonne pays trouvée à droite de 'Répartition géographique'.")

    target_col = geo_col + 1
    first_country_col = min(country_columns.values())
    first_country_letter = _col_letter(first_country_col)

    active_names = {_normalize_borrower_name(name) for name in borrowers}

    # Toutes les lignes du bloc (SANS dédoublonner par nom, contrairement à
    # `existing_rows` ci-dessous) - sert à décider quelles lignes supprimer :
    # une ligne dont le nom n'est plus dans `borrowers` est supprimée, même
    # si un doublon du même nom existe ailleurs dans le bloc.
    #
    # BUG FIXÉ 2026-08-10 : la ligne "non investi" (ajoutée le jour même,
    # juste sous la ligne "Bienprêter") n'est PAS un emprunteur, mais son
    # nom n'est jamais vide - sans l'exclusion ci-dessous, elle finissait
    # systématiquement dans `rows_to_delete` (aucun emprunteur ne s'appelle
    # "non investi") et était supprimée à chaque run, avant même que
    # fill_geographic_repartition_uninvested_amount() ait pu y écrire quoi
    # que ce soit - d'où un "non investi" introuvable/jamais rempli pour
    # Bienprêter dans le mail récapitulatif.
    rows_to_delete = []
    existing_rows = {}
    for row_idx in range(bienpreter_row + 1, end_row):
        row = grid[row_idx - 1]
        name = row[geo_col - 1].strip() if geo_col - 1 < len(row) else ""
        if not name or name.lower() == "non investi":
            continue
        normalized = _normalize_borrower_name(name)
        existing_rows.setdefault(normalized, row_idx)
        if normalized not in active_names:
            rows_to_delete.append(row_idx)

    updates = []

    logger.info(
        "Ligne %s ('%s' elle-même) ne sera PAS écrite - seules les lignes %s à %s le seront.",
        bienpreter_row, "Bienprêter", bienpreter_row + 1, end_row - 1,
    )

    # Réécrit la formule SOMME sur les lignes du bloc qui restent (pas
    # celles sur le point d'être supprimées, ni la ligne "non investi" -
    # même exclusion/raison que ci-dessus : ce n'est pas une ligne
    # emprunteur, sa colonne total (target_col) est alimentée séparément
    # par fill_geographic_repartition_uninvested_amount()).
    for row_idx in range(bienpreter_row + 1, end_row):
        if row_idx in rows_to_delete:
            continue
        row = grid[row_idx - 1]
        name = row[geo_col - 1].strip() if geo_col - 1 < len(row) else ""
        if name.lower() == "non investi":
            continue
        formula = f"=SOMME({first_country_letter}{row_idx}:{row_idx})"
        address = rowcol_to_a1(row_idx, target_col)
        updates.append({"range": address, "values": [[formula]]})
        logger.info("Préparation écriture formule : %s = %s", address, formula)

    pending_new_borrowers = []
    name_cells_to_restyle = []

    for name, country_amounts in borrowers.items():
        # {country_col: summed_amount} for every country actually resolvable
        # to a real column - a borrower with loans in several countries ends
        # up with several entries here instead of just one.
        resolved_country_cols = {}
        for country, amount in country_amounts.items():
            country = (country or "").strip()
            country_col = country_columns.get(country.lower()) if country else None
            if country and country_col is None:
                message = f"Pays '{country}' (emprunteur '{name}') introuvable comme en-tête de colonne - montant non écrit."
                logger.warning(message)
                issues.append(message)
            elif not country:
                message = f"Emprunteur '{name}' : pays inconnu pour {amount:.2f} EUR - montant non écrit (ligne quand même créée/mise à jour)."
                logger.warning(message)
                issues.append(message)
            if country_col is not None:
                resolved_country_cols[country_col] = resolved_country_cols.get(country_col, 0.0) + amount

        row_idx = existing_rows.get(_normalize_borrower_name(name))
        if row_idx is not None:
            name_cells_to_restyle.append(rowcol_to_a1(row_idx, geo_col))
            # Also clear any country column this row already has a value in
            # but that isn't part of this run's resolved countries anymore
            # (that country's loan is no longer active) - otherwise a stale
            # amount would keep being counted in the row's SOMME total.
            row = grid[row_idx - 1]
            currently_filled_cols = {
                col_idx for col_idx in country_columns.values()
                if col_idx - 1 < len(row) and row[col_idx - 1].strip()
            }
            for country_col in currently_filled_cols | set(resolved_country_cols):
                amount = resolved_country_cols.get(country_col, 0)
                address = rowcol_to_a1(row_idx, country_col)
                updates.append({"range": address, "values": [[amount]]})
                logger.info("Préparation écriture : %s (ligne %s) / colonne %s = %s (%s)", name, row_idx, country_col, amount, address)
        else:
            pending_new_borrowers.append((name, resolved_country_cols))

    if updates:
        _call_with_retry(worksheet.batch_update, updates, value_input_option="USER_ENTERED")

    # Style des lignes emprunteur (confirmé en direct le 2026-07-31 sur des
    # lignes déjà correctes, ex. 374/392) : nom aligné à droite, police
    # taille 9, non gras - réappliqué explicitement à CHAQUE emprunteur
    # touché ce run (existant ou nouveau), pas seulement les nouvelles
    # lignes : une ligne INSÉRÉE via insert_rows() hérite par défaut du
    # style de la ligne d'en-tête de plateforme suivante (gras, aligné à
    # gauche), pas de ses voisines emprunteur - un ancien run l'a déjà vécu
    # (ligne 'SAMAG'), d'où le fait de corriger aussi les lignes existantes.
    borrower_name_format = {
        "horizontalAlignment": "RIGHT",
        "textFormat": {"fontSize": 9, "bold": False},
    }

    insert_row = end_row
    for name, resolved_country_cols in pending_new_borrowers:
        row_length = max([geo_col, target_col] + list(resolved_country_cols))
        row_values = [""] * row_length
        row_values[geo_col - 1] = name
        row_values[target_col - 1] = f"=SOMME({first_country_letter}{insert_row}:{insert_row})"
        for country_col, amount in resolved_country_cols.items():
            row_values[country_col - 1] = amount

        logger.info(
            "Insertion d'une nouvelle ligne emprunteur '%s' à la ligne %s (colonnes pays=%s)",
            name, insert_row, resolved_country_cols,
        )
        _call_with_retry(worksheet.insert_rows, [row_values], insert_row, value_input_option="USER_ENTERED")
        name_cells_to_restyle.append(rowcol_to_a1(insert_row, geo_col))
        insert_row += 1

    if name_cells_to_restyle:
        _call_with_retry(worksheet.format, name_cells_to_restyle, borrower_name_format)

    # Suppression des lignes emprunteur devenues obsolètes - de la plus
    # basse à la plus haute pour ne jamais invalider l'index d'une ligne
    # encore à supprimer (supprimer une ligne ne décale que celles EN
    # DESSOUS, jamais celles au-dessus).
    for row_idx in sorted(rows_to_delete, reverse=True):
        logger.info("Suppression de la ligne emprunteur obsolète %s (plus de prêt en cours).", row_idx)
        _call_with_retry(worksheet.delete_rows, row_idx, row_idx)

    logger.info(
        "Mise à jour Répartition géographique / Bienprêter terminée (%d ligne(s) existante(s) mise(s) à "
        "jour, %d nouvelle(s) ligne(s) ajoutée(s), %d ligne(s) supprimée(s)).",
        len(existing_rows), len(pending_new_borrowers), len(rows_to_delete),
    )

    return issues


def get_geo_platform_snapshot(platform: str, next_label: str) -> dict:
    """Relevé de l'état investi d'une plateforme dans "Répartition
    géographique" (la configuration des robots - actif, taux, plafonds - vit
    désormais dans l'onglet "config robots", voir shared/robot_config.py).

    `next_label` : libellé de la plateforme/section suivante, qui borne le
    bloc de `platform`.

    Retourne :
    - `country_amounts` : {pays: montant investi} lu sur la ligne de la
      plateforme (tous loans confondus, actifs ou non).
    - `loan_invested` : {nom du loan: montant investi} pour chaque ligne du
      bloc.
    - `loan_countries` : {nom du loan: pays} pour les loans dont la ligne n'a
      qu'UNE colonne pays renseignée (repli si la colonne Pays de la config
      est vide).
    """
    worksheet = get_worksheet_by_name("Répartition géographique")
    grid = _call_with_retry(worksheet.get_all_values)

    geo_pos = find_cell_by_value(grid, "Répartition géographique")
    if not geo_pos:
        raise RuntimeError("La section 'Répartition géographique' n'a pas été trouvée.")
    geo_row, geo_col = geo_pos

    platform_row = find_first_cell_containing_below(grid, geo_row, geo_col, platform)
    if not platform_row:
        raise RuntimeError(f"La cellule '{platform}' n'a pas été trouvée sous 'Répartition géographique'.")

    end_row = find_first_cell_containing_below(grid, platform_row, geo_col, next_label)
    if not end_row:
        raise RuntimeError(
            f"La cellule '{next_label}' n'a pas été trouvée sous '{platform}' "
            f"(elle délimite la fin du bloc {platform})."
        )

    header_row = grid[geo_row - 1]
    country_columns = {
        col_idx: header_row[col_idx - 1].strip()
        for col_idx in range(geo_col + 1, len(header_row) + 1)
        if header_row[col_idx - 1].strip()
    }
    if not country_columns:
        raise RuntimeError("Aucune colonne pays trouvée à droite de 'Répartition géographique'.")

    def cell(row, col_idx) -> str:
        return row[col_idx - 1].strip() if col_idx - 1 < len(row) else ""

    platform_data_row = grid[platform_row - 1]
    country_amounts = {
        country: _parse_french_amount(cell(platform_data_row, col_idx)) or 0.0
        for col_idx, country in country_columns.items()
    }

    loan_invested = {}
    loan_countries = {}
    for row_idx in range(platform_row + 1, end_row):
        row = grid[row_idx - 1]
        name = cell(row, geo_col)
        if not name or name.lower() == "non investi":
            continue
        loan_invested[name] = _parse_french_amount(cell(row, geo_col + 1)) or 0.0
        filled = [country for col_idx, country in country_columns.items() if cell(row, col_idx)]
        if len(filled) == 1:
            loan_countries[name] = filled[0]

    logger.info(
        "Relevé géographique %s : %d pays, %d loans (investi total %.2f).",
        platform, len(country_amounts), len(loan_invested), sum(loan_invested.values()),
    )
    return {
        "country_amounts": country_amounts,
        "loan_invested": loan_invested,
        "loan_countries": loan_countries,
    }


if __name__ == "__main__":
    fill_current_month_amounts(
        platform="Bienprêter",
        amounts={
            "total": 1000,
            "gross_interest_received": 50
        }
    )