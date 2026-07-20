"""Shared form vocabulary for launcher forms and the init wizard (D5).

Thin composition over stock Textual controls - Input/Select/Button
come from the framework (amber focus arrives free via KSTRL_THEME's
variables); these widgets only add the dense one-line "label control
hint" rhythm and a path-existence marker.

Textual >=3,<6 span: only lowest-common APIs (constructor options
lists, value attributes) - no major-specific keywords.
"""

from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widget import Widget
from textual.widgets import Input, Static

from kstrl.tui import theme


class FormField(Horizontal):
    """One dense form row: muted label, the control, optional hint."""

    DEFAULT_CLASSES = "form-field"

    def __init__(
        self, label: str, control: Widget, hint: str = "",
    ) -> None:
        super().__init__()
        self._label = label
        self._control = control
        self._hint = hint

    def compose(self) -> ComposeResult:
        yield Static(
            Text(self._label, style=f"bold {theme.MUTED}"),
            classes="form-label",
        )
        yield self._control
        if self._hint:
            yield Static(
                Text(self._hint, style=theme.MUTED),
                classes="form-hint",
            )


class PathField(Horizontal):
    """Input + a live exists/absent marker for path values."""

    DEFAULT_CLASSES = "form-field"

    def __init__(
        self, value: str = "", *, placeholder: str = "",
        input_id: str = "",
    ) -> None:
        super().__init__()
        self._value = value
        self._placeholder = placeholder
        self._input_id = input_id

    def compose(self) -> ComposeResult:
        kwargs = {"id": self._input_id} if self._input_id else {}
        yield Input(
            value=self._value, placeholder=self._placeholder,
            **kwargs,  # type: ignore[arg-type]
        )
        yield Static(classes="path-marker")

    def on_mount(self) -> None:
        self._update_marker(self.query_one(Input).value)

    def on_input_changed(self, event: Input.Changed) -> None:
        self._update_marker(event.value)

    @property
    def value(self) -> str:
        return self.query_one(Input).value

    def _update_marker(self, raw: str) -> None:
        marker = self.query_one(".path-marker", Static)
        if not raw.strip():
            marker.update(Text(theme.EMPTY_CELL, style=theme.MUTED))
            return
        if Path(raw).expanduser().exists():
            marker.update(Text("●", style=theme.SUCCESS))
        else:
            marker.update(Text("✗ not found", style=theme.ERROR))


class FormErrors(Static):
    """One-line error strip; empty = valid."""

    def show(self, errors: list[str]) -> None:
        if errors:
            self.update(Text(" · ".join(errors), style=f"bold {theme.ERROR}"))
        else:
            self.update("")
