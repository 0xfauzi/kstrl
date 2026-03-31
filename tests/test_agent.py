"""Tests for ralph.agent module."""

from __future__ import annotations

from pathlib import Path

from ralph.agent import (
    COMPLETION_MARKER,
    LineRole,
    _extract_recent_handoff,
    classify_line,
    detect_completion,
)


def test_classify_system_line() -> None:
    assert classify_line("System: You are a helpful assistant") == LineRole.SYS


def test_classify_user_line() -> None:
    assert classify_line("User: Hello world") == LineRole.PROMPT


def test_classify_assistant_line() -> None:
    assert classify_line("Assistant: I'll help you") == LineRole.AI


def test_classify_thinking_line() -> None:
    assert classify_line("Thinking: Let me consider...") == LineRole.THINK


def test_classify_tool_line() -> None:
    assert classify_line("Tool: read_file result") == LineRole.TOOL


def test_classify_git_line() -> None:
    assert classify_line("GIT | Switched to branch main") == LineRole.GIT


def test_classify_guard_line() -> None:
    assert classify_line("GUARD | Disallowed changes") == LineRole.GUARD


def test_classify_plain_line_defaults_to_ai() -> None:
    assert classify_line("Just some normal output") == LineRole.AI


def test_detect_completion_true() -> None:
    assert detect_completion("<promise>COMPLETE</promise>") is True
    assert detect_completion("Some text <promise>COMPLETE</promise> more text") is True


def test_detect_completion_false() -> None:
    assert detect_completion("Not done yet") is False
    assert detect_completion("<promise>INCOMPLETE</promise>") is False


def test_completion_marker_value() -> None:
    assert COMPLETION_MARKER == "<promise>COMPLETE</promise>"


# -- _extract_recent_handoff tests --


def test_extract_recent_handoff_empty(tmp_path: Path) -> None:
    p = tmp_path / "progress.txt"
    p.write_text("# Ralph Progress Log\n\n---\n\n", encoding="utf-8")
    assert _extract_recent_handoff(p) == ""


def test_extract_recent_handoff_few_entries(tmp_path: Path) -> None:
    p = tmp_path / "progress.txt"
    p.write_text(
        "# Header\n\n"
        "## Iteration 1 - US-001\n- Did stuff\n---\n\n"
        "## Iteration 2 - US-002\n- Did more\n---\n",
        encoding="utf-8",
    )
    result = _extract_recent_handoff(p, max_entries=5)
    assert "Iteration 1" in result
    assert "Iteration 2" in result


def test_extract_recent_handoff_sliding_window(tmp_path: Path) -> None:
    lines = "# Header\n\n"
    for i in range(1, 11):
        lines += f"## Iteration {i} - US-{i:03d}\n- Work {i}\n---\n\n"
    p = tmp_path / "progress.txt"
    p.write_text(lines, encoding="utf-8")

    result = _extract_recent_handoff(p, max_entries=3)
    # Should only have last 3
    assert "Iteration 8" in result
    assert "Iteration 9" in result
    assert "Iteration 10" in result
    # Should NOT have earlier ones
    assert "Iteration 1 -" not in result
    assert "Iteration 7 -" not in result
