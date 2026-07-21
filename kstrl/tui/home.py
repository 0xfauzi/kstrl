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
        # A launched session's worker is non-daemon: stop it if the
        # shell is exiting under it, then join before releasing the
        # terminal (a dead shell must never orphan a run silently).
        run = app.run_context
        if run is not None and run.handle is not None:
            if not run.handle.done():
                run.handle.stop.request("home shell exited")
            if run.channel is not None:
                run.channel.detach()
            run.handle.join()
        session = getattr(app, "_session", None)
        if session is not None:
            session.close()
        sys.stdout.write(ANSI_RESTORE)
        sys.stdout.flush()
    return code or 0
