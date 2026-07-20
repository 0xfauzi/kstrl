"""TUI surface C5: decompose screens, kind dispatch, status --tui."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

from click.testing import CliRunner

from kstrl.cli import cli
from kstrl.tui.app import KstrlTuiApp, Mode
from kstrl.tui.dispatch import initial_screens_for_kind
from kstrl.tui.screens.component import ComponentScreen
from kstrl.tui.screens.decompose import DecomposeScreen, SpecTriageScreen
from kstrl.tui.screens.overview import OverviewScreen
from kstrl.tui.widgets.dag_table import DagTable, compute_tiers
from tests.helpers.fake_run import (
    write_fake_decompose_run,
    write_fake_run,
)


class TestComputeTiers:
    def test_chain_and_diamond(self) -> None:
        assert compute_tiers({
            "a": (), "b": ("a",), "c": ("a",), "d": ("b", "c"),
        }) == {"a": 0, "b": 1, "c": 1, "d": 2}

    def test_unknown_deps_ignored(self) -> None:
        assert compute_tiers({"a": ("ghost",), "b": ("a",)}) == {
            "a": 0, "b": 1,
        }

    def test_cycle_marks_members_not_raises(self) -> None:
        tiers = compute_tiers({"a": ("b",), "b": ("a",), "c": ()})
        assert tiers["c"] == 0
        assert tiers["a"] == -1 and tiers["b"] == -1


class TestDispatch:
    def test_kinds_map_to_stacks(self) -> None:
        decompose = initial_screens_for_kind("decompose", observe_only=True)()
        assert [type(s) for s in decompose] == [OverviewScreen, DecomposeScreen]
        understand = initial_screens_for_kind("understand", observe_only=True)()
        assert isinstance(understand[-1], ComponentScreen)
        assert understand[-1].component_id == "understand"
        feature = initial_screens_for_kind(
            "feature", observe_only=False, component="demo",
        )()
        assert isinstance(feature[-1], ComponentScreen)
        assert feature[-1].component_id == "demo"
        factory = initial_screens_for_kind("factory", observe_only=True)()
        assert [type(s) for s in factory] == [OverviewScreen]
        unknown = initial_screens_for_kind("someday", observe_only=True)()
        assert [type(s) for s in unknown] == [OverviewScreen]


def _decompose_app(root: Path, run_dir: Path) -> KstrlTuiApp:
    return KstrlTuiApp(
        run_dir=run_dir, root_dir=root, mode=Mode.DASH,
        poll_interval=0.05,
        screen_factory=initial_screens_for_kind(
            "decompose", observe_only=True,
        ),
    )


class TestDecomposeScreen:
    async def test_success_run_renders_dag_and_summary(
        self, tmp_path: Path,
    ) -> None:
        run_dir = write_fake_decompose_run(tmp_path, attempts=2)
        app = _decompose_app(tmp_path, run_dir)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            assert isinstance(app.screen, DecomposeScreen)
            table = app.screen.query_one(DagTable)
            assert list(table.rows) != []
            row_keys = {key.value for key in table.rows}
            assert row_keys == {"database", "api"}  # architect excluded
            strip = app.screen.query_one("#issues-strip")
            assert "minor" in str(strip.renderable)
            attempt = app.screen.query_one("#attempt-strip")
            assert "attempt 2" in str(attempt.renderable)
            summary = app.screen.query_one("#decompose-summary")
            assert summary.display
            assert "2 component(s)" in str(summary.renderable)

    async def test_triage_shows_blocker_banner_and_detail(
        self, tmp_path: Path,
    ) -> None:
        run_dir = write_fake_decompose_run(tmp_path, blockers=1, minors=1)
        app = _decompose_app(tmp_path, run_dir)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("i")
            await pilot.pause()
            assert isinstance(app.screen, SpecTriageScreen)
            banner = app.screen.query_one("#triage-banner")
            assert banner.display
            assert "halted" in str(banner.renderable)
            # Blockers sort first; the detail pane carries the
            # suggestion for the highlighted row.
            detail = app.screen.query_one("#triage-detail")
            text = str(detail.renderable)
            assert "[blocker]" in text
            assert "Resolve it" in text
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, DecomposeScreen)

    async def test_escape_pops_to_overview(self, tmp_path: Path) -> None:
        run_dir = write_fake_decompose_run(tmp_path)
        app = _decompose_app(tmp_path, run_dir)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)


class TestStatusTui:
    def _capture_app(self) -> tuple[list[KstrlTuiApp], Any]:
        captured: list[KstrlTuiApp] = []

        def fake_run(self: KstrlTuiApp) -> int:
            captured.append(self)
            return 0

        return captured, fake_run

    def test_explicit_tui_opens_the_newest_run_with_kind_dispatch(
        self, tmp_path: Path,
    ) -> None:
        write_fake_run(tmp_path, run_id="factory-20260718-100000.000000-old")
        write_fake_decompose_run(tmp_path)
        captured, fake_run = self._capture_app()
        runner = CliRunner()
        with patch.object(KstrlTuiApp, "run", fake_run):
            result = runner.invoke(
                cli, ["status", "--root", str(tmp_path), "--tui"],
            )
        assert result.exit_code == 0
        assert len(captured) == 1
        app = captured[0]
        assert app.run_dir.name.startswith("decompose-")  # newest wins
        assert app.screen_factory is not None
        stack = app.screen_factory()
        assert isinstance(stack[-1], DecomposeScreen)

    def test_tui_with_no_runs_falls_back_to_plain_guidance(
        self, tmp_path: Path,
    ) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cli, ["status", "--root", str(tmp_path), "--tui"],
        )
        assert result.exit_code == 1
        assert "No manifest found" in result.output

    def test_non_tty_default_stays_plain(self, tmp_path: Path) -> None:
        """CliRunner is non-TTY: the auto rule must never open the
        dashboard - the pinned plain report is the CI contract."""
        write_fake_run(tmp_path)
        captured, fake_run = self._capture_app()
        runner = CliRunner()
        with patch.object(KstrlTuiApp, "run", fake_run):
            result = runner.invoke(cli, ["status", "--root", str(tmp_path)])
        assert captured == []
        assert "No manifest found" in result.output
