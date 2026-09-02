"""TUI surface C5: decompose screens, kind dispatch, status --tui."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

from click.testing import CliRunner
from rich.text import Text
from textual.coordinate import Coordinate
from textual.widgets import DataTable

from kstrl.agents.base import ARCHITECT_COMPONENT
from kstrl.cli import cli
from kstrl.reducer import ComponentState, RunState
from kstrl.tui.app import KstrlTuiApp, Mode
from kstrl.tui.dispatch import initial_screens_for_kind
from kstrl.tui.screens.component import ComponentScreen
from kstrl.tui.screens.decompose import DecomposeScreen, SpecTriageScreen
from kstrl.tui.screens.overview import OverviewScreen
from kstrl.tui.state import architect_component_id
from kstrl.tui.widgets.dag_table import DagTable, compute_tiers
from kstrl.tui.widgets.header import RunHeader
from tests.helpers.fake_run import (
    write_fake_decompose_run,
    write_fake_run,
)
from tests.helpers.settle import mounted, settled


class TestComputeTiers:
    def test_chain_and_diamond(self) -> None:
        assert compute_tiers(
            {
                "a": (),
                "b": ("a",),
                "c": ("a",),
                "d": ("b", "c"),
            }
        ) == {"a": 0, "b": 1, "c": 1, "d": 2}

    def test_unknown_deps_ignored(self) -> None:
        assert compute_tiers({"a": ("ghost",), "b": ("a",)}) == {
            "a": 0,
            "b": 1,
        }

    def test_cycle_marks_members_not_raises(self) -> None:
        tiers = compute_tiers({"a": ("b",), "b": ("a",), "c": ()})
        assert tiers["c"] == 0
        assert tiers["a"] == -1 and tiers["b"] == -1

    def test_self_dependency_is_a_cycle(self) -> None:
        assert compute_tiers({"a": ("a",)}) == {"a": -1}


class TestArchitectComponentId:
    """#296 review: the read side of #281, for run dirs already on disk.

    The fallback exists because this seam's failure was WRONG rather than
    conservative. These pin the two conditions that stop it becoming the
    collision it was built to remove.
    """

    @staticmethod
    def _state(run_id: str, *component_ids: str) -> RunState:
        state = RunState(run_id=run_id)
        for cid in component_ids:
            state.components[cid] = ComponentState(component_id=cid)
        return state

    def test_a_pre_281_decompose_dir_resolves_to_the_bare_key(self) -> None:
        state = self._state("decompose-20260101-120000.000000-old", "architect", "api")
        assert architect_component_id(state) == "architect"

    def test_a_post_281_decompose_dir_resolves_to_the_namespaced_key(self) -> None:
        state = self._state("decompose-20260901-120000.000000-new", ARCHITECT_COMPONENT, "api")
        assert architect_component_id(state) == ARCHITECT_COMPONENT

    def test_a_new_dir_with_a_component_named_architect_never_falls_back(self) -> None:
        """Condition one. `ks decompose` writes the architect's RunPlan
        entry before the spec reaches an LLM, and the reducer creates a
        row from RunPlan - so a post-#281 dir always answers on the first
        branch, and the LLM's own `architect` is never consulted."""
        state = self._state(
            "decompose-20260901-120000.000000-new",
            ARCHITECT_COMPONENT,
            "architect",
        )
        assert architect_component_id(state) == ARCHITECT_COMPONENT

    def test_a_factory_run_never_falls_back(self) -> None:
        """Condition two, and the one that does not depend on emit order.

        A factory run's architect row is absent whenever the architect
        never reported - a resume, or an adapter that reports nothing -
        and its manifest may hold a component named `architect`. That is
        exactly the ambiguity `serve.read_run_spend` refuses to guess at,
        so the fallback is restricted to decompose runs and this stays
        pessimistic.
        """
        state = self._state("factory-20260101-120000.000000-run", "architect", "api")
        assert architect_component_id(state) == ARCHITECT_COMPONENT


class TestDispatch:
    def test_kinds_map_to_stacks(self) -> None:
        decompose = initial_screens_for_kind("decompose", observe_only=True)()
        assert [type(s) for s in decompose] == [OverviewScreen, DecomposeScreen]
        understand = initial_screens_for_kind("understand", observe_only=True)()
        assert isinstance(understand[-1], ComponentScreen)
        assert understand[-1].component_id == "understand"
        feature = initial_screens_for_kind(
            "feature",
            observe_only=False,
            component="demo",
        )()
        assert isinstance(feature[-1], ComponentScreen)
        assert feature[-1].component_id == "demo"
        factory = initial_screens_for_kind("factory", observe_only=True)()
        assert [type(s) for s in factory] == [OverviewScreen]
        unknown = initial_screens_for_kind("someday", observe_only=True)()
        assert [type(s) for s in unknown] == [OverviewScreen]


def _decompose_app(root: Path, run_dir: Path) -> KstrlTuiApp:
    return KstrlTuiApp(
        run_dir=run_dir,
        root_dir=root,
        mode=Mode.DASH,
        poll_interval=0.05,
        screen_factory=initial_screens_for_kind(
            "decompose",
            observe_only=True,
        ),
    )


class TestDecomposeScreen:
    async def test_success_run_renders_dag_and_summary(
        self,
        tmp_path: Path,
    ) -> None:
        run_dir = write_fake_decompose_run(tmp_path, attempts=2)
        app = _decompose_app(tmp_path, run_dir)
        async with app.run_test(size=(120, 40)) as pilot:
            table = await mounted(pilot, lambda: app.screen, DagTable)
            # One `refresh_state` paints the DAG, both strips and the
            # summary, so rows in the table are evidence the whole pass
            # ran and every read below is settled. This one condition
            # does coincide with the row assertion just under it: an
            # empty table then reports itself as "the DAG table never
            # rendered the plan's rows", which is the better message.
            await settled(
                pilot,
                lambda: table.rows,
                what="the DAG table to render the plan's rows",
            )
            assert isinstance(app.screen, DecomposeScreen)
            assert list(table.rows) != []
            row_keys = {key.value for key in table.rows}
            assert row_keys == {"database", "api"}  # architect excluded
            strip = app.screen.query_one("#issues-strip")
            assert "minor" in str(strip.content)
            attempt = app.screen.query_one("#attempt-strip")
            assert "attempt 2" in str(attempt.content)
            summary = app.screen.query_one("#decompose-summary")
            assert summary.display
            assert "2 component(s)" in str(summary.content)

            # A replaced event stream may contain a smaller plan; rows
            # from the old fold must not survive the rebuild.
            app.store.state.plan_order = [ARCHITECT_COMPONENT, "database"]
            app.store.state.components.pop("api")
            table.update_state(app.store.state)
            assert {key.value for key in table.rows} == {"database"}

    async def test_a_component_named_architect_is_not_filtered_out_of_the_dag(
        self,
        tmp_path: Path,
    ) -> None:
        """#281 in the TUI. ``DagTable`` hides the architect's pseudo
        row by comparing plan ids against the role's key, so while that
        key was the bare word a component the architect genuinely NAMED
        `architect` was excluded from the DAG view - present in the plan,
        holding real dependencies, and invisible.

        Asserted as membership rather than against the constant: with the
        namespace collapsed both spellings are the same word, and any
        assertion phrased in constants would be satisfied by the row
        being absent.
        """
        run_dir = write_fake_decompose_run(tmp_path, components=("architect", "api"))
        app = _decompose_app(tmp_path, run_dir)
        async with app.run_test(size=(120, 40)) as pilot:
            table = await mounted(pilot, lambda: app.screen, DagTable)
            # Weaker than the assertion on purpose: with the defect back
            # the table still has the `api` row, so the wait ends and
            # the missing `architect` row fails below rather than here.
            await settled(
                pilot,
                lambda: table.rows,
                what="the DAG table to render the plan's rows",
            )
            assert {key.value for key in table.rows} == {"architect", "api"}

    async def test_a_pre_281_run_dir_still_renders_as_the_run_it_was(
        self,
        tmp_path: Path,
    ) -> None:
        """#296 review, MEDIUM. Run directories are the durable record.

        Every decompose dir written before #281 keys the architect by the
        bare word, and reading only the new key made this screen report a
        COMPLETED run as failed - a wrong answer, not a conservative one,
        which is why this seam takes a fallback where
        ``serve.read_run_spend`` refuses one.

        Asserts all four symptoms the review named, off one old dir: the
        summary, the attempt strip, the audit-ran branch of the issue
        strip, and the stale pseudo-row leaking into the graph.
        """
        run_dir = write_fake_decompose_run(
            tmp_path,
            minors=0,
            components=("database", "api"),
            architect_key="architect",
        )
        app = _decompose_app(tmp_path, run_dir)
        async with app.run_test(size=(120, 40)) as pilot:
            table = await mounted(pilot, lambda: app.screen, DagTable)
            # The DAG, both strips and the summary are painted by one
            # `refresh_state` call, so rows in the table are evidence
            # that the pass which fills the strips has run. Weaker than
            # every assertion below, none of which is about a row count.
            await settled(
                pilot,
                lambda: table.rows,
                what="the DAG table to render the plan's rows",
            )
            screen = app.screen
            assert isinstance(screen, DecomposeScreen)

            summary = str(screen.query_one("#decompose-summary").content)
            assert "did not complete" not in summary
            assert "2 component(s)" in summary

            attempt = str(screen.query_one("#attempt-strip").content)
            assert "waiting for the architect" not in attempt
            assert "completed" in attempt

            # audit_ran, which only the architect's phase_history shows.
            assert "clean audit" in str(screen.query_one("#issues-strip").content)

            # The pseudo-row is still filtered, under the key THIS dir
            # used rather than the one the constant now names.
            assert {key.value for key in table.rows} == {"database", "api"}

            # The transcript tails the key the dir actually wrote.
            assert screen.transcript_component == "architect"

    async def test_a_pre_281_halted_run_still_shows_its_blocker_banner(
        self,
        tmp_path: Path,
    ) -> None:
        """``SpecTriageScreen._refresh`` computes ``halted`` from the
        architect's status, so on an old dir it read False for a run that
        did halt and hid the banner pointing at the spec-issues
        artifact."""
        run_dir = write_fake_decompose_run(
            tmp_path,
            blockers=1,
            minors=1,
            architect_key="architect",
        )
        app = _decompose_app(tmp_path, run_dir)
        async with app.run_test(size=(120, 40)) as pilot:
            # A halted run has no plan, so the DAG stays empty here and
            # the readiness proof is the screen being composed at all,
            # plus the app's own record of the issues the triage screen
            # reads on mount. Pressing `i` before that fold lands would
            # open a triage screen over an empty state.
            await mounted(pilot, lambda: app.screen, DagTable)
            await settled(
                pilot,
                lambda: app.store.state.spec_issues,
                what="the run's spec issues to fold out of the event stream",
            )
            await pilot.press("i")
            # By id, not by class: `DagTable` IS a `DataTable`, so a
            # class selector matches the decompose screen's own table
            # while the triage screen is still being pushed, and the
            # wait would then be satisfied by the wrong widget.
            issues = await mounted(pilot, lambda: app.screen, "#triage-table")
            # `_refresh` decides the banner and then fills the table, so
            # a row is evidence the decision has been made. Weaker than
            # the decision itself, which the assertions below own.
            await settled(
                pilot,
                lambda: issues.row_count,
                what="the triage table to render the run's spec issues",
            )
            triage = app.screen
            assert isinstance(triage, SpecTriageScreen)
            banner = triage.query_one("#triage-banner")
            assert banner.display, "an old halted run must still say it halted"
            assert "halted" in str(banner.content)

    async def test_state_update_after_header_removed_is_safe(
        self,
        tmp_path: Path,
    ) -> None:
        # Regression: a late StateChanged or age-tick during teardown finds
        # RunHeader (composed first) already removed while `ready` (checks
        # the transcript, composed last) still passes. Must drop, not raise.
        run_dir = write_fake_decompose_run(tmp_path, attempts=1)
        app = _decompose_app(tmp_path, run_dir)
        async with app.run_test(size=(120, 40)) as pilot:
            table = await mounted(pilot, lambda: app.screen, DagTable)
            # Rows prove the screen has already served one refresh, so
            # `ready` was True with the header still present - which is
            # the state this test then breaks.
            await settled(
                pilot,
                lambda: table.rows,
                what="the DAG table to render the plan's rows",
            )
            screen = app.screen
            assert isinstance(screen, DecomposeScreen)
            await screen.query_one(RunHeader).remove()
            await settled(
                pilot,
                lambda: not screen.query(RunHeader),
                what="the run header to leave the screen",
            )

            screen.refresh_state(app.store.state, None)
            screen.tick_ages(app.store.state)

    async def test_triage_shows_blocker_banner_and_detail(
        self,
        tmp_path: Path,
    ) -> None:
        run_dir = write_fake_decompose_run(tmp_path, blockers=1, minors=1)
        app = _decompose_app(tmp_path, run_dir)
        async with app.run_test(size=(120, 40)) as pilot:
            # As in the pre-#281 halted test: the screen has to be up to
            # take the key, and the issues have to have folded before
            # the triage screen reads them on mount.
            await mounted(pilot, lambda: app.screen, DagTable)
            await settled(
                pilot,
                lambda: app.store.state.spec_issues,
                what="the run's spec issues to fold out of the event stream",
            )
            await pilot.press("i")
            # By id: `DagTable` is a `DataTable` too, so the class
            # selector can match the screen this one is replacing.
            issues = await mounted(pilot, lambda: app.screen, "#triage-table")
            await settled(
                pilot,
                lambda: issues.row_count,
                what="the triage table to render the run's spec issues",
            )
            assert isinstance(app.screen, SpecTriageScreen)
            banner = app.screen.query_one("#triage-banner")
            assert banner.display
            assert "halted" in str(banner.content)
            # Blockers sort first; the detail pane carries the
            # suggestion for the highlighted row.
            detail = app.screen.query_one("#triage-detail")
            text = str(detail.content)
            assert "[blocker]" in text
            assert "Resolve it" in text

            # Spec text is untrusted content, not Rich markup. Keeping
            # every cell as Text prevents tags from being interpreted.
            app.store.state.spec_issues[0]["summary"] = "[/bold]"
            app.store.state.spec_issues[0]["location"] = (
                "[link=https://example.invalid]location[/link]"
            )
            app.screen._refresh(app.store.state)
            table = app.screen.query_one(DataTable)
            summary_cell = table.get_cell_at(Coordinate(0, 2))
            location_cell = table.get_cell_at(Coordinate(0, 3))
            assert isinstance(summary_cell, Text)
            assert summary_cell.plain == "[/bold]"
            assert isinstance(location_cell, Text)
            assert location_cell.plain.startswith("[link=")
            await pilot.press("escape")
            # Waiting for the triage screen to go is weaker than the
            # assertion that what is underneath is the decompose
            # screen: popping two screens satisfies this and still
            # fails below.
            await settled(
                pilot,
                lambda: not isinstance(app.screen, SpecTriageScreen),
                what="escape to pop the triage screen",
            )
            assert isinstance(app.screen, DecomposeScreen)

    async def test_escape_pops_to_overview(self, tmp_path: Path) -> None:
        run_dir = write_fake_decompose_run(tmp_path)
        app = _decompose_app(tmp_path, run_dir)
        async with app.run_test(size=(120, 40)) as pilot:
            await mounted(pilot, lambda: app.screen, DagTable)
            await pilot.press("escape")
            # Weaker than the assertion: any pop satisfies this, and
            # landing somewhere other than the overview still fails on
            # the line below.
            await settled(
                pilot,
                lambda: not isinstance(app.screen, DecomposeScreen),
                what="escape to pop the decompose screen",
            )
            assert isinstance(app.screen, OverviewScreen)


class TestStatusTui:
    def _capture_app(self) -> tuple[list[KstrlTuiApp], Any]:
        captured: list[KstrlTuiApp] = []

        def fake_run(self: KstrlTuiApp) -> int:
            captured.append(self)
            return 0

        return captured, fake_run

    def test_explicit_tui_opens_the_newest_run_with_kind_dispatch(
        self,
        tmp_path: Path,
    ) -> None:
        write_fake_run(tmp_path, run_id="factory-20260718-100000.000000-old")
        write_fake_decompose_run(tmp_path)
        captured, fake_run = self._capture_app()
        runner = CliRunner()
        with patch.object(KstrlTuiApp, "run", fake_run):
            result = runner.invoke(
                cli,
                ["status", "--root", str(tmp_path), "--tui"],
            )
        assert result.exit_code == 0
        assert len(captured) == 1
        app = captured[0]
        assert app.run_dir.name.startswith("decompose-")  # newest wins
        assert app.screen_factory is not None
        stack = app.screen_factory()
        assert isinstance(stack[-1], DecomposeScreen)

    def test_tui_with_no_runs_falls_back_to_plain_guidance(
        self,
        tmp_path: Path,
    ) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["status", "--root", str(tmp_path), "--tui"],
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
