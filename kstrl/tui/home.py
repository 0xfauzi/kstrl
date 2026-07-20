"""`ks` with no args on a TTY: the home shell (TUI surface D1).

Mirrors dash's wiring: the app owns the terminal; the belt-and-braces
ANSI restore covers any exit path Textual could not clean up after
(spike finding 2). Launch-session signal handling arrives with the
D6 seam - the v1 shell only observes.
"""

from __future__ import annotations

import sys
from pathlib import Path

ANSI_RESTORE = "\x1b[?1049l\x1b[?25h\x1b[0m"


def run_home_shell(root_dir: Path) -> int:
    from kstrl.tui.app import KstrlTuiApp, Mode

    app = KstrlTuiApp(root_dir=root_dir, mode=Mode.HOME)
    try:
        code = app.run()
    finally:
        sys.stdout.write(ANSI_RESTORE)
        sys.stdout.flush()
    return code or 0
