"""A minimal valid PRD payload for tests that validate or load a PRD.

Moved here from ``tests/test_prd_allowed_paths.py`` when that file was
deleted (only end-to-end tests are committed); ``tests/test_spec_issue_routing.py``
imports it.
"""

from __future__ import annotations


def _make_prd_payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "branchName": "kstrl/test",
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
