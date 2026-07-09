"""Shared bot-evasion helpers for the Playwright-driven monitors (Swaper,
Lendermarket, PeerBerry): a rotating pool of realistic desktop Chrome
UA/viewport combos (instead of Playwright's default, which advertises
"HeadlessChrome" or a mismatched version - one of the easiest bot signals),
an init script that patches over the most common "is this a real browser?"
checks, and behavioral helpers (human_pause/human_mouse_wander/human_type,
at the bottom of this file) that avoid the instant, uniformly-timed
interactions (`.fill()`, zero mouse movement) that behavioral bot detection
watches for.

IMPORTANT - only enabled where it's safe to:
on a network that inspects/validates the browser fingerprint (e.g. a
corporate proxy), overriding `user_agent` makes it mismatch the real
Chromium's Client-Hints (Sec-CH-UA) and can get the whole request blocked
outright as an "obsolete browser" - verified against the Michelin corporate
network on 2026-07-08. So this rotation is only applied when NOT running
locally: detected via the `GITHUB_ACTIONS` env var (set to "true" by GitHub
Actions runners), overridable with FORCE_BROWSER_STEALTH=true/false for
testing. When disabled, Playwright's own real UA/fingerprint is used as-is,
which avoids that mismatch entirely. The behavioral helpers are unaffected
by this flag - they don't touch the UA/fingerprint, so they're always used.
"""

import os
import random
import time

_force = os.environ.get("FORCE_BROWSER_STEALTH")
STEALTH_ENABLED = (_force.lower() == "true") if _force is not None else (os.environ.get("GITHUB_ACTIONS") == "true")

BROWSER_PROFILES = [
    {
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "viewport": {"width": 1366, "height": 768},
    },
    {
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        ),
        "viewport": {"width": 1536, "height": 864},
    },
    {
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "viewport": {"width": 1440, "height": 900},
    },
    {
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "viewport": {"width": 1920, "height": 1080},
    },
]


def _stealth_init_script(languages: str = "['en-US', 'en']") -> str:
    """Patched into every page before any site JS runs, to undo the most
    common "is this a real browser?" checks (navigator.webdriver, empty
    plugins list, missing chrome.runtime object)."""
    return f"""
Object.defineProperty(navigator, 'webdriver', {{ get: () => undefined }});
Object.defineProperty(navigator, 'languages', {{ get: () => {languages} }});
Object.defineProperty(navigator, 'plugins', {{ get: () => [1, 2, 3, 4, 5] }});
window.chrome = window.chrome || {{ runtime: {{}} }};
const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
if (originalQuery) {{
    window.navigator.permissions.query = (parameters) => (
        parameters.name === 'notifications'
            ? Promise.resolve({{ state: Notification.permission }})
            : originalQuery(parameters)
    );
}}
"""


def get_context_options() -> dict:
    """Extra kwargs for browser.new_context(...): a randomly picked realistic
    desktop Chrome user_agent/viewport when stealth is enabled, or {} to let
    Playwright use its own real fingerprint (safe on networks that block
    spoofed ones)."""
    if not STEALTH_ENABLED:
        return {}
    profile = random.choice(BROWSER_PROFILES)
    return {"user_agent": profile["user_agent"], "viewport": profile["viewport"]}


def apply_stealth(context, languages: str = "['en-US', 'en']") -> None:
    """Add the stealth init script to `context`, only when enabled."""
    if STEALTH_ENABLED:
        context.add_init_script(_stealth_init_script(languages))


# --- Behavioral (human-like interaction) helpers -----------------------
#
# Originally written for swaper_monitor.py, now shared by all three
# Playwright-driven monitors (Swaper, Lendermarket, PeerBerry) so login
# flows don't leave the instant, uniformly-timed fingerprints (`.fill()`
# setting a value in 0ms, zero mouse/scroll activity for the whole session)
# that behavioral bot-detection specifically watches for. Unlike the
# fingerprint helpers above, these are always applied (not gated behind
# STEALTH_ENABLED/GITHUB_ACTIONS) - they don't touch the UA/Client-Hints, so
# they're safe on every network, including locally.

def human_pause(min_seconds: float = 0.4, max_seconds: float = 1.2) -> None:
    """Sleep for a small random duration to avoid the instant, uniformly
    timed actions that give away scripted (non-human) interactions."""
    time.sleep(random.uniform(min_seconds, max_seconds))


def human_mouse_wander(page, moves: int = None) -> None:
    """Move the mouse through a few random points and scroll a bit, so the
    page isn't left completely idle/static the way a script would leave it -
    some bot-detection scripts specifically watch for the total absence of
    mousemove/scroll events during a session."""
    try:
        viewport = page.viewport_size or {"width": 1280, "height": 720}
        for _ in range(moves or random.randint(2, 4)):
            x = random.randint(0, max(viewport["width"] - 1, 1))
            y = random.randint(0, max(viewport["height"] - 1, 1))
            page.mouse.move(x, y, steps=random.randint(5, 15))
            human_pause(0.1, 0.4)
        page.mouse.wheel(0, random.randint(150, 500))
    except Exception:
        pass  # purely cosmetic, never fail the run because of it


def human_type(locator, text: str) -> None:
    """Type into a field one character at a time with randomized per-key
    delay, instead of `.fill()` which sets the value instantly - an easy
    signal for behavioral bot detection."""
    locator.click()
    for char in text:
        locator.type(char, delay=random.uniform(60, 180))
