"""#433 increment 1: what the operator TUI shows, driven through Pilot.

Each test names the finding it pins (F1-F12, E1-E3 in the #433 round-0
audit). Text-only behaviour is in ``test_tui_433_state.py``.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, cast

from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import DataTable, OptionList, Static

from kstrl.inbox import Inbox, InboxConfig, ItemKind
from kstrl.interaction import PromptKind, PromptRequest
from kstrl.manifest import Manifest
from kstrl.reducer import ComponentState, RunState
from kstrl.tui.app import KstrlTuiApp, Mode
from kstrl.tui.dispatch import initial_screens_for_kind
from kstrl.tui.screens.component import ComponentScreen
from kstrl.tui.screens.home import HomeScreen
from kstrl.tui.screens.inbox import InboxScreen
from kstrl.tui.screens.options import OptionsModal
from kstrl.tui.screens.overview import OverviewScreen
from kstrl.tui.screens.retry import RetryScreen
from kstrl.tui.widgets.activity import ActivityFeed
from kstrl.tui.widgets.component_table import ComponentTable
from kstrl.tui.widgets.header import RunHeader
from kstrl.tui.widgets.transcript import TranscriptTail
from tests.helpers.fake_run import FakeRunSpec, write_fake_decompose_run, write_fake_run
from tests.helpers.settle import drained, mounted, settled
from tests.helpers.tui_screens import evolve_on


def _home(root: Path) -> KstrlTuiApp:
    return KstrlTuiApp(root_dir=root, mode=Mode.HOME, poll_interval=0.05)


def _dash(root: Path, run_dir: Path, kind: str = "factory") -> KstrlTuiApp:
    return KstrlTuiApp(
        run_dir=run_dir,
        root_dir=root,
        mode=Mode.DASH,
        poll_interval=0.05,
        screen_factory=initial_screens_for_kind(kind, observe_only=True),
    )


def _cells_fit(table: DataTable[Any]) -> list[str]:
    """Every cell wider than its column, as ``row/column: text``."""
    cut = []
    for row_key in table.rows:
        for column, cell in zip(table.columns.values(), table.get_row(row_key), strict=True):
            width = cell.cell_len if isinstance(cell, Text) else len(str(cell))
            if width > column.content_width:
                cut.append(f"{row_key.value}/{column.key.value}: {cell}")
    return cut


class TestHome:
    async def test_run_table_numbers_are_whole_after_the_worker_lands(self, tmp_path: Path) -> None:
        """F1: a column sized for the placeholder dot cut 18.51M to 18."""
        write_fake_run(tmp_path, FakeRunSpec(components=3))
        app = _home(tmp_path)
        async with app.run_test(size=(120, 36)) as pilot:
            table = cast(DataTable[Any], await mounted(pilot, lambda: app.screen, "#home-runs"))
            screen = cast(HomeScreen, app.screen)
            # Column widths from update_width=True are applied on the
            # table's next idle, not in update_cell; settling on the
            # summaries alone read the widths before that idle ran.
            await settled(
                pilot,
                lambda: (
                    screen._summaries
                    and not table._updated_cells
                    and not table._require_update_dimensions
                ),
                what="the summaries to land and the widths to be applied",
            )
            assert _cells_fit(table) == []

    def test_every_command_key_is_one_keypress(self) -> None:
        """F2: the tenth command was bound to "10"."""
        keys = [b.key for b in HomeScreen.BINDINGS if b.action.startswith("command(")]
        assert keys == list("1234567890")

    async def test_launcher_labels_fit_their_column(self, tmp_path: Path) -> None:
        """F2: `via CLI: ks understand --tui` wrapped onto a second line."""
        app = _home(tmp_path)
        async with app.run_test(size=(120, 36)) as pilot:
            commands = cast(OptionList, await mounted(pilot, lambda: app.screen, "#home-commands"))
            await settled(pilot, lambda: commands.option_count, what="the launcher to fill")
            widths = [
                cast(Text, commands.get_option_at_index(i).prompt).cell_len
                for i in range(commands.option_count)
            ]
            assert max(widths) <= commands.content_region.width

    async def test_masthead_names_the_directory_not_the_manifest(self, tmp_path: Path) -> None:
        """F3: the newest manifest's projectName is a daemon's queue name."""
        root = tmp_path / "snippetvault"
        manifest = root / "scripts" / "kstrl" / "manifest.json"
        manifest.parent.mkdir(parents=True)
        Manifest(
            version="1",
            spec_file="spec.md",
            project_name="queue-793181",
            base_branch="main",
            single_pr=False,
        ).save(manifest)
        assert Manifest.load(manifest).project_name == "queue-793181"
        app = _home(root)
        async with app.run_test(size=(120, 36)) as pilot:
            masthead = cast(Static, await mounted(pilot, lambda: app.screen, "#home-masthead"))
            await settled(pilot, lambda: str(masthead.content), what="the masthead")
            text = str(masthead.content)
            assert "snippetvault" in text
            assert "queue-793181" not in text

    async def test_home_counts_what_waits_on_the_operator(self, tmp_path: Path) -> None:
        """E3: an open inbox item is counted on home, with its key."""
        Inbox(tmp_path, InboxConfig()).add(ItemKind.HALTED_RUN, "halted", dedupe_key="h")
        write_fake_run(tmp_path, FakeRunSpec(components=1))
        app = _home(tmp_path)
        async with app.run_test(size=(120, 36)) as pilot:
            attention = cast(Static, await mounted(pilot, lambda: app.screen, "#home-attention"))
            await settled(
                pilot,
                lambda: "inbox" in str(attention.content),
                what="the attention line to count the inbox",
            )
            assert "needs you: 1 inbox item (6)" in str(attention.content)

    async def test_at_80_columns_the_launcher_is_one_line_and_the_palette_lists_all(
        self, tmp_path: Path
    ) -> None:
        """F1/F2 at 80 columns: the launcher column took 44 of them."""
        app = _home(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            keys = cast(Static, await mounted(pilot, lambda: app.screen, "#home-keys"))
            await settled(pilot, lambda: str(keys.content), what="the key strip")
            assert str(keys.content).endswith("^p all")
            assert not app.screen.query_one("#home-commands-col").display
            titles = [command.title for command in app.get_system_commands(app.screen)]
            assert "0 understand" in titles


class TestRunBoard:
    async def test_the_header_names_the_project_directory(self, tmp_path: Path) -> None:
        """F3 on the board: the run records fake-project, the root is named."""
        root = tmp_path / "snippetvault"
        run_dir = write_fake_run(root, FakeRunSpec(components=1))
        app = _dash(root, run_dir)
        async with app.run_test(size=(120, 36)) as pilot:
            header = await mounted(pilot, lambda: app.screen, RunHeader)
            await settled(pilot, lambda: str(header.content), what="the header")
            assert "snippetvault" in str(header.content)
            assert "fake-project" not in str(header.content)

    async def test_a_stopped_run_is_unknown_not_in_flight(self, tmp_path: Path) -> None:
        """F4 on the board: `● in flight` over a run that stopped days ago."""
        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=2, complete=False))
        old = time.time() - 3 * 86400
        os.utime(run_dir / "events.jsonl", (old, old))
        app = _dash(tmp_path, run_dir)
        async with app.run_test(size=(120, 36)) as pilot:
            header = await mounted(pilot, lambda: app.screen, RunHeader)
            await settled(pilot, lambda: str(header.content), what="the header")
            text = str(header.content)
            assert "? unknown" in text
            assert "in flight" not in text and "running" not in text

    async def test_the_board_has_no_cut_cell_at_80_columns(self) -> None:
        """F1: at 80 columns the board drops columns rather than cut them."""
        state = RunState(run_id="factory-20260926-010000.000000-t433")
        for index, cid in enumerate(("integration-fix-1", "client-commands", "http-server")):
            state.plan_order.append(cid)
            state.components[cid] = ComponentState(
                component_id=cid,
                status="failed" if index == 0 else "completed",
                attempt=2,
                iteration=5,
                total_tokens=11_210_000,
                cost_usd=10.98,
                error="review: " + "the fix left IF-2 open and the reviewer said so " * 3,
            )
        state.plan_order.append("cli")
        state.components["cli"] = ComponentState(
            component_id="cli", deps=("integration-fix-1", "client-commands", "http-server")
        )

        class _Board(App[None]):
            def compose(self) -> ComposeResult:
                yield ComponentTable(id="board")

        app = _Board()
        async with app.run_test(size=(80, 24)) as pilot:
            board = await mounted(pilot, lambda: app.screen, ComponentTable)
            board.update_state(state)
            # The header and four rows laid out; the widths are asserted below.
            await settled(pilot, lambda: board.virtual_size.height >= 5, what="the board layout")
            assert _cells_fit(board) == []
            assert board.virtual_size.width <= board.scrollable_content_region.width

    async def test_the_feed_wraps_inside_its_pane_at_80_columns(self, tmp_path: Path) -> None:
        """F11: RichLog renders 78 cells wide by default; the pane is narrower."""
        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=3))
        app = _dash(tmp_path, run_dir)
        async with app.run_test(size=(80, 24)) as pilot:
            feed = await mounted(pilot, lambda: app.screen, ActivityFeed)
            await settled(pilot, lambda: feed.lines, what="the feed to fill")
            assert feed.virtual_size.width <= feed.scrollable_content_region.width

    async def test_a_decompose_board_keeps_its_feed_under_the_architect_screen(
        self, tmp_path: Path
    ) -> None:
        """The board under the decompose screen was fed nothing."""
        run_dir = write_fake_decompose_run(tmp_path)
        app = _dash(tmp_path, run_dir, kind="decompose")
        async with app.run_test(size=(120, 36)) as pilot:
            await mounted(pilot, lambda: app.screen, "#decompose-header")
            board = next(s for s in app.screen_stack if isinstance(s, OverviewScreen))
            await settled(
                pilot,
                lambda: board._pending_feed or board.query(ActivityFeed).first().lines,
                what="the covered board to be fed",
            )


class TestDetail:
    async def test_a_finished_transcript_is_saved_and_f_is_not_offered(
        self, tmp_path: Path
    ) -> None:
        """F8: `● following (f toggles)` over a finished run."""
        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=1))
        app = _dash(tmp_path, run_dir)
        async with app.run_test(size=(120, 36)) as pilot:
            await mounted(pilot, lambda: app.screen, "#component-table")
            app.push_screen(ComponentScreen("comp-a"))
            tail = await mounted(pilot, lambda: app.screen, TranscriptTail)
            await settled(pilot, lambda: tail.lines, what="the transcript to fill on mount")
            title = str(app.screen.query_one("#transcript-title", Static).content)
            assert "saved, 4 line(s)" in title and "following" not in title
            assert app.screen.check_action("toggle_follow", ()) is False

    async def test_a_component_with_no_transcript_says_so(self, tmp_path: Path) -> None:
        """F8: an empty pane filled half the screen."""
        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=1))
        (run_dir / "components" / "comp-a" / "engineer.log").unlink()
        app = _dash(tmp_path, run_dir)
        async with app.run_test(size=(120, 36)) as pilot:
            await mounted(pilot, lambda: app.screen, "#component-table")
            app.push_screen(ComponentScreen("comp-a"))
            tail = await mounted(pilot, lambda: app.screen, TranscriptTail)
            await drained(pilot, app.screen, what="the detail screen's on_mount")
            title = str(app.screen.query_one("#transcript-title", Static).content)
            assert "wrote no engineer transcript in this run" in title
            assert not tail.display


class TestInboxAndRetry:
    async def test_the_inbox_list_is_visible_and_decisions_need_an_open_item(
        self, tmp_path: Path
    ) -> None:
        """E1: decisions offered on an empty inbox; the list rendered 1 cell wide."""
        app = _home(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await mounted(pilot, lambda: app.screen, "#home-commands")
            app.push_screen(InboxScreen())
            table = await mounted(pilot, lambda: app.screen, "#inbox-table")
            await drained(pilot, app.screen, what="the inbox's on_mount")
            assert app.screen.check_action("approve", ()) is False
            Inbox(tmp_path, InboxConfig()).add(ItemKind.HALTED_RUN, "halted", dedupe_key="h")
            cast(InboxScreen, app.screen).action_refresh()
            await settled(pilot, lambda: table.region.width > 40, what="the list to lay out")
            assert app.screen.check_action("approve", ()) is True

    async def test_nothing_to_retry_offers_no_retry(self, tmp_path: Path) -> None:
        """E2: an empty table header and `r Retry` with nothing failed."""
        app = _home(tmp_path)
        async with app.run_test(size=(120, 36)) as pilot:
            await mounted(pilot, lambda: app.screen, "#home-commands")
            app.push_screen(RetryScreen())
            table = await mounted(pilot, lambda: app.screen, "#retry-table")
            await drained(pilot, app.screen, what="the retry screen's on_mount")
            assert not table.display
            assert app.screen.check_action("retry_selected", ()) is False


class TestEvolveAndModals:
    async def test_evolve_says_there_are_no_patterns_and_keeps_its_tabs_clear(
        self, tmp_path: Path
    ) -> None:
        """F12: tab row over the header; an empty table with no sentence."""
        app = _home(tmp_path)
        async with app.run_test(size=(120, 36)) as pilot:
            screen = await evolve_on(app, pilot)
            empty = await mounted(pilot, lambda: screen, "#patterns-empty")
            assert isinstance(empty, Static)
            tabs = await mounted(pilot, lambda: screen, "#evolve-tabs")
            await settled(pilot, lambda: tabs.region.height, what="the tabs to lay out")
            assert empty.display and "No recurring failure patterns" in str(empty.content)
            assert tabs.region.y >= 3

    async def test_a_long_question_wraps_inside_the_dialog(self, tmp_path: Path) -> None:
        """The retry confirm was cut after one line."""
        header = "Retry 'integration-fix-1'? " + "Resets it and its dependents. " * 4
        request = PromptRequest(
            kind=PromptKind.CONFIRM, header=header, options=("Start", "Cancel"), default=1
        )
        app = _home(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await mounted(pilot, lambda: app.screen, "#home-commands")
            app.push_screen(OptionsModal(request))
            question = await mounted(pilot, lambda: app.screen, "#options-question")
            dialog = app.screen.query_one("#options-dialog")
            await settled(pilot, lambda: question.region.height, what="the question to lay out")
            assert question.region.height > 1
            assert question.region.right <= dialog.region.right


class TestRunIsLive:
    # Imported per test so the rest of this file runs against a tree
    # without the function (the red run of #433).
    def test_a_quiet_run_with_no_finish_record_is_not_live(self, tmp_path: Path) -> None:
        """F4 on the board: `● in flight` over a run that stopped days ago."""
        from kstrl.tui.runs import run_is_live

        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=1, complete=False))
        old = time.time() - 3 * 86400
        os.utime(run_dir / "events.jsonl", (old, old))
        assert run_is_live(run_dir, tmp_path) is False

    def test_a_run_written_a_moment_ago_is_live(self, tmp_path: Path) -> None:
        from kstrl.tui.runs import run_is_live

        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=1, complete=False))
        assert run_is_live(run_dir, tmp_path) is True

    def test_a_finished_run_is_not_live(self, tmp_path: Path) -> None:
        from kstrl.tui.runs import run_is_live

        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=1))
        assert run_is_live(run_dir, tmp_path) is False
