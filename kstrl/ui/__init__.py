"""The terminal output protocol and its plain and Rich renderers."""

from __future__ import annotations

from kstrl.ui.base import UI, terminal_line
from kstrl.ui.plain import PlainUI
from kstrl.ui.select import get_ui

__all__ = [
    "PlainUI",
    "UI",
    "get_ui",
    "terminal_line",
]
