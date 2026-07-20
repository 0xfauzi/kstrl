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

from ralph_py import events as ev
from ralph_py import reducer
from ralph_py.agents.base import UsageRecord, UsageTotals
from ralph_py.config import RalphConfig
from ralph_py.factory import ComponentResult, FactoryConfig, run_factory
from ralph_py.manifest import Component, Manifest
from ralph_py.observability import (
    latest_run_id,
    read_progress_events,
    summarize_events,
)
from ralph_py.ui.plain import PlainUI
from ralph_py.verify import VerifyConfig


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


def _make_base_config(root_dir: Path) -> RalphConfig:
    return RalphConfig(
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
        "ralph_py.factory._run_component", side_effect=fake_component,
    ), patch("ralph_py.git.get_diff_content", return_value=""):
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
            "ralph_py.factory._run_component", return_value=result,
        ), patch("ralph_py.git.get_diff_content", return_value=""):
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
