"""Tests for ralph.git_ops module."""

from __future__ import annotations

from ralph.git_ops import path_is_allowed


def test_exact_match() -> None:
    assert path_is_allowed("scripts/ralph/codebase_map.md", ["scripts/ralph/codebase_map.md"])


def test_exact_match_no_match() -> None:
    assert not path_is_allowed("src/main.py", ["scripts/ralph/codebase_map.md"])


def test_directory_prefix() -> None:
    assert path_is_allowed("scripts/ralph/foo.md", ["scripts/ralph/"])
    assert path_is_allowed("scripts/ralph/sub/bar.py", ["scripts/ralph/"])


def test_directory_prefix_no_match() -> None:
    assert not path_is_allowed("src/main.py", ["scripts/ralph/"])


def test_multiple_rules() -> None:
    allowed = ["scripts/ralph/", "src/", "README.md"]
    assert path_is_allowed("scripts/ralph/prd.json", allowed)
    assert path_is_allowed("src/main.py", allowed)
    assert path_is_allowed("README.md", allowed)
    assert not path_is_allowed("docs/guide.md", allowed)


def test_empty_allowed_paths() -> None:
    assert not path_is_allowed("any/file.py", [])


def test_whitespace_in_rules() -> None:
    assert path_is_allowed("src/main.py", ["  src/  "])
    assert path_is_allowed("README.md", ["  README.md  "])


def test_empty_rule_ignored() -> None:
    assert not path_is_allowed("src/main.py", ["", "  ", "docs/"])
