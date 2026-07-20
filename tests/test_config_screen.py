"""TUI surface D3: the config screen over the precomputed report."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, cast

import pytest

from kstrl.config_report import build_config_report
from kstrl.tui.app import KstrlTuiApp, Mode
from kstrl.tui.runcontext import RunContext
from kstrl.tui.screens.config import ConfigScreen
from kstrl.tui.screens.home import HomeScreen


def _app(tmp_path: Path, report: Any) -> KstrlTuiApp:
    return KstrlTuiApp(
        root_dir=tmp_path, mode=Mode.HOME, poll_interval=0.05,
        config_report=report,
    )


@pytest.fixture
def report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    (tmp_path / "kstrl.toml").write_text(
        "[run]\nmax_iterations = 42\n",
    )
    monkeypatch.setenv("SLEEP_SECONDS", "9")
    return build_config_report(tmp_path)


class TestConfigScreen:
    async def test_rows_sources_and_hint(
        self, tmp_path: Path, report: Any,
    ) -> None:
        app = _app(tmp_path, report)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            app.push_screen(ConfigScreen())
            await pilot.pause(0.2)
            screen = cast(ConfigScreen, app.screen)
            table = screen.query_one("#config-table")
            assert table.row_count == len(report.rows)  # type: ignore[attr-defined]
            title = str(screen.query_one("#config-title").renderable)
            assert f"{len(report.rows)}/{len(report.rows)}" in title
            hint = str(screen.query_one("#config-hint").renderable)
            assert "kstrl.toml" in hint

    async def test_filter_narrows_and_escape_clears_then_pops(
        self, tmp_path: Path, report: Any,
    ) -> None:
        app = _app(tmp_path, report)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            app.push_screen(ConfigScreen())
            await pilot.pause(0.2)
            screen = cast(ConfigScreen, app.screen)
            await pilot.press("slash")
            from textual.widgets import Input

            filter_input = screen.query_one(Input)
            assert filter_input.has_focus  # "/" focused it
            filter_input.value = "max_iterations"
            await pilot.pause()
            table = screen.query_one("#config-table")
            assert table.row_count == 1  # type: ignore[attr-defined]
            # escape with a filter clears it first...
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, ConfigScreen)
            assert table.row_count == len(report.rows)  # type: ignore[attr-defined]
            # ...and out of the input, escape pops the screen.
            screen.query_one("#config-table").focus()
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, HomeScreen)

    async def test_refresh_refused_while_a_session_is_active(
        self, tmp_path: Path, report: Any,
    ) -> None:
        app = _app(tmp_path, report)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            # Fake an in-flight launched session: a context with a
            # not-done handle.
            run_dir = tmp_path / ".kstrl" / "runs" / "factory-x"
            run_dir.mkdir(parents=True)

            class FakeHandle:
                def done(self) -> bool:
                    return False

            context = RunContext.observe(
                run_dir, tmp_path, owns_app_exit=False,
            )
            context.handle = cast(Any, FakeHandle())
            app.run_context = context
            app.push_screen(ConfigScreen())
            await pilot.pause(0.2)
            before = app.config_report
            await pilot.press("r")
            await pilot.pause()
            assert app.config_report is before  # refused, not recomputed

    async def test_missing_report_renders_guidance(
        self, tmp_path: Path,
    ) -> None:
        app = _app(tmp_path, None)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            app.push_screen(ConfigScreen())
            await pilot.pause(0.2)
            hint = str(app.screen.query_one("#config-hint").renderable)
            assert "could not be resolved" in hint

    async def test_launcher_entry_opens_the_screen(
        self, tmp_path: Path, report: Any,
    ) -> None:
        app = _app(tmp_path, report)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            commands = app.screen.query_one("#home-commands")
            commands.focus()
            deadline = time.monotonic() + 3
            while not isinstance(app.screen, ConfigScreen):
                await pilot.press("down")
                await pilot.press("enter")
                await pilot.pause(0.1)
                assert time.monotonic() < deadline, "config never opened"
