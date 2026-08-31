"""The one guarded configuration load the home shell's screens use.

`ks <command>` resolves every kstrl.toml section at command entry
(``config_preflight``, #272), so a typo produces one named line and
exit 1 before anything is built. The home shell runs that same check -
``cli.cli`` calls ``preflight_config`` before opening it - and the gap
#289 names is not that the shell skipped it.

The gap is that the check has exactly one DEGRADING section,
``[evolution]``, which warns and lets the command proceed because the
journal is an optional audit trail. A command that is ABOUT that
section says so with ``_PREFLIGHT_REQUIRED`` and gets the error line
instead. A SCREEN cannot: it is not a click command, it is entered
after startup, and it constructs its config on demand. So the evolve
screen - the screen that section is entirely about - opened on a
warning and then raised ``ValueError`` out of ``on_mount``, taking the
shell down where the CLI had just named the key and the value.

Two properties this keeps, both stated because both were tempting to
drop:

- It does NOT degrade to an empty view. A screen whose subject is the
  evolution journal showing no patterns because the journal config
  will not parse is worse than an error: "no patterns" is a real state
  and an operator cannot tell the two apart. The banner says which
  section, what the loader said, and which input set it.
- It does NOT restate the entry check's wording. The message comes
  from ``config_preflight.load_or_report``, which builds it with the
  same helper ``preflight_config`` uses, so the two surfaces cannot
  drift. ``tests/test_tui_config_guard.py`` pins them equal.

THREAD HAZARD, inherited: naming the environment variable is measured
by clearing ``os.environ``, which is process-wide (see
``config_report.scrubbed_environ``). A home-shell session runs the
factory on another thread of this process, and those subprocesses
inherit the environment. :func:`env_scrub_is_safe` is the same gate
``ConfigScreen.action_refresh`` already applied to its own refresh,
lifted here so both screens and that one ask the question once.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from rich.text import Text
from textual.widgets import Static

from kstrl.config_preflight import load_or_report

T = TypeVar("T")


def env_scrub_is_safe(app: object) -> bool:
    """Whether clearing ``os.environ`` right now would race a run.

    ``app`` is typed ``object`` rather than ``App`` on purpose: the
    attributes read here belong to ``KstrlTuiApp``, not to Textual's
    ``App``, and the screens are also mounted on bare test harnesses
    that have neither. Absent means "no run", which is the safe answer
    for a harness and the true one for a shell that has launched
    nothing.
    """
    run_context = getattr(app, "run_context", None)
    handle = getattr(run_context, "handle", None)
    return handle is None or bool(handle.done())


def load_config(
    app: object,
    loader: Callable[[Path], T],
    root_dir: Path,
) -> tuple[T | None, str | None]:
    """The section for ``root_dir``, or the line the CLI prints for it.

    Exactly one of the pair is None. Screens render the second through
    :class:`ConfigProblemBanner` and then stop, rather than showing an
    empty table that means something else.
    """
    return load_or_report(loader, root_dir, blame_env=env_scrub_is_safe(app))


class ConfigProblemBanner(Static):
    """Full-width alert line, hidden while the configuration resolves.

    Hidden-while-clean is safe here and would not be on a status chip:
    this banner never has to carry the meaning "checked and fine" on
    its own, because the screen's own content is that answer.
    """

    def show(self, problem: str | None) -> None:
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


__all__ = ["ConfigProblemBanner", "env_scrub_is_safe", "load_config"]
