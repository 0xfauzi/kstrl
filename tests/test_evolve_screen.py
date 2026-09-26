"""TUI surface D4: the evolve screen - patterns and trends.

#217 Slice 1 deleted the proposals tab and its apply modal; a
proposals directory left in the state directory is ignored by the screen.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from rich.text import Text
from textual.coordinate import Coordinate
from textual.widgets import TabbedContent, TabPane

from kstrl.tui import theme
from kstrl.tui.screens.evolve import EvolveScreen, retry_bar
from tests.helpers.settle import mounted
from tests.helpers.tui_screens import evolve_on, home_app

CLAUDE_MD = """# CLAUDE.md

## Agent Learnings

- existing bullet
"""

TSV_HEADER = (
    "run_id\ttimestamp\tproject\tcomponents_total\tcompleted\tfailed\t"
    "skipped\tavg_iterations\tavg_duration_s\tretry_rate\tcommon_failure\t"
    "total_tokens\ttotal_cost_usd\tunreported_calls"
)


PROPOSAL_LEFT_ON_DISK = "# PROP-001: Always pin versions\n**Type**: computational\n"


def _seed(tmp_path: Path, signature: str = "test_suite:assert") -> None:
    proposals_dir = tmp_path / ".kstrl" / "proposals"
    proposals_dir.mkdir(parents=True)
    (proposals_dir / "prop-001.md").write_text(PROPOSAL_LEFT_ON_DISK)
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
            "failure_signatures": [signature],
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
    async def test_tabs_render_both_datasets(
        self,
        tmp_path: Path,
    ) -> None:
        _seed(tmp_path)
        app = home_app(tmp_path)
        async with app.run_test(size=(140, 40)) as pilot:
            screen = await evolve_on(app, pilot)
            tabs = await mounted(pilot, lambda: screen, TabbedContent)
            assert [pane.id for pane in tabs.query(TabPane)] == ["tab-patterns", "tab-trends"]
            assert not screen.query("#proposals-table")
            assert not screen.query("#proposal-detail")
            patterns = await mounted(pilot, lambda: screen, "#patterns-table")
            assert patterns.row_count == 1  # type: ignore[attr-defined]
            trends = await mounted(pilot, lambda: screen, "#trends-table")
            assert trends.row_count == 2  # type: ignore[attr-defined]
            row = trends.get_row_at(0)  # type: ignore[attr-defined]
            cells = " ".join(str(cell) for cell in row)
            assert "≥144000" in cells  # unreported -> lower bound
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

    async def test_journal_text_is_literal_and_a_writes_nothing(
        self,
        tmp_path: Path,
    ) -> None:
        _seed(tmp_path, signature="test_suite:[/bold]")
        app = home_app(tmp_path)
        async with app.run_test(size=(140, 40)) as pilot:
            screen = await evolve_on(app, pilot)
            table = await mounted(pilot, lambda: screen, "#patterns-table")
            code = table.get_cell_at(Coordinate(0, 1))  # type: ignore[attr-defined]
            assert isinstance(code, Text)
            assert code.plain == "[/bold]"
            assert "a" not in {binding.key for binding in EvolveScreen.BINDINGS}
        assert (tmp_path / "CLAUDE.md").read_text() == CLAUDE_MD
        prop = tmp_path / ".kstrl" / "proposals" / "prop-001.md"
        assert prop.read_text() == PROPOSAL_LEFT_ON_DISK

    async def test_empty_state(self, tmp_path: Path) -> None:
        app = home_app(tmp_path)
        async with app.run_test(size=(140, 40)) as pilot:
            screen = await evolve_on(app, pilot)
            patterns = await mounted(pilot, lambda: screen, "#patterns-table")
            assert patterns.row_count == 0  # type: ignore[attr-defined]
            trends = await mounted(pilot, lambda: screen, "#trends-table")
            assert trends.row_count == 0  # type: ignore[attr-defined]
