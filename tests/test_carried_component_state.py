"""A run's event stream says what state it carried each component in (#448).

A ``ks retry cli`` run schedules one component. The other components
were completed or failed by earlier runs, and the only event this run
writes for them is ``component_scope_resolved``. Before #448 the reducer
had no case for that event and ``ComponentState.status`` defaults to
``pending``, so ``ks dash`` showed three merged components as never
started under a finished run while ``ks status --no-tui`` (which reads
the manifest) said all four were completed.

The fix records the manifest status on the scope event the run already
emits, and the reducer folds it. These tests drive the real
``run_factory``, then read the run back through the surfaces that
report it: the reducer, the plain ``ks status --no-tui`` report (the
control, which reads the manifest), the real dashboard app, and the
home screen's per-run row, which must keep counting only what the run
did.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from rich.text import Text

from kstrl import events as ev
from kstrl.manifest import Component, ComponentStatus
from kstrl.reducer import fold, load_run_state
from kstrl.tui.app import KstrlTuiApp, Mode
from kstrl.tui.home_data import summarize_state
from kstrl.tui.runs import RunRef
from kstrl.tui.widgets.component_table import ComponentTable
from tests.helpers.settle import drained, mounted, settled
from tests.test_harness_path_scope import COMPONENT_ID, _component, _manifest, _setup_project
from tests.test_scope_snapshot import _run, _Seams

DONE_EARLIER = "done-earlier"
FAILED_EARLIER = "failed-earlier"

LEGACY_FIXTURE = Path(__file__).parent / "fixtures" / "reducer" / "legacy_carried_run"


def _carried(component_id: str, status: ComponentStatus) -> Component:
    """A component an earlier run left in ``status``; this run will not schedule it."""
    comp = Component(
        component_id,
        component_id,
        "finished by an earlier run",
        [],
        f"scripts/kstrl/feature/{component_id}/prd.json",
        f"kstrl/factory/{component_id}",
    )
    comp.status = status.value
    return comp


def _run_with_carried_components(root: Path) -> Path:
    """One real factory run over a manifest holding two carried components.

    Returns the run's ``events.jsonl``. ``document-format`` is the one
    component this run schedules and completes.
    """
    _setup_project(root)
    manifest = _manifest(
        [
            _carried(DONE_EARLIER, ComponentStatus.COMPLETED),
            _carried(FAILED_EARLIER, ComponentStatus.FAILED),
            _component(),
        ]
    )
    manifest.save(root / "scripts" / "kstrl" / "manifest.json")
    _run(root, _Seams(), manifest=manifest)
    _state, source = load_run_state(root)
    assert source is not None and source.name == "events.jsonl", source
    return source


def _cell_text(value: object) -> str:
    return value.plain if isinstance(value, Text) else str(value)


class TestTheRunRecordsWhatItCarried:
    def test_the_reducer_reports_carried_components_as_the_manifest_does(
        self,
        tmp_path: Path,
    ) -> None:
        _run_with_carried_components(tmp_path)
        state, _source = load_run_state(tmp_path)

        assert state.finished
        assert state.components[DONE_EARLIER].status == "completed"
        assert state.components[FAILED_EARLIER].status == "failed"
        assert state.components[COMPONENT_ID].status == "completed"

    def test_the_plain_status_report_agrees(self, tmp_path: Path) -> None:
        """The control: ``ks status --no-tui`` reads the manifest, and
        was already right. The reducer test above is only evidence if
        the two surfaces are reading the same run."""
        _run_with_carried_components(tmp_path)
        proc = subprocess.run(
            [sys.executable, "-m", "kstrl", "status", "--no-tui", "--root", str(tmp_path)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        # The plain UI writes its report to stderr under some
        # environments (the suite's conftest among them), so both
        # streams are the report.
        report = proc.stdout + proc.stderr
        assert proc.returncode == 0, report
        assert f"{DONE_EARLIER}: completed" in report
        assert f"{FAILED_EARLIER}: failed" in report
        assert f"{COMPONENT_ID}: completed" in report

    def test_the_scope_event_carries_the_manifest_status(self, tmp_path: Path) -> None:
        """On disk, not only after a fold: ``events.jsonl`` is the
        integration substrate, and a consumer that is not the reducer
        reads the raw line."""
        events_file = _run_with_carried_components(tmp_path)
        recorded = {
            obj["component"]: obj["data"]["manifest_status"]
            for obj in (
                json.loads(line) for line in events_file.read_text(encoding="utf-8").splitlines()
            )
            if obj["event"] == "component_scope_resolved"
        }
        assert recorded == {
            DONE_EARLIER: "completed",
            FAILED_EARLIER: "failed",
            COMPONENT_ID: "pending",
        }

    async def test_the_dashboard_renders_carried_components(self, tmp_path: Path) -> None:
        events_file = _run_with_carried_components(tmp_path)
        app = KstrlTuiApp(
            run_dir=events_file.parent,
            root_dir=tmp_path,
            mode=Mode.DASH,
            poll_interval=0.05,
        )
        async with app.run_test(size=(140, 40)) as pilot:
            table = await mounted(pilot, lambda: app.screen, ComponentTable)
            await settled(
                pilot,
                lambda: table.row_count == 3,
                what="the component table to render the run's three rows",
            )
            await drained(pilot, app.screen, what="the folded state to reach the table")
            assert _cell_text(table.get_row(DONE_EARLIER)[2]) == "completed"
            assert _cell_text(table.get_row(FAILED_EARLIER)[2]) == "failed"
            assert _cell_text(table.get_row(COMPONENT_ID)[2]) == "completed"

    def test_the_run_list_counts_only_what_the_run_did(self, tmp_path: Path) -> None:
        """The home screen's run row is a per-run count, like the
        factory's own summary: this run completed one component and
        failed none. The carried failure stays on the board and out of
        the row, or a successful retry would be listed as a failed run."""
        events_file = _run_with_carried_components(tmp_path)
        state, _source = load_run_state(tmp_path)
        ref = RunRef(
            run_id=state.run_id,
            run_dir=events_file.parent,
            events_path=events_file,
            mtime=0.0,
            completed=True,
        )

        summary = summarize_state(ref, state)

        assert (summary.outcome, summary.components_done, summary.components_failed) == (
            "done",
            1,
            0,
        )
        assert summary.components_total == 3


class TestRunStartStatusFold:
    """The fold of one scope event, per recorded status."""

    @staticmethod
    def _status(recorded: str) -> str:
        state = fold(
            [ev.ComponentScopeResolved(component="c", manifest_status=recorded)],
        )
        return state.components["c"].status

    def test_a_terminal_status_is_carried(self) -> None:
        assert self._status("completed") == "completed"
        assert self._status("failed") == "failed"
        assert self._status("skipped") == "skipped"
        assert self._status("merge_pending") == "merge_pending"

    def test_pending_stays_pending(self) -> None:
        assert self._status("pending") == "pending"

    def test_a_crash_leftover_is_pending(self) -> None:
        """The factory resets RUNNING and VERIFYING to PENDING before it
        schedules anything, after the scope record is written."""
        assert self._status("running") == "pending"
        assert self._status("verifying") == "pending"

    def test_an_unrecorded_or_unrecognised_status_is_unknown(self) -> None:
        """Empty is a log written before #448. Off-vocabulary is a value
        the reducer cannot parse. Neither is evidence the component has
        not started, so neither may read as pending."""
        assert self._status("") == "unknown"
        assert self._status("COMPLETED") == "unknown"

    def test_later_events_in_the_run_win(self) -> None:
        state = fold(
            [
                ev.ComponentScopeResolved(component="c", manifest_status="merge_pending"),
                ev.ComponentCompleted(component="c", iterations=1),
            ],
        )
        assert state.components["c"].status == "completed"


class TestALogWrittenBeforeTheFix:
    """The recorded #432 run ``d658d2``, trimmed to the lines that matter:
    four scope events without ``manifest_status``, and ``cli`` started
    and completed. Three components were completed by earlier runs, and
    this log does not say so."""

    def test_carried_components_are_unknown_not_pending(self, tmp_path: Path) -> None:
        run_dir = tmp_path / ".kstrl" / "runs" / "factory-20260922-063652.559123-d658d2"
        run_dir.mkdir(parents=True)
        shutil.copyfile(LEGACY_FIXTURE / "events.jsonl", run_dir / "events.jsonl")

        state, _source = load_run_state(tmp_path)

        assert state.finished
        assert {cid: state.components[cid].status for cid in state.plan_order} == {
            "link-rules": "unknown",
            "storage": "unknown",
            "http-api": "unknown",
            "cli": "completed",
        }


class TestARunThatFinishesACarriedComponent:
    """A component can start a run carried and be finished by the run
    without a ``component_started``: ``pipeline.repoll_merge_pending``
    re-polls a MERGE_PENDING component before scheduling and emits
    ``component_completed`` when its PR has merged. The run completed
    it, so the run row counts it."""

    def test_a_merge_the_run_confirms_counts_as_done(self, tmp_path: Path) -> None:
        state = fold(
            [
                ev.ComponentScopeResolved(component="c", manifest_status="merge_pending"),
                ev.ComponentCompleted(component="c", iterations=1),
                ev.RunCompleted(),
            ],
            run_id="run-c",
        )
        ref = RunRef(
            run_id="run-c",
            run_dir=tmp_path,
            events_path=tmp_path / "events.jsonl",
            mtime=0.0,
            completed=True,
        )

        summary = summarize_state(ref, state)

        assert (summary.outcome, summary.components_done, summary.components_failed) == (
            "done",
            1,
            0,
        )
