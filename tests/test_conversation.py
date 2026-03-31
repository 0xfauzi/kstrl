"""Tests for ralph.conversation module."""

from __future__ import annotations

from ralph.conversation import (
    ConversationMessage,
    build_conversation_prompt,
    try_extract_prd_from_response,
)


def test_conversation_message_construction() -> None:
    msg = ConversationMessage(role="user", content="hello")
    assert msg.role == "user"
    assert msg.content == "hello"


def test_build_prompt_empty_history() -> None:
    prompt = build_conversation_prompt([])
    assert "product manager" in prompt.lower() or "PM" in prompt
    assert "Continue the conversation" in prompt


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


def test_extract_prd_valid_json() -> None:
    response = """\
Here is your PRD:

```json
{
  "branchName": "ralph/test-feature",
  "userStories": [
    {
      "id": "US-001",
      "title": "Add login page",
      "acceptanceCriteria": ["Login form renders", "JWT token issued"],
      "priority": 1,
      "passes": false,
      "notes": ""
    }
  ]
}
```

Let me know if you want changes.
"""
    prd = try_extract_prd_from_response(response)
    assert prd is not None
    assert prd.branch_name == "ralph/test-feature"
    assert len(prd.user_stories) == 1
    assert prd.user_stories[0].title == "Add login page"


def test_extract_prd_no_json() -> None:
    response = "I have some more questions before generating the PRD."
    prd = try_extract_prd_from_response(response)
    assert prd is None


def test_extract_prd_invalid_json() -> None:
    response = """\
```json
{"branchName": "test"}
```
"""
    prd = try_extract_prd_from_response(response)
    assert prd is None  # missing userStories


def test_extract_prd_malformed_json() -> None:
    response = """\
```json
{not valid json}
```
"""
    prd = try_extract_prd_from_response(response)
    assert prd is None


def test_extract_prd_multiple_code_blocks() -> None:
    response = """\
Here is some example code:

```python
print("hello")
```

And the PRD:

```json
{
  "branchName": "ralph/multi-block",
  "userStories": [
    {
      "id": "US-001",
      "title": "Test story",
      "acceptanceCriteria": ["Criterion 1"],
      "priority": 1,
      "passes": false,
      "notes": ""
    }
  ]
}
```
"""
    prd = try_extract_prd_from_response(response)
    assert prd is not None
    assert prd.branch_name == "ralph/multi-block"
