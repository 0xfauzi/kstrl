"""Positive controls for the settle guard: source it MUST flag.

The guard lives in ``tests/test_settle_discipline.py``. Its layer-2
assertion is that a dict is empty, and an empty dict is also what a
switched-off matcher returns, so without this file every shape below
could be dropped from the matcher and that file would stay green.

Round 1 of writing that guard is what this file is made of. The first
version seeded its taint from ``async with app.run_test() as pilot``
alone, and ``tests/test_init_wizard.py`` went past it entirely: that
file opens its pilot with ``pilot_ctx.__aenter__()`` and stashes it on
``self._pilot``, so nine tests measured to be racing a mount were
invisible. The second version asked "does this call hand the app to
somebody" before "is this the settle helper", and reported all four
converted waits in ``tests/test_tui_safe_mode.py`` as the defect they
had just fixed. Both were found by running the matcher, not by reading
it.

Separate from the guard for the reason ``tests/test_event_name_shapes.py``
gives about its own sibling: that file walks the tree and pins what is
there, this one feeds the matcher snippets and pins what it says.

Every shape here was MEASURED against the matcher, before and after,
under the ``__pycache__`` discipline: CPython invalidates cached
bytecode on ``(mtime_seconds, size)``, so two same-size edits inside one
second reuse stale bytecode and hand back a false pass.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from tests.test_settle_discipline import await_sites, parsed, settle_reads


def hits(tmp_path: Path, body: str, name: str = "snippet.py") -> list[str]:
    """What the matcher says about one snippet."""
    path = tmp_path / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return settle_reads(path)


def awaits(tmp_path: Path, body: str, name: str = "netcheck.py") -> int:
    """What the net counts in one snippet."""
    path = tmp_path / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return await_sites(parsed(path))


class TestTheEvasionTable:
    """One test per row of the evasion table the sweep was briefed with.

    Every one of these settles nothing and then reads something async
    settling decides. All eight are caught; none is disclosed.
    """

    def test_a_pause_inside_a_fixed_range_loop(self, tmp_path: Path) -> None:
        """Row 1. A count is not a condition, and a loop around the
        count is still a count."""
        found = hits(
            tmp_path,
            """
            async def test_it(app):
                async with app.run_test() as pilot:
                    for _ in range(3):
                        await pilot.pause()
                    rows = app.screen.query_one("#topbar").region.y
                    assert rows == 0
            """,
        )
        assert found == [
            "line 6: reads app.screen.query_one('#topbar').region.y after the fixed wait on line 5"
        ]

    def test_a_pause_inside_a_helper_called_from_the_test(self, tmp_path: Path) -> None:
        """Row 2, and the shape that got past round 1.

        Both ends are reported: the helper's own read against the
        helper, and the caller's read against the ``await`` of it. The
        caller's half rides on the taint being per MODULE rather than
        per scope: ``app`` is seeded where ``run_test`` is called, in
        the helper, and it is still the app one method down.
        """
        found = hits(
            tmp_path,
            """
            class TestThing:
                async def _open(self, app):
                    ctx = app.run_test()
                    pilot = await ctx.__aenter__()
                    self._pilot = pilot
                    await pilot.pause(0.2)
                    return app.screen

                async def test_it(self, app):
                    screen = await self._open(app)
                    assert screen.region.y == 1
            """,
        )
        assert found == [
            "line 8: reads app.screen after the fixed wait on line 7",
            "line 12: reads screen.region.y after the fixed wait on line 11",
        ]

    def test_a_pause_through_an_aliased_name(self, tmp_path: Path) -> None:
        """Row 3. The wait is unrecognisable, so it is a fixed wait,
        which is the direction this guard is allowed to be wrong in."""
        found = hits(
            tmp_path,
            """
            async def test_it(app):
                async with app.run_test() as pilot:
                    wait = pilot.pause
                    await wait()
                    assert app.screen.focused is not None
            """,
        )
        assert found == ["line 6: reads app.screen.focused after the fixed wait on line 5"]

    def test_asyncio_sleep_instead_of_a_pause(self, tmp_path: Path) -> None:
        """Row 4. No pilot in sight, and still an await followed by a
        read: the net counts it and the matcher names it."""
        found = hits(
            tmp_path,
            """
            import asyncio

            async def test_it(app):
                async with app.run_test() as pilot:
                    await asyncio.sleep(0.2)
                    assert app.screen.query_one("#feed").line_count == 3
            """,
        )
        assert found == [
            "line 7: reads app.screen.query_one('#feed').line_count after the fixed wait on line 6"
        ]

    def test_a_delay_argument_is_still_a_fixed_wait(self, tmp_path: Path) -> None:
        """Row 5. ``pause(0.05)`` is a guess with a number on it."""
        found = hits(
            tmp_path,
            """
            async def test_it(app):
                async with app.run_test() as pilot:
                    await pilot.pause(0.05)
                    text = str(app.screen.query_one("#banner").render())
                    assert "safe" in text
            """,
        )
        assert found == [
            "line 5: reads str(app.screen.query_one('#banner').render()) "
            "after the fixed wait on line 4"
        ]

    def test_a_settle_read_in_a_fixture_rather_than_a_test(self, tmp_path: Path) -> None:
        """Row 6. The walk is over every function, not over names that
        begin with ``test_``."""
        found = hits(
            tmp_path,
            """
            import pytest

            @pytest.fixture
            async def board(app):
                async with app.run_test() as pilot:
                    await pilot.pause()
                    yield app.screen.query_one("#board")
            """,
        )
        assert found == [
            "line 8: reads app.screen.query_one('#board') after the fixed wait on line 7"
        ]

    def test_a_read_through_a_local_bound_before_the_pause(self, tmp_path: Path) -> None:
        """Row 7. Binding the widget early does not settle it early."""
        found = hits(
            tmp_path,
            """
            async def test_it(app):
                async with app.run_test() as pilot:
                    banner = app.screen.query_one("#banner")
                    banner.display = True
                    await pilot.pause()
                    assert banner.region.y == 1
            """,
        )
        assert found == ["line 7: reads banner.region.y after the fixed wait on line 6"]

    def test_a_read_inside_an_assert_in_a_called_helper(self, tmp_path: Path) -> None:
        """Row 8. The call's value is discarded, so the ordinary
        read-versus-drive line would let it through. The distinction
        that saves it is WHO is called: a method ON the app changes the
        app, and handing the app to something else means that something
        else is about to look at it."""
        found = hits(
            tmp_path,
            """
            def check(widget):
                assert widget.region.y == 1

            async def test_it(app):
                async with app.run_test() as pilot:
                    banner = app.screen.query_one("#banner")
                    await pilot.pause()
                    check(banner)
            """,
        )
        assert found == ["line 9: reads check(banner) after the fixed wait on line 8"]


class TestMoreShapesFromThisTree:
    """Shapes the sweep actually met, pinned so they stay caught."""

    def test_a_pilot_stashed_on_the_test_instance(self, tmp_path: Path) -> None:
        """``tests/test_init_wizard.py``, the file round 1 was blind to.

        ``self._pilot`` is a module-scoped name here: an attribute of
        ``self`` is keyed on the attribute rather than on the object,
        because the helper that binds it and the test that awaits it are
        different methods.
        """
        found = hits(
            tmp_path,
            """
            class TestThing:
                async def test_it(self, app):
                    ctx = app.run_test()
                    self._pilot = await ctx.__aenter__()
                    await self._pilot.pause(0.2)
                    assert app.screen.query_one("#field").value == ""
            """,
        )
        assert found == [
            "line 7: reads app.screen.query_one('#field').value after the fixed wait on line 6"
        ]

    def test_a_tuple_binding_where_only_the_pilot_is_seeded(self, tmp_path: Path) -> None:
        """``async with evolve_screen(p) as (screen, pilot)``.

        The context expression names neither the app nor a pilot, so
        only the sibling rule connects ``screen`` to the app: one
        binding, one origin.
        """
        found = hits(
            tmp_path,
            """
            async def test_it(open_screen, tmp_path):
                async with open_screen(tmp_path) as (screen, pilot):
                    await pilot.pause(0.2)
                    assert screen.query_one("#proposals-table").row_count == 0
            """,
        )
        assert found == [
            "line 5: reads screen.query_one('#proposals-table').row_count "
            "after the fixed wait on line 4"
        ]

    def test_a_widget_handed_to_the_app_and_read_back(self, tmp_path: Path) -> None:
        """``probe`` is built by the test, so nothing about its binding
        mentions the app. Mounting it puts it in the app's graph, and
        the argument rule is what carries that."""
        found = hits(
            tmp_path,
            """
            async def test_it(app, Input):
                async with app.run_test() as pilot:
                    probe = Input(id="probe")
                    await app.screen.mount(probe)
                    await pilot.pause()
                    assert probe.value == ""
            """,
        )
        assert found == ["line 7: reads probe.value after the fixed wait on line 6"]

    def test_an_unbounded_while_loop_is_still_a_fixed_wait(self, tmp_path: Path) -> None:
        """``tests/test_tui_embed.py`` spins on a condition with no
        deadline at all. Converting it to ``settled`` turns an unbounded
        spin into a bounded wait that says what never happened."""
        found = hits(
            tmp_path,
            """
            async def test_it(app, session):
                async with app.run_test() as pilot:
                    while not session.done:
                        await pilot.pause(0.05)
                    assert app.screen.query_one("#modal").display
            """,
        )
        assert found == [
            "line 6: reads app.screen.query_one('#modal').display after the fixed wait on line 5"
        ]


class TestTheSettledReadsAreLeftAlone:
    """The false-positive side. Without these, "flag everything" passes."""

    def test_a_read_after_the_enrolled_wait(self, tmp_path: Path) -> None:
        found = hits(
            tmp_path,
            """
            from tests.helpers.settle import settled

            async def test_it(app):
                async with app.run_test() as pilot:
                    await settled(pilot, lambda: app.screen.query("#x"), what="#x")
                    assert app.screen.query_one("#x").region.y == 1
            """,
        )
        assert found == []

    def test_the_enrolled_waits_own_predicate(self, tmp_path: Path) -> None:
        """The lambda reads the app inside the wait that exists to make
        that read safe. Exempting the ENROLLED await only: a read passed
        to ``pilot.press(...)`` really does happen after the last wait,
        and the next test pins that."""
        found = hits(
            tmp_path,
            """
            from tests.helpers.settle import settled

            async def test_it(app):
                async with app.run_test() as pilot:
                    await pilot.pause()
                    await settled(pilot, lambda: app.screen.focused, what="focus")
            """,
        )
        assert found == []

    def test_a_read_passed_to_a_drive_is_not_exempt(self, tmp_path: Path) -> None:
        """The other half of the previous test. Without this, widening
        the exemption from "the enrolled await" to "any await" costs
        nothing and every argument to ``pilot.press`` goes quiet."""
        found = hits(
            tmp_path,
            """
            async def test_it(app):
                async with app.run_test() as pilot:
                    await pilot.pause()
                    await pilot.press(app.screen.focused.id)
            """,
        )
        assert found == ["line 5: reads app.screen.focused.id after the fixed wait on line 4"]

    def test_a_drive_after_a_pause_is_not_a_read(self, tmp_path: Path) -> None:
        """Pushing a screen or pressing a key changes the app rather
        than asking it anything. A pause before one is not yet a
        defect, and flagging it would make this guard something to be
        silenced."""
        found = hits(
            tmp_path,
            """
            async def test_it(app, Panel):
                async with app.run_test() as pilot:
                    await pilot.pause()
                    app.push_screen(Panel())
                    await pilot.press("escape")
                    app.post_message(object())
            """,
        )
        assert found == []

    def test_a_read_with_no_wait_above_it(self, tmp_path: Path) -> None:
        """``run_test`` has already mounted the app when it yields, so
        the first read in a block is nobody's race."""
        found = hits(
            tmp_path,
            """
            async def test_it(app):
                async with app.run_test() as pilot:
                    assert app.screen.query_one("#topbar") is not None
            """,
        )
        assert found == []

    def test_a_self_attribute_that_is_not_the_app(self, tmp_path: Path) -> None:
        """``self._pilot`` taints ``_pilot``, and not ``self``.

        Without that distinction, stashing a pilot on the test instance
        makes EVERY attribute of ``self`` read as app-derived, and the
        unrelated ``self._root`` below is reported. The distinction is
        one function, :func:`self_attr`, and this is the only control
        that notices when it is stubbed to a constant - which is why it
        is here: an unexercised branch in a guard is where the next hole
        goes, and the way to keep one is to exercise it, not to trust it.
        """
        body = """
        class TestIt:
            async def open(self, app):
                async with app.run_test() as pilot:
                    self._pilot = pilot

            async def test_it(self):
                await self._pilot.pause()
                assert self._root.name
        """
        assert hits(tmp_path, body) == []

    def test_a_read_of_something_that_is_not_the_app(self, tmp_path: Path) -> None:
        """The matcher is not simply reporting every expression after a
        pause. Without this, "flag everything" passes every test above."""
        found = hits(
            tmp_path,
            """
            async def test_it(app, tmp_path):
                async with app.run_test() as pilot:
                    await pilot.pause()
                    assert (tmp_path / "kstrl.toml").exists()
                    assert len("abc") == 3
            """,
        )
        assert found == []

    def test_a_module_with_no_app_in_it_at_all(self, tmp_path: Path) -> None:
        """Half of ``tests/`` is async and has no pilot. The taint has
        no seed there, so the matcher says nothing and the net still
        counts the awaits."""
        found = hits(
            tmp_path,
            """
            import asyncio

            async def test_it():
                await asyncio.sleep(0)
                assert True
            """,
        )
        assert found == []


class TestTheNetCountsEveryShape:
    """Layer 1, which resolves nothing and so has nothing to be wrong about.

    Each of these is a wait the matcher might one day fail to recognise.
    The net counts all of them, because ``await`` is syntax.
    """

    def test_every_spelling_of_a_wait_counts_the_same(self, tmp_path: Path) -> None:
        for body in (
            "async def f(p):\n    await p.pause()\n",
            "async def f(p):\n    await p.pause(0.05)\n",
            "import asyncio\n\nasync def f(p):\n    await asyncio.sleep(1)\n",
            "async def f(p):\n    w = p.pause\n    await w()\n",
            "async def f(p):\n    await getattr(p, 'pause')()\n",
            "async def f(p):\n    await p.wait_for_scheduled_animations()\n",
            "from tests.helpers.settle import settled\n\nasync def f(p):\n"
            "    await settled(p, lambda: True, what='x')\n",
        ):
            assert awaits(tmp_path, body) == 1, body

    def test_a_wait_inside_a_comprehension_or_a_nested_function(
        self,
        tmp_path: Path,
    ) -> None:
        """Two places a per-function walk can lose a wait. The net is
        per FILE, so neither hides one."""
        body = """
        async def outer(p):
            async def inner():
                await p.pause()

            values = [x async for x in p.stream()]
            await inner()
        """
        assert awaits(tmp_path, body) == 3

    def test_the_net_is_not_simply_counting_nodes(self, tmp_path: Path) -> None:
        """The false-positive side of layer 1. Without this a net that
        returned the node count would pass every test above."""
        assert awaits(tmp_path, "def f(p):\n    p.pause()\n") == 0
        assert awaits(tmp_path, "x = 1\ny = [i for i in range(3)]\n") == 0

    def test_a_read_added_under_an_existing_fixed_wait(self, tmp_path: Path) -> None:
        """The half layer 1 cannot cover, pinned so nobody rank-orders
        the layers again.

        The guard's docstring once said layer 1 was the guard and layer
        2 was a good error message. This is the mutation that says
        otherwise: a read added under a wait that ALREADY exists adds no
        await, so the file's count does not move and layer 1 passes,
        while layer 2 names the line. A new wait moves the count and a
        new read does not, so neither layer subsumes the other.
        """
        body = """
        async def test_it(app):
            async with app.run_test() as pilot:
                await pilot.press("f2")
                assert app.screen.region.y == 0
        """
        assert awaits(tmp_path, body) == 2
        after = awaits(
            tmp_path,
            """
            async def test_it(app):
                async with app.run_test() as pilot:
                    await pilot.press("f2")
            """,
        )
        assert after == 2, "the read added no await, so the net cannot see it"
        found = hits(tmp_path, body)
        assert found and "region.y" in found[0], found


class TestTheDisclosedMisses:
    """What layer 2 does not see, asserted so the disclosure stays true.

    Both halves are asserted in each case: layer 2 silent AND layer 1
    counting. Asserting only the second would pass on a matcher that had
    never been narrowed; asserting only the first would pass on a net
    that had been switched off.
    """

    def test_a_pilot_arriving_from_another_module_is_missed(
        self,
        tmp_path: Path,
    ) -> None:
        """A fixture hands the test a pilot and a screen. Nothing in the
        file binds either from the app, and the taint is per module, so
        layer 2 has no seed and says nothing.

        The bound on it: the test still has to await something for the
        app to settle at all, so the net counts the wait and the row
        moves. Chasing the fixture would need a call graph across
        modules, which is the resolution
        ``tests/test_event_names_have_one_home.py`` records eleven
        guards getting wrong independently.
        """
        body = """
        async def test_it(live_screen, ticker):
            await ticker.tick()
            assert live_screen.region.y == 1
        """
        assert hits(tmp_path, body) == []
        assert awaits(tmp_path, body) == 1

    def test_a_read_with_no_await_in_its_own_function_is_missed(
        self,
        tmp_path: Path,
    ) -> None:
        """Attribution rather than a hole: the wait is inside the
        context manager, so the manager is where the fix belongs and
        where the matcher reports it. Seven tests in
        ``tests/test_tui_config_guard.py`` have exactly this shape, and
        all seven were fixed by fixing
        ``tests/helpers/tui_screens.py::evolve_screen``.

        The net still counts, because ``async with`` is itself one of
        the four suspension spellings: this file cannot acquire a screen
        without one, so the row moves here as well as in the manager's
        own file.

        The sharpest real instance of this shape is
        ``tests/test_tui_snapshots.py::test_component_detail_snapshot``,
        where the read is not in any test function at all: the callback
        opens a screen and pauses once, and ``snap_compare`` captures
        the SVG after it returns. Found by reading the file rather than
        by the guard, converted by hand, and named here so the next
        person knows this layer will not find the next one.
        """
        body = """
        async def test_it(open_screen, tmp_path):
            async with open_screen(tmp_path) as (screen, pilot):
                assert screen.query_one("#table").row_count == 0
        """
        assert hits(tmp_path, body) == []
        assert awaits(tmp_path, body) == 1

    def test_a_second_call_hop_is_still_caught(self, tmp_path: Path) -> None:
        """Written as a third disclosure and measured to be none.

        ``returning_app_state`` is one hop by name, so a helper that
        returns what ANOTHER helper returned looked like it should
        escape. It does not: the intermediate binding is tainted by the
        first hop, so the second call touches the app through its own
        argument and the read is named. Kept as a control rather than
        deleted, because the reasoning that predicted a miss was wrong
        and the next person to widen the taint should find that out
        here.
        """
        body = """
        class TestThing:
            async def _open(self, app):
                async with app.run_test() as pilot:
                    await pilot.pause()
                    return pilot

            def _screen(self, opened):
                return opened.app.screen

            async def test_it(self, app):
                opened = await self._open(app)
                assert self._screen(opened).region.y == 1
        """
        assert hits(tmp_path, body) == [
            "line 13: reads self._screen(opened).region.y after the fixed wait on line 12"
        ]
        assert awaits(tmp_path, body) == 3
