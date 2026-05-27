"""Tests for the new ``allowedPaths`` field on ``ralph_py.prd.PRD``.

These cover both the validator's optional-but-non-empty contract and
the loader's coercion to ``PRD.allowed_paths``. The factory then reads
``prd.allowed_paths`` and forwards it to ``verify.check_diff_scope``;
that wiring is exercised end-to-end by an integration-level factory
test, but the per-unit boundaries are covered here so a refactor that
breaks the loader fails fast.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ralph_py.prd import PRD


def _make_prd_payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "branchName": "ralph/test",
        "userStories": [
            {
                "id": "US-001",
                "title": "Implement core",
                "acceptanceCriteria": ["does X"],
                "priority": 1,
                "passes": False,
                "notes": "",
            },
        ],
    }
    base.update(overrides)
    return base


class TestValidateSchemaAllowedPaths:
    def test_absent_is_valid(self) -> None:
        """Legacy DECOMPOSE_PROMPT v1.0.0 PRDs don't carry the field;
        they must still load."""
        assert PRD.validate_schema(_make_prd_payload()) == []

    def test_array_of_strings_is_valid(self) -> None:
        payload = _make_prd_payload(
            allowedPaths=["src/", "tests/", "scripts/ralph/feature/x/"],
        )
        assert PRD.validate_schema(payload) == []

    def test_not_an_array_rejected(self) -> None:
        payload = _make_prd_payload(allowedPaths="src/")
        errors = PRD.validate_schema(payload)
        assert any("array" in e for e in errors)

    def test_empty_array_rejected(self) -> None:
        """An empty array silently disables the diff-scope check;
        rejecting it explicitly is the H3-style "halt over heroics"
        choice -- omit the field entirely to mean "no constraint"."""
        payload = _make_prd_payload(allowedPaths=[])
        errors = PRD.validate_schema(payload)
        assert any("non-empty" in e for e in errors)

    def test_non_string_item_rejected(self) -> None:
        payload = _make_prd_payload(allowedPaths=["src/", 42])
        errors = PRD.validate_schema(payload)
        assert any("strings" in e for e in errors)

    def test_empty_string_item_rejected(self) -> None:
        payload = _make_prd_payload(allowedPaths=["src/", ""])
        errors = PRD.validate_schema(payload)
        assert any("non-empty" in e for e in errors)

    def test_unexpected_top_level_key_still_rejected(self) -> None:
        """allowedPaths becoming optional must not weaken the strict
        unexpected-key check."""
        payload = _make_prd_payload(allowedPaths=["src/"], unexpected="x")
        errors = PRD.validate_schema(payload)
        assert any("Unexpected keys" in e for e in errors)


class TestLoadAllowedPaths:
    def test_load_absent_is_none(self, tmp_path: Path) -> None:
        path = tmp_path / "prd.json"
        path.write_text(json.dumps(_make_prd_payload()))
        prd = PRD.load(path)
        assert prd.allowed_paths is None

    def test_load_present_populates_field(self, tmp_path: Path) -> None:
        path = tmp_path / "prd.json"
        path.write_text(
            json.dumps(_make_prd_payload(
                allowedPaths=["src/", "tests/", "scripts/ralph/feature/x/"],
            )),
        )
        prd = PRD.load(path)
        assert prd.allowed_paths == [
            "src/", "tests/", "scripts/ralph/feature/x/",
        ]

    def test_load_with_invalid_allowed_paths_rejected_at_load(
        self, tmp_path: Path,
    ) -> None:
        """An invalid allowedPaths (e.g. empty array) makes PRD.load
        raise ValueError. The factory's per-component lookup tolerates
        this by treating it as "no constraint", but ``PRD.load`` itself
        is strict so other callers can rely on the dataclass invariant."""
        path = tmp_path / "prd.json"
        path.write_text(
            json.dumps(_make_prd_payload(allowedPaths=[])),
        )
        with pytest.raises(ValueError, match="allowedPaths"):
            PRD.load(path)
