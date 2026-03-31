"""Tests for ralph.conversation module."""

from __future__ import annotations

import json

from ralph.conversation import (
    READY_MARKER,
    ConversationMessage,
    build_conversation_prompt,
    build_generation_prompt,
    parse_prd_from_json_output,
    response_has_ready_marker,
)


def test_conversation_message_construction() -> None:
    msg = ConversationMessage(role="user", content="hello")
    assert msg.role == "user"
    assert msg.content == "hello"


def test_build_prompt_empty_history() -> None:
    prompt = build_conversation_prompt([])
    assert "product manager" in prompt.lower() or "PM" in prompt
    assert "READY_TO_GENERATE" in prompt


def test_build_prompt_with_messages() -> None:
    messages = [
        ConversationMessage(role="user", content="I want auth"),
        ConversationMessage(role="assistant", content="What kind?"),
        ConversationMessage(role="user", content="JWT"),
    ]
    prompt = build_conversation_prompt(messages)
    assert "I want auth" in prompt
    assert "What kind?" in prompt
    assert "JWT" in prompt
    assert "Developer" in prompt
    assert "PM (you)" in prompt


def test_build_generation_prompt() -> None:
    messages = [
        ConversationMessage(role="user", content="Build a login page"),
        ConversationMessage(role="assistant", content="What about OAuth?"),
        ConversationMessage(role="user", content="No, just JWT"),
    ]
    prompt = build_generation_prompt(messages)
    assert "Build a login page" in prompt
    assert "What about OAuth?" in prompt
    assert "No, just JWT" in prompt
    assert "generating a PRD" in prompt.lower() or "PRD" in prompt


def test_response_has_ready_marker() -> None:
    assert response_has_ready_marker(f"I'm satisfied. {READY_MARKER}")
    assert response_has_ready_marker(READY_MARKER)
    assert not response_has_ready_marker("Still have questions")
    assert not response_has_ready_marker("")


def test_parse_prd_from_json_output_with_envelope() -> None:
    prd_data = {
        "branchName": "ralph/test",
        "userStories": [
            {
                "id": "US-001",
                "title": "Test story",
                "acceptanceCriteria": ["Criterion 1"],
                "priority": 1,
                "passes": False,
                "notes": "",
            }
        ],
    }
    envelope = json.dumps({"structured_output": prd_data})
    prd = parse_prd_from_json_output(envelope)
    assert prd is not None
    assert prd.branch_name == "ralph/test"
    assert len(prd.user_stories) == 1


def test_parse_prd_from_json_output_direct() -> None:
    """PRD data without the result envelope."""
    prd_data = {
        "branchName": "ralph/direct",
        "userStories": [
            {
                "id": "US-001",
                "title": "Direct story",
                "acceptanceCriteria": ["Works"],
                "priority": 1,
                "passes": False,
                "notes": "",
            }
        ],
    }
    prd = parse_prd_from_json_output(json.dumps(prd_data))
    assert prd is not None
    assert prd.branch_name == "ralph/direct"


def test_parse_prd_from_json_output_invalid() -> None:
    prd = parse_prd_from_json_output('{"branchName": "test"}')
    assert prd is None


def test_parse_prd_from_json_output_not_json() -> None:
    prd = parse_prd_from_json_output("not json at all")
    assert prd is None
