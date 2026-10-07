"""Configuration des robots d'investissement (Lendermarket, Swaper, PeerBerry)
lue depuis l'onglet Google Sheets "config robots".

Structure de l'onglet (une ligne d'en-tête, repérée par la colonne "Nom",
puis un bloc par plateforme : la ligne de la plateforme suivie d'une ligne
par loan/lender/loan originator jusqu'à la plateforme ou la section
suivante) :

- Nom : nom de la plateforme (ligne de bloc) ou du loan.
- Pays : pays du loan (peut être vide).
- Actif : "x" = le robot peut investir sur ce loan.
- Taux min / Taux max : bornes du taux d'intérêt des prêts acceptés
  (vide = pas de borne).
- Pourcentage max du solde par pays : sur la ligne de la plateforme,
  plafond par pays en % du solde total (investi + non investi).
- Pourcentage max du solde par loan : plafond par loan en % du même solde.
- Montant min / max par investissement : bornes d'un investissement dans
  un prêt.
- Répartition équivalente entre les prêts si possible : "x" = le budget
  est réparti à parts égales entre les prêts disponibles du run.
- Actif marché secondaire : "x" = le robot peut acheter ce loan sur le marché
  secondaire (colonnes optionnelles ci-dessous, ignorées sur le marché primaire).
- Durée restante min / max : durée restante du prêt, en mois.
- décote / prime min / max : en % du principal, négatif = décote, positif = prime.
"""

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from math import floor, inf

from shared.google_sheet import _call_with_retry, get_worksheet_by_name

log = logging.getLogger(__name__)

CONFIG_SHEET_NAME = "config robots"

# Lignes qui ouvrent un nouveau bloc (autres plateformes/sections) et donc
# terminent le bloc de la plateforme courante - un loan ne porte jamais l'un
# de ces noms.
_BLOCK_BOUNDARY_NAMES = {
    "crowdlending", "afranga", "bienpreter", "debitum",
    "income marketplace", "iuvo", "lendermarket", "loanch", "mintos", "nectaro",
    "peerberry", "swaper", "crowdlending savings", "monefit", "go & grow",
    "crowdlending agricole", "lande", "crowdfunding immobilier", "bricks", "bourse",
}

_COLUMN_PREFIXES = {
    "name": "nom",
    "country": "pays",
    "active": "actif",
    "min_rate": "taux min",
    "max_rate": "taux max",
    "country_pct": "pourcentage max du solde par pays",
    "loan_pct": "pourcentage max du solde par loan",
    "min_amount": "montant min",
    "max_amount": "montant max",
    "equal_split": "repartition equivalente",
}

# Colonnes facultatives : absentes = fonctionnalité marché secondaire désactivée.
_OPTIONAL_COLUMN_PREFIXES = {
    "secondary_active": "actif marche secondaire",
    "min_months": "duree restante min",
    "max_months": "duree restante max",
    "min_premium": "decote / prime min",
    "max_premium": "decote / prime max",
}


def _norm(text) -> str:
    ascii_text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", ascii_text).strip().casefold()


def _parse_number(raw):
    """'8,00%' / '10,00 €' / '1 234,5' -> float ; vide/non numérique -> None."""
    cleaned = re.sub(r"[\s\u202f\u00a0€%]", "", str(raw or "")).replace(",", ".")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


@dataclass
class LoanConfig:
    name: str
    country: str | None
    active: bool
    min_rate: float | None
    max_rate: float | None
    max_loan_pct: float | None
    min_amount: float | None
    max_amount: float | None
    equal_split: bool
    secondary_active: bool = False
    min_months: float | None = None
    max_months: float | None = None
    min_premium: float | None = None
    max_premium: float | None = None

    def secondary_rejection_reason(self, rate, premium, months) -> str | None:
        """None si un prêt du marché secondaire (taux, prime en % -
        négatif = décote, durée restante en mois) peut être acheté."""
        if not self.secondary_active:
            return "loan non actif sur le marché secondaire"
        for label, value, low, high in (
            ("taux", rate, self.min_rate, self.max_rate),
            ("décote/prime", premium, self.min_premium, self.max_premium),
            ("durée restante", months, self.min_months, self.max_months),
        ):
            if low is None and high is None:
                continue
            if value is None:
                return f"{label} inconnu(e)"
            if low is not None and value < low:
                return f"{label} {value} < min {low}"
            if high is not None and value > high:
                return f"{label} {value} > max {high}"
        return None

    def rejection_reason(self, rate) -> str | None:
        """None si un prêt de taux `rate` peut être pris pour ce loan."""
        if not self.active:
            return "loan non actif"
        if self.min_rate is not None or self.max_rate is not None:
            if rate is None:
                return "taux du prêt inconnu"
            if self.min_rate is not None and rate < self.min_rate:
                return f"taux {rate}% < taux min {self.min_rate}%"
            if self.max_rate is not None and rate > self.max_rate:
                return f"taux {rate}% > taux max {self.max_rate}%"
        return None


@dataclass
class PlatformConfig:
    platform: str
    country_max_pct: float | None
    loans: dict = field(default_factory=dict)

    def active_names(self) -> list:
        return [name for name, loan in self.loans.items() if loan.active]

    def find_loan(self, raw_name) -> LoanConfig | None:
        """Retrouve le loan dont le nom correspond à `raw_name` (exact puis
        sous-chaîne, insensible à la casse/aux accents)."""
        value = _norm(raw_name)
        if not value:
            return None
        for name, loan in self.loans.items():
            if _norm(name) == value:
                return loan
        for name, loan in self.loans.items():
            normalized = _norm(name)
            if normalized in value or value in normalized:
                return loan
        return None

    def secondary_names(self) -> list:
        return [name for name, loan in self.loans.items() if loan.secondary_active]

    def lowest_min_rate(self, secondary: bool = False) -> float | None:
        """Plus petit taux min parmi les loans actifs (None si au moins un
        loan actif n'a pas de taux min) - sert de pré-filtre côté API."""
        rates = []
        for loan in self.loans.values():
            if not (loan.secondary_active if secondary else loan.active):
                continue
            if loan.min_rate is None:
                return None
            rates.append(loan.min_rate)
        return min(rates) if rates else None

    def secondary_bounds(self, low_attr: str, high_attr: str) -> tuple:
        """(min des bornes basses, max des bornes hautes) sur les loans actifs
        du marché secondaire ; None si un loan n'a pas cette borne - sert de
        pré-filtre côté API."""
        loans = [loan for loan in self.loans.values() if loan.secondary_active]
        lows = [getattr(loan, low_attr) for loan in loans]
        highs = [getattr(loan, high_attr) for loan in loans]
        low = None if not lows or any(v is None for v in lows) else min(lows)
        high = None if not highs or any(v is None for v in highs) else max(highs)
        return low, high


def get_platform_config(platform: str) -> PlatformConfig:
    """Lit le bloc `platform` de l'onglet "config robots"."""
    worksheet = get_worksheet_by_name(CONFIG_SHEET_NAME)
    grid = _call_with_retry(worksheet.get_all_values)

    header_idx = next((i for i, row in enumerate(grid) if row and _norm(row[0]) == "nom"), None)
    if header_idx is None:
        raise RuntimeError(f"En-tête 'Nom' introuvable dans l'onglet '{CONFIG_SHEET_NAME}'.")

    all_prefixes = {**_COLUMN_PREFIXES, **_OPTIONAL_COLUMN_PREFIXES}
    columns = {}
    for col_idx, header in enumerate(grid[header_idx]):
        normalized = _norm(header)
        matches = [(len(prefix), key) for key, prefix in all_prefixes.items() if normalized.startswith(prefix)]
        if matches:
            key = max(matches)[1]
            columns.setdefault(key, col_idx)
    missing = [key for key in _COLUMN_PREFIXES if key not in columns]
    if missing:
        raise RuntimeError(f"Colonnes introuvables dans '{CONFIG_SHEET_NAME}' : {missing}")

    def cell(row, key) -> str:
        idx = columns.get(key)
        return row[idx].strip() if idx is not None and idx < len(row) else ""

    target = _norm(platform)
    platform_idx = next(
        (i for i in range(header_idx + 1, len(grid)) if _norm(cell(grid[i], "name")) == target),
        None,
    )
    if platform_idx is None:
        raise RuntimeError(f"Plateforme '{platform}' introuvable dans l'onglet '{CONFIG_SHEET_NAME}'.")

    config = PlatformConfig(
        platform=platform,
        country_max_pct=_parse_number(cell(grid[platform_idx], "country_pct")),
    )

    for row in grid[platform_idx + 1:]:
        name = cell(row, "name")
        if not name:
            continue
        if _norm(name) in _BLOCK_BOUNDARY_NAMES:
            break
        config.loans[name] = LoanConfig(
            name=name,
            country=cell(row, "country") or None,
            active=cell(row, "active").lower() == "x",
            min_rate=_parse_number(cell(row, "min_rate")),
            max_rate=_parse_number(cell(row, "max_rate")),
            max_loan_pct=_parse_number(cell(row, "loan_pct")),
            min_amount=_parse_number(cell(row, "min_amount")),
            max_amount=_parse_number(cell(row, "max_amount")),
            equal_split=cell(row, "equal_split").lower() == "x",
            secondary_active=cell(row, "secondary_active").lower() == "x",
            min_months=_parse_number(cell(row, "min_months")),
            max_months=_parse_number(cell(row, "max_months")),
            min_premium=_parse_number(cell(row, "min_premium")),
            max_premium=_parse_number(cell(row, "max_premium")),
        )

    log.info(
        "Config robot %s : %d loan(s) dont %d actif(s) et %d actif(s) marché secondaire, plafond par pays=%s%%.",
        platform, len(config.loans), len(config.active_names()), len(config.secondary_names()), config.country_max_pct,
    )
    return config


class CapTracker:
    """Plafonds par pays / par loan, en % du budget total (investi + non
    investi). `country_invested`/`loan_invested` sont conservés par
    référence et mis à jour par l'appelant (add() ou resynchronisation)."""

    def __init__(self, config: PlatformConfig, total_budget: float, country_invested: dict,
                 loan_invested: dict, loan_countries: dict):
        self.config = config
        self.total_budget = total_budget
        self.country_invested = country_invested
        self.loan_invested = loan_invested
        self.loan_countries = loan_countries

    def country_of(self, loan_name: str) -> str | None:
        return self.loan_countries.get(loan_name)

    def country_cap_amount(self) -> float | None:
        if self.config.country_max_pct is None or self.total_budget <= 0:
            return None
        return self.config.country_max_pct / 100.0 * self.total_budget

    def loan_cap_amount(self, loan_name: str) -> float | None:
        loan = self.config.loans.get(loan_name)
        if loan is None or loan.max_loan_pct is None or self.total_budget <= 0:
            return None
        return loan.max_loan_pct / 100.0 * self.total_budget

    def country_room(self, country: str | None, extra: float = 0.0) -> float:
        cap = self.country_cap_amount()
        if cap is None or not country:
            return inf
        return max(0.0, cap - self.country_invested.get(country, 0.0) - extra)

    def loan_room(self, loan_name: str, extra: float = 0.0) -> float:
        cap = self.loan_cap_amount(loan_name)
        if cap is None:
            return inf
        return max(0.0, cap - self.loan_invested.get(loan_name, 0.0) - extra)

    def add(self, loan_name: str, amount: float) -> None:
        self.loan_invested[loan_name] = self.loan_invested.get(loan_name, 0.0) + amount
        country = self.country_of(loan_name)
        if country:
            self.country_invested[country] = self.country_invested.get(country, 0.0) + amount

    def blocked_countries(self) -> list:
        cap = self.country_cap_amount()
        if cap is None:
            return []
        return sorted(c for c, amount in self.country_invested.items() if amount >= cap)

    def blocked_loans(self) -> list:
        return sorted(
            name for name in self.config.loans
            if self.loan_cap_amount(name) is not None and self.loan_room(name) <= 0
        )

    def country_status(self, countries=None) -> dict:
        """{pays: {"invested", "threshold_amount", "blocked"}} pour les emails
        (tous les pays connus du tracker si `countries` est None)."""
        cap = self.country_cap_amount()
        names = set(countries) if countries is not None else set(self.country_invested)
        return {
            country: {
                "invested": self.country_invested.get(country, 0.0),
                "threshold_amount": cap,
                "blocked": cap is not None and self.country_invested.get(country, 0.0) >= cap,
            }
            for country in names
        }

    def loan_cap_status(self) -> dict:
        """{loan: {"invested", "max_percentage", "threshold_amount", "blocked"}}
        pour les loans ayant un plafond configuré."""
        return {
            name: {
                "invested": self.loan_invested.get(name, 0.0),
                "max_percentage": loan.max_loan_pct,
                "threshold_amount": self.loan_cap_amount(name),
                "blocked": self.loan_room(name) <= 0,
            }
            for name, loan in self.config.loans.items()
            if loan.max_loan_pct is not None and loan.active
        }


@dataclass
class Candidate:
    id: object
    loan_name: str
    available: float
    rate: float | None


def _floor2(value: float) -> float:
    return floor(value * 100 + 1e-9) / 100


def _split_equally(budget: float, items: list) -> dict:
    """items: [(id, cap, min_amount)] dans l'ordre du listing. Parts égales ;
    un prêt dont le plafond est inférieur à la part est plafonné et l'excédent
    redistribué ; si une part est sous le minimum d'un prêt, le dernier prêt
    de la liste est abandonné."""
    active = [item for item in items if item[1] > 0 and item[1] >= item[2]]
    shares = {}
    remaining = budget
    while active and remaining > 0:
        equal = remaining / len(active)
        if any(min_amount > equal + 1e-9 for _id, _cap, min_amount in active):
            active = active[:-1]
            continue
        capped = [item for item in active if item[1] <= equal]
        if capped:
            for loan_id, cap, _min in capped:
                shares[loan_id] = _floor2(cap)
                remaining -= cap
            active = [item for item in active if item not in capped]
            continue
        for loan_id, _cap, _min in active:
            shares[loan_id] = _floor2(equal)
        break
    return shares


def plan_allocations(candidates: list, tracker: CapTracker, budget: float, default_min_amount: float) -> dict:
    """Répartit `budget` entre les prêts candidats (dans l'ordre du listing)
    et retourne {id: montant}.

    Un prêt est écarté si son loan est inactif, hors bornes de taux, ou si les
    plafonds par pays / par loan sont atteints. Le montant d'un prêt est borné
    par sa disponibilité, le montant max configuré et la place restante sous
    les plafonds, et doit être >= au montant min configuré (à défaut
    `default_min_amount`). Les loans "répartition équivalente" sont servis en
    premier à parts égales ; les autres le sont ensuite séquentiellement
    (le premier prêt prend le maximum possible, puis le suivant, etc.)."""
    config = tracker.config
    equal_items, greedy_items = [], []
    for candidate in candidates:
        loan = config.loans.get(candidate.loan_name)
        if loan is None or loan.rejection_reason(candidate.rate) is not None:
            continue
        min_amount = loan.min_amount if loan.min_amount is not None else default_min_amount
        max_amount = loan.max_amount if loan.max_amount else inf
        country = tracker.country_of(candidate.loan_name)
        cap = min(
            candidate.available, max_amount,
            tracker.loan_room(candidate.loan_name), tracker.country_room(country),
        )
        item = (candidate.id, cap, min_amount, candidate.loan_name, country)
        (equal_items if loan.equal_split else greedy_items).append(item)

    plan = {}
    spent_loan: dict = {}
    spent_country: dict = {}
    remaining = budget

    def commit(item, amount):
        nonlocal remaining
        loan_id, _cap, min_amount, loan_name, country = item
        amount = min(
            amount, remaining,
            tracker.loan_room(loan_name, spent_loan.get(loan_name, 0.0)),
            tracker.country_room(country, spent_country.get(country, 0.0)),
        )
        amount = _floor2(amount)
        if amount <= 0 or amount < min_amount:
            return
        plan[loan_id] = amount
        remaining -= amount
        spent_loan[loan_name] = spent_loan.get(loan_name, 0.0) + amount
        if country:
            spent_country[country] = spent_country.get(country, 0.0) + amount

    if equal_items:
        shares = _split_equally(remaining, [(i[0], i[1], i[2]) for i in equal_items])
        for item in equal_items:
            if item[0] in shares:
                commit(item, shares[item[0]])

    for item in greedy_items:
        if item[1] > 0:
            commit(item, item[1])

    return plan


def build_tracker(config: PlatformConfig, balance: float, geo_snapshot: dict | None) -> CapTracker:
    """CapTracker amorcé avec le relevé de "Répartition géographique" (montants
    déjà investis par pays et par loan, tous loans confondus, actifs ou non).

    Budget total = solde disponible `balance` + total investi (somme des
    montants par pays). Pays d'un loan : colonne Pays de la config, à défaut
    le pays unique déduit de la feuille "Répartition géographique". Les noms
    de loans de la feuille géographique sont réalignés sur ceux de la config
    quand ils correspondent."""
    geo_snapshot = geo_snapshot or {}
    country_invested = dict(geo_snapshot.get("country_amounts") or {})

    def aligned(name: str) -> str:
        loan = config.find_loan(name)
        return loan.name if loan else name

    loan_invested = {}
    for name, amount in (geo_snapshot.get("loan_invested") or {}).items():
        key = aligned(name)
        loan_invested[key] = loan_invested.get(key, 0.0) + amount

    loan_countries = {aligned(name): country for name, country in (geo_snapshot.get("loan_countries") or {}).items()}
    for name, loan in config.loans.items():
        if loan.country:
            loan_countries[name] = loan.country

    total_budget = balance + sum(country_invested.values())
    return CapTracker(config, total_budget, country_invested, loan_invested, loan_countries)
