"""Stage 3 PR E (TUI rewrite): component detail screen + checkpoint modal."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

from kstrl.agents.base import UsageTotals
from kstrl.findings import Finding
from kstrl.interaction import CheckpointContext, PromptKind, PromptRequest
from kstrl.manifest import COMPONENT_STATUS_VALUES
from kstrl.reducer import ComponentState
from kstrl.tui.app import KstrlTuiApp, Mode
from kstrl.tui.screens.checkpoint import CheckpointModal
from kstrl.tui.screens.component import ComponentScreen
from kstrl.tui.screens.overview import OverviewScreen
from kstrl.tui.theme import STATUS_GLYPHS, status_glyph
from kstrl.tui.widgets.component_table import ComponentTable
from kstrl.tui.widgets.findings_table import FindingsTable
from kstrl.tui.widgets.header import RunHeader
from kstrl.tui.widgets.phase_timeline import render_timeline
from kstrl.tui.widgets.transcript import TranscriptTail
from tests.helpers.fake_run import FakeRunSpec, write_fake_run
from tests.helpers.settle import mounted, settled


def _app(root: Path, run_dir: Path) -> KstrlTuiApp:
    return KstrlTuiApp(
        run_dir=run_dir,
        root_dir=root,
        mode=Mode.DASH,
        poll_interval=0.05,
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
            review_findings=(
                Finding(
                    phase="review",
                    category="test_quality",
                    severity="advisory",
                    location="src/x.py:10",
                    explanation="weak assertion",
                ),
            ),
            security_findings=(),
            usage=_usage_totals(),
            branch="kstrl/factory/comp-a",
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
        self,
        tmp_path: Path,
    ) -> None:
        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=2))
        app = _app(tmp_path, run_dir)
        async with app.run_test(size=(120, 40)) as pilot:
            # "enter" selects the CURSOR ROW, so the board having a row
            # to select is the precondition, not a pause count.
            table = await mounted(pilot, lambda: app.screen, ComponentTable)
            await settled(
                pilot,
                lambda: table.row_count,
                what="the board to list the fixture's components",
            )
            board = app.screen
            assert isinstance(board, OverviewScreen)
            await pilot.press("enter")  # select the cursor row
            # Weaker than the assertion below on purpose: any screen
            # change satisfies it, so pushing the WRONG screen still
            # fails on the isinstance with its own message.
            await settled(
                pilot,
                lambda: app.screen is not board,
                what="enter to open a screen over the board",
            )
            assert isinstance(app.screen, ComponentScreen)
            assert app.screen.component_id == "comp-a"
            await pilot.press("escape")
            await settled(
                pilot,
                lambda: not isinstance(app.screen, ComponentScreen),
                what="escape to leave the detail screen",
            )
            assert isinstance(app.screen, OverviewScreen)

    async def test_detail_shows_timeline_findings_transcript(
        self,
        tmp_path: Path,
    ) -> None:
        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=1))
        app = _app(tmp_path, run_dir)
        async with app.run_test(size=(120, 40)) as pilot:
            await settled(
                pilot,
                lambda: "comp-a" in app.store.state.components,
                what="the first poll to fold the fixture's events into the store",
            )
            app.open_component("comp-a")
            findings = await mounted(pilot, lambda: app.screen, FindingsTable)
            transcript = await mounted(pilot, lambda: app.screen, TranscriptTail)
            header = await mounted(pilot, lambda: app.screen, "#component-header")
            # `refresh_state` fills the header and the findings table in
            # one pass, so the header carrying text is the weaker half:
            # a refresh that built the WRONG number of rows still
            # reaches the assertion below. The transcript half has no
            # such upstream signal - lines arriving IS the claim, and it
            # is what the 0.2s of polling used to guess at - so read a
            # timeout there as that assertion's own message.
            await settled(
                pilot,
                lambda: str(header.content) and transcript.lines,
                what="the detail screen's first refresh and its first transcript poll",
            )
            screen = app.screen
            assert isinstance(screen, ComponentScreen)
            assert findings.row_count == 1  # fixture's advisory finding
            assert len(transcript.lines) > 0  # engineer.log tailed
            timeline = render_timeline(
                app.store.state.components["comp-a"],
            ).plain
            assert "engineer ✓" in timeline
            assert "review ✓" in timeline

    async def test_follow_toggle(self, tmp_path: Path) -> None:
        """The waits here are on the SCREEN's record of the follow state,
        which `action_toggle_follow` sets from `toggle_follow()`'s return
        value. That is weaker than the assertions, which read the
        widget's own flag: a toggle that reports a new value without
        storing it settles the wait and fails the assertion."""
        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=1))
        app = _app(tmp_path, run_dir)
        async with app.run_test(size=(120, 40)) as pilot:
            app.open_component("comp-a")
            tail = await mounted(pilot, lambda: app.screen, TranscriptTail)
            screen = app.screen
            assert isinstance(screen, ComponentScreen)
            assert tail.follow is True
            await pilot.press("f")
            await settled(
                pilot,
                lambda: screen._following is False,
                what="'f' to record the transcript as paused",
            )
            assert tail.follow is False
            await pilot.press("f")
            await settled(
                pilot,
                lambda: screen._following is True,
                what="'f' to record the transcript as following again",
            )
            assert tail.follow is True

    async def test_poll_during_screen_mount_is_safe(self, tmp_path: Path) -> None:
        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=1))
        app = _app(tmp_path, run_dir)
        async with app.run_test(size=(120, 40)) as pilot:
            await settled(
                pilot,
                lambda: "comp-a" in app.store.state.components,
                what="the first poll to fold the fixture's events into the store",
            )
            app.open_component("comp-a")

            app._poll()  # deliberately before compose has run

            # `push_screen` puts the screen on the stack synchronously,
            # so waiting for `app.screen` to change would settle
            # instantly and prove nothing. What the interleaved poll
            # could break is the MOUNT, so that is what is waited on.
            await mounted(pilot, lambda: app.screen, TranscriptTail)
            assert isinstance(app.screen, ComponentScreen)

    async def test_findings_rollover_rebuilds_same_length_table(
        self,
        tmp_path: Path,
    ) -> None:
        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=1))
        app = _app(tmp_path, run_dir)
        async with app.run_test(size=(120, 40)) as pilot:
            await settled(
                pilot,
                lambda: "comp-a" in app.store.state.components,
                what="the first poll to fold the fixture's events into the store",
            )
            app.open_component("comp-a")
            # Everything after this is synchronous (`refresh_state` is a
            # direct call), so the table being mounted is the only wait
            # the test needs.
            table = await mounted(pilot, lambda: app.screen, FindingsTable)
            screen = app.screen
            assert isinstance(screen, ComponentScreen)
            comp = app.store.state.components["comp-a"]
            comp.recent_findings = [
                {"phase": "review", "severity": "low", "location": str(i)} for i in range(3)
            ]
            screen.refresh_state(app.store.state, None)
            assert table.row_count == 3
            comp.recent_findings = [
                {"phase": "review", "severity": "low", "location": str(i)} for i in range(1, 4)
            ]
            screen.refresh_state(app.store.state, None)

            assert table.row_count == 3
            assert str(table.get_row_at(2)[3]) == "3"


class TestOverviewScreenTeardown:
    async def test_state_update_after_header_removed_is_safe(
        self,
        tmp_path: Path,
    ) -> None:
        # Regression: a late StateChanged or age-tick can arrive while the
        # screen is tearing down and RunHeader (composed first) has already
        # been removed, even though `ready` (which checks the feed, composed
        # last) still passes. Both entry points must drop the update, not
        # raise textual.css.query.NoMatches.
        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=1))
        app = _app(tmp_path, run_dir)
        async with app.run_test(size=(120, 40)) as pilot:
            header = await mounted(pilot, lambda: app.screen, RunHeader)
            screen = app.screen
            assert isinstance(screen, OverviewScreen)
            await header.remove()
            # `remove` is awaited, but the state this test sets up is
            # "the header is gone", so that is what is waited on rather
            # than trusting one await to have finished the job.
            await settled(
                pilot,
                lambda: not screen.query(RunHeader),
                what="the run header to leave the screen",
            )

            screen.refresh_state(app.store.state)
            screen.tick_ages(app.store.state)


class TestCheckpointModal:
    async def test_renders_context_and_approves(self, tmp_path: Path) -> None:
        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=1))
        app = _app(tmp_path, run_dir)
        results: list[int | None] = []
        async with app.run_test(size=(120, 40)) as pilot:
            app.push_screen(CheckpointModal(_checkpoint_request()), results.append)
            body = await mounted(pilot, lambda: app.screen, "#checkpoint-body")
            summary_widget = await mounted(pilot, lambda: app.screen, "#checkpoint-summary")
            assert isinstance(app.screen, CheckpointModal)
            # The inspection surface is populated:
            rendered = "".join(str(static.render()) for static in body.query("Static"))
            assert "weak assertion" in rendered
            assert "+added line" in rendered
            summary = str(summary_widget.render())
            assert "kstrl/factory/comp-a" in summary
            assert "4,321+" in summary  # lower-bound marker (unreported)
            await pilot.press("a")
            # Weaker than the assertion: ANY reported choice settles it,
            # so approving with the wrong index fails on the assert.
            await settled(
                pilot,
                lambda: results,
                what="the approval to dismiss the modal and report a choice",
            )
        assert results == [0]

    async def test_reject_retry_and_escape(self, tmp_path: Path) -> None:
        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=1))
        app = _app(tmp_path, run_dir)
        results: list[int | None] = []
        async with app.run_test(size=(120, 40)) as pilot:
            for key in ("r", "t", "escape"):
                app.push_screen(
                    CheckpointModal(_checkpoint_request()),
                    results.append,
                )
                # The key has to reach the modal, so the modal being
                # mounted is the precondition for the press.
                await mounted(pilot, lambda: app.screen, "#checkpoint-body")
                await pilot.press(key)
                await settled(
                    pilot,
                    lambda: not isinstance(app.screen, CheckpointModal),
                    what=f"{key!r} to dismiss the modal",
                )
            # The dismissal hands the result back through the app's
            # queue, one hop behind the screen going away. Waiting for
            # the count is weaker than the values asserted below.
            await settled(
                pilot,
                lambda: len(results) == 3,
                what="all three dismissals to report a result",
            )
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
            component_id="comp-a",
            status="running",
            phase="engineer",
            attempt=2,
            phase_history=[
                {
                    "phase": "engineer",
                    "passed": False,
                    "duration_seconds": 1.0,
                    "attempt": 1,
                }
            ],
        )

        timeline = render_timeline(comp).plain

        assert "engineer ✗" in timeline
        # The retried phase renders as the live amber chip (● marker).
        assert "engineer ●" in timeline


class TestStatusGlyphCoverage:
    """#263 follow-on: three enumerations of the legal statuses now exist.

    ``ComponentStatus`` is the enum, ``COMPONENT_STATUS_VALUES`` is what
    the manifest validator and the CLI plan check against, and
    ``STATUS_GLYPHS`` is the TUI's hand-written table. A status added to
    the enum but not to the table would render as the ``?`` fallback -
    the glyph reserved for a status nothing recognises.
    """

    def test_glyph_table_covers_exactly_the_enum(self) -> None:
        assert set(STATUS_GLYPHS) == set(COMPONENT_STATUS_VALUES)

    def test_unknown_status_falls_back_to_question_mark(self) -> None:
        assert status_glyph("PENDING")[0] == "?"
