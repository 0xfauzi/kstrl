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
    from kstrl.config_report import ConfigReport, build_config_report
    from kstrl.tui.app import KstrlTuiApp, Mode

    # Computed BEFORE app.run(): source detection scrubs os.environ
    # process-wide, which must never race a running session's thread.
    config_report: ConfigReport | None
    try:
        config_report = build_config_report(root_dir)
    except ValueError:
        config_report = None  # the screen renders the guidance line

    app = KstrlTuiApp(
        root_dir=root_dir, mode=Mode.HOME, config_report=config_report,
    )
    try:
        code = app.run()
    finally:
        sys.stdout.write(ANSI_RESTORE)
        sys.stdout.flush()
    return code or 0
