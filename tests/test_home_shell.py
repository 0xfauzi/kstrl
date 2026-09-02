"""TUI surface D1: bare-`ks` contract, home shell, esc/q matrix."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from kstrl.cli import cli
from kstrl.tui.app import KstrlTuiApp, Mode
from kstrl.tui.screens.decompose import DecomposeScreen
from kstrl.tui.screens.home import HomeScreen
from kstrl.tui.screens.launch import FactoryLaunchForm
from kstrl.tui.screens.overview import OverviewScreen
from tests.helpers.fake_run import (
    FakeRunSpec,
    write_fake_decompose_run,
    write_fake_run,
)
from tests.helpers.settle import drained, mounted, settled


class TestBareInvocation:
    def test_non_tty_prints_help_and_exits_2(self) -> None:
        """The pipe/CI contract: byte-identical to click's no-args
        behavior from before the group callback existed."""
        result = CliRunner().invoke(cli, [])
        assert result.exit_code == 2
        assert "Usage:" in result.output
        assert "Commands:" in result.output

    def test_kstrl_no_tui_suppresses_the_shell(self) -> None:
        result = CliRunner().invoke(cli, [], env={"KSTRL_NO_TUI": "1"})
        assert result.exit_code == 2
        assert "Usage:" in result.output

    def test_help_flag_unchanged(self) -> None:
        result = CliRunner().invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "Usage:" in result.output


def _home_app(root: Path) -> KstrlTuiApp:
    return KstrlTuiApp(root_dir=root, mode=Mode.HOME, poll_interval=0.05)


class TestHomeScreen:
    async def test_renders_runs_and_identity(self, tmp_path: Path) -> None:
        write_fake_run(
            tmp_path,
            FakeRunSpec(components=1),
            run_id="factory-20260718-100000.000000-old",
        )
        write_fake_decompose_run(tmp_path)
        (tmp_path / "kstrl.toml").write_text("")
        app = _home_app(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            table = await mounted(pilot, lambda: app.screen, "#home-runs")
            # The table is queryable as soon as compose mounts it, which
            # is BEFORE the screen's own on_mount discovers the runs and
            # fills the masthead in. Draining the screen's queue observes
            # that on_mount ran without asserting what it wrote.
            await drained(pilot, app.screen, what="the home screen's on_mount to run")
            assert isinstance(app.screen, HomeScreen)
            keys = [str(k.value) for k in table.rows]  # type: ignore[attr-defined]
            assert keys[0].startswith("decompose-")  # newest first
            assert len(keys) == 2
            masthead = str(
                app.screen.query_one("#home-masthead").content,
            )
            assert "kstrl.toml ✓" in masthead

            # A run discovered after mount belongs at the top, while
            # the currently selected run stays selected after reorder.
            # Nothing async here: refresh_runs is a direct call and it
            # rewrites the rows before it returns.
            table.move_cursor(row=1)  # the older factory run
            write_fake_run(
                tmp_path,
                run_id="factory-20260720-160000.000000-newest",
            )
            app.screen.refresh_runs()
            keys = [str(k.value) for k in table.rows]
            assert keys[0].endswith("newest")
            assert table.cursor_row == 2

    async def test_missing_toml_warns_in_masthead(
        self,
        tmp_path: Path,
    ) -> None:
        app = _home_app(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            masthead_widget = await mounted(pilot, lambda: app.screen, "#home-masthead")
            # Composed empty and filled in by on_mount, so "it says
            # anything at all" is a real condition, and it is weaker
            # than the warning this test is about.
            await settled(
                pilot,
                lambda: str(masthead_widget.content),
                what="the masthead to be filled in on mount",
            )
            masthead = str(masthead_widget.content)
            assert "run ks init" in masthead

    async def test_enter_opens_run_with_kind_dispatch_and_escape_returns(
        self,
        tmp_path: Path,
    ) -> None:
        write_fake_decompose_run(tmp_path)
        app = _home_app(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            table = await mounted(pilot, lambda: app.screen, "#home-runs")
            # enter selects the highlighted row, so the row has to be
            # there and the table has to own the focus before the key
            # means anything. `focus()` defers through call_later, so
            # neither is true just because the screen mounted.
            await settled(
                pilot,
                lambda: table.rows and app.screen.focused is table,
                what="the run table to list the run and take focus",
            )
            await pilot.press("enter")
            # Deliberately weaker than the assertion: "home is no longer
            # on top", not "the decompose screen is", so a run opened
            # onto the wrong screen fails below with its own message.
            await settled(
                pilot,
                lambda: not isinstance(app.screen, HomeScreen),
                what="enter to open the highlighted run",
            )
            assert isinstance(app.screen, DecomposeScreen)
            assert app.run_context is not None
            assert not app.run_context.owns_app_exit
            # escape pops the decompose screen to the overview...
            await pilot.press("escape")
            await settled(
                pilot,
                lambda: not isinstance(app.screen, DecomposeScreen),
                what="escape to pop the decompose screen",
            )
            assert isinstance(app.screen, OverviewScreen)
            # ...and again back to home, tearing the context down.
            await pilot.press("escape")
            await settled(
                pilot,
                lambda: not isinstance(app.screen, OverviewScreen),
                what="escape to pop the run's board",
            )
            assert isinstance(app.screen, HomeScreen)
            # The teardown is home's on_screen_resume, and pop_screen
            # posts that event before it returns, so draining the home
            # screen's queue observes the handler running without
            # asserting that it closed anything.
            await drained(pilot, app.screen, what="home's resume handler to run")
            assert app.run_context is None

    async def test_q_over_a_run_pops_home_not_exit(
        self,
        tmp_path: Path,
    ) -> None:
        write_fake_run(tmp_path, FakeRunSpec(components=1))
        app = _home_app(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            table = await mounted(pilot, lambda: app.screen, "#home-runs")
            await settled(
                pilot,
                lambda: table.rows and app.screen.focused is table,
                what="the run table to list the run and take focus",
            )
            await pilot.press("enter")
            await settled(
                pilot,
                lambda: not isinstance(app.screen, HomeScreen),
                what="enter to open the highlighted run",
            )
            assert isinstance(app.screen, OverviewScreen)
            await pilot.press("q")
            await settled(
                pilot,
                lambda: not isinstance(app.screen, OverviewScreen),
                what="q over a run to pop back towards home",
            )
            assert isinstance(app.screen, HomeScreen)
            assert app.return_value is None  # still running
            await pilot.press("q")
            # Weaker than the assertion below: that the app exited at
            # all, not that it exited 0, so a wrong exit code is still
            # reported by the assertion that names it.
            await settled(
                pilot,
                lambda: app.return_value is not None,
                what="q on home to exit the app",
            )
        assert app.return_value == 0

    async def test_dash_command_opens_newest_run(
        self,
        tmp_path: Path,
    ) -> None:
        write_fake_run(tmp_path, FakeRunSpec(components=1))
        app = _home_app(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            from kstrl.tui.screens.home import HOME_COMMANDS

            commands = await mounted(pilot, lambda: app.screen, "#home-commands")
            # on_mount adds the options AND discovers the run the dash
            # command opens, and it does both before it returns, so an
            # option to highlight is evidence of the whole handler.
            await settled(
                pilot,
                lambda: commands.option_count,  # type: ignore[attr-defined]
                what="the command list to be filled in on mount",
            )
            commands.focus()
            # `focus()` schedules through call_later, so the list does
            # not own the key that follows just because focus() returned.
            await settled(
                pilot,
                lambda: app.screen.focused is commands,
                what="the command list to take focus",
            )
            dash_index = [c.command_id for c in HOME_COMMANDS].index("dash")
            commands.highlighted = dash_index  # type: ignore[attr-defined]
            await pilot.press("enter")
            await settled(
                pilot,
                lambda: not isinstance(app.screen, HomeScreen),
                what="the dash command to open the newest run",
            )
            assert isinstance(app.screen, OverviewScreen)

    async def test_digit_hotkey_opens_matching_command(
        self,
        tmp_path: Path,
    ) -> None:
        app = _home_app(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            # The digit binding lives on the home screen, so the screen
            # has to be composed before the key means anything.
            await mounted(pilot, lambda: app.screen, "#home-commands")
            await pilot.press("1")
            await settled(
                pilot,
                lambda: not isinstance(app.screen, HomeScreen),
                what="the digit hotkey to open a command screen",
            )
            assert isinstance(app.screen, FactoryLaunchForm)

    async def test_preview_tracks_highlight_and_enter_opens_that_run(
        self,
        tmp_path: Path,
    ) -> None:
        write_fake_run(
            tmp_path,
            FakeRunSpec(components=1),
            run_id="factory-20260718-100000.000000-old",
        )
        write_fake_decompose_run(tmp_path)
        app = _home_app(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            runs = await mounted(pilot, lambda: app.screen, "#home-runs")
            preview = await mounted(pilot, lambda: app.screen, "#home-preview")
            screen = app.screen
            assert isinstance(screen, HomeScreen)
            # The preview board renders from the folded state the
            # summaries worker caches, so the worker has to have landed
            # before a highlight can fill the board in. This is what the
            # 0.3s pause was buying, without the guess.
            await settled(
                pilot,
                lambda: screen._summaries,
                what="the summaries worker to land its folded run state",
            )
            previewed = screen._preview_run_id
            runs.move_cursor(row=1)
            # Weaker than the assertion: the preview followed the
            # highlight somewhere, not that it followed it to the old
            # run, so a preview that tracks the wrong row fails below.
            await settled(
                pilot,
                lambda: screen._preview_run_id != previewed,
                what="the row highlight to reach the preview",
            )
            assert screen._preview_run_id.endswith("old")
            assert preview.row_count == 1  # type: ignore[attr-defined]
            preview.focus()
            await settled(
                pilot,
                lambda: screen.focused is preview,
                what="the preview board to take focus",
            )
            await pilot.press("enter")
            await settled(
                pilot,
                lambda: not isinstance(app.screen, HomeScreen),
                what="enter on the preview to open the previewed run",
            )
            assert isinstance(app.screen, OverviewScreen)

    async def test_empty_state_renders_guidance(
        self,
        tmp_path: Path,
    ) -> None:
        app = _home_app(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            title_widget = await mounted(pilot, lambda: app.screen, "#home-runs-title")
            # Composed with a placeholder, so "it has content" is
            # already true and would settle nothing. The empty-state
            # guidance is written by on_mount, and draining the screen
            # observes that on_mount ran without asserting what it said.
            await drained(pilot, app.screen, what="the home screen's on_mount to run")
            title = str(title_widget.content)
            assert "none yet" in title


class TestDashUnchanged:
    async def test_standalone_dash_q_still_detaches(
        self,
        tmp_path: Path,
    ) -> None:
        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=1))
        app = KstrlTuiApp(
            run_dir=run_dir,
            root_dir=tmp_path,
            mode=Mode.DASH,
            poll_interval=0.05,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await mounted(pilot, lambda: app.screen, "#topbar")
            assert isinstance(app.screen, OverviewScreen)
            # escape on the base screen is a no-op in standalone dash.
            await pilot.press("escape")
            # No predicate can serve here: the CORRECT outcome is that
            # nothing changes. `drained` observes the key having been
            # handled instead of guessing how long that takes.
            await drained(pilot, app, what="the escape key to be handled")
            assert isinstance(app.screen, OverviewScreen)
            await pilot.press("q")
            await settled(
                pilot,
                lambda: app.return_value is not None,
                what="q to detach from the run and exit",
            )
        assert app.return_value == 0
