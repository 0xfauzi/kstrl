"""The banner a screen shows instead of a config it could not read.

Sibling of ``safe_mode_chip.SafeModeBanner`` and deliberately the same
alert grammar: a full-width line, hidden while nominal, one glyph and
then the words. It differs in two ways that are about its content, not
its taste. It is styled by TYPE rather than by id, because a screen
that mistyped the id would get an unstyled always-visible bar with no
error, and it is ``height: auto``, because the line carries a section,
a loader message and the input that set it, and a truncated repair
instruction is not a repair instruction.

The load lives here rather than beside the call site so that reading
the config and rendering the failure cannot come apart: a screen that
remembered the first and forgot the second would degrade silently,
which is the failure #289 exists to remove.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from rich.text import Text
from textual.widgets import Static

from kstrl.config_preflight import load_or_report
from kstrl.tui.config_guard import env_scrub_is_safe

T = TypeVar("T")


class ConfigProblemBanner(Static):
    """Hidden while the configuration resolves; the reason when it does not.

    Hidden-while-clean is safe here and would not be on a status chip:
    this banner never has to carry the meaning "checked and fine" on
    its own, because the screen's own content is that answer.
    """

    #: The line currently shown, or None while the config resolves. The
    #: one copy of that fact: a screen asks the banner rather than
    #: keeping a bool beside it that has to be kept in step.
    problem: str | None = None

    def load(self, loader: Callable[[Path], T], root_dir: Path) -> T | None:
        """The section for ``root_dir``, or None with the reason shown.

        One call, so a screen cannot do the load and forget the render.
        ``blame_env`` is answered from the app: naming the offending
        environment variable is measured by clearing ``os.environ``,
        which is process-wide (see :func:`env_scrub_is_safe`).
        """
        config, problem = load_or_report(
            loader,
            root_dir,
            blame_env=env_scrub_is_safe(self.app),
        )
        self.show(problem)
        return config

    def show(self, problem: str | None) -> None:
        self.problem = problem
        if problem is None:
            self.display = False
            self.update("")
            return
        self.display = True
        # A Text, never a str: the line's first token is the section
        # name in brackets, which Rich reads as markup and DELETES. The
        # measured result was "configuration unreadable:  invalid
        # literal for int()" - the fault named, the section missing.
        self.update(Text(f"▲ configuration unreadable: {problem}"))


__all__ = ["ConfigProblemBanner"]
