"""#433 increment 1: the verifier's pilot cases (PR #540 round 1).

Each test was measured red on the PR head, under the named defect or
plant, and green with the fix or without the plant. What remains drives
a Textual app through ``run_test``: a decided inbox item offers no
decision (E1), and a log rewraps when its pane narrows (F11).
"""

from __future__ import annotations

from pathlib import Path


class TestScreens:
    async def test_a_decided_item_offers_no_decision(self, tmp_path: Path) -> None:
        """E1: decisions are for an OPEN item, not for any selected item."""
        from kstrl.inbox import Inbox, InboxConfig, ItemKind
        from kstrl.tui.app import KstrlTuiApp, Mode
        from kstrl.tui.screens.inbox import InboxScreen
        from tests.helpers.settle import drained, mounted, settled

        box = Inbox(tmp_path, InboxConfig())
        item = box.add(ItemKind.HALTED_RUN, "halted", dedupe_key="h")
        box.approve(item.id, actor="tester")
        app = KstrlTuiApp(root_dir=tmp_path, mode=Mode.HOME, poll_interval=0.05)
        async with app.run_test(size=(120, 36)) as pilot:
            await mounted(pilot, lambda: app.screen, "#home-commands")
            app.push_screen(InboxScreen())
            await mounted(pilot, lambda: app.screen, "#inbox-table")
            await drained(pilot, app.screen, what="the inbox's on_mount")
            screen = app.screen
            assert isinstance(screen, InboxScreen)
            screen.action_toggle_decided()
            await settled(pilot, lambda: screen._items, what="the decided item to list")
            assert screen._selected() is not None
            assert screen.check_action("approve", ()) is False

    async def test_a_log_rewraps_when_it_narrows(self) -> None:
        """F11: lines wrapped at 120 columns ran past a 60-column pane."""
        from textual.app import App, ComposeResult

        from kstrl.tui.widgets.reflow_log import ReflowLog
        from tests.helpers.settle import mounted, settled

        class _Log(App[None]):
            def compose(self) -> ComposeResult:
                yield ReflowLog(max_lines=50, id="log")

        app = _Log()
        async with app.run_test(size=(120, 20)) as pilot:
            log = await mounted(pilot, lambda: app.screen, ReflowLog)
            await settled(pilot, lambda: log.wrapped_at, what="the first layout")
            log.write_source("word " * 40)
            await settled(pilot, lambda: log.lines, what="the line to be written")
            await pilot.resize_terminal(60, 20)
            await settled(pilot, lambda: log.wrapped_at < 100, what="the narrower layout")
            assert log.virtual_size.width <= log.scrollable_content_region.width
