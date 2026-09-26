"""#433 increment 1: what the operator TUI says, tested without an app.

Each test names the finding it pins (F1-F12, E1-E3 in the #433 round-0
audit). The screen-level behaviour is in ``test_tui_433_screens.py``.
"""

from __future__ import annotations

import dataclasses
import io
import time
from pathlib import Path

from rich.console import Console, RenderableType

from kstrl import events as ev
from kstrl.reducer import ComponentState, RunState, fold
from kstrl.tui.home_data import HomeStats
from kstrl.tui.home_view import attention_line, command_strip
from kstrl.tui.run_status import finished_word, run_reason, state_word
from kstrl.tui.screens.decompose import _triage_widths
from kstrl.tui.screens.evolve import readiness_block
from kstrl.tui.widgets.activity import hanging, humanize
from kstrl.tui.widgets.component_detail import (
    render_component_header,
    render_failure_detail,
)
from kstrl.tui.widgets.component_table import phase_reason, time_cell_text
from kstrl.tui.widgets.cost_meter import render_cost_meter
from kstrl.tui.widgets.dag_table import fit_columns
from kstrl.tui.widgets.header import TOPBAR_PADDING, meter_width, render_header

NOW = 1_800_000_000.0
RUN_ID = "factory-20260926-010000.000000-t433"


def _plain(renderable: RenderableType, width: int = 80) -> str:
    console = Console(width=width, record=True, file=io.StringIO())
    console.print(renderable)
    return console.export_text()


def _events(*items: ev.Event, start: float = NOW - 600) -> list[ev.Event]:
    return [
        dataclasses.replace(item, ts=start + index, run_id=RUN_ID)
        for index, item in enumerate(items)
    ]


def _failed_gate_run(log_path: Path) -> RunState:
    return fold(
        _events(
            ev.RunStarted(project="p", components=1),
            ev.ComponentStarted(component="c"),
            ev.PhaseStarted(component="c", phase="verify", attempt=1),
            ev.VerificationResultEvent(
                component="c",
                passed=False,
                failures=("Tests failed (exit code 1)",),
                gate_logs=(str(log_path),),
            ),
            ev.PhaseCompleted(
                component="c",
                phase="verify",
                passed=False,
                detail="Mechanical verification failed",
            ),
            ev.ComponentFailed(component="c", error="Mechanical verification failed"),
            ev.RunCompleted(completed=0, failed=1, skipped=0),
        ),
        run_id=RUN_ID,
    )


class TestRunStateWords:
    """F4: every run says one of four words, and why."""

    def test_outcomes_map_to_the_four_words(self) -> None:
        assert [state_word(o) for o in ("live", "done", "failed", "stale", "?")] == [
            "running",
            "completed",
            "failed",
            "unknown",
            "unknown",
        ]

    def test_unknown_run_says_its_last_error_and_how_long_ago(self) -> None:
        state = fold(
            _events(
                ev.RunStarted(project="p", components=1),
                ev.Log(severity="error", text="Refusing to run: stale branches found"),
                ev.Log(severity="error", text="  branch kstrl/factory/a"),
            ),
            run_id=RUN_ID,
        )
        reason = run_reason("unknown", state, now=NOW)
        assert reason.startswith("Refusing to run: stale branches found: branch kstrl/factory/a")
        assert "no finish record; last event 9m ago" in reason

    def test_failed_run_names_the_component_and_the_gate_failure(self, tmp_path: Path) -> None:
        state = _failed_gate_run(tmp_path / "gate.log")
        assert run_reason("failed", state) == "c (verify: Tests failed (exit code 1))"

    def test_a_finished_run_with_a_failed_component_is_failed(self, tmp_path: Path) -> None:
        state = _failed_gate_run(tmp_path / "gate.log")
        assert finished_word(state) == "failed"
        assert "✗ failed" in render_header(state).plain
        assert "finished" not in render_header(state).plain


class TestFailureDetail:
    """F7: a failed gate says what failed and where its output is."""

    def test_reducer_attaches_failures_and_gate_logs_to_the_phase(self, tmp_path: Path) -> None:
        log = tmp_path / "gate.log"
        entry = _failed_gate_run(log).components["c"].phase_history[-1]
        assert entry["failures"] == ["Tests failed (exit code 1)"]
        assert entry["gate_logs"] == [str(log)]

    def test_detail_shows_cause_path_and_the_tail_of_the_log(self, tmp_path: Path) -> None:
        log = tmp_path / "gate.log"
        log.write_text("".join(f"line {n}\n" for n in range(1, 21)), encoding="utf-8")
        detail = render_failure_detail(_failed_gate_run(log).components["c"], tmp_path)
        assert detail is not None
        text = _plain(detail)
        assert "verify failed (attempt 1)  Tests failed (exit code 1)" in text
        assert "gate.log" in text and str(tmp_path) not in text
        assert "line 20" in text and "line 13" in text and "line 12" not in text

    def test_an_unreadable_log_is_said_not_hidden(self, tmp_path: Path) -> None:
        detail = render_failure_detail(
            _failed_gate_run(tmp_path / "gone.log").components["c"], tmp_path
        )
        assert detail is not None
        assert "(file not readable)" in _plain(detail)

    def test_wrapped_output_keeps_its_indent(self, tmp_path: Path) -> None:
        log = tmp_path / "gate.log"
        log.write_text("E " + "word " * 40 + "\n", encoding="utf-8")
        detail = render_failure_detail(_failed_gate_run(log).components["c"], tmp_path)
        assert detail is not None
        lines = [line for line in _plain(detail, 60).splitlines() if "word" in line]
        assert len(lines) > 1
        assert all(line.startswith("    ") for line in lines)


class TestComponentTimes:
    """F5 and F10: durations for finished rows, reasons for carried ones."""

    def test_finished_component_shows_how_long_it_took(self) -> None:
        comp = ComponentState(
            component_id="c", status="completed", started_ts=NOW - 7200, last_event_ts=NOW - 5400
        )
        assert time_cell_text(comp, NOW) == "30m"

    def test_running_component_shows_how_long_it_has_run(self) -> None:
        """Increment 2: the time column is elapsed time on every row. A
        running row read "21s ago" (its last event) under a header that
        says time, next to rows whose time is a duration."""
        comp = ComponentState(
            component_id="c", status="running", started_ts=NOW - 100, last_event_ts=NOW - 21
        )
        assert time_cell_text(comp, NOW) == "1m"

    def test_carried_and_skipped_rows_show_no_time(self) -> None:
        carried = ComponentState(component_id="c", status="completed", carried=True)
        skipped = ComponentState(
            component_id="s", status="skipped", started_ts=NOW - 1, last_event_ts=NOW - 1
        )
        assert time_cell_text(carried, NOW) == "·"
        assert time_cell_text(skipped, NOW) == "·"

    def test_carried_row_says_so_and_a_pending_row_says_what_it_waits_on(self) -> None:
        state = RunState(run_id=RUN_ID)
        state.components["a"] = ComponentState(component_id="a", status="completed", carried=True)
        state.components["b"] = ComponentState(
            component_id="b", status="pending", carried=True, deps=("x",)
        )
        assert phase_reason(state.components["a"], state)[0] == "carried from an earlier run"
        assert phase_reason(state.components["b"], state)[0] == "waiting on x"

    def test_a_decompose_run_says_its_components_are_planned(self) -> None:
        state = RunState(run_id="decompose-20260926-010000.000000-t433")
        state.components["a"] = ComponentState(component_id="a", deps=("x",))
        assert phase_reason(state.components["a"], state)[0] == "planned; ks factory builds it"

    def test_failed_row_names_the_failed_phase_and_cause(self, tmp_path: Path) -> None:
        state = _failed_gate_run(tmp_path / "gate.log")
        assert phase_reason(state.components["c"], state)[0] == (
            "verify: Tests failed (exit code 1)"
        )

    def test_detail_header_says_carried_rather_than_nothing(self) -> None:
        comp = ComponentState(component_id="c", status="completed", carried=True)
        assert "carried from an earlier run; not run in this one" in (
            render_component_header(comp, NOW).plain
        )


class TestTopbar:
    """F6 and F1: spend beside its cap, and a topbar that fits 80 columns."""

    def _state(self) -> RunState:
        return RunState(
            run_id=RUN_ID,
            project="snippetvault",
            started_ts=NOW - 3600,
            last_event_ts=NOW - 20,
            total_tokens=14_370_000,
            max_total_tokens=100_000_000,
            cost_usd=19.24,
            max_cost_usd=78.0,
        )

    def test_cost_is_shown_beside_the_cap_amount(self) -> None:
        # 24.67%, rounded up by cap_percent (#433 advice 2.3).
        assert "$19.24 · 25% of $78.00 cost cap" in render_cost_meter(self._state()).plain

    def test_no_cap_is_said(self) -> None:
        state = self._state()
        state.max_cost_usd = 0.0
        assert "no cost cap" in render_cost_meter(state).plain

    def test_at_80_columns_the_topbar_fits_and_keeps_spend_and_cap(self) -> None:
        state = self._state()
        state.started_ts = time.time() - 3822
        state.last_event_ts = time.time() - 21
        header = render_header(state, live=True, compact=True)
        meter = render_cost_meter(state, meter_width(header, 80)).plain
        assert header.cell_len + TOPBAR_PADDING + len(meter) <= 80
        assert "● running  1:03:4" in header.plain
        assert "$19.24" in meter and "$78.00" in meter

    def test_the_short_cost_form_keeps_both_amounts(self) -> None:
        assert render_cost_meter(self._state(), 20).plain == "$19.24 of $78.00 cap 25%"

    def test_an_unfinished_run_that_is_not_live_is_unknown(self) -> None:
        state = self._state()
        header = render_header(state, live=False).plain
        assert "? unknown" in header
        assert "running" not in header and "in flight" not in header

    def test_header_prefers_the_project_directory(self) -> None:
        state = self._state()
        state.project = "queue-793181"
        assert "snippetvault" in render_header(state, "snippetvault").plain
        assert "queue-793181" not in render_header(state, "snippetvault").plain


class TestHomeLines:
    """E3 and F2: what waits on the operator, and keys that fit."""

    def test_attention_line_counts_inbox_and_failures_with_their_keys(self) -> None:
        text = attention_line(HomeStats(last=None, inbox_open=2, failed_components=1)).plain
        assert text.strip() == "needs you: 2 inbox items (6) · 1 failed component (3)"

    def test_attention_line_says_nothing_waits(self) -> None:
        text = attention_line(HomeStats(last=None, inbox_open=0, failed_components=0)).plain
        assert text.strip() == "needs you: nothing is waiting on you"

    def test_attention_line_makes_no_claim_it_could_not_read(self) -> None:
        assert attention_line(HomeStats(last=None)).plain == ""

    def test_command_strip_fits_and_points_at_the_palette(self) -> None:
        class _C:
            def __init__(self, title: str) -> None:
                self.command_id = title
                self.title = title

        commands = [_C(t) for t in ("factory", "decompose", "retry", "dashboard", "config")]
        strip = command_strip(commands * 2, 80).plain
        assert strip.endswith("^p all")
        assert len(strip) <= 78
        assert strip.startswith("1 factory")


class TestFeedAndTables:
    """F11 and F1 at 80 columns."""

    def test_a_spec_issue_is_printed_whole(self) -> None:
        line = humanize(ev.SpecIssueRecorded(severity="major", summary="x" * 300, ts=NOW))
        assert line is not None
        assert "x" * 300 in line.plain

    def test_a_wrapped_feed_line_stays_under_its_body(self) -> None:
        line = humanize(
            ev.FindingRecorded(
                component="c",
                phase="review",
                category="other",
                severity="advisory",
                location="src/" + "a/" * 40 + "x.py:1",
                ts=NOW,
            )
        )
        assert line is not None
        rows = _plain(hanging(line), 60).splitlines()
        assert len(rows) > 1
        assert all(row.startswith(" " * 10) for row in rows[1:] if row.strip())

    def test_triage_columns_fit_80(self) -> None:
        issues = [{"kind": "undefined_failure_mode", "location": "S5 " + "q" * 60}]
        location, summary = _triage_widths(issues, 80, 24)
        used = 4 + len("severity") + len("undefined_failure_mode") + 4 * 2
        assert used + location + summary <= 80
        assert summary >= 20

    def test_dag_columns_fit_80(self) -> None:
        comps = [
            ComponentState(component_id="http-server", title="Socket server and more " * 3),
            ComponentState(component_id="cli", deps=("storage", "token-crypto", "http-server")),
        ]
        fit = fit_columns(comps, 80)
        widest = len("http-server") + 2 + fit.title
        assert widest + fit.deps + len("cycle!") + len("prd") + 4 * 2 + 4 <= 80


class TestEvolveReadiness:
    """F12: the readiness lines keep their indent when they wrap."""

    def test_wrapped_readiness_line_keeps_its_indent(self) -> None:
        from rich.text import Text

        block = readiness_block(Text("summary"), ["  concern hit rate: " + "cat 1, " * 30])
        rows = _plain(block, 60).splitlines()
        assert rows[:2] == ["summary", "learning readiness"]
        assert all(row.startswith("  ") for row in rows[2:] if row.strip())
        assert len(rows) > 3
