"""Synchronise les bilans financiers P2P de liberte-financiere.net dans l'onglet
Google Sheet "notes bilans".

Pour chaque plateforme listée dans l'onglet (sections "Crowdlending", ...) :
- les sociétés de prêts liées à la plateforme sur le site (filtre "plateforme")
  sont ajoutées sous la plateforme si elles manquent ;
- une société déjà présente dans l'onglet mais non liée à la plateforme est
  recherchée par son nom dans toutes les sociétés du site (filtre "société de
  prêts" sans plateforme) ;
- sous chaque société (et sous la plateforme elle-même pour ses propres bilans),
  une ligne par bilan est (ré)écrite avec tous les détails, y compris le détail
  de la note par critère.

Données : API publique /api/public/bilans-financiers + liens plateforme/société
embarqués dans la page (payload Next.js). Aucun login, pas de navigateur.

Lancement : python -m scripts.sync_bilans_financiers   (DRY_RUN=1 pour ne rien écrire)
"""
import json
import logging
import os
import re
import unicodedata
from collections import Counter
from datetime import datetime

import requests
from gspread.utils import rowcol_to_a1

from shared.google_sheet import _call_with_retry, get_worksheet_by_name
from shared.notifier import send_new_bilans_email

log = logging.getLogger(__name__)

SHEET_NAME = "notes bilans"
PAGE_URL = "https://liberte-financiere.net/bilans-financiers-p2p"
API_URL = "https://liberte-financiere.net/api/public/bilans-financiers"
HEADERS_HTTP = {"User-Agent": "Mozilla/5.0"}
PAGE_SIZE = 20
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")

# Plateformes du Sheet absentes de la liste des plateformes du site.
EXTRA_PLATFORMS = {"bondora"}

COLUMNS = [
    "Exercice", "Audit", "Auditeur", "Devise", "Taux (1 € =)",
    "CA brut (devise)", "CA brut (€)", "Résultat net (devise)",
    "Total actif (devise)", "Trésorerie (devise)", "Total dettes (devise)",
    "Capitaux propres (devise)",
    "Marge (%)", "ROA (%)", "ROE (%)", "Dettes/CP", "Cash/Dette (%)",
    "Score marge", "Score ROA", "Score ROE", "Score Dettes/CP", "Score Cash/Dette",
    "Note /5", "PDF 1", "PDF 2", "PDF 3", "Analyse IA", "Mis à jour le",
    "Commentaire plateforme / société",
]
FIRST_DATA_COL = 2  # colonne B (la colonne A contient les noms)
INTEGER_COLUMNS = {"CA brut (devise)", "CA brut (€)", "Résultat net (devise)",
                   "Total actif (devise)", "Trésorerie (devise)",
                   "Total dettes (devise)", "Capitaux propres (devise)"}


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", text.casefold())


def fetch_rows() -> list[dict]:
    rows, offset, total = [], 0, None
    while total is None or offset < total:
        r = requests.get(API_URL, params={"offset": offset, "limit": PAGE_SIZE},
                         headers=HEADERS_HTTP, timeout=30)
        r.raise_for_status()
        data = r.json()
        total = data["total"]
        if not data["rows"]:
            break
        rows += data["rows"]
        offset += PAGE_SIZE
    log.info("%d bilans récupérés (total annoncé : %s).", len(rows), total)
    return rows


def fetch_reference_data() -> tuple[list[dict], list[dict], list[dict]]:
    """Plateformes, sociétés de prêts et liens plateforme/société (payload RSC)."""
    r = requests.get(PAGE_URL, headers=HEADERS_HTTP, timeout=30)
    r.raise_for_status()
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>', r.text, flags=re.S)
    payload = "".join(json.loads('"' + c + '"') for c in chunks)
    decoder = json.JSONDecoder()

    def extract(key: str) -> list[dict]:
        idx = payload.find(f'"{key}":[')
        if idx < 0:
            raise RuntimeError(f"Clé '{key}' introuvable dans la page des bilans.")
        value, _ = decoder.raw_decode(payload[idx + len(key) + 3:])
        return value

    return extract("plateformes"), extract("societesPrets"), extract("associationLinks")


def find_societe(name: str, societes: list[dict]) -> dict | None:
    """Filtre "société de prêts" seul : nom exact, sinon inclusion (>= 4 car.)."""
    target = norm(name)
    if not target:
        return None
    for s in societes:
        if norm(s["nom"]) == target:
            return s
    for s in societes:
        n = norm(s["nom"])
        if len(min(n, target, key=len)) >= 4 and (n in target or target in n):
            return s
    return None


def audit_label(row: dict) -> str:
    if not row.get("audite"):
        return "Non audité"
    return f"Audité par {row['auditeur_nom']}" if row.get("auditeur_nom") else "Bilan audité"


def pct(value):
    return "" if value is None else round(value * 100, 2)


def num(value, digits=2):
    return "" if value is None else round(value, digits)


def bilan_line(row: dict) -> list:
    computed = row.get("computed") or {}
    ratios, scores = computed.get("ratios") or {}, computed.get("scores") or {}
    rate = row.get("taux_vers_eur") or 1
    ca = row.get("ca_brut")
    values = {
        "Exercice": row.get("exercice_label") or "",
        "Audit": audit_label(row),
        "Auditeur": row.get("auditeur_nom") or "",
        "Devise": row.get("devise") or "",
        "Taux (1 € =)": row.get("taux_vers_eur") if row.get("taux_vers_eur") is not None else "",
        "CA brut (devise)": "" if ca is None else ca,
        "CA brut (€)": "" if ca is None else round(ca / rate),
        "Résultat net (devise)": "" if row.get("resultat_net") is None else row["resultat_net"],
        "Total actif (devise)": "" if row.get("total_actif") is None else row["total_actif"],
        "Trésorerie (devise)": "" if row.get("tresorerie") is None else row["tresorerie"],
        "Total dettes (devise)": "" if row.get("total_dettes") is None else row["total_dettes"],
        "Capitaux propres (devise)": "" if row.get("total_capitaux_propres") is None else row["total_capitaux_propres"],
        "Marge (%)": pct(ratios.get("marge")),
        "ROA (%)": pct(ratios.get("roa")),
        "ROE (%)": pct(ratios.get("roe")),
        "Dettes/CP": num(ratios.get("dettes_sur_cp")),
        "Cash/Dette (%)": pct(ratios.get("cash_sur_dette")),
        "Score marge": num(scores.get("marge")),
        "Score ROA": num(scores.get("roa")),
        "Score ROE": num(scores.get("roe")),
        "Score Dettes/CP": num(scores.get("dettes_sur_cp")),
        "Score Cash/Dette": num(scores.get("cash_sur_dette")),
        "Note /5": num(computed.get("note_sur_5")),
        "PDF 1": row.get("pdf_fichier_1") or "",
        "PDF 2": row.get("pdf_fichier_2") or "",
        "PDF 3": row.get("pdf_fichier_3") or "",
        "Analyse IA": row.get("ia_analyse") or "",
        "Mis à jour le": row.get("updated_at") or "",
        "Commentaire plateforme / société": "",
    }
    return [values[c] for c in COLUMNS]


def sorted_bilans(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda r: (r.get("exercice_label") or "", r["id"]), reverse=True)


def parse_sheet(grid: list[list[str]], platform_names: set[str]) -> list[dict]:
    """Lignes de structure du Sheet : section / plateforme (+ sociétés existantes)."""
    items: list[dict] = []
    current_platform = None
    section_has_platform = False
    for i, raw in enumerate(grid):
        b = (raw[0] if raw else "").strip()
        if i == 0 or not b:
            continue  # titre ou ligne de bilan (régénérée)
        n = norm(b)
        if n.startswith("crowd") or n == "bourse":
            items.append({"kind": "section", "b": b})
            current_platform, section_has_platform = None, False
        elif n in platform_names or n in EXTRA_PLATFORMS or not section_has_platform:
            current_platform = {"kind": "platform", "b": b, "companies": []}
            items.append(current_platform)
            section_has_platform = True
        else:
            current_platform["companies"].append(b)
    # Une plateforme dupliquée (ex. ligne société ajoutée par erreur) : on garde la dernière.
    last_index = {norm(i["b"]): idx for idx, i in enumerate(items) if i["kind"] == "platform"}
    return [i for idx, i in enumerate(items) if i["kind"] != "platform" or last_index[norm(i["b"])] == idx]


def build_grid(header: list[str], items: list[dict], rows: list[dict],
               plateformes: list[dict], societes: list[dict], links: list[dict]):
    by_platform_id = {}
    by_societe_id = {}
    for r in rows:
        if r.get("plateforme_id"):
            by_platform_id.setdefault(r["plateforme_id"], []).append(r)
        if r.get("societe_pret_id"):
            by_societe_id.setdefault(r["societe_pret_id"], []).append(r)
    platform_by_norm = {norm(p["nom"]): p for p in plateformes}
    societe_by_id = {s["id"]: s for s in societes}
    sheet_platforms = {norm(i["b"]) for i in items if i["kind"] == "platform"}
    width = 1 + len(COLUMNS)
    comment_idx = width - 1

    def line(name="", data=None, comment=""):
        out = [name] + (data if data is not None else [""] * len(COLUMNS))
        out[comment_idx] = comment
        return out

    grid, kinds = [header], ["header"]

    def add_bilans(entity_rows):
        for r in sorted_bilans(entity_rows):
            grid.append(line(data=bilan_line(r)))
            kinds.append("bilan")

    for item in items:
        if item["kind"] == "section":
            grid.append(line(item["b"]))
            kinds.append("section")
            continue

        platform = platform_by_norm.get(norm(item["b"]))
        own_rows = list(by_platform_id.get(platform["id"], [])) if platform else []
        same_name = find_societe(item["b"], societes) if not platform else None
        if same_name and norm(same_name["nom"]) == norm(item["b"]):
            own_rows += by_societe_id.get(same_name["id"], [])
        else:
            same_name = None
        grid.append(line(item["b"],
                         comment=(platform or {}).get("note_externe") or (same_name or {}).get("note_externe") or ""))
        kinds.append("platform")
        add_bilans(own_rows)

        companies: dict[str, dict | None] = {}
        for name in item["companies"]:
            companies[norm(name)] = {"label": name, "societe": find_societe(name, societes)}
        linked_ids = {l["societe_pret_id"] for l in links if platform and l["plateforme_id"] == platform["id"]}
        used_ids = {c["societe"]["id"] for c in companies.values() if c["societe"]}
        new = [societe_by_id[i] for i in linked_ids
               if i in societe_by_id and i not in used_ids
               and norm(societe_by_id[i]["nom"]) not in sheet_platforms]
        for s in sorted(new, key=lambda s: s["nom"].casefold()):
            companies.setdefault(norm(s["nom"]), {"label": s["nom"], "societe": s})
            log.info("Société ajoutée sous %s : %s", item["b"], s["nom"])

        for entry in companies.values():
            s = entry["societe"]
            if s is None:
                log.warning("Société '%s' (sous %s) introuvable sur le site.", entry["label"], item["b"])
            grid.append(line(entry["label"], comment=(s or {}).get("note_externe") or ""))
            kinds.append("company")
            if s:
                add_bilans(by_societe_id.get(s["id"], []))
    return grid, kinds


def bilan_entries(grid: list[list]) -> list[tuple[str, str, list]]:
    """(propriétaire, exercice, ligne) pour chaque ligne de bilan (colonne A vide)."""
    owner, out = "", []
    for raw in grid[1:]:
        name = str(raw[0]).strip() if raw else ""
        if name:
            owner = name
            continue
        exercice = str(raw[1]).strip() if len(raw) > 1 else ""
        if exercice:
            out.append((owner, exercice, raw))
    return out


def find_new_bilans(existing: list[list[str]], grid: list[list]) -> list[dict]:
    """Bilans du nouveau tableau absents de l'ancien (clé : propriétaire + exercice)."""
    seen = Counter((o, e) for o, e, _ in bilan_entries(existing))
    idx = {c: 1 + i for i, c in enumerate(COLUMNS)}
    new = []
    for owner, exercice, line in bilan_entries(grid):
        if seen[(owner, exercice)] > 0:
            seen[(owner, exercice)] -= 1
            continue
        new.append({"owner": owner, "exercice": exercice,
                    **{k: line[idx[c]] for k, c in (
                        ("audit", "Audit"), ("devise", "Devise"), ("ca_eur", "CA brut (€)"),
                        ("resultat_net", "Résultat net (devise)"), ("note", "Note /5"))}})
    return new


def apply_format(ws, kinds: list[str], total_rows: int) -> None:
    last_col = rowcol_to_a1(1, 1 + len(COLUMNS))[:-1]
    full = f"A2:{last_col}{total_rows}"
    formats = [{"range": full, "format": {
        "textFormat": {"bold": False, "italic": False, "fontSize": 10},
        "backgroundColor": {"red": 1, "green": 1, "blue": 1},
        "horizontalAlignment": "LEFT", "numberFormat": {"type": "TEXT"}}},
        {"range": f"A1:{last_col}1", "format": {
            "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
            "backgroundColor": {"red": 0.07, "green": 0.23, "blue": 0.26},
            "wrapStrategy": "WRAP"}}]
    for col, name in enumerate(COLUMNS, start=FIRST_DATA_COL):
        letter = rowcol_to_a1(1, col)[:-1]
        if name in INTEGER_COLUMNS:
            fmt = {"type": "NUMBER", "pattern": "#,##0"}
        elif name in ("PDF 1", "PDF 2", "PDF 3", "Exercice", "Audit", "Auditeur", "Devise",
                      "Analyse IA", "Mis à jour le", "Commentaire plateforme / société"):
            continue
        else:
            fmt = {"type": "NUMBER", "pattern": "0.0#"}
        formats.append({"range": f"{letter}2:{letter}{total_rows}", "format": {"numberFormat": fmt}})
    for idx, kind in enumerate(kinds, start=1):
        style = {
            "section": {"textFormat": {"bold": True}, "backgroundColor": {"red": 0.85, "green": 0.9, "blue": 0.92}},
            "platform": {"textFormat": {"bold": True}},
            "company": {"textFormat": {"italic": True}, "horizontalAlignment": "RIGHT"},
        }.get(kind)
        if style:
            formats.append({"range": f"A{idx}:{last_col}{idx}", "format": style})
    ws.batch_format(formats)
    ws.freeze(rows=1, cols=1)


def run() -> None:
    rows = fetch_rows()
    plateformes, societes, links = fetch_reference_data()
    log.info("Site : %d plateformes, %d sociétés, %d liens.", len(plateformes), len(societes), len(links))

    ws = get_worksheet_by_name(SHEET_NAME)
    existing = _call_with_retry(ws.get_all_values)
    platform_names = {norm(p["nom"]) for p in plateformes}
    items = parse_sheet(existing, platform_names)
    header_name = (existing[0][0] if existing and existing[0] else "") or "Nom"
    grid, kinds = build_grid([header_name] + COLUMNS, items, rows, plateformes, societes, links)

    counts = {k: kinds.count(k) for k in ("platform", "company", "bilan")}
    log.info("Tableau final : %d lignes (%s).", len(grid), counts)
    if DRY_RUN:
        for row, kind in list(zip(grid, kinds))[:60]:
            log.info("[%s] %s | %s", kind, row[0], row[1:4])
        log.info("DRY_RUN : aucune écriture.")
        return

    width = 1 + len(COLUMNS)
    total_rows = max(len(grid), len(existing))
    padded = [r + [""] * (width - len(r)) for r in grid]
    padded += [[""] * width for _ in range(total_rows - len(grid))]
    if ws.row_count < total_rows or ws.col_count < width:
        _call_with_retry(ws.resize, rows=max(ws.row_count, total_rows), cols=max(ws.col_count, width))

    _call_with_retry(ws.update, values=padded, range_name=f"A1:{rowcol_to_a1(total_rows, width)}", raw=True)
    _call_with_retry(apply_format, ws, kinds, total_rows)
    log.info("Onglet '%s' mis à jour (%s).", SHEET_NAME, datetime.now().strftime("%Y-%m-%d %H:%M"))

    # Premier remplissage (aucun bilan existant) : pas d'email, tout serait "nouveau".
    if bilan_entries(existing):
        new_bilans = find_new_bilans(existing, grid)
        if new_bilans:
            log.info("%d nouveau(x) bilan(s) ajouté(s).", len(new_bilans))
            send_new_bilans_email(new_bilans)


if __name__ == "__main__":
    run()
