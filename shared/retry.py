import logging
import time

log = logging.getLogger("retry")


def retry_call(fn, what: str, attempts: int = 3, delay: float = 5.0):
    """Call `fn()`, retrying on any exception; re-raises the last one so the caller never substitutes a fake 0."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception:
            if attempt == attempts:
                raise
            log.warning("Failed to fetch %s (attempt %d/%d) - retrying in %.0fs.", what, attempt, attempts, delay, exc_info=True)
            time.sleep(delay)
