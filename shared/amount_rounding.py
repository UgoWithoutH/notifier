"""Arrondi des montants au centime sans perdre la somme.

Les onglets "durée", "taux" et "répartition géographique" regroupent les mêmes prêts de façons différentes
(par durée restante, par durée de base, par pays). Arrondir chaque ligne séparément fait dériver le total de
quelques centimes d'un onglet à l'autre ; ici les lignes sont arrondies de sorte que leur somme soit exactement
le total voulu."""

from math import floor


def round_to_total(amounts: dict, total: float | None = None) -> dict:
    """{clé: montant} -> {clé: montant arrondi au centime}, dont la somme vaut exactement `round(total, 2)`
    (le total des montants si `total` est None). Les montants sont d'abord remis à l'échelle du total voulu
    (écart de quelques centimes, ex. intérêts courus pas encore dans les lignes), puis arrondis par la méthode
    du plus fort reste. Sans montant positif à répartir, chaque montant est simplement arrondi."""
    if not amounts:
        return {}
    current = sum(amounts.values())
    if current <= 0:
        return {key: round(amount, 2) for key, amount in amounts.items()}

    target_cents = round((current if total is None else total) * 100)
    scale = target_cents / (current * 100)
    scaled = {key: amount * 100 * scale for key, amount in amounts.items()}
    cents = {key: floor(value) for key, value in scaled.items()}
    missing = target_cents - sum(cents.values())
    for key in sorted(scaled, key=lambda k: scaled[k] - cents[k], reverse=True)[:max(missing, 0)]:
        cents[key] += 1
    return {key: value / 100 for key, value in cents.items()}
