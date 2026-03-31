"""PRD (Product Requirements Document) loading, validation, and management."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class UserStory:
    id: str
    title: str
    acceptance_criteria: list[str]
    priority: int
    passes: bool
    notes: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "acceptanceCriteria": self.acceptance_criteria,
            "priority": self.priority,
            "passes": self.passes,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UserStory:
        return cls(
            id=data["id"],
            title=data["title"],
            acceptance_criteria=data["acceptanceCriteria"],
            priority=data["priority"],
            passes=data["passes"],
            notes=data["notes"],
        )


@dataclass
class PRD:
    branch_name: str
    user_stories: list[UserStory] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "branchName": self.branch_name,
            "userStories": [s.to_dict() for s in self.user_stories],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PRD:
        return cls(
            branch_name=data["branchName"],
            user_stories=[UserStory.from_dict(s) for s in data.get("userStories", [])],
        )

    @property
    def total_stories(self) -> int:
        return len(self.user_stories)

    @property
    def passing_stories(self) -> int:
        return sum(1 for s in self.user_stories if s.passes)

    @property
    def failing_stories(self) -> int:
        return sum(1 for s in self.user_stories if not s.passes)

    def next_story(self) -> UserStory | None:
        """Return the highest-priority story that hasn't passed yet."""
        failing = [s for s in self.user_stories if not s.passes]
        if not failing:
            return None
        return min(failing, key=lambda s: s.priority)

    def all_pass(self) -> bool:
        return all(s.passes for s in self.user_stories)


def validate_prd(data: Any) -> list[str]:
    """Validate PRD data against the schema. Returns list of error strings."""
    errors: list[str] = []

    if not isinstance(data, dict):
        errors.append("top-level must be an object")
        return errors

    if set(data.keys()) != {"branchName", "userStories"}:
        errors.append('top-level keys must be exactly: "branchName", "userStories"')

    if not isinstance(data.get("branchName"), str) or not data.get("branchName"):
        errors.append('"branchName" must be a non-empty string')

    stories = data.get("userStories")
    if not isinstance(stories, list):
        errors.append('"userStories" must be an array')
        return errors

    expected_keys = {"id", "title", "acceptanceCriteria", "priority", "passes", "notes"}

    for idx, story in enumerate(stories):
        if not isinstance(story, dict):
            errors.append(f"userStories[{idx}] must be an object")
            continue

        if set(story.keys()) != expected_keys:
            errors.append(f"userStories[{idx}] keys must be exactly: {sorted(expected_keys)}")

        if not isinstance(story.get("id"), str) or not story.get("id"):
            errors.append(f"userStories[{idx}].id must be a non-empty string")
        if not isinstance(story.get("title"), str) or not story.get("title"):
            errors.append(f"userStories[{idx}].title must be a non-empty string")

        ac = story.get("acceptanceCriteria")
        if not isinstance(ac, list) or not all(isinstance(x, str) and x for x in ac):
            errors.append(
                f"userStories[{idx}].acceptanceCriteria must be an array of non-empty strings"
            )

        if not isinstance(story.get("priority"), int):
            errors.append(f"userStories[{idx}].priority must be an integer")
        if not isinstance(story.get("passes"), bool):
            errors.append(f"userStories[{idx}].passes must be a boolean")
        if not isinstance(story.get("notes"), str):
            errors.append(f"userStories[{idx}].notes must be a string")

    return errors


def load_prd(path: Path) -> PRD:
    """Load and validate a PRD from a JSON file. Raises ValueError on invalid data."""
    if not path.exists():
        raise FileNotFoundError(f"PRD file not found: {path}")

    data = json.loads(path.read_text(encoding="utf-8"))
    errors = validate_prd(data)
    if errors:
        msg = f"PRD validation failed ({path}):\n" + "\n".join(f"  - {e}" for e in errors)
        raise ValueError(msg)

    return PRD.from_dict(data)


def save_prd(prd: PRD, path: Path) -> None:
    """Save a PRD to a JSON file with pretty formatting."""
    data = prd.to_dict()
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def create_empty_prd(branch_name: str = "ralph/feature") -> PRD:
    """Create a new empty PRD with a branch name."""
    return PRD(branch_name=branch_name, user_stories=[])


def create_story(
    story_id: str,
    title: str,
    acceptance_criteria: list[str],
    priority: int,
) -> UserStory:
    """Create a new user story with defaults."""
    return UserStory(
        id=story_id,
        title=title,
        acceptance_criteria=acceptance_criteria,
        priority=priority,
        passes=False,
        notes="",
    )
