"""Stage 3 PR E (TUI rewrite): component detail screen + checkpoint modal."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

from ralph_py.agents.base import UsageTotals
from ralph_py.findings import Finding
from ralph_py.interaction import CheckpointContext, PromptKind, PromptRequest
from ralph_py.reducer import ComponentState
from ralph_py.tui.app import Mode, RalphTuiApp
from ralph_py.tui.screens.checkpoint import CheckpointModal
from ralph_py.tui.screens.component import ComponentScreen
from ralph_py.tui.screens.overview import OverviewScreen
from ralph_py.tui.widgets.findings_table import FindingsTable
from ralph_py.tui.widgets.phase_timeline import render_timeline
from ralph_py.tui.widgets.transcript import TranscriptTail
from tests.helpers.fake_run import FakeRunSpec, write_fake_run


def _app(root: Path, run_dir: Path) -> RalphTuiApp:
    return RalphTuiApp(
        run_dir=run_dir, root_dir=root, mode=Mode.DASH, poll_interval=0.05,
    )


def _checkpoint_request() -> PromptRequest:
    return PromptRequest(
        kind=PromptKind.CHECKPOINT,
        header="Approve PR creation and merge for comp-a?",
        options=("Approve", "Reject", "Retry"),
        default=0,
        component_id="comp-a",
        checkpoint=CheckpointContext(
            component_id="comp-a",
            diff_excerpt="+added line\n-removed line\n context\n",
            review_findings=(Finding(
                phase="review", category="test_quality", severity="advisory",
                location="src/x.py:10", explanation="weak assertion",
            ),),
            security_findings=(),
            usage=_usage_totals(),
            branch="ralph/factory/comp-a",
        ),
    )


def _usage_totals() -> UsageTotals:
    totals = UsageTotals()
    totals.calls = 3
    totals.known_calls = 2
    totals.total_tokens = 4321
    totals.cost_usd = 1.25
    return totals
class TestComponentScreen:
    async def test_enter_opens_detail_and_escape_returns(
        self, tmp_path: Path,
    ) -> None:
        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=2))
        app = _app(tmp_path, run_dir)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)
            await pilot.press("enter")  # select the cursor row
            await pilot.pause()
            assert isinstance(app.screen, ComponentScreen)
            assert app.screen.component_id == "comp-a"
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)

    async def test_detail_shows_timeline_findings_transcript(
        self, tmp_path: Path,
    ) -> None:
        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=1))
        app = _app(tmp_path, run_dir)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.open_component("comp-a")
            await pilot.pause(0.2)  # a couple of polls for the transcript
            screen = app.screen
            assert isinstance(screen, ComponentScreen)
            findings = screen.query_one(FindingsTable)
            assert findings.row_count == 1  # fixture's advisory finding
            transcript = screen.query_one(TranscriptTail)
            assert len(transcript.lines) > 0  # engineer.log tailed
            timeline = render_timeline(
                app.store.state.components["comp-a"],
            ).plain
            assert "engineer pass" in timeline
            assert "review pass" in timeline

    async def test_follow_toggle(self, tmp_path: Path) -> None:
        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=1))
        app = _app(tmp_path, run_dir)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.open_component("comp-a")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, ComponentScreen)
            tail = screen.query_one(TranscriptTail)
            assert tail.follow is True
            await pilot.press("f")
            assert tail.follow is False
            await pilot.press("f")
            assert tail.follow is True

    async def test_poll_during_screen_mount_is_safe(self, tmp_path: Path) -> None:
        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=1))
        app = _app(tmp_path, run_dir)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.open_component("comp-a")

            app._poll()

            await pilot.pause()
            assert isinstance(app.screen, ComponentScreen)

    async def test_findings_rollover_rebuilds_same_length_table(
        self, tmp_path: Path,
    ) -> None:
        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=1))
        app = _app(tmp_path, run_dir)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.open_component("comp-a")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, ComponentScreen)
            comp = app.store.state.components["comp-a"]
            comp.recent_findings = [
                {"phase": "review", "severity": "low", "location": str(i)}
                for i in range(3)
            ]
            screen.refresh_state(app.store.state, None)
            table = screen.query_one(FindingsTable)
            assert table.row_count == 3
            comp.recent_findings = [
                {"phase": "review", "severity": "low", "location": str(i)}
                for i in range(1, 4)
            ]
            screen.refresh_state(app.store.state, None)

            assert table.row_count == 3
            assert str(table.get_row_at(2)[3]) == "3"


class TestCheckpointModal:
    async def test_renders_context_and_approves(self, tmp_path: Path) -> None:
        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=1))
        app = _app(tmp_path, run_dir)
        results: list[int | None] = []
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.push_screen(CheckpointModal(_checkpoint_request()), results.append)
            await pilot.pause()
            assert isinstance(app.screen, CheckpointModal)
            # The inspection surface is populated:
            body = app.screen.query_one("#checkpoint-body")
            rendered = "".join(
                str(static.render()) for static in body.query("Static")
            )
            assert "weak assertion" in rendered
            assert "+added line" in rendered
            summary = str(
                app.screen.query_one("#checkpoint-summary").render(),
            )
            assert "ralph/factory/comp-a" in summary
            assert "4321+" in summary  # lower-bound marker (unreported)
            await pilot.press("a")
            await pilot.pause()
        assert results == [0]

    async def test_reject_retry_and_escape(self, tmp_path: Path) -> None:
        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=1))
        app = _app(tmp_path, run_dir)
        results: list[int | None] = []
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            for key in ("r", "t", "escape"):
                app.push_screen(
                    CheckpointModal(_checkpoint_request()), results.append,
                )
                await pilot.pause()
                await pilot.press(key)
                await pilot.pause()
        assert results == [1, 2, None]

    def test_unknown_button_does_not_default_to_approval(self) -> None:
        modal = CheckpointModal(_checkpoint_request())
        modal.dismiss = Mock()  # type: ignore[method-assign]
        event = Mock()
        event.button.id = "unexpected"

        modal.on_button_pressed(event)

        modal.dismiss.assert_not_called()


class TestPhaseTimeline:
    def test_retry_of_completed_phase_is_still_shown_running(self) -> None:
        comp = ComponentState(
            component_id="comp-a", status="running", phase="engineer",
            attempt=2, phase_history=[{
                "phase": "engineer", "passed": False,
                "duration_seconds": 1.0, "attempt": 1,
            }],
        )

        timeline = render_timeline(comp).plain

        assert "engineer fail" in timeline
        assert "engineer ..." in timeline
