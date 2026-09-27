"""What a Static shows, as plain text, for a test that reads it (#433 H3).

A Static whose content is a Rich ``Group`` or ``Table`` (a label column
with its values wrapped under them) has no useful ``str()``: it prints
the object's repr. ``shown`` prints the content the way the widget
does, at the width the widget is laid out at, one entry per screen
line, so a test can assert where a wrapped line starts. ``flat`` prints
it at a width no line reaches, for a test that asserts on a phrase
rather than on the wrap.
"""

from __future__ import annotations

import io
from typing import Any

from rich.console import Console

#: Wider than any line these screens print, so ``flat`` never wraps.
NO_WRAP = 1000


def printed(renderable: Any, width: int) -> list[str]:
    """``renderable`` printed at ``width`` columns, one entry per line."""
    console = Console(width=width, record=True, file=io.StringIO(), color_system=None)
    console.print(renderable)
    return [line.rstrip() for line in console.export_text().splitlines()]


def shown(widget: Any) -> list[str]:
    """A Static's content as it wraps on screen: at its content width."""
    return printed(widget.content, widget.content_region.width)


def flat(widget: Any) -> str:
    """A Static's content with no line wrapped."""
    return "\n".join(printed(widget.content, NO_WRAP))
