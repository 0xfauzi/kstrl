"""#433 increment 1: the verifier's cases (PR #540 round 1).

Each test was measured red on the PR head, under the named defect or
plant, and green with the fix or without the plant.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from kstrl.manifest import Manifest
from kstrl.tui import runs as runs_mod
from kstrl.tui.home_data import HomeStats, SummaryCache, failed_component_count
from kstrl.tui.home_view import attention_line
from kstrl.tui.widgets.cost_meter import render_cost_meter
from kstrl.tui.widgets.header import app_live
from tests.helpers.fake_run import FakeRunSpec, write_fake_run


def _quiet(run_dir: Path) -> None:
    old = time.time() - 3 * 86400
    os.utime(run_dir / "events.jsonl", (old, old))


class TestLiveness:
    """F4: the home table and the masthead share one liveness rule."""

    def test_a_cached_running_summary_turns_unknown_when_the_run_goes_quiet(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No byte is written when a run dies, so the stream signature holds."""
        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=1, complete=False))
        cache = SummaryCache()
        first = cache.refresh(runs_mod.discover_runs(tmp_path))
        assert first[run_dir.name].state == "running"
        later = time.time() + 3600
        monkeypatch.setattr(runs_mod, "time", SimpleNamespace(time=lambda: later))
        second = cache.refresh(runs_mod.discover_runs(tmp_path))
        assert second[run_dir.name].state == "unknown"

    def test_a_launched_session_that_ended_is_not_live(self, tmp_path: Path) -> None:
        """A refusal exits before the finish record; the masthead said running."""
        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=1, complete=False))
        _quiet(run_dir)
        run = SimpleNamespace(run_dir=run_dir, handle=SimpleNamespace(done=lambda: True))
        assert app_live(SimpleNamespace(root_dir=tmp_path, run_context=run)) is False

    def test_a_launched_session_still_going_is_live(self, tmp_path: Path) -> None:
        run_dir = write_fake_run(tmp_path, FakeRunSpec(components=1, complete=False))
        _quiet(run_dir)
        run = SimpleNamespace(run_dir=run_dir, handle=SimpleNamespace(done=lambda: False))
        assert app_live(SimpleNamespace(root_dir=tmp_path, run_context=run)) is True

    def test_a_held_lock_keeps_only_the_newest_factory_run_live(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spec = FakeRunSpec(components=1, complete=False)
        older = write_fake_run(tmp_path, spec, run_id="factory-20260720-110000.000000-old1")
        newer = write_fake_run(tmp_path, spec, run_id="factory-20260720-120000.000000-new1")
        assert runs_mod.run_sort_key(newer.name) > runs_mod.run_sort_key(older.name)
        _quiet(older)
        _quiet(newer)
        monkeypatch.setattr(runs_mod, "factory_lock_held", lambda _root: True)
        assert runs_mod.run_is_live(newer, tmp_path) is True
        assert runs_mod.run_is_live(older, tmp_path) is False
        monkeypatch.setattr(runs_mod, "factory_lock_held", lambda _root: False)
        assert runs_mod.run_is_live(newer, tmp_path) is False


class TestAttention:
    """E3: counts that were read, and no claim about one that was not."""

    def test_an_unreadable_inbox_is_not_reported_as_nothing_waiting(self) -> None:
        text = attention_line(HomeStats(last=None, inbox_open=None, failed_components=0)).plain
        assert "nothing is waiting" not in text

    def test_an_unreadable_inbox_log_is_not_counted(self, tmp_path: Path) -> None:
        """A torn multibyte write makes the scan unreadable, not empty."""
        from kstrl.inbox import Inbox, InboxConfig, ItemKind
        from kstrl.tui.home_data import gather_stats, open_inbox_count

        box = Inbox(tmp_path, InboxConfig())
        box.add(ItemKind.HALTED_RUN, "halted", dedupe_key="h")
        box.path.write_bytes(b"\xe2\x80")
        assert open_inbox_count(tmp_path) is None
        stats = gather_stats({}, "", tmp_path)
        assert "nothing is waiting" not in attention_line(stats).plain

    def test_failed_components_are_counted_from_the_manifest(self, tmp_path: Path) -> None:
        from kstrl.manifest import Component

        path = tmp_path / "scripts" / "kstrl" / "manifest.json"
        path.parent.mkdir(parents=True)
        comps = [
            Component(
                id=cid,
                title=cid,
                description="d",
                dependencies=[],
                prd_path="p.md",
                branch_name=f"b-{cid}",
                status=status,
            )
            for cid, status in (("a", "failed"), ("b", "completed"), ("c", "failed"))
        ]
        Manifest(
            version="1",
            spec_file="spec.md",
            project_name="p",
            base_branch="main",
            single_pr=False,
            components=comps,
        ).save(path)
        assert failed_component_count(tmp_path) == 2

    def test_a_project_with_no_manifest_has_nothing_to_retry(self, tmp_path: Path) -> None:
        assert failed_component_count(tmp_path) == 0


class TestMeter:
    """F6: segments drop whole, in order, before the short form is used."""

    def test_a_mid_width_meter_drops_the_run_id_and_keeps_the_percentage(self) -> None:
        from kstrl.reducer import RunState

        state = RunState(
            run_id="factory-20260926-010000.000000-t433",
            total_tokens=14_370_000,
            max_total_tokens=100_000_000,
            cost_usd=19.24,
            max_cost_usd=78.0,
        )
        full = render_cost_meter(state).plain
        assert "run " in full
        fitted = render_cost_meter(state, len(full) - 1).plain
        assert "24% of $78.00 cost cap" in fitted
        assert "run " not in fitted
        assert len(fitted) <= len(full) - 1


class TestCarried:
    """F10 and F5: a carried row gets no phases here; a span skips the scope record."""

    def test_a_carried_component_timeline_says_no_phases_in_this_run(self) -> None:
        from kstrl.reducer import ComponentState
        from kstrl.tui.widgets.phase_timeline import render_timeline

        comp = ComponentState(component_id="c", status="completed", carried=True)
        assert render_timeline(comp).plain == "no phases in this run"

    def test_a_duration_starts_at_the_first_event_after_the_scope_record(self) -> None:
        import dataclasses

        from kstrl import events as ev
        from kstrl.reducer import fold
        from kstrl.tui.widgets.component_table import time_cell_text

        start = 1_800_000_000.0
        items = [
            (ev.RunStarted(project="p", components=1), 0),
            (ev.ComponentScopeResolved(component="c"), 1),
            (ev.ComponentStarted(component="c"), 601),
            (ev.ComponentCompleted(component="c"), 661),
        ]
        run_id = "factory-20260926-010000.000000-t433"
        state = fold(
            [dataclasses.replace(e, ts=start + dt, run_id=run_id) for e, dt in items],
            run_id=run_id,
        )
        assert time_cell_text(state.components["c"], start + 700) == "1m"


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
