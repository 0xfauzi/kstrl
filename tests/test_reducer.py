"""Chunk 2 (TUI rewrite): the RunState reducer over real run directories.

``TestLoadRunState`` writes v2 ``events.jsonl`` through ``EventBus`` and
v1 ``progress.jsonl`` through ``ProgressLog`` under ``tmp_path`` and reads
them back through ``load_run_state`` and ``run_dirs_newest_first``: v2
wins over v1, the newest run directory is picked, an explicit run id is
honoured, a torn tail is skipped and a read error is not swallowed. Two
more tests fold a real ``progress.jsonl`` through ``upconvert_v1``: the
state agrees with ``summarize_events``, and ``budget_coverage`` reaches
the same state through the v1 path (#181 parity). The in-memory lifecycle,
decompose-vocabulary, fold/apply-equivalence and per-axis coverage folds
were removed; ``tests/test_carried_component_state.py`` and
``tests/test_run_record_version.py`` reduce a real ``events.jsonl``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kstrl import events as ev
from kstrl import reducer
from kstrl.observability import ProgressLog, read_progress_events, summarize_events


class TestV1Upconversion:
    def _v1_events(self, path: Path) -> list[dict[str, Any]]:
        log = ProgressLog(path, run_id="run-9")
        log.factory_started("proj", 2)
        log.component_started("a")
        log.component_usage(
            "a",
            "engineer",
            {
                "calls": 2,
                "known_calls": 1,
                "unreported_calls": 1,
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "total_tokens": 15,
                "cost_usd": 0.05,
                "duration_seconds": 3.0,
            },
        )
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


class TestLoadRunState:
    def _write_v2(self, root: Path, run_id: str, project: str) -> None:
        paths = ev.RunPaths.for_run(root, run_id)
        bus = ev.EventBus(ev.JsonlSink(paths.events_file), run_id=run_id)
        bus.emit(ev.RunStarted(project=project, components=1))
        bus.emit(ev.ComponentStarted(component="a"))
        worker = ev.EventBus(
            ev.JsonlSink(paths.engineer_events("a")),
            run_id=run_id,
            source="worker",
            component="a",
        )
        worker.emit(ev.IterationStarted(iteration=1, max_iterations=5))
        bus.emit(ev.RunCompleted(completed=1, failed=0, skipped=0))
        bus.close()
        worker.close()

    def _write_v1(self, root: Path, run_id: str, project: str) -> None:
        log = ProgressLog(root / ".kstrl" / "progress.jsonl", run_id=run_id)
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
        state, _ = reducer.load_run_state(tmp_path, "factory-20260720-000001.000000-x")
        assert state.project == "older"

    def test_neither(self, tmp_path: Path) -> None:
        state, source = reducer.load_run_state(tmp_path)
        assert source is None
        assert state.components == {}

    def test_run_dirs_newest_first_orders_and_agrees_with_the_fold(
        self,
        tmp_path: Path,
    ) -> None:
        """R10.4 exposed this listing so a caller can scan a run's raw
        stream instead of folding it. The two must not be able to
        disagree about WHICH run is newest."""
        self._write_v2(tmp_path, "factory-20260720-000001.000000-x", "older")
        self._write_v2(tmp_path, "factory-20260720-000009.000000-x", "newer")

        dirs = reducer.run_dirs_newest_first(tmp_path)
        _, source = reducer.load_run_state(tmp_path)

        assert [d.name for d in dirs] == [
            "factory-20260720-000009.000000-x",
            "factory-20260720-000001.000000-x",
        ]
        assert source is not None and dirs[0] == source.parent

    def test_run_dirs_newest_first_is_empty_without_runs(
        self,
        tmp_path: Path,
    ) -> None:
        assert reducer.run_dirs_newest_first(tmp_path) == []

    def test_run_dirs_newest_first_includes_a_run_with_no_events(
        self,
        tmp_path: Path,
    ) -> None:
        """A run with [factory] progress_log_enabled = false writes its
        accounting files and no events. Filtering it out would hide the
        newest run behind an older one, and safe mode would report a
        stale verdict as though it were current."""
        self._write_v2(tmp_path, "factory-20260720-000001.000000-x", "real")
        (tmp_path / ".kstrl" / "runs" / "factory-20260720-000009.000000-x").mkdir(parents=True)

        dirs = reducer.run_dirs_newest_first(tmp_path)

        assert [d.name for d in dirs] == [
            "factory-20260720-000009.000000-x",
            "factory-20260720-000001.000000-x",
        ]
        # The fold still sees only the run that has a stream.
        assert reducer._v2_run_dirs(tmp_path) == [
            tmp_path / ".kstrl" / "runs" / "factory-20260720-000001.000000-x",
        ]

    def test_a_tie_resolves_the_same_way_it_did_before(
        self,
        tmp_path: Path,
    ) -> None:
        """Two kinds can share a run_sort_key (same stamp, same nonce).
        sorted(reverse=True) keeps ties in their original order, so
        reversing that list FLIPS them - and load_run_state takes the
        last element, so it would pick the other run. Pin the old order
        rather than change working behaviour inside a refactor."""
        self._write_v2(tmp_path, "factory-20260720-000001.000000-x", "fac")
        self._write_v2(tmp_path, "decompose-20260720-000001.000000-x", "dec")

        ordered = reducer._v2_run_dirs(tmp_path)
        listed = [d for d in (tmp_path / ".kstrl" / "runs").iterdir() if d.is_dir()]

        assert [d.name for d in ordered] == [d.name for d in listed]

    def test_run_dirs_newest_first_does_not_swallow_a_read_error(
        self,
        tmp_path: Path,
    ) -> None:
        """_v2_run_dirs answers an unreadable runs/ with "no runs", which
        reads as "nothing was skipped". This listing must not."""
        runs = tmp_path / ".kstrl" / "runs"
        runs.parent.mkdir(parents=True, exist_ok=True)
        runs.write_text("i am a file", encoding="utf-8")

        with pytest.raises(OSError):
            reducer.run_dirs_newest_first(tmp_path)
        assert reducer._v2_run_dirs(tmp_path) == []  # unchanged contract

    def test_torn_tail_in_run_dir(self, tmp_path: Path) -> None:
        run_id = "factory-20260720-000003.000000-x"
        self._write_v2(tmp_path, run_id, "proj")
        events_file = ev.RunPaths.for_run(tmp_path, run_id).events_file
        with open(events_file, "a") as f:
            f.write(json.dumps({"event": "log"})[:9])  # torn, no newline
        state, _ = reducer.load_run_state(tmp_path)
        assert state.project == "proj"  # reader skipped the torn tail


class TestPerAxisCoverageFold:
    """R8 review finding 1: the reducer dropped every per-axis signal.

    ``apply`` folded ``cost_usd`` and ``unreported_calls`` but not
    ``cost_calls`` / ``token_calls``, and returned early for the
    run-scoped ``budget_coverage`` event - so the dashboard held no
    state that could distinguish "this cost total is a measurement" from
    "this cost total covers one role".
    """

    def test_budget_coverage_survives_the_v1_upconversion(
        self,
        tmp_path: Path,
    ) -> None:
        """progress.jsonl is the other sink (#181 parity): the same fact
        must reach the same state through the v1 path."""
        log = ProgressLog(tmp_path / "progress.jsonl", run_id="run-9")
        log.budget_coverage(
            ceiling="max_cost_usd",
            axis="cost",
            calls=2,
            covered_calls=1,
            uncovered_calls=1,
            uncovered_tokens=40_000,
            uncovered_roles=["review"],
            detail="cost coverage is PARTIAL",
        )
        log.component_usage(
            "a",
            "review",
            {
                "calls": 1,
                "known_calls": 1,
                "token_calls": 1,
                "cost_calls": 0,
                "unreported_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "total_tokens": 500,
                "cost_usd": 0.0,
                "duration_seconds": 1.0,
            },
        )
        raw = read_progress_events(tmp_path / "progress.jsonl")
        state = reducer.fold(
            [reducer.upconvert_v1(e) for e in raw],
            run_id="run-9",
        )
        assert state.token_calls == 1
        assert state.cost_calls == 0
        gap = state.coverage_gaps.get("cost")
        assert gap is not None
        assert gap.uncovered_roles == ("review",)
