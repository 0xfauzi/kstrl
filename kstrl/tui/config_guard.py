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


def _handle_is_live(handle: object) -> bool:
    """A command handle that exists and has not finished."""
    done = getattr(handle, "done", None)
    return handle is not None and callable(done) and not bool(done())


def env_scrub_is_safe(app: object) -> bool:
    """Whether clearing ``os.environ`` right now would race a thread.

    THREE readers, and missing any one of them makes this predicate a
    lie rather than a guard:

    1. A home-launched session (``run_context.handle``), which runs the
       factory on another thread whose subprocesses inherit the
       environment.
    2. An EMBEDDED-mode orchestrator (``app.orchestrator``). Same
       hazard, DIFFERENT attribute: ``embed.py`` starts the command
       thread and passes it as ``orchestrator=``, while ``run_context``
       comes from ``RunContext.observe``, which leaves ``handle`` None.
       Reading only ``run_context`` returned True with an agent
       mid-spawn.
    3. The app's own safe-mode worker (``_safe_mode_running``).
       ``app.py`` schedules ``_check_safe_mode`` every 5 seconds in
       EVERY mode, and ``safemode.safe_mode_reasons`` loads
       ``AutonomyConfig``, ``PolicyConfig`` and ``QueueConfig``, all of
       which read ``KSTRL_*``. Measured before this clause existed:
       with 84 variables set, a thread polling
       ``KSTRL_AUTONOMY_LEVEL`` across 50 blaming ``load_or_report``
       calls saw it MISSING for 95990 of 36.8M reads. A safe-mode tick
       landing in that window reports the default ladder, so a degraded
       factory reads nominal on the chip for one tick, which is the
       silent wrong answer the chip exists to prevent.

    WHY A FLAG READ IS ENOUGH, AND NO LOCK IS NEEDED. Every caller runs
    on the Textual UI thread (a message handler or an action), the
    scrub that follows is synchronous and never awaits, and a worker is
    only ever STARTED from that same thread by a timer callback. So no
    worker can begin between this check and the scrub, and
    ``_safe_mode_running`` is cleared in the worker's ``finally`` only
    after its last config read, which makes True strictly wider than
    the danger window. Calling this off the UI thread would break that
    argument, and nothing does.

    ``app`` is typed ``object`` rather than ``App`` on purpose: these
    attributes belong to ``KstrlTuiApp``, not to Textual's ``App``, and
    the screens are also mounted on bare test harnesses that have
    neither. Absent means "not running", which is the safe answer for a
    harness and the true one for a shell that has launched nothing.

    Deliberately NOT ``KstrlTuiApp.session_in_flight``, which reads one
    of the three and also requires ``Mode.HOME``. The hazard does not
    care which mode the app is in.
    """
    if _handle_is_live(getattr(getattr(app, "run_context", None), "handle", None)):
        return False
    if _handle_is_live(getattr(app, "orchestrator", None)):
        return False
    return not bool(getattr(app, "_safe_mode_running", False))


__all__ = ["env_scrub_is_safe"]
