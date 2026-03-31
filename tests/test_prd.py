"""Tests for ralph.prd module."""

from __future__ import annotations

from pathlib import Path

import pytest

from ralph.prd import (
    PRD,
    create_empty_prd,
    create_story,
    load_prd,
    parse_markdown_to_stories,
    save_prd,
    validate_prd,
)


def _make_valid_prd_data() -> dict:
    return {
        "branchName": "ralph/test",
        "userStories": [
            {
                "id": "US-001",
                "title": "First story",
                "acceptanceCriteria": ["Criterion 1", "Criterion 2"],
                "priority": 1,
                "passes": False,
                "notes": "",
            },
            {
                "id": "US-002",
                "title": "Second story",
                "acceptanceCriteria": ["Criterion A"],
                "priority": 2,
                "passes": True,
                "notes": "Done",
            },
        ],
    }


def test_validate_valid_prd() -> None:
    errors = validate_prd(_make_valid_prd_data())
    assert errors == []


def test_validate_missing_keys() -> None:
    errors = validate_prd({"branchName": "test"})
    assert any("top-level keys" in e for e in errors)


def test_validate_not_object() -> None:
    errors = validate_prd([1, 2, 3])
    assert errors == ["top-level must be an object"]


def test_validate_empty_branch_name() -> None:
    data = _make_valid_prd_data()
    data["branchName"] = ""
    errors = validate_prd(data)
    assert any("branchName" in e for e in errors)


def test_validate_story_missing_fields() -> None:
    data = {
        "branchName": "test",
        "userStories": [{"id": "US-001"}],
    }
    errors = validate_prd(data)
    assert len(errors) > 0


def test_validate_story_wrong_types() -> None:
    data = _make_valid_prd_data()
    data["userStories"][0]["priority"] = "high"  # should be int
    errors = validate_prd(data)
    assert any("priority" in e for e in errors)


def test_prd_from_dict() -> None:
    data = _make_valid_prd_data()
    prd = PRD.from_dict(data)
    assert prd.branch_name == "ralph/test"
    assert len(prd.user_stories) == 2
    assert prd.user_stories[0].id == "US-001"
    assert prd.user_stories[1].passes is True


def test_prd_to_dict_round_trip() -> None:
    data = _make_valid_prd_data()
    prd = PRD.from_dict(data)
    result = prd.to_dict()
    assert result == data


def test_prd_properties() -> None:
    prd = PRD.from_dict(_make_valid_prd_data())
    assert prd.total_stories == 2
    assert prd.passing_stories == 1
    assert prd.failing_stories == 1


def test_prd_next_story() -> None:
    prd = PRD.from_dict(_make_valid_prd_data())
    next_s = prd.next_story()
    assert next_s is not None
    assert next_s.id == "US-001"  # priority 1, not passing


def test_prd_next_story_all_passing() -> None:
    data = _make_valid_prd_data()
    data["userStories"][0]["passes"] = True
    prd = PRD.from_dict(data)
    assert prd.next_story() is None


def test_prd_all_pass() -> None:
    data = _make_valid_prd_data()
    assert PRD.from_dict(data).all_pass() is False
    data["userStories"][0]["passes"] = True
    assert PRD.from_dict(data).all_pass() is True


def test_load_save_prd(tmp_path: Path) -> None:
    prd = PRD.from_dict(_make_valid_prd_data())
    path = tmp_path / "prd.json"
    save_prd(prd, path)

    loaded = load_prd(path)
    assert loaded.branch_name == prd.branch_name
    assert len(loaded.user_stories) == len(prd.user_stories)


def test_load_prd_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_prd(tmp_path / "nonexistent.json")


def test_load_prd_invalid_schema(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text('{"foo": "bar"}', encoding="utf-8")
    with pytest.raises(ValueError, match="validation failed"):
        load_prd(path)


def test_create_empty_prd() -> None:
    prd = create_empty_prd("ralph/new")
    assert prd.branch_name == "ralph/new"
    assert prd.user_stories == []


def test_create_story() -> None:
    story = create_story("US-001", "Test story", ["Criterion 1"], 1)
    assert story.id == "US-001"
    assert story.passes is False
    assert story.notes == ""


# -- parse_markdown_to_stories tests --


def test_parse_markdown_headings_to_stories() -> None:
    md = """\
# My Feature

## User login
- Username and password required
- Session token returned on success

## Password reset
- Email sent with reset link
- Link expires after 24h
"""
    result = parse_markdown_to_stories(md)
    assert result.feature_overview == "My Feature"
    assert len(result.stories) == 2
    assert result.stories[0]["title"] == "User login"
    assert "Username and password required" in result.stories[0]["criteria"]
    assert result.stories[1]["title"] == "Password reset"


def test_parse_markdown_meta_sections() -> None:
    md = """\
## Overview
This is the project overview.

## Tech Stack
Python, FastAPI, PostgreSQL

## User registration
- Form validation works
- Confirmation email sent
"""
    result = parse_markdown_to_stories(md)
    assert "project overview" in result.feature_overview
    assert "Python" in result.tech_stack
    assert len(result.stories) == 1
    assert result.stories[0]["title"] == "User registration"


def test_parse_markdown_numbered_lists() -> None:
    md = """\
## Data export
1. CSV format supported
2. JSON format supported
3. Download link generated
"""
    result = parse_markdown_to_stories(md)
    assert len(result.stories) == 1
    assert "CSV format supported" in result.stories[0]["criteria"]
    assert "JSON format supported" in result.stories[0]["criteria"]


def test_parse_markdown_no_headings_fallback() -> None:
    md = """\
This is the first feature. It should handle user authentication
with OAuth support and session management.

This is the second feature. It should provide an API
for fetching user profiles with pagination.
"""
    result = parse_markdown_to_stories(md)
    assert len(result.stories) >= 1


def test_parse_markdown_empty() -> None:
    result = parse_markdown_to_stories("")
    assert result.stories == []
    assert result.feature_overview == ""


def test_parse_markdown_h3_headings() -> None:
    md = """\
## Features

### Search
- Full-text search
- Filters by date

### Sort
- Sort by name
- Sort by date
"""
    result = parse_markdown_to_stories(md)
    # "Features" might be treated as a story or skipped
    story_titles = [s["title"] for s in result.stories]
    assert "Search" in story_titles
    assert "Sort" in story_titles
