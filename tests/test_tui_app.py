"""Stage 3 PR D (TUI rewrite): dashboard app + overview screen.

Textual Pilot tests over the fake-run fixture. Headless: run_test()
drives the real app without a terminal.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import PropertyMock, patch

from rich.text import Text

from kstrl.reducer import RunState, load_run_state
from kstrl.tui.app import KstrlTuiApp, Mode
from kstrl.tui.screens.overview import CheckpointBanner
from kstrl.tui.widgets.component_table import ComponentTable
from kstrl.tui.widgets.cost_meter import render_cost_meter
from kstrl.tui.widgets.header import render_header
from tests.helpers.fake_run import FakeRunSpec, stream_fake_run, write_fake_run


def _app(root: Path, run_dir: Path) -> KstrlTuiApp:
    return KstrlTuiApp(
        run_dir=run_dir, root_dir=root, mode=Mode.DASH, poll_interval=0.05,
    )


def _cell_text(value: object) -> str:
    return value.plain if isinstance(value, Text) else str(value)


class TestOverview:
    async def test_renders_fake_run(self, tmp_path: Path) -> None:
        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=3))
        app = _app(tmp_path, run_dir)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            table = app.screen.query_one(ComponentTable)
            assert table.row_count == 3
            row = table.get_row("comp-a")
            texts = [_cell_text(cell) for cell in row]
            assert "comp-a" in texts[1]
            assert "completed" in texts[2]

    async def test_live_updates_arrive(self, tmp_path: Path) -> None:
        run_id = "factory-20260720-160000.000000-live"
        stepper = stream_fake_run(
            tmp_path, FakeRunSpec(components=2), run_id=run_id,
        )
        next(stepper)  # factory_started written; run dir exists
        run_dir = tmp_path / ".kstrl" / "runs" / run_id
        app = _app(tmp_path, run_dir)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            table = app.screen.query_one(ComponentTable)
            initial_rows = table.row_count
            for _ in stepper:  # stream the rest while the app is live
                pass
            await pilot.pause(0.3)  # a few poll intervals
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
            await pilot.pause()
            banner = app.screen.query_one(CheckpointBanner)
            assert banner.display is True
            rendered = str(banner.render())
            assert "checkpoint pending" in rendered
            assert "ralph factory" in rendered  # observe-only hint

    async def test_q_detaches_with_zero(self, tmp_path: Path) -> None:
        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=1))
        app = _app(tmp_path, run_dir)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("q")
        assert app.return_value == 0

    async def test_ctrl_c_bound_and_detaches(self, tmp_path: Path) -> None:
        """Spike finding 1: ctrl+c arrives as a key and must be bound."""
        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=1))
        app = _app(tmp_path, run_dir)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("ctrl+c")
        assert app.return_value == 0

    async def test_stream_replacement_resets_before_rebuild(
        self, tmp_path: Path,
    ) -> None:
        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=1))
        app = _app(tmp_path, run_dir)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            initial_tokens = app.store.state.total_tokens
            replacement = tmp_path / "events.jsonl"
            replacement.write_bytes((run_dir / "events.jsonl").read_bytes())
            os.replace(replacement, run_dir / "events.jsonl")
            app._poll()
            await pilot.pause()

            assert app.store.state.total_tokens == initial_tokens

    async def test_timers_ignore_empty_screen_stack_during_teardown(
        self, tmp_path: Path,
    ) -> None:
        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=1))
        app = _app(tmp_path, run_dir)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            with patch.object(
                KstrlTuiApp, "screen_stack", new_callable=PropertyMock,
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
            tmp_path, "factory-20260720-170000.000000-clean",
        )
        plain = render_cost_meter(state).plain
        assert "lower bound" not in plain
