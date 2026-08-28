"""Safe-mode chip: the dashboard's answer to "is the factory holding back".

`kstrl.safemode` reads four degraded states the factory already enters,
and R10.4 put that answer on the plain `ks status` report and on `ks
serve --dry-run`. It reached neither of the surfaces a person actually
looks at: on a terminal `ks status` opens this dashboard whenever a run
directory exists, so the default interactive path never showed it.

Three states, and the chip is ALWAYS rendered. Hiding it while nominal
would have been calmer and would have reinstated the exact fault the
predicate exists to prevent, because a missing chip and a clean one
would look the same. The calm state is dim instead: present, readable,
not competing with the run.
"""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

from kstrl.safemode import SafeModeReason
from kstrl.tui import theme


def render_chip(reasons: list[SafeModeReason] | None) -> Text:
    """A status light, at most five cells wide in every state.

    Measured first: the topbar is one line holding the run identity and
    the cost meter, and at 120 columns a chip carrying the sources and a
    key hint (33 cells) pushed the run's own state label from
    "✓ finished" down to "✓". The header's whole hierarchy is brand,
    project, state, so the chip gives that space back and the banner
    below carries the words.

    ``None`` means not evaluated yet, which is neither of the others.
    """
    text = Text()
    if reasons is None:
        text.append("◍ ?", style=theme.MUTED)
    elif not reasons:
        text.append("◍ ok", style=theme.MUTED)
    else:
        text.append(
            f" ▲ {len(reasons)} ",
            style=f"bold {theme.BACKGROUND} on {theme.WARNING}",
        )
    return text


def render_banner(reasons: list[SafeModeReason], *, key: str = "f2") -> str:
    """The words the chip has no room for. Only shown when degraded."""
    seen: list[str] = []
    for reason in reasons:
        if reason.source not in seen:
            seen.append(reason.source)
    return f"▲ safe mode: {len(reasons)} reason(s) - {', '.join(seen)} - press {key} for why"


class SafeModeChip(Static):
    """Status light on the masthead; re-rendered when the check reports."""

    def on_mount(self) -> None:
        self.update(render_chip(None))

    def update_reasons(self, reasons: list[SafeModeReason] | None) -> None:
        self.update(render_chip(reasons))


class SafeModeBanner(Static):
    """Full-width alert line, hidden while nominal.

    Hidden-while-clear is safe HERE and would not be on the chip: the
    chip is always rendered, so "no banner" never has to carry the
    meaning "checked and clear" on its own.
    """

    def update_reasons(self, reasons: list[SafeModeReason] | None) -> None:
        if not reasons:
            self.display = False
            return
        self.display = True
        self.update(render_banner(reasons))
