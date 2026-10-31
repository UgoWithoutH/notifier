"""One-off fix of a *_duration_status_state.json history: moves every recorded "default" share into "late".

Used after a platform's default rule changed (a loan whose buyback is still expected is late, not a real default),
so the already recorded days don't keep counting those loans as defaulted.

Usage: python scripts/reclassify_duration_defaults.py <state file> [<state file> ...]
"""

import json
import sys
from pathlib import Path


def reclassify(path: Path) -> int:
    """Rewrites `path` in place, returns how many (day, originator) entries had a default share."""
    data = json.loads(path.read_text(encoding="utf-8"))
    changed = 0
    for shares_by_key in data.get("days", {}).values():
        for shares in shares_by_key.values():
            normal, late, default = shares
            if default:
                shares[:] = [normal, late + default, 0.0]
                changed += 1
    path.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    return changed


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        file = Path(arg)
        if not file.exists():
            print(f"{file}: not found, skipped")
            continue
        print(f"{file}: {reclassify(file)} entr(y/ies) reclassified from default to late")
