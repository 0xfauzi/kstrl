"""Chunk 3 (TUI rewrite): orchestrator dual-write equivalence.

Runs the real run_factory with a stubbed worker and asserts the three
contracts of the dual-write:

1. .ralph/runs/<run_id>/events.jsonl exists and every line is schema 2.
2. The v1 progress.jsonl produced through V1CompatSink is exactly the
   projection of the v2 stream (same events, same order, same data) -
   the load-bearing regression net for every progress.jsonl consumer.
3. fold(events.jsonl) agrees with summarize_events(progress.jsonl) on
   the shared fields, so ralph status can migrate in chunk 8 without
   semantic drift.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from kstrl import events as ev
from kstrl import reducer
from kstrl.agents.base import UsageRecord, UsageTotals
from kstrl.config import KstrlConfig
from kstrl.factory import ComponentResult, FactoryConfig, run_factory
from kstrl.manifest import Component, Manifest
from kstrl.observability import (
    latest_run_id,
    read_progress_events,
    summarize_events,
)
from kstrl.ui.plain import PlainUI
from kstrl.verify import VerifyConfig


def _setup_project(tmp_path: Path, component_ids: list[str]) -> Path:
    ralph_dir = tmp_path / "scripts" / "ralph"
    ralph_dir.mkdir(parents=True, exist_ok=True)
    (ralph_dir / "prompt.md").write_text("test prompt")
    (ralph_dir / "prd.json").write_text(
        '{"branchName": "test", "userStories": []}'
    )
    (tmp_path / "ralph.toml").write_text("[knowledge]\nenabled = false\n")
    for comp_id in component_ids:
        feature_dir = ralph_dir / "feature" / comp_id
        feature_dir.mkdir(parents=True, exist_ok=True)
        (feature_dir / "prd.json").write_text(json.dumps({
            "branchName": "test",
            "userStories": [{
                "id": "US-001", "title": "Test",
                "acceptanceCriteria": ["AC1"],
                "priority": 1, "passes": True, "notes": "",
            }],
        }))
    return tmp_path


def _component(comp_id: str, deps: list[str] | None = None) -> Component:
    return Component(
        comp_id, comp_id.title(), "Desc", deps or [],
        f"scripts/ralph/feature/{comp_id}/prd.json",
        f"ralph/factory/{comp_id}",
    )


def _make_manifest(components: list[Component]) -> Manifest:
    return Manifest(
        version="1", spec_file="spec.md", project_name="test",
        base_branch="main", single_pr=False, components=components,
    )


def _make_base_config(root_dir: Path) -> KstrlConfig:
    return KstrlConfig(
        prompt_file=root_dir / "scripts" / "ralph" / "prompt.md",
        prd_file=root_dir / "scripts" / "ralph" / "prd.json",
        sleep_seconds=0, agent_cmd="echo test",
        ralph_branch="", ralph_branch_explicit=True,
        ui_mode="plain", no_color=True,
    )


def _factory_config(tmp_path: Path, **overrides: Any) -> FactoryConfig:
    defaults: dict[str, Any] = dict(
        use_worktrees=False, create_prs=False, max_parallel=1,
        max_retries=0, retry_delay=0, review_mode="skip",
        verify_config=VerifyConfig(
            test_command="true", typecheck_command="true",
            lint_command="true", check_diff_scope=False,
            check_bad_patterns=False, subprocess_timeout=5.0,
        ),
        progress_log_path=tmp_path / "progress.jsonl",
    )
    defaults.update(overrides)
    return FactoryConfig(**defaults)


def _usage(total: int, cost: float = 0.01) -> UsageTotals:
    totals = UsageTotals()
    totals.add_record(UsageRecord(
        input_tokens=total // 2, output_tokens=total - total // 2,
        total_tokens=total, cost_usd=cost, duration_seconds=1.0,
        source="claude-stream-json",
    ))
    return totals


def _run_stub_factory(root: Path, comp_ids: list[str],
                      success: bool = True) -> Path:
    """Run run_factory with a stubbed worker; returns the root."""
    _setup_project(root, comp_ids)
    manifest = _make_manifest([_component(c) for c in comp_ids])
    config = _factory_config(root)

    def fake_component(comp_id: str, *args: Any, **kwargs: Any) -> ComponentResult:
        return ComponentResult(
            comp_id, success=success, iterations=2, duration_seconds=1.0,
            error=None if success else "stub failure",
            usage=_usage(500),
        )

    with patch(
        "kstrl.factory._run_component", side_effect=fake_component,
    ), patch("kstrl.git.get_diff_content", return_value=""):
        run_factory(
            manifest, config, _make_base_config(root),
            PlainUI(no_color=True, file=io.StringIO()), root,
        )
    return root


def _events_file(root: Path) -> Path:
    run_dirs = sorted((root / ".ralph" / "runs").iterdir())
    assert run_dirs, "no v2 run dir written"
    return run_dirs[-1] / "events.jsonl"


class TestDualWrite:
    def test_v2_stream_written_all_schema_2(self, tmp_path: Path) -> None:
        root = _run_stub_factory(tmp_path, ["comp-a", "comp-b"])
        events_file = _events_file(root)
        lines = [
            json.loads(line)
            for line in events_file.read_text().splitlines() if line.strip()
        ]
        assert lines, "events.jsonl is empty"
        assert all(line["schema"] == 2 for line in lines)
        names = [line["event"] for line in lines]
        assert names[0] == "factory_started"
        assert names[-1] == "factory_completed"
        assert "component_started" in names
        assert "component_usage" in names

    def test_v1_projection_equivalence(self, tmp_path: Path) -> None:
        """progress.jsonl == the v1-named projection of events.jsonl,
        in order, with identical component and data fields."""
        root = _run_stub_factory(tmp_path, ["comp-a", "comp-b"])
        v1 = read_progress_events(root / "progress.jsonl")
        v2 = ev.read_events(_events_file(root))

        v1_names = {e["event"] for e in v1}
        projected = [
            e for e in v2
            if type(e).type in v1_names or type(e).type in {
                "merge_pending", "phase_skipped",
            }
        ]
        # Project v2 events into (event, component, selected-data) and
        # compare against the parsed v1 lines pairwise, in order.
        assert len(v1) == len(projected), (
            f"v1 has {len(v1)} events, v2 projection has {len(projected)}"
        )
        for v1_event, v2_event in zip(v1, projected, strict=True):
            assert v1_event["event"] == type(v2_event).type
            assert v1_event.get("component", "") == v2_event.component
            v1_data = v1_event.get("data", {})
            v2_data = v2_event.to_dict()["data"]
            for key, value in v1_data.items():
                v2_key = "agent_source" if (
                    v1_event["event"] == "adversarial_agent_selected"
                    and key == "source"
                ) else key
                assert v2_data.get(v2_key) == value, (
                    f"{v1_event['event']}.{key}: v1={value!r} "
                    f"v2={v2_data.get(v2_key)!r}"
                )

    def test_fold_agrees_with_summarize_events(self, tmp_path: Path) -> None:
        root = _run_stub_factory(tmp_path, ["comp-a", "comp-b"])
        raw_v1 = read_progress_events(root / "progress.jsonl")
        activity = summarize_events(raw_v1, latest_run_id(raw_v1))
        state = reducer.fold(ev.read_events(_events_file(root)))

        assert state.finished is activity.finished
        assert set(state.components) == set(activity.components)
        for cid, comp_activity in activity.components.items():
            comp = state.components[cid]
            assert comp.phase == comp_activity.phase, cid
            assert comp.usage_calls == comp_activity.usage_calls, cid
            assert comp.total_tokens == comp_activity.total_tokens, cid

    def test_failure_path_events_match(self, tmp_path: Path) -> None:
        root = _run_stub_factory(tmp_path, ["comp-a"], success=False)
        v1_names = [e["event"] for e in
                    read_progress_events(root / "progress.jsonl")]
        v2_names = [type(e).type for e in ev.read_events(_events_file(root))]
        assert "component_failed" in v1_names
        assert v1_names == [n for n in v2_names if n in set(v1_names)]

    def test_progress_log_disabled_suppresses_both(self, tmp_path: Path) -> None:
        """Symmetric opt-out: progress_log_enabled=false writes NEITHER
        progress.jsonl NOR events.jsonl."""
        root = _setup_project(tmp_path, ["comp-a"])
        manifest = _make_manifest([_component("comp-a")])
        config = _factory_config(root, progress_log_enabled=False)
        result = ComponentResult(
            "comp-a", success=True, iterations=1, usage=_usage(10),
        )
        with patch(
            "kstrl.factory._run_component", return_value=result,
        ), patch("kstrl.git.get_diff_content", return_value=""):
            run_factory(
                manifest, config, _make_base_config(root),
                PlainUI(no_color=True, file=io.StringIO()), root,
            )
        assert not (root / "progress.jsonl").exists()
        assert not (root / ".ralph" / "runs").exists()

    def test_journal_offsets_still_bracket_v1_file(self, tmp_path: Path) -> None:
        """Manifest journal_offset_start/end stay pegged to the v1 file."""
        root = _run_stub_factory(tmp_path, ["comp-a"])
        manifest = Manifest.load(root / "scripts" / "ralph" / "manifest.json")
        comp = manifest.get_component("comp-a")
        assert comp is not None
        assert comp.journal_offset_start >= 0
        assert comp.journal_offset_end >= comp.journal_offset_start
        v1_size = (root / "progress.jsonl").stat().st_size
        assert comp.journal_offset_end <= v1_size


def _v2_names_for(events: list[ev.Event], comp: str) -> list[str]:
    out = []
    for e in events:
        if e.component != comp:
            continue
        name = type(e).type
        if name in ("phase_started", "phase_completed"):
            name = f"{name}:{e.to_dict()['data']['phase']}"
        out.append(name)
    return out


class TestSemanticEvents:
    def test_run_plan_follows_factory_started(self, tmp_path: Path) -> None:
        root = _run_stub_factory(tmp_path, ["comp-a", "comp-b"])
        events = ev.read_events(_events_file(root))
        names = [type(e).type for e in events]
        assert names[0] == "factory_started"
        assert names[1] == "run_plan"
        plan = events[1]
        assert isinstance(plan, ev.RunPlan)
        assert [c["id"] for c in plan.components] == ["comp-a", "comp-b"]

    def test_phase_bracket_ordering_success(self, tmp_path: Path) -> None:
        root = _run_stub_factory(tmp_path, ["comp-a"])
        events = ev.read_events(_events_file(root))
        names = _v2_names_for(events, "comp-a")

        def pos(name: str) -> int:
            return names.index(name)

        assert pos("component_started") < pos("phase_started:engineer")
        assert pos("phase_started:engineer") < pos("phase_completed:engineer")
        assert pos("phase_completed:engineer") < pos("phase_started:verify")
        assert pos("phase_started:verify") < pos("verification_result")
        assert pos("verification_result") < pos("phase_completed:verify")
        assert pos("phase_completed:verify") < pos("phase_started:diff")
        assert pos("phase_completed:diff") < pos("phase_started:review")
        assert pos("phase_completed:review") < pos("phase_started:security")
        assert pos("phase_completed:security") < pos("phase_started:distill")
        assert pos("phase_completed:distill") < pos("component_completed")

    def test_failure_path_stops_at_engineer(self, tmp_path: Path) -> None:
        root = _run_stub_factory(tmp_path, ["comp-a"], success=False)
        events = ev.read_events(_events_file(root))
        names = _v2_names_for(events, "comp-a")
        assert "phase_completed:engineer" in names
        engineer_done = [
            e for e in events
            if isinstance(e, ev.PhaseCompleted) and e.phase == "engineer"
        ]
        assert engineer_done[0].passed is False
        assert "stub failure" in engineer_done[0].detail
        assert "phase_started:verify" not in names

    def test_v1_file_has_no_semantic_events(self, tmp_path: Path) -> None:
        root = _run_stub_factory(tmp_path, ["comp-a"])
        v1_names = {e["event"] for e in
                    read_progress_events(root / "progress.jsonl")}
        assert not v1_names & {
            "run_plan", "phase_started", "phase_completed",
            "checkpoint_requested", "checkpoint_resolved", "pr_created",
            "pr_merged", "pr_merge_pending", "distill_result",
            "finding_recorded",
        }

    def test_reducer_sees_explicit_phases(self, tmp_path: Path) -> None:
        root = _run_stub_factory(tmp_path, ["comp-a"])
        state = reducer.fold(ev.read_events(_events_file(root)))
        comp = state.components["comp-a"]
        assert comp.phase_explicit is True
        assert comp.status == "completed"
        assert comp.phase == "done"

    def test_checkpoint_events_auto_resolution(self, tmp_path: Path) -> None:
        """pause_before_pr_merge on a non-TTY: requested then resolved
        with decided_by=auto, and the run proceeds (NO_GH path)."""
        root = _setup_project(tmp_path, ["comp-a"])
        manifest = _make_manifest([_component("comp-a")])
        config = _factory_config(
            root, create_prs=True, pause_before_pr_merge=True,
        )
        result = ComponentResult(
            "comp-a", success=True, iterations=1, usage=_usage(10),
        )
        with patch(
            "kstrl.factory._run_component", return_value=result,
        ), patch("kstrl.git.get_diff_content", return_value=""), patch(
            "kstrl.pr.is_gh_available", return_value=False,
        ):
            run_factory(
                manifest, config, _make_base_config(root),
                PlainUI(no_color=True, file=io.StringIO()), root,
            )
        events = ev.read_events(_events_file(root))
        requested = [e for e in events if isinstance(e, ev.CheckpointRequested)]
        resolved = [e for e in events if isinstance(e, ev.CheckpointResolved)]
        assert len(requested) == 1
        assert requested[0].kind == "pr_merge"
        assert len(resolved) == 1
        assert resolved[0].decision == "not_prompted"
        assert resolved[0].decided_by == "auto"


class TestPhaseTranscripts:
    def test_review_transcript_written(self, tmp_path: Path) -> None:
        """The pipeline threads a transcript writer into the review
        hook; lines the reviewer streams land in review.log."""
        from kstrl.review import ReviewResult

        root = _setup_project(tmp_path, ["comp-a"])
        manifest = _make_manifest([_component("comp-a")])
        config = _factory_config(root, review_mode="advisory")
        result = ComponentResult(
            "comp-a", success=True, iterations=1, usage=_usage(10),
        )

        def fake_review(*args: Any, **kwargs: Any) -> ReviewResult:
            on_line = kwargs.get("on_line")
            assert on_line is not None, "pipeline must pass a transcript writer"
            on_line("reviewer line one")
            on_line("reviewer line two")
            return ReviewResult(passed=True, mode="advisory")

        with patch(
            "kstrl.factory._run_component", return_value=result,
        ), patch("kstrl.git.get_diff_content", return_value="+x\n"), patch(
            "kstrl.factory.run_review", side_effect=fake_review,
        ):
            run_factory(
                manifest, config, _make_base_config(root),
                PlainUI(no_color=True, file=io.StringIO()), root,
            )

        run_dir = _events_file(root).parent
        review_log = run_dir / "components" / "comp-a" / "review.log"
        assert review_log.exists()
        assert review_log.read_text() == "reviewer line one\nreviewer line two\n"

    def test_run_review_streams_lines(self, tmp_path: Path) -> None:
        """review.run_review forwards each streamed agent line to on_line."""
        from kstrl.review import ReviewMode, run_review
        from kstrl.ui.plain import PlainUI as _PlainUI
        from kstrl.verify import VerificationResult

        class _Agent:
            @property
            def name(self) -> str:
                return "fake"

            def run(self, prompt: str, cwd: Path | None = None,
                    timeout: float | None = None) -> Any:
                yield from ["line-a", "line-b"]

            @property
            def final_message(self) -> str | None:
                return None

        (tmp_path / "prd.json").write_text(
            '{"branchName": "b", "userStories": []}'
        )
        seen: list[str] = []
        run_review(
            _Agent(), tmp_path / "prd.json", tmp_path, "main",
            VerificationResult(passed=True, checks=[]),
            ReviewMode.ADVISORY, _PlainUI(no_color=True, file=io.StringIO()),
            diff_content="+x\n",
            on_line=seen.append,
        )
        assert seen == ["line-a", "line-b"]


class TestWorkerChannel:
    def _worker_args(self, root: Path, events_dir: Path | None,
                     agent_cmd: str) -> dict[str, Any]:
        from kstrl.factory import _run_component  # noqa: F401 - existence
        _setup_project(root, ["comp-a"])
        return dict(
            component_id="comp-a",
            prd_path_str="scripts/ralph/feature/comp-a/prd.json",
            worktree_path_str=str(root),
            root_dir_str=str(root),
            prompt_file_str="scripts/ralph/prompt.md",
            agent_cmd=agent_cmd,
            model=None, reasoning=None, agent_type=None,
            sleep_seconds=0.0,
            max_iterations=1,
            events_dir_str=str(events_dir) if events_dir else None,
            run_id="run-w",
            redirect_output=False,  # NEVER dup2 inside the test process
        )

    def test_worker_writes_events_and_transcript(
        self, tmp_path: Path, capfd: Any,
    ) -> None:
        from kstrl.factory import _run_component

        events_dir = tmp_path / ".ralph" / "runs" / "run-w"
        result = _run_component(**self._worker_args(
            tmp_path, events_dir, "echo engineer-output-line",
        ))
        assert result.component_id == "comp-a"

        comp_dir = events_dir / "components" / "comp-a"
        worker_events = ev.read_events(comp_dir / "engineer.jsonl")
        assert worker_events, "worker emitted no events"
        assert all(e.source == "worker" for e in worker_events)
        assert all(e.component == "comp-a" for e in worker_events)
        names = [type(e).type for e in worker_events]
        assert "iteration_started" in names
        assert "iteration_completed" in names
        assert "log" in names  # bridge narration (banner, sections)

        transcript = (comp_dir / "engineer.log").read_text()
        assert "engineer-output-line" in transcript

        # The bridge path writes NOTHING to the inherited terminal.
        out, err = capfd.readouterr()
        assert out == ""
        assert err == ""

    def test_worker_without_events_dir_keeps_legacy_stderr(
        self, tmp_path: Path, capfd: Any,
    ) -> None:
        from kstrl.factory import _run_component

        _run_component(**self._worker_args(
            tmp_path, None, "echo legacy-line",
        ))
        _, err = capfd.readouterr()
        assert "legacy-line" in err  # PlainUI on stderr, as before

    def test_setup_failure_does_not_start_heartbeat(self, tmp_path: Path) -> None:
        from kstrl.factory import _run_component

        with patch(
            "kstrl.agents.get_agent", side_effect=RuntimeError("no agent"),
        ), patch("kstrl.factory._start_heartbeat") as start_heartbeat:
            try:
                _run_component(**self._worker_args(
                    tmp_path, tmp_path / ".ralph" / "runs" / "run-w", "bad",
                ))
            except RuntimeError as exc:
                assert str(exc) == "no agent"
            else:  # pragma: no cover - get_agent must fail
                raise AssertionError("expected get_agent failure")
        start_heartbeat.assert_not_called()

    def test_agent_crash_closes_iteration_event(self, tmp_path: Path) -> None:
        from kstrl.factory import _run_component

        class CrashingAgent:
            usage_records: list[Any] = []

            @property
            def name(self) -> str:
                return "crashing"

            def run(self, *args: Any, **kwargs: Any) -> Any:
                raise RuntimeError("agent crashed")

            @property
            def final_message(self) -> str | None:
                return None

        events_dir = tmp_path / ".ralph" / "runs" / "run-w"
        with patch("kstrl.agents.get_agent", return_value=CrashingAgent()):
            result = _run_component(**self._worker_args(
                tmp_path, events_dir, "crash",
            ))
        assert result.success is False
        assert result.error == "agent crashed"
        events = ev.read_events(
            events_dir / "components" / "comp-a" / "engineer.jsonl"
        )
        starts = [e for e in events if isinstance(e, ev.IterationStarted)]
        completed = [e for e in events if isinstance(e, ev.IterationCompleted)]
        assert len(starts) == len(completed) == 1
        assert completed[0].completed is False

    def test_heartbeat_thread_emits(self) -> None:
        import time as _time

        from kstrl.factory import _start_heartbeat

        captured: list[ev.Event] = []
        bus = ev.EventBus(
            ev.CallbackSink(captured.append),
            run_id="run-h", source="worker", component="comp-a",
        )
        stop = _start_heartbeat(bus, interval=0.01)
        _time.sleep(0.08)
        stop()
        beats = [e for e in captured if isinstance(e, ev.WorkerHeartbeat)]
        assert beats, "no heartbeat emitted"
        assert beats[0].pid > 0
        count_after_stop = len(beats)
        _time.sleep(0.05)
        assert len([
            e for e in captured if isinstance(e, ev.WorkerHeartbeat)
        ]) == count_after_stop  # stopped means stopped

    def test_inline_factory_tees_live_lines_and_persists_files(
        self, tmp_path: Path,
    ) -> None:
        """max_parallel=1 runs the REAL worker in-process: the parent UI
        still shows live AI lines, while events + transcript land in the
        run dir (the same layout as pool mode)."""
        root = _setup_project(tmp_path, ["comp-a"])
        manifest = _make_manifest([_component("comp-a")])
        config = _factory_config(root)
        ui_buffer = io.StringIO()

        marker = "<promise>COMPLETE</promise>"
        base = _make_base_config(root)
        base.agent_cmd = f"echo '{marker}'"
        with patch("kstrl.git.get_diff_content", return_value=""):
            result = run_factory(
                manifest, config, base,
                PlainUI(no_color=True, file=ui_buffer), root,
            )
        # The echo agent emits the completion marker: component succeeds.
        assert "comp-a" in result.completed

        # Live tee: the AI line reached the parent UI.
        assert marker in ui_buffer.getvalue()

        run_dir = _events_file(root).parent
        comp_dir = run_dir / "components" / "comp-a"
        assert (comp_dir / "engineer.log").exists()
        assert marker in (comp_dir / "engineer.log").read_text()
        worker_events = ev.read_events(comp_dir / "engineer.jsonl")
        assert any(
            isinstance(e, ev.IterationCompleted) and e.completed
            for e in worker_events
        )

        # The reducer merges worker files into the run view.
        state, _ = reducer.load_run_state(root)
        assert state.components["comp-a"].iteration == 1
