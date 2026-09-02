"""The settle helper's own contract, pinned rather than asserted in prose.

`tests/helpers/settle.py` is what every converted TUI test now waits on,
and the property the conversion rests on is the ANTI-SWALLOW rule:
`settled` raises when the condition never holds, so a real defect can
never come back as a timeout-shaped pass. A helper that quietly returned
after failing to observe its condition would be the same defect one
level up, and every planted-defect result in this PR would be worthless.

That property was documented in three places and tested in none. The
module docstring went further and said `tests/test_settle_discipline.py`
PLANTS the `dock: top` regression to prove it. It does not, and nothing
else did either: the test that NAMES that defect
(`test_the_banners_do_not_overlap_each_other_or_the_topbar`) exists and
asserts on it, but no test in the tree plants it. So the claim sent a
reader looking for a guarantee that was not there. These tests are that
guarantee.

Planting the production `dock: top` defect stays a per-PR mutation
discipline rather than a test: a test that edits `kstrl/tui/styles.tcss`
to prove a point would mutate production source during a normal run.
What a test CAN close by construction is the helper's own contract, and
that is what fails if the anti-swallow rule is ever weakened.
"""

from __future__ import annotations

import time

import pytest
from textual.app import App, ComposeResult
from textual.screen import Screen
from textual.widgets import Static

from tests.helpers.settle import drained, mounted, settled


class _Tiny(App[None]):
    """The smallest app that can mount a widget on demand."""

    def compose(self) -> ComposeResult:
        yield Static("present", id="present")


class TestTheAntiSwallowRule:
    """The rule the planted-defect battery depends on."""

    async def test_a_condition_that_never_holds_raises_rather_than_returning(
        self,
    ) -> None:
        """The whole point. If this ever returns instead of raising,
        every settle in the tree becomes a wait that can silently not
        wait, and a real defect reaches its assertion having been
        declared settled."""
        async with _Tiny().run_test() as pilot:
            with pytest.raises(AssertionError) as excinfo:
                await settled(pilot, lambda: False, what="a thing that never happens", timeout=0.1)
        assert "never settled" in str(excinfo.value)

    async def test_the_failure_names_the_condition_it_waited_for(self) -> None:
        """`what` is the whole error message for the next reader, so a
        wait that timed out has to say which one."""
        async with _Tiny().run_test() as pilot:
            with pytest.raises(AssertionError) as excinfo:
                await settled(
                    pilot,
                    lambda: False,
                    what="the config table to mount",
                    timeout=0.1,
                )
        assert "the config table to mount" in str(excinfo.value)

    async def test_mounted_raises_when_the_selector_never_matches(self) -> None:
        """`mounted` is the dominant shape in the tree, so it carries
        the same rule and is pinned separately rather than by
        inheritance from `settled`."""
        async with _Tiny().run_test() as pilot:
            with pytest.raises(AssertionError) as excinfo:
                await mounted(pilot, lambda: pilot.app.screen, "#never-mounted", timeout=0.1)
        message = str(excinfo.value)
        assert "never settled" in message
        # The half of the message `mounted` itself builds. Asserting only
        # on "never settled" tests `settled`, which is already pinned
        # above, and would pass a `mounted` that named no selector at all.
        assert "#never-mounted to mount" in message

    async def test_a_type_selector_is_named_by_its_class_in_the_failure(self) -> None:
        """`mounted` takes a string or a CLASS, and the class branch had
        no coverage: every other call here passes a string, so a
        mistyped `selector.__name__` would only ever fire in production.
        """

        class _Absent(Static):
            pass

        async with _Tiny().run_test() as pilot:
            with pytest.raises(AssertionError) as excinfo:
                await mounted(pilot, lambda: pilot.app.screen, _Absent, timeout=0.1)
        assert "_Absent to mount" in str(excinfo.value)

    async def test_drained_raises_on_a_pump_that_will_not_run_the_callback(self) -> None:
        """The anti-swallow rule through `drained`, and the pump's
        identity, in one test and without stubbing anything.

        A removed widget is a real refusal path: `MessagePump.call_later`
        delegates to `post_message`, which returns False once the pump is
        closing, so the callback never runs. `drained` must therefore
        raise here.

        This is also what makes the pump argument load-bearing. A
        `drained` that ignored `pump` and always drained `pilot.app`
        would find the app perfectly healthy and RETURN, so this test
        fails on it. That defect was planted, and an earlier version of
        this file, which only ever passed `pilot.app` as the pump,
        passed it.
        """
        async with _Tiny().run_test() as pilot:
            target = pilot.app.screen.query_one("#present", Static)
            await target.remove()
            with pytest.raises(AssertionError) as excinfo:
                await drained(pilot, target, what="a removed widget's queue", timeout=0.1)
        assert "never settled" in str(excinfo.value)


class TestThePredicateIsNotShielded:
    """The second half of the rule stated in the module docstring."""

    async def test_the_predicates_own_exception_propagates_unchanged(self) -> None:
        """A typo or a renamed attribute must surface at the line that
        made it. Folded into "it never settled" five seconds later, it
        sends the reader to the wait instead of to the mistake."""

        def boom() -> bool:
            raise AttributeError("no such attribute")

        async with _Tiny().run_test() as pilot:
            with pytest.raises(AttributeError, match="no such attribute"):
                await settled(pilot, boom, what="something that cannot be read", timeout=5.0)

    async def test_a_raising_predicate_does_not_wait_out_the_deadline(self) -> None:
        """The same rule from the other side: propagating AT ONCE is the
        behaviour, not propagating eventually. A 5s timeout with a
        predicate that raises must not take 5s."""

        def boom() -> bool:
            raise KeyError("nope")

        async with _Tiny().run_test() as pilot:
            start = time.monotonic()
            with pytest.raises(KeyError):
                await settled(pilot, boom, what="never reached", timeout=5.0)
            elapsed = time.monotonic() - start
        assert elapsed < 1.0, (
            f"a raising predicate waited {elapsed:.2f}s instead of failing at once"
        )


class TestThePositiveDirection:
    """A guard that only ever raises would pass all of the above."""

    async def test_a_condition_already_true_returns_without_pausing_at_all(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`settled` checks BEFORE it pauses. That order is what makes a
        predicate satisfied by the state you are trying to LEAVE settle
        nothing, which the module docstring warns callers about.

        Counting the pauses rather than just calling it, because the
        obvious version of this test is vacuous: with the order
        reversed, one 0.02s pause still finishes inside any timeout and
        the call still returns. Planting that reordering is how the
        weaker version was caught, so the spy is the test.
        """
        paused: list[float | None] = []

        async with _Tiny().run_test() as pilot:
            original = pilot.pause

            async def counting_pause(delay: float | None = None) -> None:
                paused.append(delay)
                await original(delay)

            monkeypatch.setattr(pilot, "pause", counting_pause)
            await settled(pilot, lambda: True, what="a condition already true", timeout=0.1)

        assert not paused, f"settled paused {len(paused)} time(s) before checking"

    async def test_mounted_returns_the_widget_it_waited_for(self) -> None:
        """The return value is used by 106 call sites, so it is part of
        the contract, not an implementation detail."""
        async with _Tiny().run_test() as pilot:
            found = await mounted(pilot, lambda: pilot.app.screen, "#present")
        assert isinstance(found, Static)
        assert found.id == "present"

    async def test_mounted_waits_for_a_widget_that_arrives_late(self) -> None:
        """The shape the sweep exists for: 35 of the 48 load-bearing
        fixed pauses were racing a mount, so a widget that appears after
        the call must still be found rather than missed.

        The obvious version of this test does not test that. Calling
        `screen.mount(...)` and then awaiting `mounted` waits for
        nothing: `Widget.mount` registers the child synchronously before
        it returns, so the predicate is already true on `settled`'s
        first check and the poll loop never runs. Measured by planting a
        `settled` with no loop at all, which that version passed. A
        timer puts the mount behind a real dispatch, so the loop is
        load-bearing and the plant is caught here.
        """
        async with _Tiny().run_test() as pilot:
            pilot.app.set_timer(0.15, lambda: pilot.app.screen.mount(Static("late", id="late")))
            found = await mounted(pilot, lambda: pilot.app.screen, "#late")
        assert found.id == "late"

    async def test_mounted_re_reads_the_node_on_every_poll(self) -> None:
        """`node` is a callable, not a node, and `settle.py` is explicit
        about why: passing `app.screen` directly captures the screen the
        push is REPLACING, so the wait would be for a widget to appear
        on a screen on its way out. Nothing pinned that, so a `mounted`
        that hoisted `dom = node()` above the wait passed the whole
        file. Planted exactly that, and this is the test that fails on
        it.
        """

        class _Second(Screen[None]):
            def compose(self) -> ComposeResult:
                yield Static("arrived", id="arrival")

        async with _Tiny().run_test() as pilot:
            # On a timer, not inline. `push_screen` takes effect before it
            # returns, so pushing inline lets a hoisted `dom = node()`
            # capture the NEW screen and the plant survives. Measured.
            pilot.app.set_timer(0.15, lambda: pilot.app.push_screen(_Second()))
            found = await mounted(pilot, lambda: pilot.app.screen, "#arrival")
            # Inside the block: outside it the app has shut down and the
            # screen stack is empty.
            assert isinstance(pilot.app.screen, _Second)
        assert found.id == "arrival"

    async def test_drained_returns_once_the_given_pump_has_been_worked(self) -> None:
        """The positive direction, on a pump that is not the app: what
        was queued before the call has run by the time it returns."""
        async with _Tiny().run_test() as pilot:
            target = pilot.app.screen.query_one("#present", Static)
            order: list[str] = []
            target.call_later(order.append, "queued first")
            await drained(pilot, target, what="the widget's own queue to be worked")
            assert order == ["queued first"], "drained returned before the pump had been worked"
