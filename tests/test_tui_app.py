"""Stage 3 PR D (TUI rewrite): dashboard app + overview screen.

Textual Pilot tests over the fake-run fixture. Headless: run_test()
drives the real app without a terminal.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import PropertyMock, patch

from rich.text import Text

from kstrl import events as ev
from kstrl.reducer import RunState, fold, load_run_state
from kstrl.tui.app import KstrlTuiApp, Mode
from kstrl.tui.screens.overview import CheckpointBanner
from kstrl.tui.widgets.component_table import ComponentTable
from kstrl.tui.widgets.cost_meter import render_cost_meter
from kstrl.tui.widgets.header import render_header
from tests.helpers.fake_run import FakeRunSpec, stream_fake_run, write_fake_run
from tests.helpers.settle import drained, mounted, settled


def _app(root: Path, run_dir: Path) -> KstrlTuiApp:
    return KstrlTuiApp(
        run_dir=run_dir,
        root_dir=root,
        mode=Mode.DASH,
        poll_interval=0.05,
    )


def _cell_text(value: object) -> str:
    return value.plain if isinstance(value, Text) else str(value)


class TestOverview:
    async def test_renders_fake_run(self, tmp_path: Path) -> None:
        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=3))
        app = _app(tmp_path, run_dir)
        async with app.run_test(size=(120, 40)) as pilot:
            table = await mounted(pilot, lambda: app.screen, ComponentTable)
            # The overview is fed by a message, so the rows land a hop
            # after the mount. "Any row at all" is weaker than the count
            # below, so a table that renders the wrong number of rows
            # still fails on the assertion and not on this wait.
            await settled(
                pilot,
                lambda: table.row_count,
                what="the component table to render the run's rows",
            )
            assert table.row_count == 3
            row = table.get_row("comp-a")
            texts = [_cell_text(cell) for cell in row]
            assert "comp-a" in texts[1]
            assert "completed" in texts[2]

    async def test_live_updates_arrive(self, tmp_path: Path) -> None:
        run_id = "factory-20260720-160000.000000-live"
        stepper = stream_fake_run(
            tmp_path,
            FakeRunSpec(components=2),
            run_id=run_id,
        )
        next(stepper)  # factory_started written; run dir exists
        run_dir = tmp_path / ".kstrl" / "runs" / run_id
        app = _app(tmp_path, run_dir)
        async with app.run_test(size=(120, 40)) as pilot:
            table = await mounted(pilot, lambda: app.screen, ComponentTable)
            initial_rows = table.row_count
            for _ in stepper:  # stream the rest while the app is live
                pass
            # The loop above is synchronous, so no poll can have folded
            # half of it: the whole stream is on disk before this await.
            # Waiting for the app's own record of comp-b is weaker than
            # asserting the table painted it, which is the point.
            await settled(
                pilot,
                lambda: "comp-b" in app.store.state.components,
                what="a poll to fold the events streamed while the app was live",
            )
            # The overview screen receives state as a StateChanged
            # message, so the fold above is one hop ahead of the table.
            # `drained` observes that hop instead of guessing at it.
            await drained(
                pilot,
                app.screen,
                what="the folded state to reach the overview screen",
            )
            assert table.row_count == 2
            assert table.row_count >= initial_rows
            row = table.get_row("comp-b")
            assert "completed" in _cell_text(row[2])

    async def test_checkpoint_banner_in_dash_mode(self, tmp_path: Path) -> None:
        run_dir = write_fake_run(
            tmp_path,
            FakeRunSpec(components=2, include_checkpoint=True, complete=False),
        )
        app = _app(tmp_path, run_dir)
        async with app.run_test(size=(120, 40)) as pilot:
            banner = await mounted(pilot, lambda: app.screen, CheckpointBanner)
            # `display` is what the assertion is about, so the wait is
            # on the state instead. The screen's own on_mount HIDES the
            # banner; only the StateChanged the first poll posted shows
            # it again, so both hops have to be observed.
            await settled(
                pilot,
                lambda: app.store.state.components,
                what="the first poll to fold the run's event stream",
            )
            await drained(
                pilot,
                app.screen,
                what="the folded state to reach the overview screen",
            )
            assert banner.display is True
            rendered = str(banner.render())
            assert "checkpoint pending" in rendered
            assert "ks factory" in rendered  # observe-only hint

    async def test_q_detaches_with_zero(self, tmp_path: Path) -> None:
        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=1))
        app = _app(tmp_path, run_dir)
        async with app.run_test(size=(120, 40)) as pilot:
            await mounted(pilot, lambda: app.screen, ComponentTable)
            await pilot.press("q")
            # Condition and assertion nearly coincide here, so the wait
            # takes the weaker half - the app exited with SOME value -
            # and the assertion below still owns which one. A key that
            # is not bound at all reports itself as the `what` here.
            await settled(
                pilot,
                lambda: app.return_value is not None,
                what="q to detach the dashboard",
            )
        assert app.return_value == 0

    async def test_ctrl_c_bound_and_detaches(self, tmp_path: Path) -> None:
        """Spike finding 1: ctrl+c arrives as a key and must be bound."""
        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=1))
        app = _app(tmp_path, run_dir)
        async with app.run_test(size=(120, 40)) as pilot:
            await mounted(pilot, lambda: app.screen, ComponentTable)
            await pilot.press("ctrl+c")
            # Same shape as the q test: the weaker half of the claim.
            await settled(
                pilot,
                lambda: app.return_value is not None,
                what="ctrl+c to detach the dashboard",
            )
        assert app.return_value == 0

    async def test_stream_replacement_resets_before_rebuild(
        self,
        tmp_path: Path,
    ) -> None:
        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=1))
        app = _app(tmp_path, run_dir)
        async with app.run_test(size=(120, 40)) as pilot:
            await settled(
                pilot,
                lambda: app.store.state.components,
                what="the first poll to fold the run's event stream",
            )
            initial_tokens = app.store.state.total_tokens
            replacement = tmp_path / "events.jsonl"
            replacement.write_bytes((run_dir / "events.jsonl").read_bytes())
            os.replace(replacement, run_dir / "events.jsonl")
            # `_poll` resets and re-folds into the store synchronously,
            # and nothing between here and the assertion awaits, so no
            # timer can interleave. The pause that used to stand after
            # this call settled nothing the assertion reads.
            app._poll()

            assert app.store.state.total_tokens == initial_tokens

    async def test_timers_ignore_empty_screen_stack_during_teardown(
        self,
        tmp_path: Path,
    ) -> None:
        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=1))
        app = _app(tmp_path, run_dir)
        async with app.run_test(size=(120, 40)) as pilot:
            # Folded state first, or the timers below would be called on
            # an app with nothing to render and the teardown path would
            # not be exercised at all.
            await settled(
                pilot,
                lambda: app.store.state.components,
                what="the first poll to fold the run's event stream",
            )
            with patch.object(
                KstrlTuiApp,
                "screen_stack",
                new_callable=PropertyMock,
                return_value=[],
            ):
                app._poll()
                app._tick_ages()


class TestRenderHelpers:
    def _state(self, tmp_path: Path) -> RunState:
        write_fake_run(tmp_path, FakeRunSpec(components=2))
        state, _ = load_run_state(tmp_path)
        return state

    def test_header_contains_project_and_state(self, tmp_path: Path) -> None:
        text = render_header(self._state(tmp_path)).plain
        assert "fake-project" in text
        assert "finished" in text

    def test_cost_meter_lower_bound_marker(self, tmp_path: Path) -> None:
        state = self._state(tmp_path)
        assert state.unreported_calls > 0  # fixture includes unreported
        plain = render_cost_meter(state).plain
        assert "+" in plain
        assert "lower bound" in plain
        assert "%" in plain  # cap percentage present

    def test_cost_meter_without_unreported(self, tmp_path: Path) -> None:
        write_fake_run(
            tmp_path,
            FakeRunSpec(components=1, include_unreported_usage=False),
            run_id="factory-20260720-170000.000000-clean",
        )
        state, _ = load_run_state(
            tmp_path,
            "factory-20260720-170000.000000-clean",
        )
        plain = render_cost_meter(state).plain
        assert "lower bound" not in plain


class TestCostMeterPerAxisLowerBound:
    """R8 review finding 1, reproduced on the RENDERED meter.

    The defect was not that the state was wrong - it was that the
    dashboard rendered a partially covered total as an exact one. So
    these assert on ``render_cost_meter(...).plain``, never on state.
    """

    @staticmethod
    def _state(
        *,
        token_calls: int,
        cost_calls: int,
        gap: bool,
    ) -> RunState:
        # The reviewer's shape: two metered calls, both reporting tokens,
        # only one reporting a cost; $5 against a $10 cap.
        events: list[ev.Event] = [
            ev.RunPlan(components=(), max_cost_usd=10.0, max_total_tokens=100_000),
            ev.ComponentUsage(
                component="a",
                phase="engineer",
                calls=2,
                known_calls=2,
                token_calls=token_calls,
                cost_calls=cost_calls,
                total_tokens=1_000,
                cost_usd=5.0,
            ),
        ]
        if gap:
            events.append(
                ev.BudgetCoverage(
                    ceiling="max_cost_usd",
                    axis="cost",
                    calls=2,
                    covered_calls=1,
                    uncovered_calls=1,
                    uncovered_tokens=500,
                    uncovered_roles=("review",),
                    detail="cost coverage is PARTIAL",
                )
            )
        return fold(events)

    def test_partial_cost_coverage_marks_the_cost_figure(self) -> None:
        plain = render_cost_meter(
            self._state(token_calls=2, cost_calls=1, gap=True),
        ).plain
        assert "$5.00+" in plain
        assert "lower bound" in plain
        assert "cost" in plain

    def test_the_covered_token_axis_stays_unmarked(self) -> None:
        """The two axes differ in the measured run - tokens were fully
        covered while cost was not - so one shared marker would be
        wrong."""
        plain = render_cost_meter(
            self._state(token_calls=2, cost_calls=1, gap=True),
        ).plain
        assert "1.0k tok" in plain
        assert "1.0k+" not in plain
        assert "% of token cap" in plain
        assert "%+ of token cap" not in plain

    def test_the_cap_percentage_is_marked_too(self) -> None:
        """The percentage is what an operator reads as headroom; leaving
        it unmarked reports 50% of a cap that counts half the calls."""
        plain = render_cost_meter(
            self._state(token_calls=2, cost_calls=1, gap=True),
        ).plain
        assert "50%+ of cost cap" in plain

    def test_the_marker_needs_no_budget_coverage_event(self) -> None:
        """The usage events alone carry the fact; the run-scoped event is
        corroboration, not the only source."""
        plain = render_cost_meter(
            self._state(token_calls=2, cost_calls=1, gap=False),
        ).plain
        assert "$5.00+" in plain

    def test_a_tokenless_axis_marks_tokens_only(self) -> None:
        plain = render_cost_meter(
            self._state(token_calls=0, cost_calls=2, gap=False),
        ).plain
        assert "1.0k+ tok" in plain
        assert "$5.00 " in plain
        assert "lower bound (tokens)" in plain

    def test_full_coverage_says_nothing(self) -> None:
        plain = render_cost_meter(
            self._state(token_calls=2, cost_calls=2, gap=False),
        ).plain
        assert "+" not in plain
        assert "lower bound" not in plain

    def test_no_price_is_invented_for_the_uncovered_calls(self) -> None:
        """Standing constraint on this PR, extended to the dashboard: the
        uncovered magnitude is never converted into dollars."""
        plain = render_cost_meter(
            self._state(token_calls=2, cost_calls=1, gap=True),
        ).plain
        # Exactly one dollar figure on the line: the reported total.
        assert plain.count("$") == 1
