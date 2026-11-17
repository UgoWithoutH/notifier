"""Synchronise les fiches des plateformes de liberte-financiere.net (cadre réglementaire,
sièges sociaux, sociétés de prêts, fiscalité, notes...) dans l'onglet Google Sheet
"plateformes".

Sources (pages publiques, aucun login, pas de navigateur ; payload Next.js des pages) :
- /carte-p2p : plateformes (catégorie, pays du siège) et sociétés de prêts (pays du siège,
  groupe, pays d'activité) ;
- /bilans-financiers-p2p : plateformes et sociétés absentes de la carte, société mère,
  commentaires du site, page des indicateurs de performance, date de clôture ;
- /comparateur-p2p : pour chaque plateforme du comparateur, cadre réglementaire (ECSP,
  MiFID II, ...), adresse du siège, autorité de surveillance, indemnisation, fonds séparés,
  sanctions, nature du contrat, fiscalité (WHT, IFU, 3916...), ticket minimal, AutoInvest,
  Trustpilot, avantages / inconvénients, notes ;
- /reglementation-p2p : protection investisseur de chaque cadre, pays d'agrément et modèle.

L'onglet est entièrement réécrit à chaque lancement : une section par catégorie, une ligne
par plateforme et, dessous, une ligne par société de prêts rattachée. Les commentaires
internes du site ne sont pas lus.

Lancement : python -m scripts.sync_plateformes   (DRY_RUN=1 pour ne rien écrire)
"""
import html
import json
import logging
import os
import re
import traceback
from collections import defaultdict
from datetime import datetime

import gspread
import requests
from gspread.utils import rowcol_to_a1

from scripts.sync_bilans_financiers import (COUNTRY_NAMES_FR, HEADERS_HTTP, PAGE_URL as BILANS_URL, norm,
                                            fetch_reference_data)
from shared.google_sheet import _call_with_retry, get_worksheet_by_name
from shared.notifier import send_plateformes_changes_email, send_plateformes_sync_issues_email

log = logging.getLogger(__name__)

SHEET_NAME = "plateformes"
ANCHOR_SHEET_NAME = "notes bilans"  # onglet voisin, sert uniquement à retrouver le classeur si SHEET_NAME est absent
BASE_URL = "https://liberte-financiere.net"
MAP_URL = f"{BASE_URL}/carte-p2p"
COMPARATOR_URL = f"{BASE_URL}/comparateur-p2p"
REGULATION_URL = f"{BASE_URL}/reglementation-p2p"
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")

COUNTRY_NAMES = {**COUNTRY_NAMES_FR, "DK": "Danemark", "NL": "Pays-Bas", "LK": "Sri Lanka",
                 "UA": "Ukraine", "HU": "Hongrie", "DE": "Allemagne", "BE": "Belgique", "VC": "Saint-Vincent"}
THEMES = [("p2p_crowdlending", "Crowdlending"), ("crowdfunding_immobilier", "Crowdfunding immobilier"),
          ("immobilier_fractionne", "Immobilier fractionné"), ("autres", "Autres")]
OTHER_SECTION = "Autres"
ORPHAN_SECTION = "Sociétés de prêts sans plateforme (groupes, holdings)"

# Libellé du critère du comparateur -> colonne. Les trois premiers sont indispensables : s'ils
# disparaissent du site, l'écriture est annulée plutôt que de vider ces colonnes.
REQUIRED_CRITERIA = {"Cadre réglementaire": "Cadre réglementaire", "Pays Siège Social": "Pays du siège",
                     "Adresses Plateformes": "Adresse Cerfa 3916"}
COMPARATOR_CRITERIA = {
    **REQUIRED_CRITERIA,
    "Autorité de surveillance": "Autorité de surveillance",
    "Indemnisation plafond": "Indemnisation plafond",
    "Fonds séparés": "Fonds séparés",
    "Sanctions du régulateur": "Sanctions du régulateur",
    "Nature du contrat": "Nature du contrat",
    "WHT Tax": "WHT",
    "IFU": "IFU",
    "Tax report": "Tax report",
    "Compte étranger 3916": "Compte étranger 3916",
    "Impôts": "Impôts",
    "Ticket minimal": "Ticket minimal",
    "AutoInvest": "AutoInvest",
    "TrustPilot Note": "Trustpilot note",
    "TrustPilot Nb Avis": "Trustpilot nb avis",
    "Bonus": "Bonus",
}
# Critères dont les valeurs sont regroupées dans une seule colonne.
PROS_LABELS = [f"Avantage {i}" for i in range(1, 5)]
CONS_LABELS = [f"Inconvénient {i}" for i in range(1, 5)]
# Notes /5 du comparateur (critères "notation:*"), une colonne par libellé.
NOTE_LABELS = ["Note globale", "Solidité prêteurs", "Solidité plateforme", "Historique et performance",
               "Réglementation", "Frais et usage", "Rendement réel", "Liquidité", "Fiscalité",
               "Meilleur Avis/Bonus"]
NOTE_COLUMNS = {label: label if label == "Note globale" else f"Note {label}" for label in NOTE_LABELS}
# Pour les colonnes "commentaire" dédiées, les autres critères alimentent "Autres commentaires".
OWN_COMMENT_COLUMNS = {"Cadre réglementaire": "Commentaire cadre", "Sanctions du régulateur": "Commentaire sanctions"}

COLUMNS = [
    "Catégorie", "Cadre réglementaire", "Protection investisseur", "Pays du siège",
    "Pays d'agrément", "Adresse Cerfa 3916", "Autorité de surveillance", "Indemnisation plafond",
    "Fonds séparés", "Sanctions du régulateur", "Nature du contrat", "Modèle économique",
    *NOTE_COLUMNS.values(), "Base de la note globale", "Détail des points",
    "WHT", "IFU", "Tax report", "Compte étranger 3916", "Impôts", "Ticket minimal", "AutoInvest",
    "Trustpilot note", "Trustpilot nb avis", "Bonus", "Avantages", "Inconvénients",
    "Indicateurs de performance", "Clôturée le",
    "Groupe", "Pays du siège du groupe", "Pays d'activité",
    "Commentaire cadre", "Commentaire sanctions", "Autres commentaires comparateur", "Commentaire du site",
]
WIDTH = 1 + len(COLUMNS)
# Noms (normalisés) de la page réglementation différents de ceux de la carte.
REGULATION_ALIASES = {"bondora": "goandgrow", "monefitsmartsaver": "monefit"}


def fetch_page(url: str) -> str:
    r = requests.get(url, headers=HEADERS_HTTP, timeout=30)
    r.raise_for_status()
    r.encoding = "utf-8"
    return r.text


def rsc_payload(page: str) -> str:
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>', page, flags=re.S)
    return "".join(json.loads('"' + c + '"') for c in chunks)


def extract_list(payload: str, key: str) -> list[dict]:
    idx = payload.find(f'"{key}":[')
    if idx < 0:
        raise RuntimeError(f"Clé '{key}' introuvable dans la page.")
    value, _ = json.JSONDecoder().raw_decode(payload[idx + len(key) + 3:])
    return value


def country_name(code: str | None) -> str:
    code = (code or "").strip().upper()
    return COUNTRY_NAMES.get(code, code)


def strip_decorations(text: str | None) -> str:
    """Retire drapeaux et pictogrammes d'alerte ("🇧🇬 Bulgarie" -> "Bulgarie", "ECSP ⚠️" -> "ECSP")."""
    text = re.sub("[\U0001F1E6-\U0001F1FF\u26A0\uFE0F]", "", text or "")
    return re.sub(r"\s+", " ", text).strip()


def fr_number(value: float) -> str:
    return f"{round(value, 2):g}".replace(".", ",")


def score_breakdown(tabs: list[dict], criteria: list[dict], broker_id: str) -> tuple[str, str]:
    """(base de la note globale, détail des points) d'une plateforme.

    Le site calcule la note globale comme la moyenne, pondérée par le poids de chaque onglet,
    des seules notes renseignées. Chaque note d'onglet est elle-même la somme des points de ses
    critères notés rapportée à leur maximum ("rollup:<onglet>")."""
    def cell(criterion: dict) -> dict:
        return criterion["values"].get(broker_id) or {}

    rated = [t for t in tabs if t.get("notation")
             and any(c["id"] == f"notation:{t['id']}" and cell(c).get("note") is not None for c in criteria)]
    weighted = [t for t in tabs if t.get("notation") and t.get("poids") is not None]
    filled = [t for t in rated if t.get("poids") is not None]
    base = ""
    if filled:
        base = (f"{len(filled)}/{len(weighted)} notes renseignées · poids "
                f"{fr_number(sum(t['poids'] for t in filled))}/{fr_number(sum(t['poids'] for t in weighted))}")

    lines = []
    for tab in rated:
        parts = []
        for c in criteria:
            if c["tabId"] != tab["id"] or not c.get("notation"):
                continue
            points = cell(c).get("note")
            if points is None:
                continue
            maximum = c.get("pointsMax")
            parts.append(f"{c['label']} {fr_number(points)}" + (f" / {fr_number(maximum)}" if maximum else ""))
        if not parts:
            continue
        total = next((cell(c).get("text") for c in criteria if c["id"] == f"rollup:{tab['id']}"), "") or ""
        lines.append(f"{tab['label']} | {total} | {' ; '.join(parts)}")
    return base, "\n".join(lines)


def parse_comparator(page: str) -> dict[int, dict[str, str]]:
    """plateformeId -> valeurs par colonne (vide si le critère n'est pas renseigné)."""
    payload = rsc_payload(page)
    brokers = extract_list(payload, "brokers")
    criteria = extract_list(payload, "criteria")
    tabs = extract_list(payload, "tabs")
    by_label = {c["label"]: c for c in criteria if not c["id"].startswith(("rollup", "notation"))}
    notes = {c["label"]: c for c in criteria if c["id"].startswith("notation:")}
    missing = set(REQUIRED_CRITERIA) - set(by_label)
    if missing:
        raise RuntimeError(f"Critères introuvables dans le comparateur : {sorted(missing)}")
    absent = (set(COMPARATOR_CRITERIA) | set(PROS_LABELS) | set(CONS_LABELS)) - set(by_label)
    absent |= set(NOTE_LABELS) - set(notes)
    if absent:
        log.warning("Critères du comparateur absents (colonnes vides) : %s", sorted(absent))

    def value(label: str, broker_id: str, source=by_label) -> dict:
        return (source.get(label, {}).get("values", {}).get(broker_id)) or {}

    out = {}
    for broker in brokers:
        if broker.get("affichage") is False:  # masquée du comparateur public : on ne reprend pas ses données
            continue
        bid = broker["id"]
        fields = {}
        for label, column in COMPARATOR_CRITERIA.items():
            fields[column] = (value(label, bid).get("text") or "").strip()
        for label, column in NOTE_COLUMNS.items():
            text = (value(label, bid, notes).get("text") or "").strip()
            fields[column] = "" if text == "—" else text
        fields["Base de la note globale"], fields["Détail des points"] = score_breakdown(tabs, criteria, bid)
        fields["Avantages"] = " ; ".join(t for t in ((value(l, bid).get("text") or "").strip()
                                                     for l in PROS_LABELS) if t)
        fields["Inconvénients"] = " ; ".join(t for t in ((value(l, bid).get("text") or "").strip()
                                                          for l in CONS_LABELS) if t)
        others = []
        for label, c in by_label.items():
            comment = (c["values"].get(bid) or {}).get("commentaireExterne", "").strip()
            if not comment:
                continue
            if label in OWN_COMMENT_COLUMNS:
                fields[OWN_COMMENT_COLUMNS[label]] = comment
            else:
                others.append(f"{label} : {comment}")
        fields["Autres commentaires comparateur"] = "\n".join(others)
        out[broker["plateformeId"]] = fields
    return out


def regulation_key(name: str) -> str:
    key = norm(name.replace("*", ""))
    return REGULATION_ALIASES.get(key, key)


def parse_regulation(page: str) -> dict[str, dict[str, str]]:
    """Tableaux de la page réglementation, indexés par nom de plateforme normalisé :
    {"levels": {cadre: protection}, "agrement": {plateforme: pays}, "modele": {plateforme: modèle}}."""
    payload = rsc_payload(page)
    out = {"levels": {}, "agrement": {}, "modele": {}}
    for table in re.findall(r"<table>.*?</table>", payload, flags=re.S):
        rows = [[html.unescape(re.sub(r"<[^>]+>", "", c)).replace("\xa0", " ").strip()
                 for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row, flags=re.S)]
                for row in re.findall(r"<tr>(.*?)</tr>", table, flags=re.S)]
        if not rows:
            continue
        header = rows[0]
        if "Cadre" in header and "Protection investisseur" in header:
            cadre_idx, level_idx = header.index("Cadre"), header.index("Protection investisseur")
            for row in rows[1:]:
                cadre = re.sub(r"\s*/\s*PSFP|\s+agrément investisseur", "", row[cadre_idx])
                out["levels"][norm(cadre)] = row[level_idx]
        elif len(header) == 2 and "agrément" in header[1].casefold():
            for name, country in (r for r in rows[1:] if len(r) == 2):
                out["agrement"][regulation_key(name)] = country
        elif header[:1] == ["Plateforme"] and "Modèle" in header:
            idx = header.index("Modèle")
            for row in rows[1:]:
                out["modele"][regulation_key(row[0])] = row[idx]
    if not out["levels"]:
        raise RuntimeError("Tableau des cadres réglementaires introuvable dans la page réglementation.")
    return out


def protection_level(cadre: str, levels: dict[str, str]) -> str:
    key = norm(cadre)
    matches = [k for k in levels if k and key.startswith(k)]
    return levels[max(matches, key=len)] if matches else ""


def merge_sources(map_plateformes: list[dict], map_societes: list[dict], bilans_plateformes: list[dict],
                  bilans_societes: list[dict], bilans_links: list[dict]) -> tuple[list[dict], list[dict]]:
    """Fusionne la carte et la page des bilans (par id) en plateformes / sociétés homogènes."""
    platforms: dict[int, dict] = {}
    for p in bilans_plateformes:
        themes = [t for t in (p.get("plateforme_themes") or "").split(",") if t]
        platforms[p["id"]] = {
            "id": p["id"], "nom": p["nom"], "pays": p.get("pays_siege"), "themes": themes,
            "indicateurs": p.get("url_indicateurs_performance") or "",
            "cloturee": p.get("plateforme_cloturee_le") or "", "commentaire": p.get("note_externe") or ""}
    for p in map_plateformes:
        merged = platforms.setdefault(p["id"], {"id": p["id"], "indicateurs": "", "cloturee": "", "commentaire": ""})
        merged.update(nom=p["nom"], pays=p.get("pays") or merged.get("pays"),
                      themes=p.get("themes") or merged.get("themes") or [])

    societes: dict[int, dict] = {}
    for s in bilans_societes:
        societes[s["id"]] = {"id": s["id"], "nom": s["nom"], "siege": s.get("pays_siege"), "mere_id": s.get("societe_mere_id"),
                             "groupe": "", "siege_groupe": "", "pays": [], "plateformeIds": set(),
                             "commentaire": s.get("note_externe") or ""}
    for s in map_societes:
        merged = societes.setdefault(s["id"], {"id": s["id"], "nom": s["nom"], "mere_id": None, "commentaire": "",
                                               "plateformeIds": set()})
        merged.update(siege=s.get("siege") or merged.get("siege"), pays=s.get("pays") or [],
                      groupe=s.get("groupeNom") or "", siege_groupe=s.get("groupeSiege") or "")
        merged["plateformeIds"].update(s.get("plateformeIds") or [])
    for link in bilans_links:
        if link["societe_pret_id"] in societes:
            societes[link["societe_pret_id"]]["plateformeIds"].add(link["plateforme_id"])
    for s in societes.values():
        parent = societes.get(s.get("mere_id"))
        if parent:
            s["groupe"], s["siege_groupe"] = parent["nom"], parent.get("siege") or ""
        if norm(s["groupe"]) == norm(s["nom"]):
            s["groupe"] = s["siege_groupe"] = ""
    return list(platforms.values()), list(societes.values())


def platform_section(platform: dict) -> str:
    themes = platform.get("themes") or []
    return next((label for code, label in THEMES if code in themes), OTHER_SECTION)


def build_grid(plateformes: list[dict], societes: list[dict], comparator: dict[int, dict],
               regulation: dict[str, dict[str, str]]) -> tuple[list[list[str]], list[str], list[str]]:
    """Tableau complet (en-tête inclus), type de chaque ligne et anomalies relevées."""
    issues: list[str] = []
    companies_by_platform = defaultdict(list)
    linked = set()
    for s in societes:
        for pid in s["plateformeIds"]:
            companies_by_platform[pid].append(s)
            linked.add(s["id"])

    def line(name="", **values) -> list[str]:
        return [name] + [values.get(c, "") for c in COLUMNS]

    def company_line(s: dict) -> list[str]:
        return line(s["nom"], **{
            "Pays du siège": country_name(s.get("siege")), "Groupe": s["groupe"],
            "Pays du siège du groupe": country_name(s["siege_groupe"]),
            "Pays d'activité": ", ".join(sorted(country_name(c) for c in s["pays"])),
            "Commentaire du site": s["commentaire"]})

    grid, kinds = [line("Nom", **{c: c for c in COLUMNS})], ["header"]
    by_section = defaultdict(list)
    for p in plateformes:
        by_section[platform_section(p)].append(p)

    used_regulation = set()
    for section in [label for _, label in THEMES]:
        platforms = sorted(by_section.get(section, []), key=lambda p: p["nom"].casefold())
        if not platforms:
            continue
        grid.append(line(section))
        kinds.append("section")
        for p in platforms:
            key = regulation_key(p["nom"])
            site_country = country_name(p.get("pays"))
            labels = [label for code, label in THEMES if code in (p.get("themes") or [])]
            values = {"Catégorie": " / ".join(labels), "Pays du siège": site_country,
                      "Indicateurs de performance": p["indicateurs"], "Clôturée le": p["cloturee"],
                      "Commentaire du site": p["commentaire"]}
            info = comparator.get(p["id"])
            if info:
                cadre = strip_decorations(info["Cadre réglementaire"])
                comparator_country = strip_decorations(info["Pays du siège"])
                if comparator_country and site_country and norm(comparator_country) != norm(site_country):
                    issues.append(f"{p['nom']} : pays du siège différent (carte : {site_country}, "
                                  f"comparateur : {comparator_country})")
                info = {**info, "Cadre réglementaire": cadre, "Pays du siège": comparator_country or site_country,
                        "Fonds séparés": strip_decorations(info["Fonds séparés"]),
                        "Adresse Cerfa 3916": ", ".join(part.strip() for part in
                                                        info["Adresse Cerfa 3916"].splitlines() if part.strip()),
                        "Protection investisseur": protection_level(cadre, regulation["levels"])}
                values.update(info)
            for table, column in (("agrement", "Pays d'agrément"), ("modele", "Modèle économique")):
                if key in regulation[table]:
                    values[column] = regulation[table][key]
                    used_regulation.add((table, key))
            grid.append(line(p["nom"], **values))
            kinds.append("platform")
            for s in sorted(companies_by_platform.get(p["id"], []), key=lambda s: s["nom"].casefold()):
                grid.append(company_line(s))
                kinds.append("company")

    orphans = sorted((s for s in societes if s["id"] not in linked), key=lambda s: s["nom"].casefold())
    if orphans:
        grid.append(line(ORPHAN_SECTION))
        kinds.append("section")
        for s in orphans:
            grid.append(company_line(s))
            kinds.append("company")

    for table in ("agrement", "modele"):
        for key in regulation[table]:
            if (table, key) not in used_regulation:
                issues.append(f"Page réglementation ({table}) : '{key}' ne correspond à aucune plateforme")
    unknown = sorted(set(comparator) - {p["id"] for p in plateformes})
    if unknown:
        issues.append(f"Plateformes du comparateur absentes de la carte (ids) : {unknown}")
    return grid, kinds, issues


def apply_format(ws, kinds: list[str], total_rows: int) -> None:
    last_col = rowcol_to_a1(1, WIDTH)[:-1]
    formats = [
        {"range": f"A2:{last_col}{total_rows}", "format": {
            "textFormat": {"bold": False, "italic": False, "fontSize": 10},
            "backgroundColor": {"red": 1, "green": 1, "blue": 1},
            "horizontalAlignment": "LEFT", "wrapStrategy": "CLIP", "numberFormat": {"type": "TEXT"}}},
        {"range": f"A1:{last_col}1", "format": {
            "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
            "backgroundColor": {"red": 0.07, "green": 0.23, "blue": 0.26}, "wrapStrategy": "WRAP"}},
    ]
    for idx, kind in enumerate(kinds, start=1):
        style = {
            "section": {"textFormat": {"bold": True},
                        "backgroundColor": {"red": 0.85, "green": 0.9, "blue": 0.92}},
            "platform": {"textFormat": {"bold": True}},
            "company": {"textFormat": {"italic": True}, "horizontalAlignment": "RIGHT"},
        }.get(kind)
        if style:
            formats.append({"range": f"A{idx}:{last_col}{idx}", "format": style})
    ws.batch_format(formats)
    ws.freeze(rows=1, cols=1)


def get_or_create_worksheet():
    try:
        return get_worksheet_by_name(SHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        log.info("Onglet '%s' absent : création.", SHEET_NAME)
        spreadsheet = get_worksheet_by_name(ANCHOR_SHEET_NAME).spreadsheet
        return _call_with_retry(spreadsheet.add_worksheet, title=SHEET_NAME, rows=500, cols=WIDTH)


def _clean(value) -> str:
    return " ".join(str(value).split())


def _short(value: str, limit: int = 160) -> str:
    return value if len(value) <= limit else value[:limit - 1] + "…"


def find_changes(existing: list[list[str]], grid: list[list[str]]) -> tuple[list[str], list[str], list[dict]]:
    """(ajouts, suppressions, modifications) entre l'onglet actuel et le nouveau tableau.

    Les lignes sont appariées par nom et rang d'apparition (une société peut figurer sous
    plusieurs plateformes). Seules les colonnes présentes dans l'ancien en-tête sont comparées.
    """
    def index(rows):
        seen, result = defaultdict(int), {}
        for row in rows[1:]:
            name = _clean(row[0]) if row else ""
            if name:
                seen[name] += 1
                result[(name, seen[name])] = row
        return result

    old_header = [_clean(c) for c in existing[0]]
    columns = [(c, old_header.index(c), 1 + i) for i, c in enumerate(COLUMNS) if c in old_header]
    old_rows, new_rows = index(existing), index(grid)
    added = [name for name, n in new_rows if (name, n) not in old_rows]
    removed = [name for name, n in old_rows if (name, n) not in new_rows]
    modified = []
    for key, new_row in new_rows.items():
        old_row = old_rows.get(key)
        if old_row is None:
            continue
        changes = []
        for column, old_idx, new_idx in columns:
            old = _clean(old_row[old_idx]) if old_idx < len(old_row) else ""
            new = _clean(new_row[new_idx]) if new_idx < len(new_row) else ""
            if old != new:
                changes.append((column, _short(old), _short(new)))
        if changes:
            modified.append({"name": key[0], "changes": changes})
    return added, removed, modified


def run() -> None:
    try:
        sync()
    except Exception:
        log.exception("Échec de la synchronisation des plateformes.")
        if not DRY_RUN:
            send_plateformes_sync_issues_email([], error=traceback.format_exc())
        raise


def sync() -> None:
    map_payload = rsc_payload(fetch_page(MAP_URL))
    map_plateformes, map_societes = extract_list(map_payload, "plateformes"), extract_list(map_payload, "societes")
    bilans_plateformes, bilans_societes, bilans_links = fetch_reference_data(fetch_page(BILANS_URL))
    comparator = parse_comparator(fetch_page(COMPARATOR_URL))
    try:
        regulation = parse_regulation(fetch_page(REGULATION_URL))
    except Exception:
        log.exception("Page réglementation illisible : colonnes correspondantes laissées vides.")
        regulation = {"levels": {}, "agrement": {}, "modele": {}}
    plateformes, societes = merge_sources(map_plateformes, map_societes, bilans_plateformes,
                                          bilans_societes, bilans_links)
    log.info("Site : %d plateformes, %d sociétés, %d plateformes au comparateur, %d cadres.",
             len(plateformes), len(societes), len(comparator), len(regulation["levels"]))
    if not plateformes or not societes or not comparator:
        raise RuntimeError("Données du site vides : écriture annulée.")

    grid, kinds, issues = build_grid(plateformes, societes, comparator, regulation)
    for issue in issues:
        log.warning(issue)
    if issues and not DRY_RUN:
        send_plateformes_sync_issues_email(issues)
    counts = {k: kinds.count(k) for k in ("section", "platform", "company")}
    log.info("Tableau final : %d lignes (%s).", len(grid), counts)
    if DRY_RUN:
        for row, kind in list(zip(grid, kinds))[:40]:
            log.info("[%s] %s | %s", kind, row[0], row[1:5])
        log.info("DRY_RUN : aucune écriture.")
        return

    ws = get_or_create_worksheet()
    existing = _call_with_retry(ws.get_all_values)
    existing_rows = len(existing)
    total_rows = max(len(grid), existing_rows)
    padded = [r + [""] * (WIDTH - len(r)) for r in grid] + [[""] * WIDTH for _ in range(total_rows - len(grid))]
    if ws.row_count < total_rows or ws.col_count < WIDTH:
        _call_with_retry(ws.resize, rows=max(ws.row_count, total_rows), cols=max(ws.col_count, WIDTH))
    _call_with_retry(ws.update, values=padded, range_name=f"A1:{rowcol_to_a1(total_rows, WIDTH)}", raw=True)
    _call_with_retry(apply_format, ws, kinds, total_rows)
    log.info("Onglet '%s' mis à jour (%s).", SHEET_NAME, datetime.now().strftime("%Y-%m-%d %H:%M"))

    # Onglet vide (première exécution) : rien à comparer, pas de mail.
    if len(existing) > 1:
        added, removed, modified = find_changes(existing, grid)
        if added or removed or modified:
            log.info("Changements : %d ajout(s), %d suppression(s), %d modification(s).",
                     len(added), len(removed), len(modified))
            send_plateformes_changes_email(added, removed, modified)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    run()
