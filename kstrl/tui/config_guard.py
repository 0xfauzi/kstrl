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
    """Whether clearing ``os.environ`` right now would race a run.

    TWO readers, both of them a live command handle, because both spawn
    SUBPROCESSES that inherit the environment and a subprocess cannot be
    made to wait on a lock:

    1. A home-launched session (``run_context.handle``).
    2. An EMBEDDED-mode orchestrator (``app.orchestrator``). Same
       hazard, DIFFERENT attribute: ``embed.py`` starts the command
       thread and passes it as ``orchestrator=``, while ``run_context``
       comes from ``RunContext.observe``, which leaves ``handle`` None.
       Reading only ``run_context`` returned True with an agent
       mid-spawn.

    THE THIRD READER IS NOT HERE, AND THAT IS THE POINT. The app's own
    safe-mode worker also reads ``KSTRL_*``, every 5 seconds, in every
    mode. Round one of #289 added it to THIS condition, and that was
    wrong: it closed a race by making two working features
    intermittent. Measured on an EMPTY project with no run of any kind,
    ``run_context`` None, the worker's flag was set for 51 to 84 ms out
    of every 5 s, so ``ConfigScreen.action_refresh`` was denied at
    random with a message falsely blaming a launched run, and the
    evolve banner silently dropped the variable it exists to name. On a
    project whose events.jsonl makes the check exceed its own interval
    (``app.py`` measures 5.65 s at 500 MiB) the flag never clears and
    the refusal is permanent.

    That reader is SERIALIZED instead, on ``config_report.environ_lock``,
    which ``safemode`` takes around its config loads and every scrub
    takes for its whole duration. The scrub waits at most a few
    milliseconds rather than being refused, and the race is still
    closed. Refusal is kept only where waiting cannot work.

    ``app`` is typed ``object`` rather than ``App`` on purpose: these
    attributes belong to ``KstrlTuiApp``, not to Textual's ``App``, and
    the screens are also mounted on bare test harnesses that have
    neither. Absent means "not running", which is the safe answer for a
    harness and the true one for a shell that has launched nothing.

    Deliberately NOT ``KstrlTuiApp.session_in_flight``, which reads one
    of the two and also requires ``Mode.HOME``. The hazard does not care
    which mode the app is in.
    """
    if _handle_is_live(getattr(getattr(app, "run_context", None), "handle", None)):
        return False
    return not _handle_is_live(getattr(app, "orchestrator", None))


__all__ = ["env_scrub_is_safe"]
