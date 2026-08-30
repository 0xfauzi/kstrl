"""RichUI renders PLAIN TEXT, never Rich markup.

The UI protocol (kstrl/ui/base.py) carries strings a caller wants shown.
Rich's `console.print(str)` parses those as MARKUP, so anything in
square brackets is read as a style tag and dropped: `ks init` printed
its `ks run [iterations]` help line as `ks run`, losing the argument the
line exists to teach (#256 review). PlainUI never had the problem, so a
test written against PlainUI could not see it.
"""

from __future__ import annotations

import io
import re

import pytest

from kstrl.ui.rich_ui import RichUI

# Rich still emits style codes with no_color=True (dim, bold), and the
# repr highlighter splits a styled run at the brackets, so a substring
# check has to compare what a reader would SEE.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

# One line of real help text: a bracketed argument, an angle-bracket
# placeholder, and a flag.
SAMPLE = "ks feature [iterations] --prd scripts/kstrl/feature/<name>/prd.json"

TEXT_METHODS = ("title", "section", "subsection", "info", "ok", "warn", "err")


def visible(rendered: str) -> str:
    """The rendered text with style codes removed."""
    return _ANSI.sub("", rendered)


@pytest.mark.parametrize("method", TEXT_METHODS)
@pytest.mark.parametrize("ascii_only", [False, True])
def test_text_methods_do_not_eat_markup(method: str, ascii_only: bool) -> None:
    buffer = io.StringIO()
    ui = RichUI(no_color=True, ascii_only=ascii_only, file=buffer)

    getattr(ui, method)(SAMPLE)

    assert SAMPLE in visible(buffer.getvalue())


def test_kv_and_stream_line_keep_their_argument() -> None:
    # Short enough that neither line wraps at the 80-column default,
    # which is a different failure from markup eating the argument.
    short = "ks run [iterations]"
    buffer = io.StringIO()
    ui = RichUI(no_color=True, file=buffer)

    ui.kv("mode", short)
    ui.stream_line("SYS", short)

    assert visible(buffer.getvalue()).count(short) == 2


def test_a_lone_style_tag_is_printed_not_interpreted() -> None:
    """The failure mode in miniature: `[dim]` is text, not a style."""
    buffer = io.StringIO()
    ui = RichUI(no_color=True, file=buffer)

    ui.info("[dim]not a style[/dim]")

    assert "[dim]not a style[/dim]" in visible(buffer.getvalue())
