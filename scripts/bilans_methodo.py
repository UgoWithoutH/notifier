"""Méthode de notation des bilans lue directement dans la page de liberte-financiere.net :
pondérations, barèmes par critère et règles de lecture (rien n'est saisi à la main).

Le HTML rendu côté serveur est stable : chaque grille est un <table> "Critère / Ce qu'on
mesure / Calcul / Poids" dont chaque <tbody> contient la ligne du critère puis une ligne
de détail (explication + barème <li> "seuil / note").
"""
import html
import re
import unicodedata

CRITERIA_KEYS = {"rentabilite": "rentabilite", "fondspropres": "fonds_propres", "impayes": "impayes",
                 "tresorerie": "tresorerie", "fiabilite": "fiabilite"}
GRID_BY_SECTION = {"note-calcul": ("preteur", "Société de prêt"), "note-plateformes": ("plateforme", "Plateforme")}
SKIPPED_SECTIONS = {"note-calcul", "note-plateformes"}  # critères et barèmes : onglet dédié


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    return re.sub(r"[^a-z0-9]", "", "".join(c for c in text if not unicodedata.combining(c)).casefold())


def _clean(page: str) -> str:
    page = re.sub(r"<script.*?</script>", "", page, flags=re.S)
    return re.sub(r' (?:class|style)="[^"]*"', "", page)


def _text(fragment: str) -> str:
    fragment = re.sub(r"<!--.*?-->", "", fragment, flags=re.S)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", fragment))).strip()


def _lines(fragment: str) -> list[str]:
    fragment = re.sub(r"<!--.*?-->", "", fragment, flags=re.S)
    fragment = re.sub(r"</span>\s*<span[^>]*>", " | ", fragment)
    fragment = re.sub(r"</?(?:p|li|div|ul|ol|h[1-6]|tr|table|section)\b[^>]*>", "\n", fragment)
    fragment = re.sub(r"<[^>]+>", "", fragment)
    lines = (re.sub(r"\s+", " ", html.unescape(line)).strip() for line in fragment.split("\n"))
    return [line for line in lines if line and not line.isdigit()]


def _sections(page: str) -> list[tuple[str, str, str]]:
    """(id, titre, html) de chaque section "note-*" (jusqu'au titre h2/h3 suivant)."""
    headings = list(re.finditer(r'<h([23])([^>]*)>(.*?)</h\1>', page, flags=re.S))
    out = []
    for i, h in enumerate(headings):
        m = re.search(r'id="(note-[^"]+)"', h.group(2))
        if not m:
            continue
        end = headings[i + 1].start() if i + 1 < len(headings) else len(page)
        out.append((m.group(1), _text(h.group(3)), page[h.end():end]))
    return out


def _criteria(section_html: str, grid_key: str, grid_label: str) -> list[dict]:
    criteria = []
    for table in re.findall(r"<table[^>]*>(.*?)</table>", section_html, flags=re.S):
        if "<th>Critère</th>" not in table:
            continue
        for body in table.split("<tbody")[1:]:
            rows = re.findall(r"<tr[^>]*>(.*?)</tr>", body, flags=re.S)
            cells = re.findall(r"<td[^>]*>(.*?)</td>", rows[0], flags=re.S) if rows else []
            if len(cells) < 4:
                continue
            name = _text(re.sub(r"<span>\d+</span>", "", re.sub(r"<label.*?</label>", "", cells[0], flags=re.S), count=1))
            weight = re.search(r"\d+(?:[.,]\d+)?", _text(cells[3]))
            detail = rows[1] if len(rows) > 1 else ""
            scale = []
            for li in re.findall(r"<li[^>]*>(.*?)</li>", detail, flags=re.S):
                spans = [s for s in (_text(x) for x in re.findall(r"<span[^>]*>(.*?)</span>", li, flags=re.S)) if s]
                if len(spans) >= 2:
                    scale.append((spans[0], spans[-1]))
            criteria.append({
                "grid": grid_key, "grid_label": grid_label, "name": name,
                "weight": float(weight.group(0).replace(",", ".")) if weight else None,
                "measures": _text(cells[1]), "formula": _text(cells[2]),
                "explanation": "\n".join(_text(p) for p in re.findall(r"<p[^>]*>(.*?)</p>", detail, flags=re.S)),
                "scale": scale,
            })
    return criteria


def parse_methodo(page: str) -> dict:
    """{"criteria": [...], "rules": [(rubrique, ligne)], "weights": {grille: {critère: poids 0-1}}}."""
    page = _clean(page)
    criteria, rules = [], []
    for sid, title, body in _sections(page):
        if sid in GRID_BY_SECTION:
            criteria += _criteria(body, *GRID_BY_SECTION[sid])
        if sid in SKIPPED_SECTIONS:
            intro = body.split("<table", 1)[0]
            rules += [(title, _text(p)) for p in re.findall(r"<p[^>]*>(.*?)</p>", intro, flags=re.S)]
        else:
            for line in _lines(body):
                if line == "Table des matières":  # sommaire en bas de page, hors méthode
                    break
                rules.append((title, line))
    weights: dict[str, dict[str, float]] = {}
    for c in criteria:
        key = next((v for k, v in CRITERIA_KEYS.items() if _norm(c["name"]).startswith(k)), None)
        if key and c["weight"] is not None:
            weights.setdefault(c["grid"], {})[key] = c["weight"] / 100
    weights = {g: w for g, w in weights.items() if abs(sum(w.values()) - 1) < 0.001}
    return {"criteria": criteria, "rules": rules, "weights": weights}
