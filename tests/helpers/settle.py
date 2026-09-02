"""A bounded wait on a CONDITION, for tests that read async-settled state.

A Textual test that counts pauses is asserting on a race it usually
wins. ``tests/test_tui_safe_mode.py`` lost one on CI: two pauses, then
``banner.region.y``, and the banner had not been laid out, so the read
returned the zero region and the row assertion failed with ``[0, 0, 1]``.
The same commit passed on rerun and passes locally, which is the whole
point: a fixed pause count is not a settle condition however often it
happens to be enough.

WHY ONE PAUSE IS NOT ENOUGH, from the source rather than from folklore.
``Pilot.pause`` is::

    await self._wait_for_screen()      # drain what is queued RIGHT NOW
    await wait_for_idle(0)             # or asyncio.sleep(delay)
    self.app.screen._on_timer_update()  # then drive one refresh

``_wait_for_screen`` snapshots ``screen.walk_children()`` and waits for
the messages queued at that instant. Messages those messages go on to
produce are not waited for, and the layout that ``_on_timer_update``
kicks off at the END of the call is not waited for at all. So a cascade
- set ``display``, get a refresh, get an arrange, get a region - takes
an unknown number of pauses, and "two" is a guess that a loaded runner
falsifies. The 5-second deadline below is wall-clock for the same
reason: on a busy CI box an iteration count measures nothing.

THE ANTI-SWALLOW RULE. :func:`settled` RAISES on timeout. It never
returns having failed to observe the condition, because a helper that
turned a real defect into a timeout-shaped pass would be the same defect
one level up: the planted ``dock: top`` regression in
``kstrl/tui/styles.tcss`` must still take
``test_the_banners_do_not_overlap_each_other_or_the_topbar`` red.

Two separate things hold that up, and an earlier version of this
docstring ran them together and named a test that did not exist.
``tests/test_settle_helper.py`` pins the RULE: a condition that never
holds raises, the failure names the condition, and the predicate's own
exceptions are not caught. The PLANT is a per-PR mutation discipline
rather than a test, because a test that edited ``kstrl/tui/styles.tcss``
to prove a point would mutate production source during an ordinary run.

The second half of that rule is that the predicate's own exceptions are
NOT caught. A predicate that raises propagates at once, at the line that
raised, instead of being folded into "it never settled" five seconds
later. That is why ``mounted`` exists: ``query_one`` raises ``NoMatches``
on a widget that has not mounted yet, which is the ordinary case rather
than an error, so the predicate has to use ``query``, whose empty result
is merely falsy. Write ``lambda: node.query(sel)``, never
``lambda: node.query_one(sel)``.

CHOOSING A PREDICATE. It must describe the state you are waiting FOR,
and it must not already be true. ``settled`` checks before it pauses, so
a predicate that is satisfied by the state you are trying to leave
returns immediately and settles nothing. When what you want to assert IS
the condition, wait for something weaker that the assertion depends on -
"the three widgets have been laid out" rather than "their rows differ" -
so that a real defect still reaches the assertion and fails there with
its own message.

Truthiness is the test, deliberately: an empty ``DOMQuery`` is falsy, and
``[]`` is falsy too, so a predicate that has to tell "checked and clean"
from "not checked yet" must spell ``is not None`` the way the tests that
predate this helper already do.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, TypeVar, overload

from textual.dom import DOMNode
from textual.message_pump import MessagePump
from textual.pilot import Pilot
from textual.widget import Widget

#: How long a settle may take before the test is called failed. Wall
#: clock, not iterations. The bounded loops this helper replaces used
#: 2.0s (40 x 0.05) and 5.0s; the longer of the two is kept, because the
#: shorter one is what CI fell off.
SETTLE_TIMEOUT = 5.0

#: How long to wait between polls. Every poll is a ``pilot.pause``, so
#: this is also how much settling each attempt buys.
SETTLE_INTERVAL = 0.02

W = TypeVar("W", bound=Widget)


async def settled(
    pilot: Pilot[Any],
    predicate: Callable[[], object],
    *,
    what: str,
    timeout: float = SETTLE_TIMEOUT,
) -> None:
    """Pause until ``predicate`` is truthy, or fail saying what never was.

    Args:
        pilot: the pilot driving the app under test.
        predicate: called before each pause and after each one. Must not
            raise: an exception propagates rather than counting as "not
            yet", so that a typo or a renamed attribute is reported at
            the line that made it and not as a five-second silence.
        what: named in the failure. Write the condition as a noun phrase
            the reader can act on - "the config table to mount", not
            "the thing".
        timeout: wall-clock seconds before the wait is a failure. The
            poll interval is not a parameter: no caller overrides it in
            the 308 waits this tree now has, and neither wrapper below
            exposed it, so it was an unexercised branch in the one
            function every TUI test depends on.

    Raises:
        AssertionError: if the deadline passes with the predicate still
            falsy. This is the only exit that is not the condition
            holding, and it is a failure, never a pass.
    """
    if not what.strip():
        # Not cosmetic. `what` IS the failure message, so an empty one
        # renders "waited 5s (0 polls) for , and it never settled" and
        # sends the reader nowhere. There is no caller for whom an
        # anonymous wait is the right thing, so this is a contract
        # rather than a default.
        raise ValueError("settled() needs a non-empty `what`: it is the whole failure message")
    deadline = time.monotonic() + timeout
    polls = 0
    while True:
        if predicate():
            return
        polls += 1
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"waited {timeout:g}s ({polls} polls) for {what}, and it never settled"
            )
        await pilot.pause(SETTLE_INTERVAL)


@overload
async def mounted(
    pilot: Pilot[Any],
    node: Callable[[], DOMNode],
    selector: str,
    *,
    timeout: float = ...,
) -> Widget: ...


@overload
async def mounted(
    pilot: Pilot[Any],
    node: Callable[[], DOMNode],
    selector: type[W],
    *,
    timeout: float = ...,
) -> W: ...


async def mounted(
    pilot: Pilot[Any],
    node: Callable[[], DOMNode],
    selector: str | type[W],
    *,
    timeout: float = SETTLE_TIMEOUT,
) -> Widget | W:
    """Wait for ``selector`` to match under ``node``, and return the match.

    The dominant shape of this defect by a long way. Measured on the
    pre-conversion tree ``583acd0``, one file at a time under a 420s
    bound: 52 tests are load-bearing, of which 50 fail and 2 hang once
    the fixed pauses go, and 39 of the 50 fail with ``NoMatches``,
    which is to say they were racing a mount. These numbers follow the
    measurement, never the other way round: an earlier draft said 48
    and 35, which undercounted by four because the file that hangs was
    never attributed.

    ``node`` is a callable and not a node, because the node is usually
    ``app.screen`` and the screen is the thing being waited for. Passing
    ``app.screen`` would capture the screen the push is replacing, and
    the wait would then be for a widget to appear on a screen that is on
    its way out.
    """
    await settled(
        pilot,
        lambda: node().query_one_optional(selector),
        what=f"{selector if isinstance(selector, str) else selector.__name__} to mount",
        timeout=timeout,
    )
    # No await since the predicate last saw a match, so nothing can have
    # unmounted it, and this call is the memoised one: measured on a
    # 40-widget screen with the cache forced to miss, ``query("#id")``
    # costs 205.3 us against 1.5 us for the id fast path, because
    # ``query`` walks the whole subtree with no early exit while
    # ``query_one`` stops at the first breadth-first match.
    found = node().query_one(selector)
    return found


async def drained(
    pilot: Pilot[Any],
    pump: MessagePump,
    *,
    what: str,
    timeout: float = SETTLE_TIMEOUT,
) -> None:
    """Wait until everything already queued on ``pump`` has been handled.

    The case a predicate cannot serve: a test posts a message whose
    CORRECT outcome is that nothing changes. ``test_a_late_check_does_not
    _overwrite_a_newer_one`` posts a superseded result and asserts the
    warning is still there, so there is no new state to wait for and a
    fixed pause looks like the only option. It is not.

    ``MessagePump.call_later`` posts an ``events.Callback`` onto the same
    queue that ``post_message`` writes to, so FIFO on that queue means
    the callback cannot run until the message posted before it has been
    handled. Waiting for the callback is therefore a real observation of
    "my message was processed", not a guess at how long that takes.

    It covers exactly one hop. A handler that posts further messages of
    its own needs a predicate on the outcome, not this.

    TWO STRONGER PRIMITIVES TEXTUAL SHIPS, and why neither is used here.
    ``MessagePump.call_after_refresh`` posts the same ``InvokeLater``
    but the screen holds it back: ``Screen._on_idle`` returns early
    while a layout, scroll, repaint or recompose is pending, so the
    callback lands after the frame rather than merely after the queue.
    That is strictly stronger than ``call_later`` and worth measuring as
    a follow-up; it is not taken here because swapping the primitive
    under 20 call sites needs the planted-defect battery re-run, and
    this PR already has one race it did not expect. ``wait_for_refresh``
    wraps it and is NOT a candidate: it awaits an ``asyncio.Event`` with
    no deadline, so a frame that never comes is a hung test rather than
    a failing one, and it returns ``False`` as a silent null-op when
    called from the node's own task. A wait that can quietly not wait is
    the defect this module exists to remove, and a wait that hangs
    instead of failing is the one next to it: planting ``asyncio.sleep``
    in place of ``pilot.pause`` hung ``tests/test_settle_helper.py``
    for want of a fuse that reads real time, which is why
    ``_FakeClock.monotonic`` now carries one.
    """
    handled: list[bool] = []
    pump.call_later(handled.append, True)
    await settled(pilot, lambda: handled, what=what, timeout=timeout)
