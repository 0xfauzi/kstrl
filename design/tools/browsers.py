"""Where Chromium is. Every check here renders and measures in Chromium, and its numbers (text widths, baselines,
contrast) are that build's: they were measured with Playwright 1.63's Chromium 1194 (chromium-1194).

CHROME drives the interaction and pixel checks through Playwright; HEADLESS takes the audit's and the renderer's
screenshots. Set KSTRL_DESIGN_CHROME and KSTRL_DESIGN_HEADLESS to use other binaries. Without them the newest build
under PLAYWRIGHT_BROWSERS_PATH (or ~/.cache/ms-playwright) is used, and failing that Playwright's own default."""
from __future__ import annotations

import glob
import os


def _find(env: str, pattern: str) -> str | None:
    if os.environ.get(env):
        return os.environ[env]
    for base in (os.environ.get('PLAYWRIGHT_BROWSERS_PATH'), os.path.expanduser('~/.cache/ms-playwright')):
        hits = sorted(glob.glob(os.path.join(base, pattern))) if base else []
        if hits:
            return hits[-1]
    return None


CHROME = _find('KSTRL_DESIGN_CHROME', 'chromium-*/chrome-linux/chrome')
HEADLESS = _find('KSTRL_DESIGN_HEADLESS', 'chromium_headless_shell-*/chrome-linux/headless_shell') or CHROME


def launch(p):  # type: ignore[no-untyped-def]
    """A Playwright Chromium on CHROME, or Playwright's default when none was found."""
    return p.chromium.launch(**({'executable_path': CHROME} if CHROME else {}), args=['--no-sandbox'])
