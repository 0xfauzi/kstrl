"""UI module for kstrl terminal output."""

from kstrl.ui.base import UI
from kstrl.ui.plain import PlainUI
from kstrl.ui.rich_ui import RichUI

__all__ = ["UI", "RichUI", "PlainUI", "get_ui"]


def get_ui(
    mode: str = "auto",
    no_color: bool = False,
    ascii_only: bool = False,
    force_rich: bool = False,
) -> UI:
    """Get appropriate UI implementation based on mode and environment.

    Deprecated for cli commands (TUI rewrite chunk 7): new command code
    should use ``kstrl.output.build_console()``, which wraps this
    same resolution in the event-native console. This stays for tests
    and embedders that want a bare concrete UI.
    """
    import sys

    normalized = (mode or "auto").strip().lower()
    if normalized == "gum":
        normalized = "rich"

    if normalized in {"plain", "off", "no", "0"}:
        return PlainUI(no_color=no_color, ascii_only=ascii_only)

    # auto or rich mode
    try:
        # Check if we have a real terminal
        is_tty = sys.stderr.isatty()
        if normalized == "auto" and not is_tty and not force_rich:
            return PlainUI(no_color=no_color, ascii_only=ascii_only)
        return RichUI(no_color=no_color, ascii_only=ascii_only)
    except Exception:
        return PlainUI(no_color=no_color, ascii_only=ascii_only)
