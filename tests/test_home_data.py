"""TUI surface D2: run summaries, cache, quick stats."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

from kstrl.tui.app import KstrlTuiApp, Mode
from kstrl.tui.home_data import (
    SummaryCache,
    pending_proposal_count,
    summarize_run,
)
from kstrl.tui.runs import discover_runs
from tests.helpers.fake_run import (
    FakeRunSpec,
    write_fake_decompose_run,
    write_fake_run,
    write_fake_understand_run,
)

CONVENTION_PROP = """# PROP-001: Pin versions
**Type**: computational
**Target**: claude_md

> Pin them.
"""


class TestSummarizeRun:
    def test_factory_run_with_lower_bound_marker(
        self, tmp_path: Path,
    ) -> None:
        write_fake_run(
            tmp_path, FakeRunSpec(components=2, include_unreported_usage=True),
        )
        ref = discover_runs(tmp_path)[0]
        summary = summarize_run(ref)
        assert summary.outcome == "done"
        assert summary.components_done == 2
        assert summary.components_total == 2
        assert summary.tokens_lower_bound  # unreported calls -> "+"
        assert summary.total_tokens > 0
        assert summary.cost_usd > 0

    def test_understand_and_halted_decompose(self, tmp_path: Path) -> None:
        write_fake_understand_run(tmp_path)
        write_fake_decompose_run(
            tmp_path, blockers=1,
            run_id="decompose-20260720-160000.000000-halt",
        )
        by_kind = {ref.kind: ref for ref in discover_runs(tmp_path)}
        understand = summarize_run(by_kind["understand"])
        assert understand.outcome == "done"
        assert understand.components_done == 1
        halted = summarize_run(by_kind["decompose"])
        assert halted.outcome == "failed"
        assert halted.components_failed == 1

    def test_incomplete_stale_run(self, tmp_path: Path) -> None:
        import os

        write_fake_run(tmp_path, FakeRunSpec(components=1, complete=False))
        ref = discover_runs(tmp_path)[0]
        old = time.time() - 3600
        os.utime(ref.events_path, (old, old))
        ref = discover_runs(tmp_path)[0]
        assert summarize_run(ref).outcome == "stale"


class TestSummaryCache:
    def test_unchanged_mtime_hits_cache(self, tmp_path: Path) -> None:
        write_fake_run(tmp_path, FakeRunSpec(components=1))
        refs = discover_runs(tmp_path)
        cache = SummaryCache()
        with patch(
            "kstrl.tui.home_data.summarize_run",
            wraps=summarize_run,
        ) as spy:
            cache.refresh(refs)
            cache.refresh(refs)
        assert spy.call_count == 1

    def test_moved_mtime_recomputes(self, tmp_path: Path) -> None:
        import os

        write_fake_run(tmp_path, FakeRunSpec(components=1))
        refs = discover_runs(tmp_path)
        cache = SummaryCache()
        cache.refresh(refs)
        os.utime(refs[0].events_path, None)
        moved = discover_runs(tmp_path)
        with patch(
            "kstrl.tui.home_data.summarize_run",
            wraps=summarize_run,
        ) as spy:
            cache.refresh(moved)
        assert spy.call_count == 1


class TestPendingProposals:
    def test_counts_unapplied_only(self, tmp_path: Path) -> None:
        proposals_dir = tmp_path / ".kstrl" / "proposals"
        proposals_dir.mkdir(parents=True)
        (proposals_dir / "prop-001.md").write_text(CONVENTION_PROP)
        (proposals_dir / "prop-002.md").write_text(
            CONVENTION_PROP.replace("PROP-001", "PROP-002")
            + "\n**Applied**: 2026-07-19T00:00:00Z\n",
        )
        assert pending_proposal_count(tmp_path) == 1
        assert pending_proposal_count(tmp_path / "empty") == 0


class TestHomeSummariesPilot:
    async def test_cells_fill_after_the_worker_lands(
        self, tmp_path: Path,
    ) -> None:
        write_fake_run(tmp_path, FakeRunSpec(components=2))
        proposals_dir = tmp_path / ".kstrl" / "proposals"
        proposals_dir.mkdir(parents=True)
        (proposals_dir / "prop-001.md").write_text(CONVENTION_PROP)

        app = KstrlTuiApp(root_dir=tmp_path, mode=Mode.HOME,
                          poll_interval=0.05)
        async with app.run_test(size=(130, 40)) as pilot:
            deadline = time.monotonic() + 5
            while True:
                await pilot.pause(0.1)
                stats = str(
                    app.screen.query_one("#home-stats").renderable,
                )
                if "last run" in stats:
                    break
                assert time.monotonic() < deadline, "summaries never landed"
            assert "✓ done 2/2" in stats
            assert "proposal(s) pending" in stats
            table = app.screen.query_one("#home-runs")
            row = table.get_row_at(0)  # type: ignore[attr-defined]
            cells = " ".join(str(cell) for cell in row)
            assert "2/2" in cells
            assert "+" in cells  # lower-bound marker rides along
