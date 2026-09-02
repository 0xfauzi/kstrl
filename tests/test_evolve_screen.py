"""TUI surface D4: the evolve screen - proposals, patterns, trends."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import cast

from rich.text import Text
from textual.coordinate import Coordinate
from textual.widgets import DataTable, TabbedContent

from kstrl.tui import theme
from kstrl.tui.screens.evolve import EvolveScreen, retry_bar
from kstrl.tui.screens.options import OptionsModal
from tests.helpers.settle import drained, mounted, settled
from tests.helpers.tui_screens import evolve_on, home_app

CONVENTION_PROP = """# PROP-001: Always pin versions
**Type**: computational
**Target**: claude_md

Suggested change:

> Pin every dependency version in pyproject.toml.
"""

MANUAL_PROP = """# PROP-002: Bump feedforward budget
**Type**: inferential
**Target**: feedforward_config

Suggested change:

> Raise max_context_tokens to 12000.
"""

CLAUDE_MD = """# CLAUDE.md

## Agent Learnings

- existing bullet
"""

TSV_HEADER = (
    "run_id\ttimestamp\tproject\tcomponents_total\tcompleted\tfailed\t"
    "skipped\tavg_iterations\tavg_duration_s\tretry_rate\tcommon_failure\t"
    "total_tokens\ttotal_cost_usd\tunreported_calls"
)


def _seed(tmp_path: Path) -> None:
    proposals_dir = tmp_path / ".kstrl" / "proposals"
    proposals_dir.mkdir(parents=True)
    (proposals_dir / "prop-001.md").write_text(CONVENTION_PROP)
    (proposals_dir / "prop-002.md").write_text(MANUAL_PROP)
    (tmp_path / "CLAUDE.md").write_text(CLAUDE_MD)
    (tmp_path / ".kstrl" / "experiments.tsv").write_text(
        TSV_HEADER + "\n" + "factory-20260718-100000.000000-aaa\t2026-07-18\tdemo\t3\t3\t0\t0"
        "\t2.0\t120\t0.33\t\t144000\t2.25\t2\n"
        + "factory-20260719-100000.000000-bbb\t2026-07-19\tdemo\t2\t1\t1\t0"
        "\t3.0\t150\t0\ttest_suite:assert\t\t\t0\n",
    )
    entries = [
        {
            "event_type": "component_result",
            "run_id": run,
            "component_id": comp,
            "failure_signatures": ["test_suite:assert"],
        }
        for run, comp in (("r1", "c1"), ("r2", "c2"))
    ]
    (tmp_path / ".kstrl" / "evolution.jsonl").write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n",
    )


class TestRetryBar:
    def test_scaling(self) -> None:
        assert retry_bar(0) == theme.EMPTY_CELL
        assert retry_bar(0.5) in "▃▄▅"
        assert retry_bar(1.0) == "▇"
        assert retry_bar(math.nan) == theme.EMPTY_CELL
        assert retry_bar(math.inf) == theme.EMPTY_CELL


class TestEvolveScreen:
    async def test_tabs_render_all_three_datasets(
        self,
        tmp_path: Path,
    ) -> None:
        _seed(tmp_path)
        app = home_app(tmp_path)
        async with app.run_test(size=(140, 40)) as pilot:
            screen = await evolve_on(app, pilot)
            proposals = await mounted(pilot, lambda: screen, "#proposals-table")
            assert proposals.row_count == 2  # type: ignore[attr-defined]
            patterns = await mounted(pilot, lambda: screen, "#patterns-table")
            assert patterns.row_count == 1  # type: ignore[attr-defined]
            trends = await mounted(pilot, lambda: screen, "#trends-table")
            assert trends.row_count == 2  # type: ignore[attr-defined]
            row = trends.get_row_at(0)  # type: ignore[attr-defined]
            cells = " ".join(str(cell) for cell in row)
            assert "144000+" in cells  # unreported -> lower bound
            second = trends.get_row_at(1)  # type: ignore[attr-defined]
            second_cells = [str(cell) for cell in second]
            assert theme.EMPTY_CELL in second_cells  # empty tokens honest

            # Non-finite data is invalid, but must degrade to an empty
            # cell rather than crashing the whole screen.
            cells = screen._trend_cells(
                {
                    "retry_rate": "nan",
                    "unreported_calls": "inf",
                }
            )
            assert str(cells[3]) == theme.EMPTY_CELL

    async def test_repository_text_is_literal_and_apply_is_tab_scoped(
        self,
        tmp_path: Path,
    ) -> None:
        _seed(tmp_path)
        proposal_path = tmp_path / ".kstrl" / "proposals" / "prop-001.md"
        proposal_path.write_text(
            proposal_path.read_text()
            .replace("Always pin versions", "[/bold]")
            .replace("Pin every dependency", "[/bold] every dependency"),
        )
        app = home_app(tmp_path)
        async with app.run_test(size=(140, 40)) as pilot:
            screen = await evolve_on(app, pilot)
            table = await mounted(pilot, lambda: screen, "#proposals-table")
            title = table.get_cell_at(Coordinate(0, 1))  # type: ignore[attr-defined]
            assert isinstance(title, Text)
            assert title.plain == "[/bold]"

            tabs = await mounted(pilot, lambda: screen, TabbedContent)
            tabs.active = "tab-patterns"
            # A direct, synchronous call: off the proposals tab it
            # returns before it touches the screen stack, so there is
            # nothing here to settle and nothing to wait for.
            screen.action_apply_selected()
            assert isinstance(app.screen, EvolveScreen)

            tabs.active = "tab-proposals"
            screen.action_apply_selected()
            # push_screen stacks the modal synchronously; its Label
            # mounts a frame later, and the Label is what is read here.
            # Waiting for the Label rather than for the screen type
            # leaves the assertion below its own failure message.
            question = await mounted(pilot, lambda: app.screen, "#options-question")
            assert isinstance(app.screen, OptionsModal)
            assert isinstance(question.content, Text)  # type: ignore[attr-defined]
            assert "[/bold]" in question.content.plain  # type: ignore[attr-defined]
            await pilot.press("escape")

    async def test_apply_via_modal_mutates_and_stamps(
        self,
        tmp_path: Path,
    ) -> None:
        _seed(tmp_path)
        app = home_app(tmp_path)
        async with app.run_test(size=(140, 40)) as pilot:
            screen = await evolve_on(app, pilot)
            await pilot.press("a")
            # "a screen opened over the evolve screen" is weaker than
            # "that screen is the OptionsModal": a wrong screen ends
            # the wait at once and fails on the assertion below.
            await settled(
                pilot,
                lambda: app.screen is not screen,
                what="the 'a' key to open a screen over the evolve screen",
            )
            assert isinstance(app.screen, OptionsModal)
            assert "PROP-001" in app.screen.request.header
            await pilot.press("1")  # Apply
            # Screen.dismiss hands the result to the callback queue
            # BEFORE it pops the modal, so the pop proves the callback
            # is scheduled and the drain proves it has run. Measured:
            # the requester is the app, not the screen, because the
            # binding's action runs in the app's message-pump context.
            await settled(
                pilot,
                lambda: app.screen is screen,
                what="the Apply choice to close the modal",
            )
            await drained(pilot, app, what="the apply callback to run")
            content = (tmp_path / "CLAUDE.md").read_text()
            assert "Pin every dependency version" in content
            assert "applied from PROP-001" in content
            prop = (tmp_path / ".kstrl" / "proposals" / "prop-001.md").read_text()
            assert "**Applied**:" in prop
            detail_widget = await mounted(pilot, lambda: screen, "#proposal-detail")
            detail = str(
                detail_widget.content,  # type: ignore[attr-defined]
            )
            assert "✓ applied" in detail

    async def test_cancel_writes_nothing(self, tmp_path: Path) -> None:
        _seed(tmp_path)
        app = home_app(tmp_path)
        async with app.run_test(size=(140, 40)) as pilot:
            screen = await evolve_on(app, pilot)
            await pilot.press("a")
            await settled(
                pilot,
                lambda: app.screen is not screen,
                what="the 'a' key to open a screen over the evolve screen",
            )
            assert isinstance(app.screen, OptionsModal)
            await pilot.press("2")  # Cancel
            # Same two hops as the apply case. The drain matters more
            # here, not less: the assertion is that the callback wrote
            # NOTHING, which is indistinguishable from a callback that
            # has not run yet unless the drain has observed it run.
            await settled(
                pilot,
                lambda: app.screen is screen,
                what="the Cancel choice to close the modal",
            )
            await drained(pilot, app, what="the cancel callback to run")
            assert (tmp_path / "CLAUDE.md").read_text() == CLAUDE_MD
            prop = (tmp_path / ".kstrl" / "proposals" / "prop-001.md").read_text()
            assert "**Applied**:" not in prop

    async def test_manual_proposal_never_opens_the_modal(
        self,
        tmp_path: Path,
    ) -> None:
        _seed(tmp_path)
        app = home_app(tmp_path)
        async with app.run_test(size=(140, 40)) as pilot:
            screen = await evolve_on(app, pilot)
            table = await mounted(pilot, lambda: screen, "#proposals-table")
            table.focus()
            # Widget.focus routes through call_later, so focus is not
            # in place when it returns, and a "down" pressed before it
            # lands moves nothing.
            await settled(
                pilot,
                lambda: table.has_focus,
                what="the proposals table to take focus",
            )
            await pilot.press("down")  # PROP-002, the inferential one
            await settled(
                pilot,
                lambda: cast(DataTable, table).cursor_row == 1,
                what="the down key to move the cursor to the inferential proposal",
            )
            await pilot.press("a")
            # Deliberately the OR. The correct outcome of "a" here is
            # that NO modal opens, so the only positive thing to wait
            # for is the notice the manual branch raises; a modal
            # opening satisfies the wait at once and leaves the
            # assertion below to fail in its own words.
            await settled(
                pilot,
                lambda: app.screen is not screen or len(app._notifications),
                what="the 'a' key to either open a modal or refuse with a notice",
            )
            assert isinstance(app.screen, EvolveScreen)  # no modal
            assert (tmp_path / "CLAUDE.md").read_text() == CLAUDE_MD

    async def test_empty_state(self, tmp_path: Path) -> None:
        app = home_app(tmp_path)
        async with app.run_test(size=(140, 40)) as pilot:
            screen = await evolve_on(app, pilot)
            detail_widget = await mounted(pilot, lambda: screen, "#proposal-detail")
            detail = str(
                detail_widget.content,  # type: ignore[attr-defined]
            )
            assert "no proposals yet" in detail
