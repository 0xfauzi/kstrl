"""#486: a failed scaffold command or codebase scan is recorded, not passed over.

Both failures stay non-fatal. Each one becomes a note that ``_run_component``
warns on the worker's UI once ``ui`` is bound: a ``log`` row with severity
``warn`` in the component's engineer.jsonl in event mode, a line on stderr in
legacy mode. The note is for the operator, never for the engineer, so it must
not reach the prompt.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

from kstrl import events as ev
from kstrl.feedforward import CodebaseScanConfig, build_codebase_scan_context
from tests.test_event_stream import _setup_project

SCAFFOLD_EXIT_3 = "  Scaffold command failed for comp-a: exit code 3"
SCAN_BOOM = "  Codebase scan failed for comp-a: RuntimeError: boom"


def test_scaffold_nonzero_exit_is_a_note(tmp_path: Path) -> None:
    from kstrl.factory import _prepare_component_tree

    # The stderr line is there so a note that quotes the command's output
    # fails the exact comparison: the note gives the exit code only.
    result = _prepare_component_tree(
        tmp_path, "comp-a", "echo scaffold-stderr >&2; exit 3", None, None
    )
    assert result == ("", [SCAFFOLD_EXIT_3])


def test_scaffold_that_cannot_start_names_the_exception(tmp_path: Path) -> None:
    from kstrl.factory import _prepare_component_tree

    prefix, notes = _prepare_component_tree(tmp_path / "missing", "comp-a", "true", None, None)
    assert prefix == ""
    assert len(notes) == 1
    assert notes[0].startswith("  Scaffold command failed for comp-a: FileNotFoundError: ")


def test_scaffold_timeout_says_it_timed_out(tmp_path: Path) -> None:
    from kstrl.factory import _prepare_component_tree

    def _times_out(cmd: str, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        # Raise with the timeout the caller actually passed, so the note's
        # "120 seconds" comes from the call and not from this test.
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    with patch("kstrl.factory.subprocess.run", side_effect=_times_out):
        _prefix, notes = _prepare_component_tree(tmp_path, "comp-a", "sleep 200", None, None)
    assert notes == [
        "  Scaffold command failed for comp-a: TimeoutExpired: "
        "Command 'sleep 200' timed out after 120 seconds"
    ]


def test_scan_crash_is_a_note(tmp_path: Path) -> None:
    from kstrl.factory import _prepare_component_tree

    with patch(
        "kstrl.factory.build_codebase_scan_context",
        side_effect=RuntimeError("boom"),
    ):
        result = _prepare_component_tree(tmp_path, "comp-a", None, {"enabled": True}, None)
    assert result == ("", [SCAN_BOOM])


def test_scan_config_that_cannot_be_built_is_a_note(tmp_path: Path) -> None:
    from kstrl.factory import _prepare_component_tree

    # Unmocked: CodebaseScanConfig itself raises TypeError. This fails if the
    # config is built outside the try, or if the handler is narrowed to the
    # RuntimeError the test above uses.
    prefix, notes = _prepare_component_tree(tmp_path, "comp-a", None, {"no_such_key": True}, None)
    assert prefix == ""
    assert len(notes) == 1
    assert notes[0].startswith("  Codebase scan failed for comp-a: TypeError: ")
    assert "no_such_key" in notes[0]


def test_success_on_both_is_no_note(tmp_path: Path) -> None:
    from kstrl.factory import _prepare_component_tree

    (tmp_path / "mod.py").write_text("def f() -> int:\n    return 1\n", encoding="utf-8")
    prefix, notes = _prepare_component_tree(
        tmp_path, "comp-a", "touch scaffolded.txt", {"enabled": True}, None
    )
    assert notes == []
    assert (tmp_path / "scaffolded.txt").is_file()  # ran, and in the worktree
    assert prefix != ""
    assert prefix == build_codebase_scan_context(
        tmp_path, CodebaseScanConfig(enabled=True), component_id="comp-a", component_deps=None
    )


def _worker_args(root: Path, events_dir: Path | None) -> dict[str, Any]:
    _setup_project(root, ["comp-a"])
    return dict(
        component_id="comp-a",
        prd_path_str="scripts/kstrl/feature/comp-a/prd.json",
        worktree_path_str=str(root),
        root_dir_str=str(root),
        prompt_file_str="scripts/kstrl/prompt.md",
        # `cat` echoes the prompt it is given, so the transcript (engineer.log
        # in event mode, stderr in legacy mode) holds the text the engineer saw.
        agent_cmd="cat",
        model=None,
        reasoning=None,
        agent_type=None,
        sleep_seconds=0.0,
        max_iterations=1,
        scaffold_cmd="exit 3",
        codebase_scan_config_dict={"enabled": True},
        events_dir_str=str(events_dir) if events_dir else None,
        run_id="run-w",
        redirect_output=False,  # NEVER dup2 inside the test process
    )


def test_run_component_warns_each_setup_failure_in_engineer_jsonl(tmp_path: Path) -> None:
    from kstrl.factory import _run_component

    events_dir = tmp_path / ".kstrl" / "runs" / "run-w"
    with patch(
        "kstrl.factory.build_codebase_scan_context",
        side_effect=RuntimeError("boom"),
    ):
        result = _run_component(**_worker_args(tmp_path, events_dir))

    assert result.component_id == "comp-a"
    comp_dir = events_dir / "components" / "comp-a"
    rows = ev.read_events(comp_dir / "engineer.jsonl")
    names = [type(e).type for e in rows]
    assert "iteration_started" in names  # still reached its loop
    warns = [
        (i, e.text) for i, e in enumerate(rows) if isinstance(e, ev.Log) and e.severity == "warn"
    ]
    texts = [text for _i, text in warns]
    assert SCAFFOLD_EXIT_3 in texts
    assert SCAN_BOOM in texts
    first_iteration = names.index("iteration_started")
    assert all(i < first_iteration for i, text in warns if text in (SCAFFOLD_EXIT_3, SCAN_BOOM))

    # The notes are for the operator. The prompt the engineer saw (echoed by
    # `cat` into engineer.log) must not carry them (H3: no unenrolled
    # engineer-facing text). "test prompt" is the control that proves the
    # prompt reached the log at all.
    transcript = (comp_dir / "engineer.log").read_text(encoding="utf-8")
    assert "test prompt" in transcript
    assert "Scaffold command failed" not in transcript
    assert "Codebase scan failed" not in transcript


def test_run_component_without_events_dir_warns_on_stderr(tmp_path: Path, capfd: Any) -> None:
    from kstrl.factory import _run_component

    with patch(
        "kstrl.factory.build_codebase_scan_context",
        side_effect=RuntimeError("boom"),
    ):
        _run_component(**_worker_args(tmp_path, None))
    _out, err = capfd.readouterr()
    lines = err.splitlines()
    assert any("test prompt" in line for line in lines)  # `cat` echoed the prompt here
    # Exactly one line names each failure, and it is the WARN line. A note
    # that also reached the prompt would appear a second time, echoed by `cat`.
    assert [line for line in lines if "Scaffold command failed" in line] == [
        "WARN: " + SCAFFOLD_EXIT_3
    ]
    assert [line for line in lines if "Codebase scan failed" in line] == ["WARN: " + SCAN_BOOM]
