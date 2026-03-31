"""Tests for ralph.agent module."""

from __future__ import annotations

from ralph.agent import (
    COMPLETION_MARKER,
    LineRole,
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
