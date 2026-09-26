"""#433 increment 2: the operator queue's rules, tested without an app.

Home became an operator queue (needs you, active, delivery, history), the
run overview gained the integration review and a delivery row, retry
became a failure queue with a scope preview, and the inbox says what each
choice does. These tests pin the RULES under those screens; the screens
themselves are driven through Pilot in ``test_tui_433_screens.py``.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path

import pytest

from kstrl import events as ev
from kstrl.inbox import Inbox, InboxConfig, InboxItem, ItemKind
from kstrl.manifest import Component, ComponentStatus, Manifest, park_dedupe_key
from kstrl.reducer import ComponentState, RunState, fold
from kstrl.tui.agent_health import ALIVE, EXITED, UNKNOWN, agent_health
from kstrl.tui.delivery import Delivery, Merge, integration_summary, merge_lines
from kstrl.tui.home_data import RunSummary
from kstrl.tui.home_view import attention_line, fit_rows, history_note
from kstrl.tui.inbox_consequences import consequences
from kstrl.tui.integration_view import (
    FIXED,
    HANDED_OFF,
    OPEN,
    read_integration_review,
)
from kstrl.tui.operator_queue import (
    DECISION,
    FAILURE,
    build_queue,
    failure_queue,
    supersessions,
)
from kstrl.tui.retry_carry import Carry
from kstrl.tui.retry_scope import retry_scope
from kstrl.tui.run_status import component_took
from kstrl.tui.runs import RunRef
from kstrl.tui.serve_view import NOT_RUNNING, RUNNING, read_serve_state
from kstrl.tui.widgets.component_detail import render_component_header
from kstrl.tui.widgets.phase_timeline import render_timeline

NOW = 1_800_000_000.0


def _ref(tmp_path: Path, run_id: str) -> RunRef:
    run_dir = tmp_path / ".kstrl" / "runs" / run_id
    return RunRef(run_id, run_dir, run_dir / "events.jsonl", NOW, True, kind="factory")


def _state(**statuses: str) -> RunState:
    """A finished run whose components end in ``statuses``; a value of
    ``carried`` is a component the run only carried."""
    state = RunState(finished=True)
    for cid, status in statuses.items():
        carried = status == "carried"
        state.components[cid] = ComponentState(
            component_id=cid,
            status="completed" if carried else status,
            carried=carried,
            last_event_ts=NOW,
        )
    return state


def _summary(run_id: str, word: str) -> RunSummary:
    return RunSummary(
        run_id, "failed" if word == "failed" else "done", 0, 0, 1, 0, False, 0.0, word
    )


def _manifest(root: Path, **statuses: str) -> Manifest:
    path = root / "scripts" / "kstrl" / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest = Manifest(
        version="1",
        spec_file="s",
        project_name="p",
        base_branch="main",
        single_pr=False,
        components=[
            Component(
                id=cid,
                title=cid,
                description="",
                dependencies=[],
                prd_path="p.json",
                branch_name=f"kstrl/{cid}",
                status=status,
            )
            for cid, status in statuses.items()
        ],
    )
    manifest.save(path)
    return manifest


class TestSupersession:
    """A failed run superseded by a later run is history and names it."""

    def test_a_failed_run_names_the_later_run_that_ran_its_component(self, tmp_path: Path) -> None:
        old, mid, new = (
            _ref(tmp_path, f"factory-2026092{i}-000000.000000-r{i}") for i in (1, 2, 3)
        )
        states = {
            old.run_id: _state(api="failed"),
            mid.run_id: _state(api="carried"),
            new.run_id: _state(api="completed"),
        }
        summaries = {old.run_id: _summary(old.run_id, "failed")}
        # Newest first, as discovery returns them. The middle run only
        # carried api, so it did not supersede anything.
        assert supersessions([new, mid, old], summaries, states) == {old.run_id: new.run_id}

    def test_a_failure_nothing_later_ran_is_not_superseded(self, tmp_path: Path) -> None:
        old, new = (_ref(tmp_path, f"factory-2026092{i}-000000.000000-r{i}") for i in (1, 2))
        states = {old.run_id: _state(api="failed"), new.run_id: _state(web="completed")}
        summaries = {old.run_id: _summary(old.run_id, "failed")}
        assert supersessions([new, old], summaries, states) == {}


class TestFailureQueue:
    def test_current_failures_come_first_and_superseded_ones_name_their_successor(
        self, tmp_path: Path
    ) -> None:
        manifest = _manifest(tmp_path, api="failed", web="completed")
        old, new = (_ref(tmp_path, f"factory-2026092{i}-000000.000000-r{i}") for i in (1, 2))
        states = {
            old.run_id: _state(web="failed"),
            new.run_id: _state(web="completed", api="failed"),
        }
        entries = failure_queue(manifest, [new, old], states)
        assert [(e.component_id, e.retryable, e.successor) for e in entries] == [
            ("api", True, ""),
            ("web", False, new.run_id),
        ]

    def test_an_older_failure_of_a_still_failed_component_is_not_current(
        self, tmp_path: Path
    ) -> None:
        """Only the NEWEST run that failed a component carries its current failure."""
        manifest = _manifest(tmp_path, api="failed")
        old, new = (_ref(tmp_path, f"factory-2026092{i}-000000.000000-r{i}") for i in (1, 2))
        states = {old.run_id: _state(api="failed"), new.run_id: _state(api="failed")}
        entries = failure_queue(manifest, [new, old], states)
        assert [(e.run_id, e.retryable) for e in entries] == [
            (new.run_id, True),
            (old.run_id, False),
        ]
        assert entries[1].recovery == f"superseded by {new.run_id}"

    def test_a_failure_the_manifest_no_longer_records_is_not_retryable(
        self, tmp_path: Path
    ) -> None:
        manifest = _manifest(tmp_path, api="pending")
        ref = _ref(tmp_path, "factory-20260921-000000.000000-r1")
        [entry] = failure_queue(manifest, [ref], {ref.run_id: _state(api="failed")})
        assert not entry.retryable
        assert entry.recovery == "no longer failed in the manifest"


class TestNeedsYou:
    def test_open_decisions_and_retryable_failures_only(self, tmp_path: Path) -> None:
        """A superseded failure is history, not a row that needs you."""
        _manifest(tmp_path, api="failed", web="completed")
        Inbox(tmp_path, InboxConfig()).add(ItemKind.HALTED_RUN, "halted", dedupe_key="h")
        old, new = (_ref(tmp_path, f"factory-2026092{i}-000000.000000-r{i}") for i in (1, 2))
        states = {
            old.run_id: _state(web="failed"),
            new.run_id: _state(web="completed", api="failed"),
        }
        summaries = {
            old.run_id: _summary(old.run_id, "failed"),
            new.run_id: _summary(new.run_id, "failed"),
        }
        queue = build_queue(tmp_path, [new, old], summaries, states, NOW)
        assert [(row.kind, row.key) for row in queue.needs_you][1:] == [(FAILURE, "api")]
        assert queue.needs_you[0].kind == DECISION
        assert (queue.decisions, queue.failures) == (1, 1)
        assert queue.superseded == {old.run_id: new.run_id}
        assert history_note(old.run_id, "failed", queue) == f"superseded by {new.run_id[-2:]}"
        assert history_note(new.run_id, "failed", queue) == "current: see needs you"

    def test_an_unreadable_inbox_is_not_counted_as_nothing_waiting(self, tmp_path: Path) -> None:
        box = Inbox(tmp_path, InboxConfig())
        box.add(ItemKind.HALTED_RUN, "halted", dedupe_key="h")
        box.path.write_bytes(b"\xe2\x80")
        queue = build_queue(tmp_path, [], {}, {}, NOW)
        assert queue.decisions is None
        from kstrl.tui.home_data import HomeStats

        stats = HomeStats(last=None, inbox_open=queue.decisions, failed_components=queue.failures)
        assert "nothing is waiting" not in attention_line(stats).plain

    def test_an_inbox_config_that_does_not_load_is_not_counted_as_nothing(
        self, tmp_path: Path
    ) -> None:
        """A TOML date where a number belongs makes InboxConfig.load raise
        TypeError; that is "not counted", never zero decisions."""
        (tmp_path / "kstrl.toml").write_text(
            "[inbox]\nsnooze_hours = 1979-05-27\n", encoding="utf-8"
        )
        Inbox(tmp_path, InboxConfig()).add(ItemKind.HALTED_RUN, "halted", dedupe_key="h")
        queue = build_queue(tmp_path, [], {}, {}, NOW)
        assert queue.decisions is None
        assert queue.unreadable == ("inbox",)


def _review(root: Path, run_id: str, number: int, **payload: object) -> None:
    directory = root / ".kstrl" / "runs" / run_id / "integration"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"review-{number}.json").write_text(json.dumps(payload), encoding="utf-8")


def _integration_state(
    root: Path, findings: list[dict[str, object]], fixes: list[dict[str, object]]
) -> None:
    path = root / ".kstrl" / "integration" / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"findings": findings, "fixes": fixes}), encoding="utf-8")


def _finding(fid: str, status: str, text: str, run_id: str = "r") -> dict[str, object]:
    return {
        "id": fid,
        "status": status,
        "kind": "criterion",
        "storyId": "IC1",
        "text": text,
        "locations": [],
        "history": [{"runId": run_id, "event": "opened"}],
    }


RUN = "factory-20260925-000000.000000-e3"


class TestIntegrationReview:
    def test_no_round_means_no_review(self, tmp_path: Path) -> None:
        assert read_integration_review(tmp_path, tmp_path / ".kstrl" / "runs" / RUN) is None

    def test_dispositions_join_by_id_not_by_text(self, tmp_path: Path) -> None:
        """Two findings with the SAME text; only IF-2 is closed and in the fix."""
        _review(
            tmp_path,
            RUN,
            1,
            outcome="open_findings",
            opened=[{"id": "IF-1", "text": "same"}, {"id": "IF-2", "text": "same"}],
        )
        _integration_state(
            tmp_path,
            [_finding("IF-1", "open", "same", RUN), _finding("IF-2", "closed", "same", RUN)],
            [{"id": "integration-fix-1", "findings": ["IF-2"]}],
        )
        review = read_integration_review(tmp_path, tmp_path / ".kstrl" / "runs" / RUN)
        assert review is not None
        by_id = {f.finding_id: (f.disposition, f.detail) for f in review.findings}
        assert by_id == {
            "IF-1": (OPEN, "no fix built yet"),
            "IF-2": (FIXED, "by integration-fix-1"),
        }

    def test_a_closed_finding_names_the_last_fix_that_carried_it(self, tmp_path: Path) -> None:
        _review(tmp_path, RUN, 1, outcome="open_findings", opened=[{"id": "IF-3", "text": "t"}])
        _integration_state(
            tmp_path,
            [_finding("IF-3", "closed", "t", RUN)],
            [
                {"id": "integration-fix-1", "findings": ["IF-3"]},
                {"id": "integration-fix-2", "findings": ["IF-3"]},
            ],
        )
        review = read_integration_review(tmp_path, tmp_path / ".kstrl" / "runs" / RUN)
        assert review is not None and review.findings[0].detail == "by integration-fix-2"

    def test_an_open_finding_says_which_fix_carries_it_and_how_that_ended(
        self, tmp_path: Path
    ) -> None:
        _review(tmp_path, RUN, 1, outcome="open_findings", opened=[{"id": "IF-1", "text": "t"}])
        _integration_state(
            tmp_path,
            [_finding("IF-1", "open", "t", RUN)],
            [{"id": "integration-fix-1", "findings": ["IF-1"]}],
        )
        review = read_integration_review(
            tmp_path, tmp_path / ".kstrl" / "runs" / RUN, {"integration-fix-1": "failed"}
        )
        assert review is not None
        assert (review.findings[0].disposition, review.findings[0].detail) == (
            OPEN,
            "integration-fix-1 carries it (failed)",
        )

    def test_a_finding_the_state_file_lost_is_unknown_never_open(self, tmp_path: Path) -> None:
        _review(tmp_path, RUN, 1, outcome="open_findings", opened=[{"id": "IF-1", "text": "t"}])
        _integration_state(tmp_path, [], [])
        review = read_integration_review(tmp_path, tmp_path / ".kstrl" / "runs" / RUN)
        assert review is not None and review.findings[0].disposition == "unknown"
        assert "state.json does not record it" in review.findings[0].detail

    def test_handed_off_and_verdicts_from_the_newest_round_that_ran(self, tmp_path: Path) -> None:
        criteria = [
            {"storyId": "IC1", "verdict": "fail"},
            {"storyId": "IC2", "verdict": "pass"},
            {"storyId": "IF-9", "verdict": "pass"},
        ]
        _review(
            tmp_path,
            RUN,
            1,
            outcome="open_findings",
            review={"criteria": criteria},
            opened=[{"id": "IF-4", "text": "t"}],
        )
        _review(tmp_path, RUN, 2, outcome="not_run", reason="the last fix failed")
        _integration_state(tmp_path, [_finding("IF-4", "handoff", "t", RUN)], [])
        review = read_integration_review(tmp_path, tmp_path / ".kstrl" / "runs" / RUN)
        assert review is not None
        # IF-9's verdict is a carried finding's, not a criterion.
        assert [(c.story_id, c.verdict) for c in review.criteria] == [
            ("IC1", "fail"),
            ("IC2", "pass"),
        ]
        assert review.findings[0].disposition == HANDED_OFF
        summary = integration_summary(review).plain
        assert "open findings" in summary and "round 2 not run: the last fix failed" in summary
        assert "IF 1 handed off" in summary


class TestDelivery:
    def test_a_delivery_whose_ledger_was_not_read_is_unknown_and_says_why(self) -> None:
        delivery = Delivery("r", (Merge("api", 8, "4ab99ae6"),), "4c4706b3", "", None)
        [line] = merge_lines(delivery, NOW, 200)
        assert line.plain == "merged PR #8 4ab99ae  CI unknown · the CI ledger was not read"

    def test_a_run_that_merged_nothing_makes_no_ci_claim(self) -> None:
        [line] = merge_lines(Delivery("r", (), "", "", None), NOW, 200)
        assert "none recorded in this run" in line.plain and "CI" not in line.plain

    def test_the_reducer_keeps_the_merge_sha_release_ref_and_heartbeat_pid(self) -> None:
        state = fold(
            [
                ev.PrMerged(component="api", pr_number=8, merge_sha="4ab99ae6", ts=NOW),
                ev.WorkerHeartbeat(component="api", pid=4242, ts=NOW + 1),
                ev.RunCompleted(
                    release_ref="4c4706b3", release_withheld="release_disabled", ts=NOW + 2
                ),
            ]
        )
        assert state.components["api"].merge_sha == "4ab99ae6"
        assert state.components["api"].heartbeat_pid == 4242
        assert (state.release_ref, state.release_withheld) == ("4c4706b3", "release_disabled")


class TestAgentHealth:
    def _comp(self, pid: int) -> ComponentState:
        # A heartbeat 5 s old: its pid is still evidence (HEARTBEAT_FRESH_SECONDS).
        return ComponentState(
            component_id="api", status="running", heartbeat_pid=pid, last_heartbeat_ts=NOW - 5
        )

    def test_no_heartbeat_is_process_unknown_never_alive(self, tmp_path: Path) -> None:
        health = agent_health(tmp_path, self._comp(0), NOW, probe=lambda _pid: True)
        assert health.process == UNKNOWN
        assert health.text() == "no output yet · process unknown"

    def test_a_probe_that_finds_no_process_reads_exited(self, tmp_path: Path) -> None:
        assert (
            agent_health(tmp_path, self._comp(4242), NOW, probe=lambda _pid: False).process
            == EXITED
        )
        assert (
            agent_health(tmp_path, self._comp(4242), NOW, probe=lambda _pid: True).process == ALIVE
        )

    def test_output_age_is_the_newest_transcript(self, tmp_path: Path) -> None:
        comp_dir = tmp_path / "components" / "api"
        comp_dir.mkdir(parents=True)
        for name, age in (("engineer.log", 300), ("review.log", 21)):
            (comp_dir / name).write_text("x", encoding="utf-8")
            os.utime(comp_dir / name, (NOW - age, NOW - age))
        health = agent_health(tmp_path, self._comp(4242), NOW, probe=lambda _pid: True)
        assert health.text() == "output 21s ago · worker 4242 alive"


class TestServe:
    def test_an_in_flight_item_joins_the_run_whose_lock_its_lease_pid_holds(
        self, tmp_path: Path
    ) -> None:
        from kstrl.workqueue import Queue

        queue = Queue(tmp_path)
        running = queue.add("spec one", title="one")
        running = queue.lease(running, pid=os.getpid())
        running = queue.start(running)
        queue.add("spec two", title="two")
        (tmp_path / ".kstrl" / "factory.lock").write_text(f"{os.getpid()}\n", encoding="utf-8")
        from kstrl.serve import serve_lock

        with serve_lock(tmp_path):
            state = read_serve_state(tmp_path, "factory-x", factory_lock_held=True)
        assert state is not None and (state.daemon, state.daemon_pid) == (RUNNING, os.getpid())
        assert [(i.title, i.state, i.position, i.run_id) for i in state.items] == [
            ("one", "running", 0, "factory-x"),
            ("two", "queued", 1, ""),
        ]
        # The lock not held: the pid match proves nothing, so no run.
        unheld = read_serve_state(tmp_path, "factory-x", factory_lock_held=False)
        assert unheld is not None and unheld.in_flight[0].run_id == ""

    def test_no_queue_is_no_serve_and_a_released_lock_is_not_running(self, tmp_path: Path) -> None:
        """The daemon never clears its pid. A live pid left in a lock
        nobody holds (this process's own, here) is not a running daemon."""
        from kstrl.serve import serve_lock

        assert read_serve_state(tmp_path, "", False) is None
        with serve_lock(tmp_path):
            pass
        state = read_serve_state(tmp_path, "", False)
        assert state is not None and state.daemon == NOT_RUNNING


class TestRetryScope:
    def _manifest(self, tmp_path: Path) -> Manifest:
        return _manifest(tmp_path, api="failed")

    def test_a_branch_git_could_not_look_up_withholds_the_retry(self, tmp_path: Path) -> None:
        scope = retry_scope(
            tmp_path,
            self._manifest(tmp_path),
            "api",
            carry=lambda: Carry(runs_under="no cost ceiling, 1 in parallel"),
            probe_branch=lambda _root, _branch: None,
        )
        assert scope.unknown == ("branch",)
        assert not scope.offerable

    def test_every_known_part_offers_it_and_a_missing_branch_is_known(self, tmp_path: Path) -> None:
        scope = retry_scope(
            tmp_path,
            self._manifest(tmp_path),
            "api",
            carry=lambda: Carry(runs_under="no cost ceiling, 1 in parallel"),
            probe_branch=lambda _root, _branch: 128,
        )
        assert scope.offerable
        lines = {line.label: line.value for line in scope.lines}
        assert lines["branch"] == "git rev-parse exited 128 for kstrl/api; nothing is deleted"
        assert lines["resets"] == "api to pending"

    def test_a_carry_refusal_withholds_the_retry(self, tmp_path: Path) -> None:
        scope = retry_scope(
            tmp_path,
            self._manifest(tmp_path),
            "api",
            carry=lambda: Carry(refusal="the recorded run's flags cannot be carried"),
            probe_branch=lambda _root, _branch: 0,
        )
        assert not scope.offerable and scope.refusal


class TestInboxChoices:
    def _park(self) -> InboxItem:
        return InboxItem(
            id="i1",
            kind=ItemKind.MERGE_GATE,
            title="t",
            created_at="",
            component="api",
            dedupe_key=park_dedupe_key("api"),
            evidence={"head_sha": "87c3e2efbe2c"},
        )

    def test_a_park_offers_approve_and_reject_only_while_parked(self) -> None:
        parked = consequences(self._park(), ComponentStatus.AWAITING_APPROVAL.value, 24.0)
        assert [name for name, _ in parked.offered] == ["approve", "reject", "snooze"]
        assert "head is still 87c3e2efbe2c" in parked.offered[0][1]
        moved = consequences(self._park(), "completed", 24.0)
        assert [name for name, _ in moved.offered] == ["snooze"]
        assert "api is completed, not parked" in moved.withheld
        unread = consequences(self._park(), None, 24.0)
        assert "effect is unknown" in unread.withheld

    def test_other_kinds_are_record_only_and_snooze_names_its_hours(self) -> None:
        item = dataclasses.replace(self._park(), kind=ItemKind.HALTED_RUN, dedupe_key="h")
        choices = consequences(item, None, 6.0)
        assert "no kstrl step reads a halted run decision" in choices.offered[0][1]
        assert "for 6h" in choices.offered[2][1]


class TestDetailAndBoard:
    def test_took_is_the_phase_durations_when_the_span_is_shorter(self) -> None:
        """Round 1: "took 0s" over a strip reading engineer 312s."""
        comp = ComponentState(
            component_id="c",
            status="failed",
            started_ts=NOW,
            last_event_ts=NOW,
            phase_history=[{"phase": "engineer", "passed": True, "duration_seconds": 312.0}],
        )
        assert component_took(comp) == 312.0
        assert "took 5m" in render_component_header(comp, NOW).plain

    def test_the_phase_strip_labels_each_attempt(self) -> None:
        history = [
            {"phase": p, "passed": p == "engineer", "duration_seconds": 1.0, "attempt": a}
            for a in (1, 2)
            for p in ("engineer", "verify")
        ]
        comp = ComponentState(component_id="c", status="failed", attempt=2, phase_history=history)
        text = render_timeline(comp).plain
        assert text.index("attempt 1") < text.index("engineer") < text.index("attempt 2")

    def test_a_single_attempt_is_not_labelled(self) -> None:
        comp = ComponentState(
            component_id="c",
            status="completed",
            attempt=1,
            phase_history=[{"phase": "engineer", "passed": True, "attempt": 1}],
        )
        assert "attempt" not in render_timeline(comp).plain

    @pytest.mark.parametrize("width", [80, 120])
    def test_queue_rows_fit_their_width_by_the_widest_other_cells(self, width: int) -> None:
        from rich.text import Text

        rows = [
            [Text("●"), Text("ks serve q-249119"), Text("queued #1"), Text("x" * 200)],
            [Text("●"), Text("factory live01"), Text("running"), Text("short")],
        ]
        fitted = fit_rows(rows, width, flex=3)
        total = sum(max(row[i].cell_len for row in fitted) for i in range(4)) + 2 * 4 + 4
        assert total <= width
