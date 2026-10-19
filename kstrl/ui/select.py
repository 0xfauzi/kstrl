"""The choice of the concrete terminal UI: plain or Rich."""

from __future__ import annotations

import sys
from typing import TextIO

from kstrl.ui.base import UI
from kstrl.ui.plain import PlainUI
from kstrl.ui.rich_ui import RichUI


def get_ui(
    mode: str = "auto",
    no_color: bool = False,
    ascii_only: bool = False,
    force_rich: bool = False,
    file: TextIO | None = None,
) -> UI:
    """Get appropriate UI implementation based on mode and environment.

    ``file`` is where it writes; None is stderr.

    Deprecated for cli commands (TUI rewrite chunk 7): new command code
    should use ``kstrl.events.output.build_console()``, which wraps this
    same resolution in the event-native console. This stays for tests
    and embedders that want a bare concrete UI.
    """
    normalized = (mode or "auto").strip().lower()
    if normalized in {"plain", "off", "no", "0"}:
        return PlainUI(no_color=no_color, ascii_only=ascii_only, file=file)

    # auto or rich mode
    try:
        # Check if we have a real terminal
        is_tty = (file or sys.stderr).isatty()
        if normalized == "auto" and not is_tty and not force_rich:
            return PlainUI(no_color=no_color, ascii_only=ascii_only, file=file)
        return RichUI(no_color=no_color, ascii_only=ascii_only, file=file)
    except Exception:
        return PlainUI(no_color=no_color, ascii_only=ascii_only, file=file)
