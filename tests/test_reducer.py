"""Chunk 2 (TUI rewrite): RunState reducer, v1 up-conversion, disk loading."""

from __future__ import annotations

import json
import random
import time
from pathlib import Path
from typing import Any

from ralph_py import events as ev
from ralph_py import reducer
from ralph_py.observability import ProgressLog, read_progress_events, summarize_events


def _stamped(events: list[ev.Event], run_id: str = "run-1") -> list[ev.Event]:
    bus = ev.EventBus(run_id=run_id)
    return [bus.emit(e) for e in events]


class TestLifecycleFold:
    def test_component_lifecycle_v2(self) -> None:
        events = _stamped([
            ev.RunStarted(project="proj", components=2),
            ev.RunPlan(
                components=(
                    {"id": "a", "title": "Alpha", "deps": []},
                    {"id": "b", "title": "Beta", "deps": ["a"]},
                ),
                max_total_tokens=1000, max_adversarial_calls=7,
            ),
            ev.ComponentStarted(component="a"),
            ev.PhaseStarted(component="a", phase="engineer", attempt=1),
            ev.IterationStarted(component="a", iteration=1, max_iterations=10),
            ev.IterationCompleted(component="a", iteration=1, duration_seconds=5.0),
            ev.PhaseCompleted(component="a", phase="engineer", passed=True),
            ev.PhaseStarted(component="a", phase="review", attempt=1),
            ev.ComponentCompleted(component="a", duration_seconds=60.0, iterations=3),
            ev.RunCompleted(completed=1, failed=0, skipped=0),
        ])
        state = reducer.fold(events, run_id="run-1")
        assert state.project == "proj"
        assert state.finished is True
        assert state.plan_order == ["a", "b"]
        assert state.max_total_tokens == 1000
        assert state.max_adversarial_calls == 7
        a = state.components["a"]
        assert a.title == "Alpha"
        assert a.status == "completed"
        assert a.phase == "done"
        assert a.iteration == 3
        assert a.max_iterations == 10
        b = state.components["b"]
        assert b.status == "pending"
        assert b.deps == ("a",)

    def test_explicit_phase_beats_inference(self) -> None:
        events = _stamped([
            ev.ComponentStarted(component="a"),
            ev.PhaseStarted(component="a", phase="verify", attempt=1),
            # v1-style usage event would infer "engineer"; must NOT win.
            ev.ComponentUsage(component="a", phase="engineer", calls=1),
        ])
        state = reducer.fold(events)
        assert state.components["a"].phase == "verify"
        assert state.components["a"].status == "verifying"

    def test_v1_inference_without_phase_events(self) -> None:
        events = _stamped([
            ev.ComponentStarted(component="a"),
            ev.VerificationResultEvent(component="a", passed=True),
            ev.ReviewResultEvent(component="a", passed=True, mode="security-hard"),
        ])
        state = reducer.fold(events)
        assert state.components["a"].phase == "security"

    def test_failure_and_retry(self) -> None:
        events = _stamped([
            ev.ComponentStarted(component="a"),
            ev.ComponentFailed(component="a", error="tests failed"),
            ev.ComponentRetrying(component="a", attempt=2, reason="tests failed"),
        ])
        state = reducer.fold(events)
        a = state.components["a"]
        assert a.status == "running"  # back in flight after retry
        assert a.attempt == 2
        assert a.error == "tests failed"

    def test_usage_accumulates_component_and_run(self) -> None:
        events = _stamped([
            ev.ComponentUsage(component="a", phase="engineer", calls=2,
                              unreported_calls=1, total_tokens=100, cost_usd=0.5),
            ev.ComponentUsage(component="a", phase="review", calls=1,
                              total_tokens=50, cost_usd=0.25),
            ev.ComponentUsage(component="b", phase="engineer", calls=1,
                              total_tokens=10, cost_usd=0.1),
        ])
        state = reducer.fold(events)
        assert state.components["a"].total_tokens == 150
        assert state.components["a"].usage_calls == 3
        assert state.total_tokens == 160
        assert state.cost_usd == 0.85
        assert state.unreported_calls == 1

    def test_pr_states_and_checkpoint(self) -> None:
        events = _stamped([
            ev.CheckpointRequested(component="a", kind="checkpoint", question="ok?"),
        ])
        state = reducer.fold(events)
        assert state.components["a"].checkpoint_open == "checkpoint"
        more = _stamped([
            ev.CheckpointResolved(component="a", kind="checkpoint",
                                  decision="approved", decided_by="operator"),
            ev.PrCreated(component="a", pr_number=5, pr_url="u5"),
            ev.PrMerged(component="a", pr_number=5, pr_url="u5"),
        ])
        for event in more:
            reducer.apply(state, event)
        a = state.components["a"]
        assert a.checkpoint_open == ""
        assert a.pr_state == "merged"
        assert a.pr_number == 5

    def test_merge_pending_v1_twin_does_not_regress_v2(self) -> None:
        events = _stamped([
            ev.PrMergePending(component="a", pr_url="u", error="pending"),
            ev.MergePendingV1(component="a", pr_url="", error=""),
        ])
        state = reducer.fold(events)
        a = state.components["a"]
        assert a.pr_state == "merge_pending"
        assert a.pr_url == "u"
        assert a.status == "merge_pending"

    def test_findings_bounded(self) -> None:
        events = _stamped([
            ev.FindingRecorded(component="a", phase="review", category="c",
                               severity="fail", location=f"f.py:{i}",
                               explanation="x", attempt=1)
            for i in range(reducer.MAX_RECENT_FINDINGS + 10)
        ])
        state = reducer.fold(events)
        a = state.components["a"]
        assert a.findings_count == reducer.MAX_RECENT_FINDINGS + 10
        assert len(a.recent_findings) == reducer.MAX_RECENT_FINDINGS
        assert a.recent_findings[-1]["location"] == (
            f"f.py:{reducer.MAX_RECENT_FINDINGS + 9}"
        )

    def test_heartbeat_freshness(self) -> None:
        events = _stamped([
            ev.WorkerHeartbeat(component="a", pid=1, elapsed_seconds=10.0),
        ])
        state = reducer.fold(events)
        assert state.components["a"].last_heartbeat_ts > 0

    def test_contract_result_attributed_to_breaker(self) -> None:
        events = _stamped([
            ev.ContractResult(tier=1, passed=False, breaker="a",
                              duration_seconds=3.0),
        ])
        state = reducer.fold(events)
        assert "contract failed at tier 1" in state.components["a"].error

    def test_unknown_events_counted_not_fatal(self) -> None:
        events: list[ev.Event] = list(_stamped([
            ev.ComponentStarted(component="a"),
        ]))
        events.append(ev.event_from_dict({
            "event": "from_the_future", "ts": time.time(),
            "component": "a", "data": {"x": 1},
        }))
        events.extend(_stamped([
            ev.ComponentCompleted(component="a", iterations=1),
        ]))
        state = reducer.fold(events)
        assert state.unknown_events == 1
        assert state.components["a"].status == "completed"

    def test_run_id_filter(self) -> None:
        mixed = _stamped([ev.ComponentStarted(component="a")], run_id="r1") + \
            _stamped([ev.ComponentStarted(component="b")], run_id="r2")
        state = reducer.fold(mixed, run_id="r1")
        assert "a" in state.components
        assert "b" not in state.components


class TestFoldApplyEquivalence:
    def test_fold_equals_incremental_apply_at_random_splits(self) -> None:
        rng = random.Random(7)
        pool: list[ev.Event] = []
        comps = ["a", "b", "c"]
        for _ in range(300):
            c = rng.choice(comps)
            pool.append(rng.choice([
                ev.ComponentStarted(component=c),
                ev.PhaseStarted(component=c, phase=rng.choice(
                    ["engineer", "verify", "review"]), attempt=1),
                ev.ComponentUsage(component=c, phase="engineer", calls=1,
                                  total_tokens=rng.randint(1, 100)),
                ev.FindingRecorded(component=c, phase="review", category="x",
                                   severity="fail", location="f:1",
                                   explanation="e", attempt=1),
                ev.ComponentCompleted(component=c, iterations=rng.randint(1, 5)),
                ev.ComponentFailed(component=c, error="err"),
                ev.WorkerHeartbeat(component=c, pid=1, elapsed_seconds=1.0),
            ]))
        stamped = _stamped(pool)
        folded = reducer.fold(stamped, run_id="run-1")
        incremental = reducer.RunState(run_id="run-1")
        for event in stamped:
            reducer.apply(incremental, event)
        assert incremental == folded


class TestV1Upconversion:
    def _v1_events(self, path: Path) -> list[dict[str, Any]]:
        log = ProgressLog(path, run_id="run-9")
        log.factory_started("proj", 2)
        log.component_started("a")
        log.component_usage("a", "engineer", {
            "calls": 2, "known_calls": 1, "unreported_calls": 1,
            "input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 0,
            "cache_creation_tokens": 0, "total_tokens": 15,
            "cost_usd": 0.05, "duration_seconds": 3.0,
        })
        log.verification_result("a", True, ["tests"], [], 2.0)
        log.review_result("a", True, "standard", 0, 1, 30.0)
        log.component_retrying("a", 2, "flaky")
        log.component_completed("a", 100.0, 4)
        log.component_started("b")
        log.component_failed("b", "boom")
        log.factory_completed(1, 1, 0, 200.0)
        return read_progress_events(path)

    def test_equivalence_with_summarize_events(self, tmp_path: Path) -> None:
        raw = self._v1_events(tmp_path / "progress.jsonl")
        activity = summarize_events(raw, "run-9")
        state = reducer.fold([reducer.upconvert_v1(e) for e in raw], run_id="run-9")

        assert state.finished is activity.finished
        assert set(state.components) == set(activity.components)
        for cid, comp_activity in activity.components.items():
            comp = state.components[cid]
            assert comp.phase == comp_activity.phase, cid
            assert comp.attempt == comp_activity.attempt, cid
            assert comp.usage_calls == comp_activity.usage_calls, cid
            assert comp.unreported_calls == comp_activity.unreported_calls, cid
            assert comp.total_tokens == comp_activity.total_tokens, cid
            assert comp.cost_usd == comp_activity.cost_usd, cid
            assert comp.last_event == comp_activity.last_event, cid

    def test_adversarial_source_key_renamed(self) -> None:
        event = reducer.upconvert_v1({
            "ts": "2026-07-20T12:00:00Z", "event": "adversarial_agent_selected",
            "run_id": "r", "data": {"phase": "review", "source": "config",
                                    "identity": "i", "agent_type": "t",
                                    "model": "m", "homogeneous": False},
        })
        assert isinstance(event, ev.AdversarialAgentSelected)
        assert event.agent_source == "config"

    def test_junk_never_raises(self) -> None:
        for obj in [{}, {"event": None}, {"event": 7, "ts": object()},
                    {"event": "component_failed", "data": "not-a-dict"}]:
            out = reducer.upconvert_v1(obj)  # type: ignore[arg-type]
            assert isinstance(out, ev.Event)


class TestLoadRunState:
    def _write_v2(self, root: Path, run_id: str, project: str) -> None:
        paths = ev.RunPaths.for_run(root, run_id)
        bus = ev.EventBus(ev.JsonlSink(paths.events_file), run_id=run_id)
        bus.emit(ev.RunStarted(project=project, components=1))
        bus.emit(ev.ComponentStarted(component="a"))
        worker = ev.EventBus(
            ev.JsonlSink(paths.engineer_events("a")),
            run_id=run_id, source="worker", component="a",
        )
        worker.emit(ev.IterationStarted(iteration=1, max_iterations=5))
        bus.emit(ev.RunCompleted(completed=1, failed=0, skipped=0))
        bus.close()
        worker.close()

    def _write_v1(self, root: Path, run_id: str, project: str) -> None:
        log = ProgressLog(root / ".ralph" / "progress.jsonl", run_id=run_id)
        log.factory_started(project, 1)
        log.component_started("z")

    def test_v2_only(self, tmp_path: Path) -> None:
        self._write_v2(tmp_path, "factory-20260720-000001.000000-x", "v2proj")
        state, source = reducer.load_run_state(tmp_path)
        assert source is not None and source.name == "events.jsonl"
        assert state.project == "v2proj"
        # worker events merged in
        assert state.components["a"].iteration == 1

    def test_v1_only(self, tmp_path: Path) -> None:
        self._write_v1(tmp_path, "run-v1", "v1proj")
        state, source = reducer.load_run_state(tmp_path)
        assert source is not None and source.name == "progress.jsonl"
        assert state.project == "v1proj"
        assert "z" in state.components

    def test_both_v2_wins(self, tmp_path: Path) -> None:
        self._write_v1(tmp_path, "run-v1", "v1proj")
        self._write_v2(tmp_path, "factory-20260720-000002.000000-x", "v2proj")
        state, source = reducer.load_run_state(tmp_path)
        assert source is not None and source.name == "events.jsonl"
        assert state.project == "v2proj"

    def test_newest_run_dir_selected(self, tmp_path: Path) -> None:
        self._write_v2(tmp_path, "factory-20260720-000001.000000-x", "older")
        self._write_v2(tmp_path, "factory-20260720-000009.000000-x", "newer")
        state, _ = reducer.load_run_state(tmp_path)
        assert state.project == "newer"

    def test_explicit_run_id(self, tmp_path: Path) -> None:
        self._write_v2(tmp_path, "factory-20260720-000001.000000-x", "older")
        self._write_v2(tmp_path, "factory-20260720-000009.000000-x", "newer")
        state, _ = reducer.load_run_state(
            tmp_path, "factory-20260720-000001.000000-x")
        assert state.project == "older"

    def test_neither(self, tmp_path: Path) -> None:
        state, source = reducer.load_run_state(tmp_path)
        assert source is None
        assert state.components == {}

    def test_torn_tail_in_run_dir(self, tmp_path: Path) -> None:
        run_id = "factory-20260720-000003.000000-x"
        self._write_v2(tmp_path, run_id, "proj")
        events_file = ev.RunPaths.for_run(tmp_path, run_id).events_file
        with open(events_file, "a") as f:
            f.write(json.dumps({"event": "log"})[:9])  # torn, no newline
        state, _ = reducer.load_run_state(tmp_path)
        assert state.project == "proj"  # reader skipped the torn tail
