"""When a home-shell screen may clear os.environ to blame a variable.

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

Two properties the fix keeps, both stated because both were tempting
to drop:

- It does NOT degrade to an empty view. A screen whose subject is the
  evolution journal showing no patterns because the journal config
  will not parse is worse than an error: "no patterns" is a real state
  and an operator cannot tell the two apart.
- It does NOT restate the entry check's wording. The message comes
  from ``config_preflight.load_or_report``, which builds it with the
  same helper ``preflight_config`` uses, so the two surfaces cannot
  drift. ``tests/test_tui_config_guard.py`` pins them equal.

What is left in THIS module is the one decision a screen has to make
that the entry check never faces. Naming the environment variable is
measured by clearing ``os.environ``, which is process-wide (see
``config_report.scrubbed_environ``). A home-shell session runs the
factory on another thread of this process and those subprocesses
inherit the environment, so the question is asked once, here, for the
three surfaces that ask it. The rendering lives on the widget
(``tui/widgets/config_problem.py``); this module stays logic only and
imports no widget, so ``screens/config.py`` can take the predicate
without taking a banner it does not show.
"""

from __future__ import annotations


def env_scrub_is_safe(app: object) -> bool:
    """Whether clearing ``os.environ`` right now would race a run.

    ``app`` is typed ``object`` rather than ``App`` on purpose: the
    attributes read here belong to ``KstrlTuiApp``, not to Textual's
    ``App``, and the screens are also mounted on bare test harnesses
    that have neither. Absent means "no run", which is the safe answer
    for a harness and the true one for a shell that has launched
    nothing.

    Deliberately NOT ``KstrlTuiApp.session_in_flight``, which walks the
    same three attributes but also requires ``Mode.HOME``. The hazard
    does not care which mode the app is in: a dash or embedded run is
    just as able to read the environment while it is empty.
    """
    run_context = getattr(app, "run_context", None)
    handle = getattr(run_context, "handle", None)
    return handle is None or bool(handle.done())


__all__ = ["env_scrub_is_safe"]
