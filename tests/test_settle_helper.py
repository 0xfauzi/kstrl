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
from textual.pilot import Pilot
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


class _FakeClock:
    """A monotonic clock the test advances by hand.

    Patched over `settle.time`, not over `time.monotonic` itself, so
    nothing outside the helper sees a frozen clock: Textual reads the
    real one throughout.

    THE FUSE IS NOT DECORATION. A fake clock only advances where the
    test advances it, so any defect that stops the helper reaching that
    place converts a five-second failure into an unbounded hang. The
    first version put the runaway guard inside the stubbed `pause`,
    which is precisely what such a defect stops calling: planting
    `asyncio.sleep` in place of `pilot.pause` hung this file rather
    than failing it, and a hang in CI is worse than a red. The fuse
    lives here instead, in the one method the poll loop cannot skip,
    and it reads REAL time so no plant can hold it still.
    """

    #: Real seconds this clock may exist for while a wait is running.
    #: Every honest test here finishes in milliseconds, so any value
    #: that is not "a hang" will do; this one is far above the noise
    #: and far below a CI timeout.
    FUSE = 10.0

    def __init__(self) -> None:
        self.now = 0.0
        self._real_start = time.monotonic()

    def monotonic(self) -> float:
        if time.monotonic() - self._real_start > self.FUSE:
            raise AssertionError(
                f"the fake clock has been alive for more than {self.FUSE:g} real "
                "seconds, so the wait is not advancing it: the helper has stopped "
                "calling pilot.pause, and this test would otherwise hang"
            )
        return self.now


class TestTheDeadlineIsTheOneItWasGiven:
    """The tests above all pass `timeout=0.1`, which is why a whole
    class of deadline defects survived them.

    A 0.1s timeout cannot tell "gave up at 0.1s" from "gave up at 5s"
    from "gave up after 50 polls whenever that was", because at 0.1s
    they all look the same and none takes long enough to notice. Three
    defects lived in that gap: the deadline branch returning instead of
    raising once the poll count was high, the `timeout` argument being
    ignored in favour of the module default, and the module default
    itself being changed to 600 seconds.

    A fake clock closes all three, and it is the only way to do it
    without a test that really waits five seconds. Time advances inside
    a stubbed `pause` rather than inside the predicate, which is both
    where it really passes and what keeps these tests instant: at a 600
    second deadline the real helper polls 30,000 times, and against a
    real pause that is ten minutes of wall clock per test.
    """

    def _wire(
        self,
        monkeypatch: pytest.MonkeyPatch,
        pilot: Pilot[None],
    ) -> tuple[_FakeClock, list[int]]:
        from tests.helpers import settle as settle_mod

        clock = _FakeClock()
        monkeypatch.setattr(settle_mod, "time", clock)
        calls: list[int] = []

        async def instant_pause(delay: float | None = None) -> None:
            # A zero delay must still advance the clock or a spin defect
            # turns this into a hang rather than a failure.
            step = delay if delay is not None and delay > 0 else 0.001
            clock.now += step
            if len(calls) > 200_000:
                raise AssertionError("the wait never reached its deadline at all")

        monkeypatch.setattr(pilot, "pause", instant_pause)
        return clock, calls

    async def test_the_default_wait_raises_rather_than_giving_up_quietly(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """At the DEFAULT timeout, not a short one. A deadline branch
        that returns once the poll count is high enough never fires
        under `timeout=0.1`, because 0.1s is only a handful of polls."""
        async with _Tiny().run_test() as pilot:
            clock, calls = self._wire(monkeypatch, pilot)

            def never() -> bool:
                calls.append(1)
                return False

            with pytest.raises(AssertionError) as excinfo:
                await settled(pilot, never, what="something that never happens")

        assert "never settled" in str(excinfo.value)
        assert len(calls) > 50, (
            f"only {len(calls)} polls before giving up, so a defect that returns "
            "after 50 polls would not even be reached by this test"
        )

    async def test_the_default_deadline_is_seconds_rather_than_minutes(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A `SETTLE_TIMEOUT` of 600 breaks nothing and fails nothing:
        every wait in the tree still settles, and the only visible
        change is that a genuinely stuck test hangs for ten minutes
        instead of failing in five seconds."""
        async with _Tiny().run_test() as pilot:
            clock, calls = self._wire(monkeypatch, pilot)

            def never() -> bool:
                calls.append(1)
                return False

            with pytest.raises(AssertionError):
                await settled(pilot, never, what="something that never happens")

        assert clock.now <= 30.0, (
            f"the default wait ran {clock.now:g} virtual seconds before failing; "
            "a stuck test has to fail in seconds, not minutes"
        )

    async def test_an_explicit_timeout_is_the_one_that_is_used(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`timeout=` silently ignored in favour of the module default
        is invisible to every other test here, because they all fail
        either way. The virtual clock says WHEN it gave up."""
        from tests.helpers import settle as settle_mod

        async with _Tiny().run_test() as pilot:
            clock, calls = self._wire(monkeypatch, pilot)

            def never() -> bool:
                calls.append(1)
                return False

            with pytest.raises(AssertionError):
                await settled(pilot, never, what="a short wait", timeout=1.0)

        assert clock.now < settle_mod.SETTLE_TIMEOUT, (
            f"asked for 1s and it ran {clock.now:g} virtual seconds, which is the "
            "module default rather than the argument that was passed"
        )

    async def test_a_condition_true_at_an_expired_deadline_still_returns(self) -> None:
        """The check-first order at the boundary. With `timeout=0` the
        deadline has already passed on the first iteration, so a helper
        that tests the clock before the predicate raises on a condition
        that is TRUE. That is a false failure rather than a false pass,
        and it is the one ordering defect the pause-counting test above
        cannot see."""
        async with _Tiny().run_test() as pilot:
            await settled(pilot, lambda: True, what="a condition already true", timeout=0.0)


class TestThePollDrivesTheApp:
    """`pilot.pause` is the primitive, and the module docstring is an
    extended argument for why: it drains the queued messages and drives
    a refresh. `asyncio.sleep` does neither, so a poll that slept would
    spin without ever letting the app settle, and every wait in the tree
    would become a race that usually wins. Nothing noticed the swap.
    """

    async def test_the_poll_pauses_the_pilot_with_a_positive_delay(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        paused: list[float | None] = []

        async with _Tiny().run_test() as pilot:
            original = pilot.pause

            async def counting_pause(delay: float | None = None) -> None:
                paused.append(delay)
                await original(delay)

            monkeypatch.setattr(pilot, "pause", counting_pause)
            pilot.app.set_timer(0.15, lambda: pilot.app.screen.mount(Static("x", id="x")))
            await mounted(pilot, lambda: pilot.app.screen, "#x")

        assert paused, "the poll never paused the pilot, so it never let the app settle"
        assert all(d is not None and d > 0 for d in paused), (
            f"the poll paused with {paused[0]!r}: a zero delay makes it a spin rather than a wait"
        )


class TestTheReadIsTheFirstMatch:
    async def test_mounted_returns_the_same_widget_query_one_would(self) -> None:
        """`mounted`'s contract is `query_one`'s: the first
        breadth-first match. Re-querying with `query(...)` and taking a
        different element of the result is invisible while every test
        fixture has exactly one match, and wrong the moment one does
        not."""
        async with _Tiny().run_test() as pilot:
            screen = pilot.app.screen
            # Both in one call: two awaited mounts would put a read of
            # app-derived state after a fixed wait, which this PR's own
            # guard flags, correctly.
            await screen.mount(Static("first", classes="twin"), Static("second", classes="twin"))
            found = await mounted(pilot, lambda: pilot.app.screen, ".twin")
            expected = screen.query_one(".twin")
            assert found is expected, "mounted did not return query_one's match"


class TestThePredicateIsTheCallersToRaise:
    """A parser's error taxonomy belongs to the parser, and a
    predicate's belongs to the predicate. These are the two spellings a
    developer would plausibly reach for.
    """

    async def test_a_nomatches_from_the_predicate_is_not_treated_as_not_yet(self) -> None:
        """The plausible one. This module's own docstring tells callers
        to write `node.query(sel)` and never `node.query_one(sel)`,
        precisely because the latter raises `NoMatches` before the
        widget mounts. So a developer who hits `NoMatches` has an
        obvious wrong place to fix it: catch it in the helper. That
        turns a precise error at the predicate's line into a five-second
        "never settled" pointing at the wait.
        """
        from textual.css.query import NoMatches

        async with _Tiny().run_test() as pilot:
            with pytest.raises(NoMatches):
                await settled(
                    pilot,
                    lambda: pilot.app.screen.query_one("#absent"),
                    what="a widget queried the wrong way",
                    timeout=5.0,
                )

    async def test_an_assertionerror_from_the_predicate_is_not_treated_as_not_yet(
        self,
    ) -> None:
        """Swallowing this one is the worst of the three, because the
        helper's own timeout is also an AssertionError, so a caught one
        is indistinguishable from a wait that expired."""

        def asserting() -> bool:
            raise AssertionError("the predicate's own complaint")

        async with _Tiny().run_test() as pilot:
            with pytest.raises(AssertionError, match="the predicate's own complaint"):
                await settled(pilot, asserting, what="a predicate that asserts", timeout=5.0)


class TestTheFailureMessageIsUsable:
    async def test_an_empty_what_is_refused_rather_than_rendered(self) -> None:
        """`what` is the whole message. Empty, it renders "waited 5s (0
        polls) for , and it never settled", which names nothing."""
        async with _Tiny().run_test() as pilot:
            with pytest.raises(ValueError, match="non-empty"):
                await settled(pilot, lambda: True, what="")
            with pytest.raises(ValueError, match="non-empty"):
                await settled(pilot, lambda: True, what="   ")
