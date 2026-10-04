"""Every path that lifts the queue pause is enrolled here (#707).

The poison breaker pauses the queue when ``consecutive_poison`` in the
spend ledger reaches ``max_consecutive_poison``, and it re-reads that
streak on every serve cycle. A path that lifts the pause without
clearing the streak is undone by the next cycle, which is what #707
measured for ``ks queue resume`` before it called
``SpendLedger.reset_poison_streak``.

The net is :func:`spells` on ``resume``: every node in ``kstrl/`` that
holds the string, whatever its shape (a call, a click command name, a
``getattr`` literal, a def). A new path that lifts the pause is a new row
in the census and has to be classified here before it can land.
``tests/test_queue_cli.py::TestResumeClearsThePoisonStreak`` proves the
behaviour end to end; this file is the guard for the next path.

Disclosed blind spot: a write of ``pause.json`` that never spells
``resume`` (a hand-built ``PauseState()`` written by another helper) is
not a reference this walk can see. ``Queue.resume`` is the only writer
of an unpaused marker today.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.helpers.astwalk import (
    REPO_ROOT,
    assert_census,
    census,
    label,
    package_sources,
    parsed,
    scope_of,
    spells,
)

#: Every scope in ``kstrl/`` that spells ``resume``, with its count.
RESUME_SITES = {
    # The operator's verb: the click command name and the Queue.resume call.
    "kstrl/cli.py::queue_resume": 2,
    # Serve lifts a pause whose resume_after has passed. Only the daily
    # budget gate sets resume_after, so this never lifts a breaker pause.
    "kstrl/serve.py::serve_cycle": 1,
    # The definition of Queue.resume.
    "kstrl/workqueue.py::<module>": 1,
}

#: The rows above that cannot lift a pause the poison breaker set.
NOT_AN_OPERATOR_RESUME = frozenset({"kstrl/serve.py::serve_cycle", "kstrl/workqueue.py::<module>"})


def _scope_key(source_file: Path, node: ast.AST) -> str:
    scope = scope_of(parsed(source_file)).get(id(node), "<module>")
    return f"{label(source_file, root=REPO_ROOT)}::{scope}"


def test_every_path_that_lifts_the_pause_is_enrolled() -> None:
    assert_census(
        sources=package_sources(),
        sees=spells("resume"),
        key=_scope_key,
        expected=RESUME_SITES,
        control="queue.resume(actor='op')\n",
        message=(
            "A path that spells `resume` changed. A path that lifts the queue "
            "pause must also call SpendLedger.reset_poison_streak in the same "
            "function, as kstrl/cli.py::queue_resume does, or the next serve "
            "cycle pauses the queue again (#707). Classify it in RESUME_SITES."
        ),
    )


def test_every_operator_resume_clears_the_poison_streak() -> None:
    clears = census(package_sources(), spells("reset_poison_streak"), _scope_key)
    unclear = sorted(site for site in RESUME_SITES if site not in NOT_AN_OPERATOR_RESUME)
    assert unclear, "no operator resume is enrolled, so this check covers nothing"
    missing = [site for site in unclear if site not in clears]
    assert missing == [], (
        f"{missing} lift the queue pause without clearing the poison streak, "
        "so the next serve cycle undoes them (#707)"
    )
