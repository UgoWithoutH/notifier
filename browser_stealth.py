"""Shared browser-fingerprint helpers for the Playwright-driven monitors
(Swaper, Lendermarket), used to make automated traffic look less bot-like:
a rotating pool of realistic desktop Chrome UA/viewport combos (instead of
Playwright's default, which advertises "HeadlessChrome" or a mismatched
version - one of the easiest bot signals), plus an init script that patches
over the most common "is this a real browser?" checks.

IMPORTANT - only enabled where it's safe to:
on a network that inspects/validates the browser fingerprint (e.g. a
corporate proxy), overriding `user_agent` makes it mismatch the real
Chromium's Client-Hints (Sec-CH-UA) and can get the whole request blocked
outright as an "obsolete browser" - verified against the Michelin corporate
network on 2026-07-08. So this rotation is only applied when NOT running
locally: detected via the `GITHUB_ACTIONS` env var (set to "true" by GitHub
Actions runners), overridable with FORCE_BROWSER_STEALTH=true/false for
testing. When disabled, Playwright's own real UA/fingerprint is used as-is,
which avoids that mismatch entirely.
"""

import os
import random

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
