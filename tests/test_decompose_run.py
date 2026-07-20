"""TUI surface C4: decompose as an event-stream run."""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from kstrl.commandrun import open_command_run
from kstrl.decompose import SpecBlockerError, decompose_spec
from kstrl.reducer import load_run_state
from kstrl.ui.plain import PlainUI
from tests.test_decompose import VALID_DECOMPOSE_OUTPUT, MockDecomposeAgent

MINOR_ISSUE_OUTPUT = json.dumps({
    **json.loads(VALID_DECOMPOSE_OUTPUT),
    "spec_issues": [{
        "severity": "minor",
        "kind": "missing_detail",
        "summary": "Edge case unspecified",
        "location": "spec.md:9",
        "suggestion": "Name the edge case",
    }],
})

BLOCKER_OUTPUT = json.dumps({
    "spec_issues": [{
        "severity": "blocker",
        "kind": "ambiguity",
        "summary": "Spec is empty",
        "location": "everywhere",
        "suggestion": "Write actual requirements",
    }],
    "components": [],
})


class TwoShotAgent:
    """Garbage on the first attempt, valid JSON on the second."""

    def __init__(self, good_output: str) -> None:
        self._good = good_output
        self._calls = 0
        self.final_message: str | None = None

    @property
    def name(self) -> str:
        return "two-shot"

    def run(self, prompt: str, cwd: Path | None = None) -> Iterator[str]:
        self._calls += 1
        if self._calls == 1:
            yield "this is not json at all"
            self.final_message = "this is not json at all"
            return
        yield from self._good.splitlines()
        self.final_message = self._good.splitlines()[-1]


def _spec_root(tmp_path: Path) -> Path:
    spec_file = tmp_path / "spec.md"
    spec_file.write_text("# Spec\nBuild it.")
    (tmp_path / "scripts" / "kstrl").mkdir(parents=True)
    return spec_file


def _decompose(tmp_path: Path, agent: object) -> object:
    spec_file = _spec_root(tmp_path)
    ui = PlainUI(no_color=True, file=io.StringIO())
    run = open_command_run(
        ui, tmp_path, "decompose", component="architect",
        enabled=True, heartbeat=False,
    )
    try:
        return decompose_spec(
            spec_path=spec_file,
            project_name="test",
            base_branch="main",
            single_pr=False,
            agent=agent,  # type: ignore[arg-type]
            ui=ui,
            root_dir=tmp_path,
            bus=run.bus,
            transcript=run.transcript_writer("architect"),
        )
    finally:
        run.close()


class TestDecomposeRun:
    def test_success_run_folds_with_plan_and_artifacts(
        self, tmp_path: Path,
    ) -> None:
        manifest = _decompose(
            tmp_path, MockDecomposeAgent(MINOR_ISSUE_OUTPUT),
        )

        state, _ = load_run_state(tmp_path)
        assert state.kind == "decompose"
        assert state.finished
        # The forming DAG: architect first, then the manifest order.
        assert state.plan_order == [
            "architect",
            *[c.id for c in manifest.components],  # type: ignore[attr-defined]
        ]
        architect = state.components["architect"]
        assert architect.status == "completed"
        assert [p["phase"] for p in architect.phase_history] == [
            "decompose", "audit",
        ]
        assert all(p["passed"] for p in architect.phase_history)
        assert [a["label"] for a in state.artifacts] == [
            "spec_issues", "prd", "prd", "manifest",
        ]
        # Planned components carry their deps for the DAG view.
        assert state.components["api"].deps == ("database",)

    def test_folded_issues_match_the_disk_artifact(
        self, tmp_path: Path,
    ) -> None:
        _decompose(tmp_path, MockDecomposeAgent(MINOR_ISSUE_OUTPUT))
        state, _ = load_run_state(tmp_path)
        payload = json.loads(
            (tmp_path / "scripts" / "kstrl" / "spec-issues.json").read_text(),
        )
        disk_issues = [
            {
                "severity": i["severity"], "kind": i["kind"],
                "summary": i["summary"], "location": i.get("location", ""),
                "suggestion": i.get("suggestion", ""),
            }
            for i in payload["issues"]
        ]
        assert state.spec_issues == disk_issues
        assert state.spec_issue_counts == {"minor": 1}

    def test_retry_folds_a_failed_then_passed_attempt(
        self, tmp_path: Path,
    ) -> None:
        _decompose(tmp_path, TwoShotAgent(VALID_DECOMPOSE_OUTPUT))
        state, _ = load_run_state(tmp_path)
        architect = state.components["architect"]
        decompose_phases = [
            p for p in architect.phase_history if p["phase"] == "decompose"
        ]
        assert [p["passed"] for p in decompose_phases] == [False, True]
        assert architect.attempt == 2
        assert architect.status == "completed"

    def test_blocker_halt_finishes_the_run_before_raising(
        self, tmp_path: Path,
    ) -> None:
        with pytest.raises(SpecBlockerError):
            _decompose(tmp_path, MockDecomposeAgent(BLOCKER_OUTPUT))

        state, _ = load_run_state(tmp_path)
        assert state.finished  # not dead - the architect judged and halted
        architect = state.components["architect"]
        assert architect.status == "failed"
        audit = [
            p for p in architect.phase_history if p["phase"] == "audit"
        ]
        assert audit and audit[0]["passed"] is False
        assert state.spec_issue_counts == {"blocker": 1}
        assert [a["label"] for a in state.artifacts] == ["spec_issues"]
        # No plan beyond the architect: nothing was decomposed.
        assert state.plan_order == ["architect"]

    def test_transcript_captures_the_architect_stream(
        self, tmp_path: Path,
    ) -> None:
        _decompose(tmp_path, MockDecomposeAgent(MINOR_ISSUE_OUTPUT))
        runs_root = tmp_path / ".kstrl" / "runs"
        run_dir = next(iter(runs_root.iterdir()))
        transcript = run_dir / "components" / "architect" / "engineer.log"
        assert "spec_issues" in transcript.read_text()

    def test_without_bus_no_run_dir_and_same_result(
        self, tmp_path: Path,
    ) -> None:
        """bus=None is exactly the pre-C4 behavior."""
        spec_file = _spec_root(tmp_path)
        manifest = decompose_spec(
            spec_path=spec_file,
            project_name="test",
            base_branch="main",
            single_pr=False,
            agent=MockDecomposeAgent(MINOR_ISSUE_OUTPUT),  # type: ignore[arg-type]
            ui=PlainUI(no_color=True, file=io.StringIO()),
            root_dir=tmp_path,
        )
        assert len(manifest.components) == 2
        assert not (tmp_path / ".kstrl" / "runs").exists()
