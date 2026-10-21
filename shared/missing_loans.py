"""Alerte par mail quand une société active dans l'onglet "config robots" reste
introuvable sur la plateforme (nom inconnu / mal orthographié).

Une société qui a déjà été trouvée une fois n'est plus jamais signalée (elle
n'a simplement plus de prêt pour l'instant, les prêts étant très volatils).
Une société jamais trouvée est signalée une seule fois, après `grace_seconds`
sans avoir été trouvée (0 = dès que son absence est établie). Si elle
disparaît ensuite du suivi (ligne désactivée/supprimée), son état est effacé."""

import logging
import time

from shared.notifier import send_missing_loans_email
from shared.robot_config import _norm

log = logging.getLogger(__name__)

_KEY_PREFIX = "missing:"
# Platforms where "not found" can't be told from "no loan right now" (Swaper,
# Afranga): a never-seen company is only reported after this long.
TRANSIENT_GRACE_SECONDS = 6 * 3600


def report_missing_loans(platform: str, found: dict, gates: dict, all_names: list | None = None,
                         grace_seconds: float = 0, detail: str | None = None) -> list:
    """`found` : {nom dans la sheet: True si la société a été trouvée} - ne
    contient que les noms dont le résultat est établi ce run-ci.
    `gates` : sous-dict d'un fichier d'état JSON, conservé d'un run à l'autre.
    `all_names` : tous les noms suivis (défaut : les clés de `found`), l'état
    des autres noms est supprimé.
    Retourne les noms signalés par mail ce run-ci."""
    now = time.time()
    tracked = {_KEY_PREFIX + _norm(name) for name in (all_names if all_names is not None else found)}
    for key in [k for k in gates if k.startswith(_KEY_PREFIX) and k not in tracked]:
        del gates[key]

    newly_missing = []
    for name, is_found in found.items():
        gate = gates.setdefault(_KEY_PREFIX + _norm(name), {"ever_found": False, "missing_since": None, "notified": False})
        if is_found:
            gate.update(ever_found=True, missing_since=None, notified=False)
            continue
        if gate["ever_found"]:
            continue
        if gate["missing_since"] is None:
            gate["missing_since"] = now
        log.warning("%s : société '%s' de la sheet introuvable.", platform, name)
        if not gate["notified"] and now - gate["missing_since"] >= grace_seconds:
            gate["notified"] = True
            newly_missing.append(name)

    if newly_missing:
        send_missing_loans_email(platform, newly_missing, detail)
    return newly_missing
