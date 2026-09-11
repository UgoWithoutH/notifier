"""Generic `requests.Session` persistence (cookies + headers, e.g. a
bearer `Authorization` token set on `session.headers` after login), shared
by every pure-HTTP `*_diversification.py` module that wants to reuse a
login session across separate process invocations - most importantly
across every month of a `scripts/run_diversification_for_month_range.sh`
backfill (which runs `python -m <module>` once per month, each a brand-new
process/session otherwise) instead of doing a real login for every single
month.

Callers are expected to:
    1. `extra = load_session_state(session, path)` right after creating the
       `requests.Session()` - returns the persisted `extra` dict (possibly
       empty `{}`) if a persisted state existed and was restored, or
       `None` if there was nothing to load. This does NOT prove the
       session still works, only that something was found on disk.
    2. Try the platform's own first real authenticated call. If it fails
       AND a persisted state was loaded (`extra is not None`), log in
       again for real (the persisted session/token has likely expired)
       and retry once; if no persisted state existed, let the failure
       propagate as before.
    3. `save_session_state(session, path, extra=...)` right after a
       successful login (whether that login was skipped via reuse or
       freshly performed) so the next invocation can try reusing it too.
       `extra` is for anything a platform's auth needs that isn't stored
       on the session itself (e.g. Iuvo's `session_token` query param, or
       Lendermarket's `investorId`/Debitum's/Nectaro's bearer token passed
       explicitly to each call instead of living on `session.headers`).
"""

import json
import logging
from pathlib import Path

import requests

log = logging.getLogger(__name__)


def save_session_state(session: requests.Session, path: Path, extra: dict | None = None) -> None:
    """Persist `session`'s cookies (name/value/domain/path/secure/expires),
    headers (e.g. a bearer `Authorization` token set after login), and any
    caller-provided `extra` metadata (e.g. a token/id not stored on the
    session itself) to `path` as JSON."""
    data = {
        "cookies": [
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path,
                "secure": cookie.secure,
                "expires": cookie.expires,
            }
            for cookie in session.cookies
        ],
        "headers": dict(session.headers),
        "extra": extra or {},
    }
    path.write_text(json.dumps(data), encoding="utf-8")


def load_session_state(session: requests.Session, path: Path) -> dict | None:
    """Restore a previously persisted `session` state (if any) into
    `session`'s own cookie jar/headers. Returns the persisted `extra` dict
    (possibly empty) if a state file existed and was restored, or `None`
    if there was nothing to load - callers must still verify the session
    actually works with a real authenticated call, since a token/cookie
    can have expired since it was saved."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        log.warning("Could not parse persisted session state at %s - ignoring.", path)
        return None

    for cookie in data.get("cookies", []):
        session.cookies.set(
            cookie["name"],
            cookie["value"],
            domain=cookie.get("domain") or "",
            path=cookie.get("path") or "/",
            secure=cookie.get("secure", False),
            expires=cookie.get("expires"),
        )
    headers = data.get("headers") or {}
    if headers:
        session.headers.update(headers)
    return data.get("extra") or {}
