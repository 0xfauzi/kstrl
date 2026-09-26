"""#433 increment 3: home, the run board and the cost surfaces.

G9 and G10 of the #433 round-3 audit and items 2.3, 2.4 and 2.6 of the
design advice, each driven through Pilot over a run written by
``EventBus``. Liveness is by mtime (``runs.LIVE_MTIME_WINDOW_SECONDS``),
so a run meant to be stopped is backdated with ``os.utime``.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, cast

import pytest
from textual.widgets import DataTable, Static

from kstrl import events as ev
from kstrl.tui.app import KstrlTuiApp, Mode
from kstrl.tui.dispatch import initial_screens_for_kind
from kstrl.tui.screens.component import ComponentScreen
from kstrl.tui.widgets.cost_meter import cap_percent
from tests.helpers.settle import mounted, settled

LIVE = "factory-20260926-080000.000000-live01"
STOPPED = "factory-20260925-080000.000000-stop01"
QUIET = "factory-20260925-070000.000000-stop02"
PRICED = "factory-20260924-080000.000000-cost01"
HEADLINE = (
    "Refusing to run: stale component branches found: kstrl/api, kstrl/cli, "
    "kstrl/storage, kstrl/http-app; delete them or pass --resume"
)


def _home(root: Path) -> KstrlTuiApp:
    return KstrlTuiApp(root_dir=root, mode=Mode.HOME, poll_interval=0.05)


def _dash(root: Path, run_id: str) -> KstrlTuiApp:
    return KstrlTuiApp(
        run_dir=root / ".kstrl" / "runs" / run_id,
        root_dir=root,
        mode=Mode.DASH,
        poll_interval=0.05,
        screen_factory=initial_screens_for_kind("factory", observe_only=True),
    )


def _bus(root: Path, run_id: str) -> tuple[ev.EventBus, Path]:
    paths = ev.RunPaths.for_run(root, run_id)
    return ev.EventBus(ev.JsonlSink(paths.events_file), run_id=run_id), paths.events_file


def _backdate(path: Path, seconds: float) -> None:
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


def _running(root: Path, run_id: str = LIVE, output_age: float = 5.0) -> None:
    """A live run: api in engineer iteration 3/10, its log written ``output_age`` ago."""
    bus, _ = _bus(root, run_id)
    bus.emit(ev.RunStarted(project="demo", components=1))
    bus.emit(ev.RunPlan(components=({"id": "api", "title": "API", "deps": []},)))
    bus.emit(ev.ComponentStarted(component="api"))
    bus.emit(ev.PhaseStarted(component="api", phase="engineer", attempt=1))
    bus.emit(ev.IterationStarted(component="api", iteration=3, max_iterations=10))
    # The worker that runs the agent is this test process: alive.
    bus.emit(ev.WorkerHeartbeat(component="api", pid=os.getpid()))
    bus.close()
    log = root / ".kstrl" / "runs" / run_id / "components" / "api" / "engineer.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("running the tests\n", encoding="utf-8")
    _backdate(log, output_age)


def _stopped(root: Path, run_id: str, error: str = "") -> None:
    """A run with no finish record whose file is an hour old: unknown."""
    bus, events = _bus(root, run_id)
    bus.emit(ev.RunStarted(project="demo", components=1))
    if error:
        bus.emit(ev.Log(text=error, severity="error"))
        bus.emit(ev.Log(text="  branch 'kstrl/api' already exists", severity="error"))
    bus.close()
    _backdate(events, 3600)


def _priced(root: Path) -> None:
    """A finished run that spent $19.24 of a $78.00 cap: 24.67%."""
    bus, events = _bus(root, PRICED)
    bus.emit(ev.RunStarted(project="demo", components=1))
    bus.emit(ev.RunPlan(components=({"id": "api", "title": "API", "deps": []},), max_cost_usd=78.0))
    bus.emit(
        ev.ComponentUsage(
            component="api",
            phase="engineer",
            calls=1,
            known_calls=1,
            token_calls=1,
            cost_calls=1,
            total_tokens=1_000,
            cost_usd=19.24,
        )
    )
    bus.emit(ev.RunCompleted(completed=1, failed=0, skipped=0, duration_seconds=1.0))
    bus.close()
    _backdate(events, 7200)


class TestCostRounding:
    @pytest.mark.parametrize(
        ("spent", "cap", "shown"),
        [
            (19.24, 78.0, 25),
            (0.0, 78.0, 0),
            (0.01, 78.0, 1),
            (80.0, 78.0, 103),
            (0.07, 7.0, 1),
            (1.1, 1.0, 110),
        ],
    )
    def test_one_rule_rounds_up_and_never_caps(self, spent: float, cap: float, shown: int) -> None:
        """Advice 2.3: never below the spend, float noise does not add a point
        (100 * 0.07 / 7.0 is 1.0000000000000002, 100 * 1.1 / 1.0 is 110.00000000000001)."""
        assert cap_percent(spent, cap) == shown

    async def test_the_board_and_home_show_the_same_percentage(self, tmp_path: Path) -> None:
        """Advice 2.3: the board said 24% where $19.24 of $78.00 is 24.67%;
        home showed the spend with no cap at all."""
        _priced(tmp_path)
        app = _dash(tmp_path, PRICED)
        async with app.run_test(size=(120, 36)) as pilot:
            meter = cast(Static, await mounted(pilot, lambda: app.screen, "#cost-meter"))
            await settled(pilot, lambda: "cost cap" in str(meter.content), what="the meter")
            assert "$19.24 · 25% of $78.00 cost cap" in str(meter.content)
        app = _home(tmp_path)
        async with app.run_test(size=(120, 36)) as pilot:
            stats = cast(Static, await mounted(pilot, lambda: app.screen, "#home-stats"))
            await settled(pilot, lambda: "$19.24" in str(stats.content), what="the last run")
            assert "$19.24 of $78.00 cap 25%" in str(stats.content)


class TestHome:
    @pytest.mark.parametrize("size", [(120, 36), (80, 24)])
    async def test_an_unknown_run_says_why_and_the_chip_names_what_it_checked(
        self, tmp_path: Path, size: tuple[int, int]
    ) -> None:
        """G9: "unknown" with an empty note beside a chip saying "ok"."""
        _stopped(tmp_path, STOPPED, error=HEADLINE)
        _stopped(tmp_path, QUIET)
        _priced(tmp_path)
        app = _home(tmp_path)
        async with app.run_test(size=size) as pilot:
            runs = cast(DataTable[Any], await mounted(pilot, lambda: app.screen, "#home-runs"))
            await settled(
                pilot,
                lambda: (
                    STOPPED in [str(k.value) for k in runs.rows]
                    and str(runs.get_cell(STOPPED, "note"))
                ),
                what="the stopped run's note",
            )
            # Fitted to the table with an ellipsis at both sizes, never cut
            # at the edge; the preview line under the table has it whole.
            note = str(runs.get_cell(STOPPED, "note"))
            assert note.endswith("…") and len(note) > 20 and HEADLINE.startswith(note[:-1]), note
            assert str(runs.get_cell(QUIET, "note")) == "no reason recorded"
            assert runs.virtual_size.width <= runs.region.width
            chip = cast(Static, app.screen.query_one("#safe-mode-chip"))
            await settled(pilot, lambda: "safe mode" in str(chip.render()), what="the chip")
            assert "ok" not in str(chip.render()).split()

    @pytest.mark.parametrize("size", [(120, 36), (80, 24)])
    async def test_a_live_row_keeps_component_phase_progress_and_output_age(
        self, tmp_path: Path, size: tuple[int, int]
    ) -> None:
        """Advice 2.6: the narrow row began with the health and cut the phase.
        An alive process adds no words at 80; any other state follows the age.
        The age is matched, not pinned: the 1 s tick moves it under load."""
        _running(tmp_path)
        app = _home(tmp_path)
        async with app.run_test(size=size) as pilot:
            active = cast(DataTable[Any], await mounted(pilot, lambda: app.screen, "#home-active"))
            await settled(pilot, lambda: active.row_count, what="the active row")
            await settled(
                pilot, lambda: "output" in str(active.get_row_at(0)[3]), what="the agent health"
            )
            cell = str(active.get_row_at(0)[3])
            wanted = (
                r"api engineer 3/10 · output \d+s ago"
                if size[0] < 100
                else r"api engineer iteration 3/10 .*output \d+s ago · worker \d+ alive"
            )
            assert re.fullmatch(wanted, cell), cell
            await settled(pilot, lambda: active.region.width, what="the active table to lay out")
            assert active.virtual_size.width <= active.region.width, active.virtual_size


class TestRunBoard:
    async def test_delivery_is_its_own_titled_section(self, tmp_path: Path) -> None:
        """G10: the delivery lines sat flush under the table with no title;
        G9: "integration not recorded in this run"."""
        _priced(tmp_path)
        app = _dash(tmp_path, PRICED)
        async with app.run_test(size=(120, 36)) as pilot:
            row = cast(Static, await mounted(pilot, lambda: app.screen, "#delivery-row"))
            table = app.screen.query_one("#component-table")
            await settled(pilot, lambda: row.display and row.region.height, what="the section")
            text = str(row.content)
            assert text.splitlines()[0] == "delivery", text
            assert "integration no review recorded for this run" in text, text
            assert row.region.y >= table.region.bottom + 1, (table.region, row.region)

    async def test_output_age_and_process_are_two_answers_and_stale_names_its_window(
        self, tmp_path: Path
    ) -> None:
        """Advice 2.4: output five minutes old is stale, and the detail says
        after how long, beside the process answer."""
        _running(tmp_path, output_age=300.0)
        app = _dash(tmp_path, LIVE)
        async with app.run_test(size=(120, 36)) as pilot:
            await mounted(pilot, lambda: app.screen, "#component-table")
            app.push_screen(ComponentScreen("api"))
            header = cast(Static, await mounted(pilot, lambda: app.screen, "#component-header"))
            await settled(pilot, lambda: "output" in str(header.content), what="the health")
            text = str(header.content)
            assert f"output 5m ago, stale (stale after 1m) · worker {os.getpid()} alive" in text
