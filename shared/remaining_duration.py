"""Calendar-exact remaining term helpers, shared by every *_diversification.py
that fills the "durée" Google Sheet tab (see
shared.google_sheet.fill_remaining_duration_amounts)."""

from datetime import date, timedelta


def _add_months(start: date, months: int) -> date:
    index = start.year * 12 + start.month - 1 + months
    year, month = divmod(index, 12)
    month += 1
    next_month_start = date(year + (month == 12), month % 12 + 1, 1)
    last_day = (next_month_start - timedelta(days=1)).day
    return date(year, month, min(start.day, last_day))


def _months_and_days_between(start: date, end: date) -> tuple:
    months = (end.year - start.year) * 12 + end.month - start.month
    if _add_months(start, months) > end:
        months -= 1
    return (months, (end - _add_months(start, months)).days)


def remaining_months_and_days(today: date, maturity: date) -> tuple:
    """Calendar-exact remaining term as (whole months, leftover days), e.g.
    2026-10-16 -> 2029-07-15 is (32, 29). (0, 0) on the maturity day itself.
    A maturity already past (late loan) gives the lateness with both
    components negated, e.g. 2026-10-16 vs 2026-08-11 is (-2, -5), so late
    terms sort before every non-late one (most late first)."""
    if maturity == today:
        return (0, 0)
    if maturity < today:
        months, days = _months_and_days_between(maturity, today)
        return (-months, -days)
    return _months_and_days_between(today, maturity)


def base_term_between(start: date, end: date) -> tuple | None:
    """Durée de base d'un prêt émis le `start` et échéant le `end` : (mois entiers, jours restants), ex. 2024-03-15
    -> 2025-03-15 donne (12, 0). Pour absorber les décalages de calendrier (échéance la veille de l'anniversaire,
    report au jour ouvré, délai entre émission et mise en vente), un reliquat de 24 jours ou plus compte pour un mois de plus et un reliquat de 3 jours
    ou moins est ignoré dès qu'il y a au moins un mois : 2024-03-15 -> 2025-03-14 donne (12, 0). None si l'une des
    dates manque ou si `end` ne suit pas `start`."""
    if start is None or end is None or end <= start:
        return None
    months, days = _months_and_days_between(start, end)
    if days >= 24:
        return (months + 1, 0)
    return (months, 0) if months >= 1 and days <= 3 else (months, days)


def base_term_from_days(days) -> tuple | None:
    """Durée de base exprimée en nombre de jours (ex. 90, 365) -> (mois, jours) selon la même règle que
    `base_term_between` : 365 -> (12, 0), 90 -> (3, 0), 45 -> (1, 14), 20 -> (0, 20). None si `days` n'est pas un
    nombre positif."""
    try:
        days = int(round(float(days)))
    except (TypeError, ValueError):
        return None
    if days <= 0:
        return None
    reference = date(2001, 1, 1)
    return base_term_between(reference, reference + timedelta(days=days))


def term_in_months(term: tuple) -> float:
    """(mois, jours) -> nombre de mois (jours comptés pour 365,25 / 12 jour par mois)."""
    return term[0] + term[1] / (365.25 / 12)


def months_to_term(months: float) -> tuple:
    """Inverse de `term_in_months` : nombre de mois (ex. 6.13) -> (mois, jours) arrondi au jour, ex. (6, 4)."""
    whole = int(months)
    days = round((months - whole) * 365.25 / 12)
    if days >= 30:
        return (whole + 1, 0)
    return (whole, days)


class RateWeights:
    """Taux de rendement (%) des prêts, regroupés par (clé, durée) comme les montants de l'onglet "durée".

    Chaque `add()` ajoute un prêt ; le taux d'une ligne regroupant plusieurs prêts est la moyenne pondérée
    par leur montant (voir `weighted_rate()`). Un prêt sans taux ou sans montant positif est ignoré (il ne
    pèse ni sur le taux ni sur le dénominateur). `key` = société de prêt (ou projet pour une plateforme de projets),
    comparée sans tenir compte de la casse ni des espaces autour.

    `base_term` (optionnel, (mois, jours) : durée de base du prêt, de son émission à son échéance) alimente aussi
    l'onglet "taux" (voir `shared.rate_sheet.fill_rate_amounts`) : chaque prêt y est mémorisé avec son montant, sa
    durée de base et son taux (None si inconnu), même quand son taux est absent."""

    def __init__(self):
        self._sums = {}
        self._loans = []  # (clé comparable, nom affiché, durée de base, montant, taux en % ou None)

    @staticmethod
    def _key(key) -> str:
        return str(key).strip().casefold()

    def add(self, key, term: tuple, amount, rate_percent, base_term: tuple | None = None) -> None:
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            return
        try:
            rate = float(rate_percent)
        except (TypeError, ValueError):
            rate = None
        if rate is not None and rate != rate:  # NaN
            rate = None
        if amount <= 0:
            return
        if base_term is not None:
            self._loans.append((self._key(key), str(key).strip(), (base_term[0], base_term[1]), amount, rate))
        if rate is None:
            return
        weighted_sum, base = self._sums.get((self._key(key), term), (0.0, 0.0))
        self._sums[(self._key(key), term)] = (weighted_sum + amount * rate, base + amount)

    def loans(self) -> list:
        """[(clé comparable, nom affiché, (mois, jours) de base, montant, taux % ou None)] des prêts ajoutés avec
        une durée de base."""
        return list(self._loans)

    def entry(self, key, term: tuple) -> tuple:
        """(somme montant x taux, somme des montants) du couple (clé, durée) ; (0.0, 0.0) si inconnu."""
        return self._sums.get((self._key(key), term), (0.0, 0.0))


def weighted_rate(entries) -> float | None:
    """Moyenne pondérée par montant (en %) de plusieurs `RateWeights.entry()`, None sans montant."""
    weighted_sum = sum(entry[0] for entry in entries)
    base = sum(entry[1] for entry in entries)
    return weighted_sum / base if base > 0 else None


def overdue_months_and_days(today: date, due: date, default: bool = False):
    """Term of a loan that is late (or in default) since the missed payment
    date `due`, as a duration key for the "durée" sheet: late loans use the
    negated (months, days) of remaining_months_and_days() ("2 mois 5 jours de
    retard"), defaulted ones (months, days, "default") ("2 mois 5 jours de
    défaut"). None if `due` isn't in the past (the platform's date can't
    explain any lateness)."""
    if due >= today:
        return None
    months, days = _months_and_days_between(due, today)
    return (months, days, "default") if default else (-months, -days)
