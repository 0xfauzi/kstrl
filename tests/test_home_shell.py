"""TUI surface D1: bare-`ks` contract, home shell, esc/q matrix."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from kstrl.cli import cli
from kstrl.tui.app import KstrlTuiApp, Mode
from kstrl.tui.screens.decompose import DecomposeScreen
from kstrl.tui.screens.home import HomeScreen
from kstrl.tui.screens.overview import OverviewScreen
from tests.helpers.fake_run import (
    FakeRunSpec,
    write_fake_decompose_run,
    write_fake_run,
)


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
            tmp_path, FakeRunSpec(components=1),
            run_id="factory-20260718-100000.000000-old",
        )
        write_fake_decompose_run(tmp_path)
        (tmp_path / "kstrl.toml").write_text("")
        app = _home_app(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            assert isinstance(app.screen, HomeScreen)
            table = app.screen.query_one("#home-runs")
            keys = [str(k.value) for k in table.rows]  # type: ignore[attr-defined]
            assert keys[0].startswith("decompose-")  # newest first
            assert len(keys) == 2
            masthead = str(
                app.screen.query_one("#home-masthead").renderable,
            )
            assert "kstrl.toml ✓" in masthead

    async def test_missing_toml_warns_in_masthead(
        self, tmp_path: Path,
    ) -> None:
        app = _home_app(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            masthead = str(
                app.screen.query_one("#home-masthead").renderable,
            )
            assert "run ks init" in masthead

    async def test_enter_opens_run_with_kind_dispatch_and_escape_returns(
        self, tmp_path: Path,
    ) -> None:
        write_fake_decompose_run(tmp_path)
        app = _home_app(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("enter")
            await pilot.pause(0.2)
            assert isinstance(app.screen, DecomposeScreen)
            assert app.run_context is not None
            assert not app.run_context.owns_app_exit
            # escape pops the decompose screen to the overview...
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)
            # ...and again back to home, tearing the context down.
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, HomeScreen)
            assert app.run_context is None

    async def test_q_over_a_run_pops_home_not_exit(
        self, tmp_path: Path,
    ) -> None:
        write_fake_run(tmp_path, FakeRunSpec(components=1))
        app = _home_app(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("enter")
            await pilot.pause(0.2)
            assert isinstance(app.screen, OverviewScreen)
            await pilot.press("q")
            await pilot.pause()
            assert isinstance(app.screen, HomeScreen)
            assert app.return_value is None  # still running
            await pilot.press("q")
            await pilot.pause()
        assert app.return_value == 0

    async def test_dash_command_opens_newest_run(
        self, tmp_path: Path,
    ) -> None:
        write_fake_run(tmp_path, FakeRunSpec(components=1))
        app = _home_app(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            commands = app.screen.query_one("#home-commands")
            commands.focus()
            await pilot.press("down")  # highlight the first entry
            await pilot.press("enter")
            await pilot.pause(0.2)
            assert isinstance(app.screen, OverviewScreen)

    async def test_empty_state_renders_guidance(
        self, tmp_path: Path,
    ) -> None:
        app = _home_app(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            title = str(
                app.screen.query_one("#home-runs-title").renderable,
            )
            assert "none yet" in title


class TestDashUnchanged:
    async def test_standalone_dash_q_still_detaches(
        self, tmp_path: Path,
    ) -> None:
        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=1))
        app = KstrlTuiApp(
            run_dir=run_dir, root_dir=tmp_path, mode=Mode.DASH,
            poll_interval=0.05,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            assert isinstance(app.screen, OverviewScreen)
            # escape on the base screen is a no-op in standalone dash.
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)
            await pilot.press("q")
            await pilot.pause()
        assert app.return_value == 0
