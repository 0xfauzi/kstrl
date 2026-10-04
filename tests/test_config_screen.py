"""TUI surface D3: the config screen over the precomputed report."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar, cast
from unittest.mock import patch

import pytest
from rich.text import Text
from textual.coordinate import Coordinate
from textual.widgets import DataTable

from kstrl.config_report import (
    ConfigReport,
    ConfigRow,
    build_config_report,
)
from kstrl.tui.app import KstrlTuiApp, Mode
from kstrl.tui.runcontext import RunContext
from kstrl.tui.screens.config import ConfigScreen, display_value
from kstrl.tui.screens.home import HomeScreen
from tests.helpers.settle import mounted, settled

#: What every test here waits on after pushing the screen, spelled once.
#: ``ConfigScreen.on_mount`` adds the four columns and THEN renders the
#: report, in one synchronous call, so a table that has columns is a
#: screen whose on_mount has returned: the rows, the title and the hint
#: are all drawn. A poll runs only between messages and cannot catch it
#: half-done. It is also weaker than every assertion below - it says the
#: screen finished loading, not what it loaded.
_LOADED = "the config screen's on_mount to fill the table"


def _app(tmp_path: Path, report: Any) -> KstrlTuiApp:
    return KstrlTuiApp(
        root_dir=tmp_path,
        mode=Mode.HOME,
        poll_interval=0.05,
        config_report=report,
    )


def test_display_value_only_relativizes_paths_inside_root() -> None:
    root = "/work/repo"
    assert display_value("/work/repo/src/app.py", root).plain == "src/app.py"
    assert display_value("/work/repository/app.py", root).plain == ("/work/repository/app.py")
    assert (
        display_value(
            "['/work/repo/src', '/work/repository/tests']",
            root,
        ).plain
        == "['src', '/work/repository/tests']"
    )
    assert display_value("/work/repo", root).plain == "."


@pytest.fixture
def report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    (tmp_path / "kstrl.toml").write_text(
        "[run]\nmax_iterations = 42\n",
    )
    monkeypatch.setenv("SLEEP_SECONDS", "9")
    return build_config_report(tmp_path)


class TestConfigScreen:
    async def test_rows_sources_and_hint(
        self,
        tmp_path: Path,
        report: Any,
    ) -> None:
        app = _app(tmp_path, report)
        async with app.run_test(size=(120, 40)) as pilot:
            # The app installs its home screen from its own on_mount,
            # so a screen pushed before that lands under it and never
            # becomes active. This is a precondition of the push, not
            # politeness.
            await mounted(pilot, lambda: app.screen, "#home-commands")
            app.push_screen(ConfigScreen())
            screen = cast(ConfigScreen, app.screen)
            table = await mounted(pilot, lambda: screen, "#config-table")
            await settled(pilot, lambda: cast(DataTable, table).columns, what=_LOADED)
            assert table.row_count == len(report.rows)  # type: ignore[attr-defined]
            title_widget = await mounted(pilot, lambda: screen, "#config-title")
            title = str(title_widget.content)  # type: ignore[attr-defined]
            assert f"{len(report.rows)}/{len(report.rows)}" in title
            hint_widget = await mounted(pilot, lambda: screen, "#config-hint")
            hint = str(hint_widget.content)  # type: ignore[attr-defined]
            assert "kstrl.toml" in hint

    async def test_values_render_as_literal_text(
        self,
        tmp_path: Path,
    ) -> None:
        report = ConfigReport(
            root_dir=tmp_path,
            toml_path=tmp_path / "kstrl.toml",
            toml_exists=False,
            rows=(ConfigRow("run", "value", "[/bold]", "env"),),
        )
        app = _app(tmp_path, report)
        async with app.run_test(size=(120, 40)) as pilot:
            await mounted(pilot, lambda: app.screen, "#home-commands")
            app.push_screen(ConfigScreen())
            table = await mounted(pilot, lambda: app.screen, "#config-table")
            await settled(pilot, lambda: cast(DataTable, table).columns, what=_LOADED)
            value = table.get_cell_at(Coordinate(0, 2))  # type: ignore[attr-defined]
            assert isinstance(value, Text)
            assert value.plain == "[/bold]"

    async def test_filter_narrows_and_escape_clears_then_pops(
        self,
        tmp_path: Path,
        report: Any,
    ) -> None:
        app = _app(tmp_path, report)
        async with app.run_test(size=(120, 40)) as pilot:
            await mounted(pilot, lambda: app.screen, "#home-commands")
            app.push_screen(ConfigScreen())
            screen = cast(ConfigScreen, app.screen)
            table = await mounted(pilot, lambda: screen, "#config-table")
            await settled(pilot, lambda: cast(DataTable, table).columns, what=_LOADED)
            from textual.widgets import Input

            filter_input = await mounted(pilot, lambda: screen, Input)
            # Textual auto-focuses the first focusable widget, which IS
            # this Input, so the assertion below passed on a screen
            # whose "/" did nothing at all - measured by neutering
            # action_focus_filter. Dropping focus first is what makes
            # it about the key.
            #
            # The wait and that assertion then very nearly coincide,
            # which is allowed as long as the `what` reads as the
            # failure wanted. "Focus moved somewhere" is the weaker
            # half of it: a "/" that focuses the WRONG widget ends the
            # wait and fails on the assertion below, in its own words.
            # A "/" that does nothing leaves no state to wait for and
            # times out here instead, naming the key.
            screen.set_focus(None)
            await pilot.press("slash")
            await settled(
                pilot,
                lambda: screen.focused is not None,
                what="the / key to move focus",
            )
            assert filter_input.has_focus  # "/" focused it
            filter_input.value = "max_iterations"
            await settled(
                pilot,
                lambda: cast(DataTable, table).row_count < len(report.rows),
                what="the filter to narrow the table",
            )
            assert table.row_count == 1  # type: ignore[attr-defined]
            # Escape clears an active filter even after focus moved
            # into the results table.
            table.focus()  # type: ignore[attr-defined]
            await pilot.press("escape")
            await settled(
                pilot,
                lambda: cast(DataTable, table).row_count > 1,
                what="escape to clear the filter and redraw the table",
            )
            assert isinstance(app.screen, ConfigScreen)
            assert table.row_count == len(report.rows)  # type: ignore[attr-defined]
            # The next escape pops the screen.
            await pilot.press("escape")
            await settled(
                pilot,
                lambda: app.screen is not screen,
                what="escape to pop the config screen",
            )
            assert isinstance(app.screen, HomeScreen)

    async def test_refresh_refused_while_a_session_is_active(
        self,
        tmp_path: Path,
        report: Any,
    ) -> None:
        app = _app(tmp_path, report)
        async with app.run_test(size=(120, 40)) as pilot:
            await mounted(pilot, lambda: app.screen, "#home-commands")
            # Fake an in-flight launched session: a context with a
            # not-done handle.
            run_dir = tmp_path / ".kstrl" / "runs" / "factory-x"
            run_dir.mkdir(parents=True)

            class FakeHandle:
                finished = False
                # Read by the HOME session watcher once done() flips.
                # Keep this in sync with kstrl.tui.bridge's handle
                # interface: the watcher's completion path reads
                # exit_code AND error_box unconditionally.
                exit_code = 0
                error_box: ClassVar[list[BaseException]] = []

                def done(self) -> bool:
                    return self.finished

            context = RunContext.observe(
                run_dir,
                tmp_path,
                owns_app_exit=False,
            )
            handle = FakeHandle()
            context.handle = cast(Any, handle)
            app.run_context = context
            app.push_screen(ConfigScreen())
            screen = cast(ConfigScreen, app.screen)
            table = await mounted(pilot, lambda: screen, "#config-table")
            await settled(pilot, lambda: cast(DataTable, table).columns, what=_LOADED)
            before = app.config_report
            # action_refresh is a direct call and every branch of it is
            # synchronous: the refusal, the notify and the assignment
            # to app.config_report all happen before it returns, so
            # there is nothing left to settle once it has.
            screen.action_refresh()
            assert app.config_report is before  # refused, not recomputed

            # A finished handle no longer has a thread reading the
            # environment, so refresh is safe again.
            handle.finished = True
            refreshed = build_config_report(tmp_path)

            with patch(
                "kstrl.config_report.build_config_report",
                return_value=refreshed,
            ) as build:
                screen.action_refresh()
            build.assert_called_once_with(tmp_path)
            assert app.config_report is refreshed

    async def test_missing_report_renders_guidance(
        self,
        tmp_path: Path,
    ) -> None:
        app = _app(tmp_path, None)
        async with app.run_test(size=(120, 40)) as pilot:
            await mounted(pilot, lambda: app.screen, "#home-commands")
            app.push_screen(ConfigScreen())
            table = await mounted(pilot, lambda: app.screen, "#config-table")
            await settled(pilot, lambda: cast(DataTable, table).columns, what=_LOADED)
            hint_widget = await mounted(pilot, lambda: app.screen, "#config-hint")
            hint = str(hint_widget.content)  # type: ignore[attr-defined]
            assert "could not be resolved" in hint

    async def test_launcher_entry_opens_the_screen(
        self,
        tmp_path: Path,
        report: Any,
    ) -> None:
        app = _app(tmp_path, report)
        async with app.run_test(size=(120, 40)) as pilot:
            from kstrl.tui.screens.home import HOME_COMMANDS

            commands = await mounted(pilot, lambda: app.screen, "#home-commands")
            home = app.screen
            commands.focus()
            # Widget.focus routes through call_later, so focus is not
            # in place when it returns and an enter pressed before it
            # lands would go somewhere else.
            await settled(
                pilot,
                lambda: commands.has_focus,
                what="the command list to take focus",
            )
            commands.highlighted = [  # type: ignore[attr-defined]
                c.command_id for c in HOME_COMMANDS
            ].index("config")
            await pilot.press("enter")
            # "some screen opened" is weaker than "the config screen
            # opened": enter that opens the wrong screen ends the wait
            # and fails on the assertion below, in its own words.
            await settled(
                pilot,
                lambda: app.screen is not home,
                what="enter on the config entry to open a screen",
            )
            assert isinstance(app.screen, ConfigScreen)
