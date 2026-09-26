"""#433 increment 6: one account of approve, and nothing an operator needs is cut.

Each test names the round-6 finding it pins (K1-K8 in the #433 round-6
audit). Every one drives the real app through Pilot over state written by
kstrl's own writers (``EventBus``, ``Manifest``, ``Inbox``, ``Queue``,
``poll_ci``, ``write_launch_record``), at 120x36 and at 80x24.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import pytest
from textual.widgets import DataTable, Static

from kstrl.ci_state import read_ci_ledger
from kstrl.config_report import build_config_report
from kstrl.inbox import Inbox, InboxConfig, ItemKind
from kstrl.manifest import ComponentStatus, Manifest, park_dedupe_key
from kstrl.pipeline import PARK_DETAIL
from kstrl.tui.app import KstrlTuiApp, Mode
from kstrl.tui.integration_view import read_integration_review
from kstrl.tui.screens.config import ConfigScreen
from kstrl.tui.screens.inbox import InboxScreen
from kstrl.tui.screens.integration import IntegrationScreen
from kstrl.tui.theme import short_run_id
from tests.helpers.rendered import flat, shown
from tests.helpers.settle import mounted, settled
from tests.test_tui_433_inc4 import LIMIT_ENV, SHA_UNKNOWN, SHA_UNREAD, SHIP, _dash, poll_four
from tests.test_tui_433_inc5 import (
    SIZES,
    _failed_manifest,
    _home,
    _live_run_with_serve_item,
    _open_retry,
)
from tests.test_tui_433_queue import _finding, _integration_state, _review

HEAD = "87c3e2efbe2c4d0a9b1e"
#: A round's review with as many criteria as the e3 build's, so the tables fill.
FIVE_CRITERIA = {"criteria": [{"storyId": f"IC{n}", "verdict": "fail"} for n in range(1, 6)]}


def _one_line(text: str) -> str:
    return " ".join(text.split())


@pytest.fixture
def polled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return poll_four(tmp_path, monkeypatch)


@pytest.fixture
def no_limit_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in LIMIT_ENV:
        monkeypatch.delenv(name, raising=False)


class TestInboxPark:
    @pytest.mark.parametrize("size", SIZES)
    async def test_approve_is_said_once_for_this_screen_and_the_shell_is_labelled(
        self, tmp_path: Path, size: tuple[int, int]
    ) -> None:
        """K1: the item said ks inbox approve pushes and merges, the choice list
        said a records only. K2: "PR head" named a PR no park has opened."""
        _failed_manifest(tmp_path, ())
        manifest_file = tmp_path / "scripts" / "kstrl" / "manifest.json"
        manifest = Manifest.load(manifest_file)
        comp = manifest.get_component("comp-a")
        assert comp is not None
        comp.status = ComponentStatus.AWAITING_APPROVAL.value
        manifest.save(manifest_file)
        # What Pipeline._phase_checkpoint files when it parks.
        item = Inbox(tmp_path, InboxConfig()).add(
            ItemKind.MERGE_GATE,
            "comp-a awaiting merge approval",
            detail=PARK_DETAIL,
            component="comp-a",
            dedupe_key=park_dedupe_key("comp-a"),
            evidence={"branch": "kstrl/comp-a", "head_sha": HEAD, "review_fail_count": 0},
        )
        app = _home(tmp_path)
        async with app.run_test(size=size) as pilot:
            await mounted(pilot, lambda: app.screen, "#home-runs")
            app.push_screen(InboxScreen())
            detail = cast(Static, await mounted(pilot, lambda: app.screen, "#inbox-detail"))
            await settled(pilot, lambda: "what each choice does" in flat(detail), what="choices")
            text = _one_line(flat(detail))
            assert "PR head" not in text and f"branch head: {HEAD}" in text, text
            assert f"id {item.id[:8]}" in text, text
            # Every claim that approving pushes or merges is the shell's, and says so.
            for sentence in re.split(r"(?<=\.) ", text):
                if "opens the PR" in sentence or "ks inbox approve" in sentence:
                    assert sentence.startswith("From the shell, "), sentence
            approve = re.search(r"a approve: (.*?) r reject:", text)
            assert approve is not None, text
            assert approve.group(1).startswith("records approval only; nothing merges"), approve
            assert "ks inbox" not in approve.group(1), approve.group(1)


class TestCiReason:
    @pytest.mark.parametrize("size", SIZES)
    @pytest.mark.parametrize("screen", ["home", "overview"])
    async def test_the_whole_ci_reason_is_on_screen_under_its_merge(
        self,
        polled: Path,
        screen: str,
        size: tuple[int, int],
    ) -> None:
        """K3: gh's reason for an unknown reading was cut at the edge. K4: an
        unread commit said "run ks ci poll" as if nothing else read it."""
        reason = read_ci_ledger(polled).latest(SHA_UNKNOWN)
        assert reason is not None and len(reason.reason) > 60, reason
        app = _home(polled) if screen == "home" else _dash(polled, SHIP)
        selector = "#home-delivery" if screen == "home" else "#delivery-row"
        async with app.run_test(size=size) as pilot:
            widget = cast(Static, await mounted(pilot, lambda: app.screen, selector))
            await settled(pilot, lambda: "merged PR #4" in str(widget.content), what="merges")
            lines = str(widget.content).splitlines()
            await settled(pilot, lambda: widget.region.height == len(lines), what="laid out")
            text = _one_line(str(widget.content))
            assert _one_line(reason.reason) in text, text
            assert "…" not in text, lines
            assert max(len(line) for line in lines) <= widget.content_region.width, lines
            unread = f"merged PR #4 {SHA_UNREAD[:7]} CI not read yet"
            assert unread in text and "ks serve reads it after each cycle" in text, text
            assert "ks ci poll reads it now" in text and "no CI reading" not in text, text
            start = next(i for i, line in enumerate(lines) if "PR #3" in line)
            indent = lines[start].index("merged")
            for line in lines[start + 1 :]:
                if "merged PR" in line:
                    break
                assert line[: indent + 4].strip() == "", (line, lines)


class TestConfig:
    @pytest.mark.parametrize("size", SIZES)
    async def test_a_value_is_cut_once_and_the_table_does_not_scroll_sideways(
        self, tmp_path: Path, size: tuple[int, int]
    ) -> None:
        """K5: at 80x24 a value was cut in its middle and then again at the edge."""
        app = KstrlTuiApp(
            root_dir=tmp_path,
            mode=Mode.HOME,
            poll_interval=0.05,
            config_report=build_config_report(tmp_path),
        )
        async with app.run_test(size=size) as pilot:
            await mounted(pilot, lambda: app.screen, "#home-runs")
            app.push_screen(ConfigScreen())
            table = cast(DataTable[Any], await mounted(pilot, lambda: app.screen, "#config-table"))
            await settled(pilot, lambda: table.row_count and table.size.width, what="the rows")
            assert table.max_scroll_x == 0, (table.virtual_size, table.size)
            cells = [str(table.get_row_at(i)[2]) for i in range(table.row_count)]
            assert all(cell.count("…") <= 1 for cell in cells), cells
            assert any("…" in cell for cell in cells), "no value is cut; the test needs one"
            if size[0] == 80:
                # The value column takes the room 80 columns leave, not a fixed short cut.
                assert table.virtual_size.width >= table.size.width - 6, table.virtual_size
            # Narrower: the value column is re-cut and the selected row stays selected.
            table.focus()
            await pilot.press("down", "down", "down")
            await settled(pilot, lambda: table.cursor_row == 3, what="the cursor on row 4")
            section, key = str(list(table.rows)[3].value).split(".", 1)
            await pilot.resize_terminal(size[0] - 10, size[1])
            await settled(
                pilot,
                lambda: table.size.width == size[0] - 10 and table.max_scroll_x == 0,
                what="the table re-cut to the narrower screen",
            )
            hint = flat(app.screen.query_one("#config-hint", Static))
            assert table.cursor_row == 3, table.cursor_row
            assert f"[{section}] {key} = " in hint, hint


class TestRetryScope:
    @pytest.mark.parametrize("size", SIZES)
    async def test_the_parallel_count_is_stated_once(
        self, tmp_path: Path, no_limit_env: None, size: tuple[int, int]
    ) -> None:
        """K6: "runs under ... 1 in parallel" and "replays --max-parallel 1"."""
        _failed_manifest(tmp_path, (("max_parallel", 1), ("review_mode", "advisory")))
        app = _home(tmp_path)
        async with app.run_test(size=size) as pilot:
            await _open_retry(app, pilot)
            await settled(
                pilot,
                lambda: app.screen.check_action("retry_selected", ()) is True,
                what="r to be offered once the carry is read",
            )
            await pilot.press("r")
            body = await mounted(pilot, lambda: app.screen, "#options-detail Static")
            text = flat(body)
            assert len(re.findall(r"parallel", text)) == 1, text
            assert re.search(r"runs under\s+.*, 1 in parallel", text), text
            assert re.search(r"replays\s+--review-mode advisory\n", text + "\n"), text


class TestIntegrationRounds:
    @pytest.mark.parametrize("size", SIZES)
    async def test_every_round_reason_is_whole_and_wraps_under_itself(
        self, tmp_path: Path, size: tuple[int, int]
    ) -> None:
        """K7: at 80x24 the round lines ended in "0..." and "3...", so the counts
        could not be checked against the findings list."""
        run = "factory-20260925-214816.991354-fda682"
        reasons = [
            "5 findings opened, 1 handed off, 0 carried closed, 0 carried still open; blocking",
            "the last fix integration-fix-1 ended failed, not merged, so no tree holds it",
            "0 findings opened, 0 handed off, 1 carried closed, 3 carried still open; blocking",
        ]
        for number, (outcome, reason) in enumerate(
            zip(("open_findings", "not_run", "open_findings"), reasons, strict=True), start=1
        ):
            _review(tmp_path, run, number, outcome=outcome, reason=reason, review=FIVE_CRITERIA)
        findings = [_finding(f"IF-{n}", "open", "t", run) for n in range(1, 6)]
        _integration_state(tmp_path, findings, [])
        review = read_integration_review(tmp_path, tmp_path / ".kstrl" / "runs" / run, {})
        assert review is not None
        app = _home(tmp_path)
        async with app.run_test(size=size) as pilot:
            await mounted(pilot, lambda: app.screen, "#home-runs")
            app.push_screen(IntegrationScreen(review, run))
            rounds = cast(Static, await mounted(pilot, lambda: app.screen, "#integration-rounds"))
            detail = cast(Static, app.screen.query_one("#integration-detail"))
            await settled(pilot, lambda: rounds.content_region.width, what="the rounds laid out")
            text = _one_line(flat(rounds))
            for reason in reasons:
                assert reason in text, (reason, text)
            lines = shown(rounds)
            await settled(pilot, lambda: rounds.region.height == len(lines), what="every line")
            column = lines[0].index("5 findings")
            for line in lines:
                assert line.startswith("round ") or line[:column].strip() == "", lines
            assert detail.region.height >= 4, detail.region


class TestServeRow:
    @pytest.mark.parametrize("size", SIZES)
    async def test_the_serve_row_keeps_its_whole_title(
        self, tmp_path: Path, size: tuple[int, int]
    ) -> None:
        """K8: at 80x24 the ks serve row cut "snippetvault slice 3: ..." to "snippetvau..."."""
        from kstrl.serve import serve_lock

        title = "snippetvault slice 3: export and import"
        run_dir, lock = _live_run_with_serve_item(tmp_path, True, title)
        try:
            with serve_lock(tmp_path):
                app = _home(tmp_path)
                async with app.run_test(size=size) as pilot:
                    active = cast(
                        DataTable[Any], await mounted(pilot, lambda: app.screen, "#home-active")
                    )
                    await settled(
                        pilot,
                        lambda: (
                            active.row_count == 2 and "folding" not in str(active.get_row_at(0)[3])
                        ),
                        what="the factory row and the serve row",
                    )
                    serve = str(active.get_row_at(1)[3])
                    assert serve.startswith(title), serve
                    assert active.max_scroll_x == 0, (active.virtual_size, active.size)
                    if size[0] >= 120:
                        assert f"{title} · run {short_run_id(run_dir.name)}" in serve, serve
        finally:
            lock.close()
