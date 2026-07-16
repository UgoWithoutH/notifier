"""Shared notification-gate logic, used by both swaper_monitor.py and
lendermarket_monitor.py to decide whether an email should be sent.

The rule (same for both monitors): notify once while something is
"available" for a given key, and only re-open the gate once that key
becomes unavailable again (drops to 0) - not on every fluctuation while it
stays available. This is what avoids spamming while still catching genuinely
new opportunities.

`gate_state` is a plain dict (typically a sub-dict of a JSON state file)
mapping an arbitrary key (e.g. "swaper", or a Lendermarket segment key) to
{"notified": bool}.
"""


def should_notify(gate_state: dict, key: str, available: bool, record: bool = True) -> tuple:
    """Decide whether to send a notification for `key` right now.

    Returns (should_send, was_reset):
    - should_send: True exactly once each time `available` goes from
      False/unseen to True and stays True, until it goes back to False.
    - was_reset: True if the gate for `key` was just closed because
      `available` is False and it had previously been notified.

    If `record` is False, the decision is computed but not persisted into
    `gate_state` when `available` is True - used for a forced/manual test
    send that must not consume the real notification gate.
    """
    gate = gate_state.get(key) or {"notified": False}

    if not available:
        was_reset = bool(gate.get("notified"))
        gate_state[key] = {"notified": False}
        return False, was_reset

    already_notified = bool(gate.get("notified", False))
    should_send = not already_notified
    if record:
        gate_state[key] = {"notified": True if should_send else already_notified}
    return should_send, False
