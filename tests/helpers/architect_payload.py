"""Stub architect payloads that meet the #639 slice 2 contract.

DECOMPOSE_PROMPT 5.0.0 requires ``requirements`` whenever ``components`` is
not empty, and every user story must be named by a requirement. A stub
architect whose test is about something else uses ``traced`` to meet that
contract with one requirement naming every story.
"""

from __future__ import annotations

from typing import Any


def traced(payload: dict[str, Any]) -> dict[str, Any]:
    """``payload`` plus one requirement, R-1, naming every user story in it.

    A payload with no story (a halt) is returned unchanged.
    """
    stories = [
        story["id"]
        for component in payload.get("components", [])
        if isinstance(component, dict)
        for story in component.get("userStories", [])
        if isinstance(story, dict) and isinstance(story.get("id"), str)
    ]
    if not stories:
        return payload
    requirement = {
        "id": "R-1",
        "kind": "requirement",
        "statement": "The specification is built.",
        "stories": stories,
    }
    return {**payload, "requirements": [requirement]}
