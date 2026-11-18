"""Détection des nouveautés de liberte-financiere.net que les scripts de synchronisation ne
connaissent pas (nouveau champ, critère, tableau, code...).

Chaque script déclare ce qu'il connaît (lu ou volontairement ignoré). Tout le reste est signalé
dans l'email d'alerte, à chaque lancement, jusqu'à ce que le script soit mis à jour."""
import json
import logging
from pathlib import Path

from shared.notifier import send_site_novelties_email
from shared.state import load_state, save_state

log = logging.getLogger(__name__)

PREFIX = "Nouveauté du site non intégrée"


def _sample(value) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= 80 else text[:79] + "…"


def new_keys(label: str, items: list, known: set[str]) -> list[str]:
    """Clés des dictionnaires `items` absentes de `known`, avec un exemple de valeur."""
    samples: dict[str, object] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        for key, value in item.items():
            if key not in known and (key not in samples or samples[key] in (None, "", [], {})):
                samples[key] = value
    return [f"{PREFIX} : {label}, champ « {key} » (exemple : {_sample(value)})"
            for key, value in sorted(samples.items())]


def new_values(label: str, values, known) -> list[str]:
    """Valeurs observées absentes de `known` (code pays, thème, type de compte...)."""
    unknown = sorted({str(v) for v in values if v not in (None, "")} - {str(k) for k in known})
    return [f"{PREFIX} : {label}, valeur inconnue « {v} »" for v in unknown]


def split_novelties(issues: list[str]) -> tuple[list[str], list[str]]:
    """(anomalies, nouveautés du site)."""
    novelties = [i for i in issues if i.startswith(PREFIX)]
    return [i for i in issues if not i.startswith(PREFIX)], novelties


def _identity(novelty: str) -> str:
    return novelty.split(" (exemple :")[0]  # l'exemple de valeur peut varier d'un lancement à l'autre


def notify_novelties(site: str, novelties: list[str], state_file: Path) -> None:
    """Envoie un email pour les seules nouveautés jamais signalées. Elles sont mémorisées dans
    `state_file` seulement si l'email est parti : sinon elles seront de nouveau signalées."""
    state = load_state(state_file, {"notified": []})
    notified = set(state["notified"])
    fresh = [n for n in novelties if _identity(n) not in notified]
    if not fresh:
        log.info("Aucune nouvelle nouveauté du site à signaler (%d déjà signalée(s)).", len(novelties))
        return
    if send_site_novelties_email(site, fresh):
        state["notified"] = sorted(notified | {_identity(n) for n in fresh})
        save_state(state_file, state)
