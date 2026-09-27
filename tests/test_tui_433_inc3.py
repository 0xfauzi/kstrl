"""#433 increment 3: the failure queue, the decision screens and the detail views.

Each test names the round-3 finding it pins (G1-G8 in the #433 round-3
audit, "advice" for the design advice's section 2). Every one drives the
real app through Pilot over state written by kstrl's own writers
(``EventBus``, ``Manifest``, ``Inbox``, ``write_launch_record``).
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest
from rich.console import Console
from textual.widgets import DataTable, Static

from kstrl import events as ev
from kstrl.agents.base import UsageTotals
from kstrl.config_report import build_config_report
from kstrl.factory import FactoryConfig
from kstrl.inbox import Inbox, InboxConfig, ItemKind
from kstrl.interaction import CheckpointContext, PromptKind, PromptRequest
from kstrl.launch_record import run_limits, write_launch_record
from kstrl.manifest import Component, ComponentStatus, Manifest, park_dedupe_key
from kstrl.timeout import TimeoutConfig
from kstrl.tui.app import KstrlTuiApp, Mode
from kstrl.tui.dispatch import initial_screens_for_kind
from kstrl.tui.screens.checkpoint import CheckpointModal
from kstrl.tui.screens.component import ComponentScreen
from kstrl.tui.screens.config import ConfigScreen
from kstrl.tui.screens.decompose import DecomposeScreen
from kstrl.tui.screens.gate_log import GateLogScreen
from kstrl.tui.screens.inbox import InboxScreen
from kstrl.tui.screens.options import OptionsModal
from kstrl.tui.screens.retry import RetryScreen
from kstrl.tui.widgets.transcript import TranscriptTail
from tests.helpers.fake_run import write_fake_decompose_run
from tests.helpers.rendered import flat
from tests.helpers.settle import drained, mounted, settled
from tests.helpers.tui_screens import evolve_on

RUN_ID = "factory-20260926-072240.147891-fail01"
LIMIT_ENV = (
    "KSTRL_FACTORY_MAX_COST_USD",
    "KSTRL_FACTORY_MAX_TOTAL_TOKENS",
    "KSTRL_FACTORY_MAX_ADVERSARIAL_CALLS",
    "KSTRL_TIMEOUT_AGENT_ITERATION",
    "KSTRL_TIMEOUT_COMPONENT",
)
GATE_OUTPUT = "".join(f"line {n} of the gate output\n" for n in range(1, 41))


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


@pytest.fixture
def no_limit_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in LIMIT_ENV:
        monkeypatch.delenv(name, raising=False)


def _failed_run(root: Path, cid: str = "comp-a") -> Path:
    """A run whose verify gate failed with its output stored (#462), and a
    manifest that still records the component FAILED and names the run."""
    paths = ev.RunPaths.for_run(root, RUN_ID)
    bus = ev.EventBus(ev.JsonlSink(paths.events_file), run_id=RUN_ID)
    bus.emit(ev.RunStarted(project="demo", components=1))
    bus.emit(ev.RunPlan(components=({"id": cid, "title": "A", "deps": []},)))
    bus.emit(ev.ComponentStarted(component=cid))
    bus.emit(ev.PhaseStarted(component=cid, phase="verify", attempt=1))
    log = root / ".kstrl" / "debug" / RUN_ID / cid / "attempt-1" / "test_suite.log"
    log.parent.mkdir(parents=True)
    log.write_text(GATE_OUTPUT, encoding="utf-8")
    bus.emit(
        ev.VerificationResultEvent(
            component=cid,
            passed=False,
            checks=("test_suite",),
            failures=("Tests failed (exit code 1)",),
            duration_seconds=0.9,
            gate_logs=(str(log),),
        )
    )
    bus.emit(
        ev.PhaseCompleted(
            component=cid, phase="verify", passed=False, detail="Mechanical verification failed"
        )
    )
    bus.emit(ev.ComponentFailed(component=cid, error="Mechanical verification failed"))
    bus.emit(ev.RunCompleted(completed=0, failed=1, skipped=0, duration_seconds=1.0))
    bus.close()
    manifest_file = root / "scripts" / "kstrl" / "manifest.json"
    manifest_file.parent.mkdir(parents=True)
    Manifest(
        version="1",
        spec_file="s",
        project_name="demo",
        base_branch="main",
        single_pr=False,
        run_id=RUN_ID,
        components=[
            Component(
                id=cid,
                title="A",
                description="",
                dependencies=[],
                prd_path="p.json",
                branch_name=f"kstrl/{cid}",
                status=ComponentStatus.FAILED.value,
                failed_phase="verify",
                failed_check="test_suite",
                error="Mechanical verification failed",
            )
        ],
    ).save(manifest_file)
    return manifest_file


def _record(root: Path, manifest_file: Path, **limits: float) -> None:
    every = {**run_limits(FactoryConfig(), TimeoutConfig()), **limits}
    assert write_launch_record(root, RUN_ID, manifest_file, (), every) == []


def _plain(renderable: Any) -> str:
    """What a renderable prints, as text."""
    console = Console(width=200, record=True, file=io.StringIO())
    console.print(renderable)
    return console.export_text()


def _text(widget: Any) -> str:
    """A Static's content as text, a Rich Group included (#433 H3)."""
    return flat(widget)


class TestFailureQueue:
    @pytest.mark.parametrize("size", [(120, 36), (80, 24)])
    async def test_a_retry_this_screen_cannot_carry_names_the_command_and_withholds_r(
        self, tmp_path: Path, no_limit_env: None, size: tuple[int, int]
    ) -> None:
        """G1: no launch record, so four limits are unknown. The row and the
        detail say the CLI carries it, name every option once, and r is off."""
        (tmp_path / "kstrl.toml").write_text("[factory]\nmax_cost_usd = 5\n", encoding="utf-8")
        _failed_run(tmp_path)
        app = _home(tmp_path)
        with patch("kstrl.tui.screens.retry.prepare_retry") as prepare:
            async with app.run_test(size=size) as pilot:
                await mounted(pilot, lambda: app.screen, "#home-runs")
                command = (
                    "ks retry comp-a --max-total-tokens N --max-adversarial-calls N "
                    "--agent-timeout N --component-timeout N"
                )
                app.push_screen(RetryScreen())
                detail = await mounted(pilot, lambda: app.screen, "#retry-detail")
                await settled(
                    pilot, lambda: "ks retry comp-a" in _text(detail), what="the queue detail"
                )
                text = _text(detail)
                table = cast(DataTable[Any], app.screen.query_one("#retry-table"))
                assert command in " ".join(text.split()), text
                assert "left no launch record of 4 run limit(s)" in text, text
                assert "retry available" not in text, text
                # Once, in the output path; the refusal names the run by its short id.
                assert text.count(RUN_ID) == 1, text
                assert str(table.get_cell_at((0, len(table.columns) - 1))) == "retry via CLI"
                assert app.screen.check_action("retry_selected", ()) is False
                await pilot.press("r")
                await drained(pilot, app.screen, what="r to be handled")
                assert isinstance(app.screen, RetryScreen)
                prepare.assert_not_called()

    async def test_a_recorded_limit_the_config_no_longer_sets_is_given_its_value(
        self, tmp_path: Path, no_limit_env: None
    ) -> None:
        """G1: the command carries the value the run ran under, from the
        refusal's data, spelled as the option takes it (no 5e+06)."""
        manifest_file = _failed_run(tmp_path)
        _record(tmp_path, manifest_file, max_total_tokens=5_000_000.0)
        app = _home(tmp_path)
        async with app.run_test(size=(120, 36)) as pilot:
            await mounted(pilot, lambda: app.screen, "#home-runs")
            app.push_screen(RetryScreen())
            detail = await mounted(pilot, lambda: app.screen, "#retry-detail")
            await settled(
                pilot, lambda: "ks retry comp-a" in _text(detail), what="the queue detail"
            )
            text = _text(detail)
            assert "ks retry comp-a --max-total-tokens 5000000" in text, text
            # --max-total-tokens is an int option: click refuses "5000000.0".
            assert "5000000" in text.split(), text
            assert "ran under 1 run limit(s)" in text, text
            assert "N: a value" not in text, text

    async def test_a_retry_the_screen_can_carry_is_still_offered(
        self, tmp_path: Path, no_limit_env: None
    ) -> None:
        """G1's other half: every limit recorded as off, nothing to carry."""
        manifest_file = _failed_run(tmp_path)
        _record(tmp_path, manifest_file)
        app = _home(tmp_path)
        async with app.run_test(size=(120, 36)) as pilot:
            await mounted(pilot, lambda: app.screen, "#home-runs")
            app.push_screen(RetryScreen())
            detail = await mounted(pilot, lambda: app.screen, "#retry-detail")
            await settled(
                pilot,
                lambda: "r works out what a retry would do" in _text(detail),
                what="the queue detail",
            )
            screen = cast(RetryScreen, app.screen)
            await settled(pilot, lambda: screen._carry is not None, what="the carry to be read")
            assert screen.check_action("retry_selected", ()) is True
            await pilot.press("r")
            await settled(
                pilot, lambda: isinstance(app.screen, OptionsModal), what="the confirmation"
            )

    async def test_an_unreadable_carry_withholds_r_and_names_the_cli(
        self, tmp_path: Path, no_limit_env: None
    ) -> None:
        """G1: when the screen cannot tell whether it can carry the retry,
        the row does not say "retry available" and r is withheld."""
        manifest_file = _failed_run(tmp_path)
        _record(tmp_path, manifest_file)
        app = _home(tmp_path)
        with patch("kstrl.tui.screens.retry.read_carry", side_effect=RuntimeError("boom")):
            async with app.run_test(size=(120, 36)) as pilot:
                await mounted(pilot, lambda: app.screen, "#home-runs")
                app.push_screen(RetryScreen())
                detail = await mounted(pilot, lambda: app.screen, "#retry-detail")
                await settled(
                    pilot, lambda: "ks retry comp-a" in _text(detail), what="the queue detail"
                )
                table = cast(DataTable[Any], app.screen.query_one("#retry-table"))
                assert "RuntimeError('boom')" in _text(detail), _text(detail)
                assert str(table.get_cell_at((0, len(table.columns) - 1))) == "retry via CLI"
                assert app.screen.check_action("retry_selected", ()) is False

    async def test_at_80x24_the_table_fits_and_sits_on_the_detail(
        self, tmp_path: Path, no_limit_env: None
    ) -> None:
        """G2: no cell wider than its column, no scrollbar band, no empty
        rows between the table and the detail."""
        _failed_run(tmp_path)
        app = _home(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await mounted(pilot, lambda: app.screen, "#home-runs")
            app.push_screen(RetryScreen())
            detail = await mounted(pilot, lambda: app.screen, "#retry-detail")
            await settled(
                pilot, lambda: "ks retry comp-a" in _text(detail), what="the queue detail"
            )
            table = cast(DataTable[Any], app.screen.query_one("#retry-table"))
            scroll = app.screen.query_one("#retry-detail-scroll")
            await settled(pilot, lambda: scroll.region.height, what="the detail to lay out")
            assert table.virtual_size.width <= table.region.width, table.virtual_size
            assert scroll.region.y == table.region.bottom, (table.region, scroll.region)
            assert "failed" not in [str(c.label) for c in table.columns.values()]

    async def test_o_opens_the_whole_gate_output(self, tmp_path: Path, no_limit_env: None) -> None:
        """Advice 2.5: the queue shows four lines; o shows all forty."""
        _failed_run(tmp_path)
        app = _home(tmp_path)
        async with app.run_test(size=(120, 36)) as pilot:
            await mounted(pilot, lambda: app.screen, "#home-runs")
            app.push_screen(RetryScreen())
            detail = await mounted(pilot, lambda: app.screen, "#retry-detail")
            await settled(pilot, lambda: "test_suite.log" in _text(detail), what="the queue detail")
            await pilot.press("o")
            log = await mounted(pilot, lambda: app.screen, "#gate-log")
            assert isinstance(app.screen, GateLogScreen)
            await settled(pilot, lambda: len(log.lines) >= 40, what="the whole log")  # type: ignore[attr-defined]
            text = "\n".join(strip.text for strip in log.lines)  # type: ignore[attr-defined]
            assert "line 1 of the gate output" in text
            assert "line 40 of the gate output" in text


class TestComponentDetail:
    async def test_a_failed_gate_is_said_once_and_o_opens_its_output(self, tmp_path: Path) -> None:
        """G8: the evidence panel repeated the error the failed gate shows."""
        _failed_run(tmp_path)
        app = _dash(tmp_path, tmp_path / ".kstrl" / "runs" / RUN_ID)
        async with app.run_test(size=(120, 36)) as pilot:
            await mounted(pilot, lambda: app.screen, "#component-table")
            app.push_screen(ComponentScreen("comp-a"))
            evidence = cast(Static, await mounted(pilot, lambda: app.screen, "#evidence"))
            failure = cast(Static, app.screen.query_one("#failure-detail"))
            await settled(pilot, lambda: failure.display, what="the failed gate")
            assert "Mechanical verification failed" not in str(evidence.render())
            assert app.screen.check_action("open_output", ()) is True
            await pilot.press("o")
            await mounted(pilot, lambda: app.screen, "#gate-log")
            assert isinstance(app.screen, GateLogScreen)


class TestDecisionScreens:
    @pytest.mark.parametrize("size", [(120, 36), (80, 24)])
    async def test_the_inbox_speaks_plainly_and_says_what_approve_does_first(
        self, tmp_path: Path, size: tuple[int, int]
    ) -> None:
        """G6 and advice 2.2: no slugs, an age, and the approve line opens
        with the fact that it only records; at 80x24 the detail scrolls to it."""
        _failed_run(tmp_path, cid="client-commands")
        manifest_file = tmp_path / "scripts" / "kstrl" / "manifest.json"
        manifest = Manifest.load(manifest_file)
        comp = manifest.get_component("client-commands")
        assert comp is not None
        comp.status = ComponentStatus.AWAITING_APPROVAL.value
        manifest.save(manifest_file)
        Inbox(tmp_path, InboxConfig()).add(
            ItemKind.MERGE_GATE,
            "approve PR #9 for client-commands",
            component="client-commands",
            dedupe_key=park_dedupe_key("client-commands"),
            evidence={"head_sha": "87c3e2efbe2c", "open_findings": ["IF-1", "IF-2"]},
        )
        app = _home(tmp_path)
        async with app.run_test(size=size) as pilot:
            await mounted(pilot, lambda: app.screen, "#home-runs")
            app.push_screen(InboxScreen())
            table = cast(DataTable[Any], await mounted(pilot, lambda: app.screen, "#inbox-table"))
            detail = cast(Static, app.screen.query_one("#inbox-detail"))
            await settled(pilot, lambda: "what each choice" in flat(detail), what="choices")
            text = flat(detail)
            row = [str(cell) for cell in table.get_row_at(0)]
            assert row[1] == "merge gate" and row[3].endswith("s") and row[4] == "open", row
            for slug in ("merge_gate", "priority=", "head_sha", "['IF-1'"):
                assert slug not in text, (slug, text)
            assert "branch head: 87c3e2efbe2c" in text and "open findings: IF-1, IF-2" in text
            assert re.search(r"a approve:\s+records approval only; nothing merges until", text)
            scroll = app.screen.query_one("#inbox-detail-scroll")
            await settled(pilot, lambda: scroll.region.height, what="the detail to lay out")
            assert scroll.region.bottom <= app.screen.query_one("Footer").region.y

    async def test_the_checkpoint_names_the_decision_and_hides_git_headers(
        self, tmp_path: Path
    ) -> None:
        """G7: no roadmap id, no index line, file names kept, and a lower
        bound says itself (``≥$4.50``) instead of a legend-less ``$4.50+``."""
        from tests.helpers.fake_run import FakeRunSpec, write_fake_run

        run_dir = write_fake_run(tmp_path, FakeRunSpec(include_checkpoint=True, complete=False))
        diff = (
            "diff --git a/src/app.py b/src/app.py\nindex 980e96d..79a0275 100644\n"
            "--- a/src/app.py\n+++ b/src/app.py\n@@ -1,2 +1,2 @@\n-old line\n+new line\n"
        )
        request = PromptRequest(
            kind=PromptKind.CHECKPOINT,
            header="Approve PR creation and merge for comp-c?",
            options=("Approve", "Reject", "Retry"),
            default=0,
            component_id="comp-c",
            checkpoint=CheckpointContext(
                component_id="comp-c",
                diff_excerpt=diff,
                review_findings=(),
                security_findings=(),
                # One call reported nothing: both totals are lower bounds.
                usage=UsageTotals(calls=2, known_calls=1, total_tokens=1200, cost_usd=4.5),
                branch="kstrl/factory/comp-c",
            ),
        )
        app = _dash(tmp_path, run_dir)
        async with app.run_test(size=(120, 36)) as pilot:
            await mounted(pilot, lambda: app.screen, "#component-table")
            app.push_screen(CheckpointModal(request))
            dialog = await mounted(pilot, lambda: app.screen, "#checkpoint-dialog")
            body = "\n".join(str(w.render()) for w in app.screen.query(Static))
            assert "E6" not in str(dialog.border_title)
            assert "index 980e96d" not in body and "+++ b/" not in body, body
            assert "file src/app.py" in body and "+new line" in body, body
            assert "≥$4.50" in body and "$4.50+" not in body, body


class TestPlainValues:
    async def test_config_shows_values_not_python_reprs_and_the_whole_path(
        self, tmp_path: Path
    ) -> None:
        """G4: None, True, False, [] and quoted strings were reprs; the path cut."""
        (tmp_path / "kstrl.toml").write_text("[factory]\nmax_parallel = 2\n", encoding="utf-8")
        app = KstrlTuiApp(
            root_dir=tmp_path,
            mode=Mode.HOME,
            poll_interval=0.05,
            config_report=build_config_report(tmp_path),
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await mounted(pilot, lambda: app.screen, "#home-runs")
            app.push_screen(ConfigScreen())
            table = cast(DataTable[Any], await mounted(pilot, lambda: app.screen, "#config-table"))
            hint = cast(Static, app.screen.query_one("#config-hint"))
            await settled(pilot, lambda: table.row_count, what="the config rows")
            values = {str(table.get_row_at(i)[2]) for i in range(table.row_count)}
            assert not values & {"None", "True", "False", "[]", "''", "'auto'"}, values
            assert {"unset", "yes", "no", "auto"} <= values, values
            await settled(pilot, lambda: hint.region.height, what="the hint to lay out")
            assert str(tmp_path / "kstrl.toml") in str(hint.content).replace("\n", "")
            assert hint.region.height >= 2

    async def test_evolve_says_nothing_recurred_once_and_names_no_field(
        self, tmp_path: Path
    ) -> None:
        """G5: runs_with_referenced and claim_disagreement were raw keys; the
        empty-patterns sentence was on screen twice."""
        journal = tmp_path / ".kstrl" / "evolution.jsonl"
        journal.parent.mkdir(parents=True)
        journal.write_text(
            json.dumps(
                {
                    "event_type": "component_result",
                    "run_id": "r1",
                    "component_id": "a",
                    "findings_summary": {"by_category": {"claim_disagreement": 2}},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        app = _home(tmp_path)
        async with app.run_test(size=(120, 36)) as pilot:
            screen = await evolve_on(app, pilot)
            summary = await mounted(pilot, lambda: screen, "#evolve-summary")
            await settled(
                pilot,
                lambda: "learning readiness" in _plain(cast(Static, summary).content),
                what="readiness",
            )
            empty = screen.query_one("#patterns-empty", Static)
            shown = _plain(cast(Static, summary).content) + _plain(empty.content)
            assert shown.count("No recurring failure patterns") == 1, shown
            assert "runs_with_referenced" not in shown and "claim_disagreement" not in shown
            assert "claim disagreement 2" in shown, shown


class TestDecomposeBoard:
    async def test_the_plan_is_counted_and_the_json_reply_waits_behind_t(
        self, tmp_path: Path
    ) -> None:
        """G3: the plan heading was bare and the transcript showed raw JSON."""
        run_dir = write_fake_decompose_run(tmp_path, components=("database", "api"))
        log = next((run_dir / "components").glob("*/engineer.log"))
        reply = {"components": [{"id": "database"}, {"id": "api"}], "spec_issues": [{}]}
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(reply) + "\n")
        app = _dash(tmp_path, run_dir, kind="decompose")
        async with app.run_test(size=(120, 36)) as pilot:
            title = cast(Static, await mounted(pilot, lambda: app.screen, "#dag-title"))
            assert isinstance(app.screen, DecomposeScreen)
            tail = cast(TranscriptTail, app.screen.query_one(TranscriptTail))
            await settled(pilot, lambda: "components" in str(title.content), what="plan title")
            assert "2 components in" in str(title.content)
            await settled(pilot, lambda: tail.lines_written, what="the saved output")
            assert not tail.display
            await pilot.press("t")
            await settled(pilot, lambda: tail.display, what="t to show the output")
            text = "\n".join(strip.text for strip in tail.lines)
            assert json.dumps(reply) not in text, text
            assert "2 components, 1 spec issues" in text, text
