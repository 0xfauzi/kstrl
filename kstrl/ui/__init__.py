"""The terminal output protocol and its plain and Rich renderers."""

from __future__ import annotations

from kstrl.ui.base import UI
from kstrl.ui.plain import PlainUI
from kstrl.ui.rich_ui import RichUI
from kstrl.ui.select import get_ui

__all__ = ["UI", "PlainUI", "RichUI", "get_ui"]
