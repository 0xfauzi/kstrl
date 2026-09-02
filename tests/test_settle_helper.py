"""The settle helper's own contract, pinned rather than asserted in prose.

`tests/helpers/settle.py` is what every converted TUI test now waits on,
and the property the conversion rests on is the ANTI-SWALLOW rule:
`settled` raises when the condition never holds, so a real defect can
never come back as a timeout-shaped pass. A helper that quietly returned
after failing to observe its condition would be the same defect one
level up, and every planted-defect result in this PR would be worthless.

That property was documented in three places and tested in none. The
module docstring went further and named a test that plants the
`dock: top` regression to prove it; no such test existed anywhere in
`tests/`, so the claim sent a reader looking for a guarantee that was
not there. These tests are that guarantee.

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
        assert "never settled" in str(excinfo.value)

    async def test_drained_raises_when_the_pump_never_runs_the_callback(self) -> None:
        """`drained` is the one wrapper whose condition is invisible to
        the caller, so a silent return here would be the hardest to
        notice."""

        class _Deaf(App[None]):
            def call_later(self, callback: object, *args: object, **kwargs: object) -> bool:
                return False  # accepted and never run

        async with _Deaf().run_test() as pilot:
            with pytest.raises(AssertionError) as excinfo:
                await drained(pilot, pilot.app, what="a queue that never drains", timeout=0.1)
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

    async def test_a_condition_already_true_returns_without_pausing_at_all(self) -> None:
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

            pilot.pause = counting_pause  # type: ignore[method-assign]
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
        the call must still be found rather than missed."""
        async with _Tiny().run_test() as pilot:
            pilot.app.screen.mount(Static("late", id="late"))
            found = await mounted(pilot, lambda: pilot.app.screen, "#late")
        assert found.id == "late"

    async def test_drained_returns_once_the_queue_has_been_worked(self) -> None:
        """The FIFO observation `drained` rests on: a callback posted
        after a message cannot run before that message is handled."""
        async with _Tiny().run_test() as pilot:
            await drained(pilot, pilot.app, what="the app's queue to be worked")
