"""PRD (Product Requirements Document) loading and validation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class UserStory:
    """A single user story from the PRD."""

    id: str
    title: str
    acceptance_criteria: list[str]
    priority: int
    passes: bool
    notes: str


@dataclass
class PRD:
    """Product Requirements Document."""

    branch_name: str
    user_stories: list[UserStory]
    # Allow-list of path prefixes the engineer is permitted to write to.
    # Populated by the architect (DECOMPOSE_PROMPT v1.1.0+); legacy PRDs
    # without this field load as None which preserves the prior
    # "scope unconstrained" behavior. The factory forwards this to
    # ``verify.check_diff_scope`` so the agent's diff is bounded per-
    # component rather than allowed to touch anywhere in the worktree.
    allowed_paths: list[str] | None = None

    @classmethod
    def load(cls, path: Path) -> PRD:
        """Load PRD from JSON file."""
        with open(path) as f:
            data = json.load(f)

        errors = cls.validate_schema(data)
        if errors:
            raise ValueError(f"Invalid PRD schema: {'; '.join(errors)}")

        stories = [
            UserStory(
                id=s["id"],
                title=s["title"],
                acceptance_criteria=s["acceptanceCriteria"],
                priority=s["priority"],
                passes=s["passes"],
                notes=s["notes"],
            )
            for s in data["userStories"]
        ]

        allowed_paths = data.get("allowedPaths")
        if allowed_paths is not None and not isinstance(allowed_paths, list):
            allowed_paths = None
        return cls(
            branch_name=data["branchName"],
            user_stories=stories,
            allowed_paths=allowed_paths,
        )

    @classmethod
    def validate_schema(cls, data: Any) -> list[str]:
        """Validate PRD JSON schema, returning list of errors.

        Schema requirements:
        - Top-level must be dict with ``branchName`` and ``userStories``,
          optionally ``allowedPaths``.
        - branchName: non-empty string.
        - userStories: array of story objects, each with exactly 6 keys
          (id, title, acceptanceCriteria, priority, passes, notes).
        - allowedPaths (optional): non-empty array of non-empty strings
          when present. An empty array is rejected because it silently
          disables diff-scope enforcement -- omit the field entirely
          to mean "no constraint".
        - Field types are strictly enforced.
        """
        errors: list[str] = []

        if not isinstance(data, dict):
            errors.append("PRD must be a JSON object")
            return errors

        required_keys = {"branchName", "userStories"}
        optional_keys = {"allowedPaths"}
        actual_keys = set(data.keys())
        missing = required_keys - actual_keys
        extra = actual_keys - required_keys - optional_keys

        if missing or extra:
            if missing:
                errors.append(f"Missing required keys: {', '.join(sorted(missing))}")
            if extra:
                errors.append(f"Unexpected keys: {', '.join(sorted(extra))}")
            return errors

        # Validate optional allowedPaths shape
        if "allowedPaths" in data:
            ap = data["allowedPaths"]
            if not isinstance(ap, list):
                errors.append("allowedPaths must be an array")
            elif not ap:
                errors.append(
                    "allowedPaths must be non-empty when present "
                    "(omit the field entirely to leave scope unconstrained)"
                )
            elif not all(isinstance(p, str) and p for p in ap):
                errors.append("allowedPaths: all items must be non-empty strings")

        # Validate branchName
        branch_name = data.get("branchName")
        if not isinstance(branch_name, str):
            errors.append(f"branchName must be a string (got: {type(branch_name).__name__})")
        elif not branch_name:
            errors.append("branchName must be non-empty")

        # Validate userStories
        user_stories = data.get("userStories")
        if not isinstance(user_stories, list):
            errors.append(f"userStories must be an array (got: {type(user_stories).__name__})")
            return errors

        # Validate each story
        story_keys = {"id", "title", "acceptanceCriteria", "priority", "passes", "notes"}
        for i, story in enumerate(user_stories):
            story_prefix = f"userStories[{i}]"

            if not isinstance(story, dict):
                errors.append(f"{story_prefix}: must be an object")
                continue

            # Check story keys
            story_actual_keys = set(story.keys())
            if story_actual_keys != story_keys:
                missing = story_keys - story_actual_keys
                extra = story_actual_keys - story_keys
                if missing:
                    errors.append(f"{story_prefix}: missing keys: {', '.join(sorted(missing))}")
                if extra:
                    errors.append(f"{story_prefix}: unexpected keys: {', '.join(sorted(extra))}")
                continue

            # Type validation
            if not isinstance(story.get("id"), str):
                errors.append(f"{story_prefix}.id: must be a string")
            if not isinstance(story.get("title"), str):
                errors.append(f"{story_prefix}.title: must be a string")
            if not isinstance(story.get("acceptanceCriteria"), list):
                errors.append(f"{story_prefix}.acceptanceCriteria: must be an array")
            elif not all(isinstance(c, str) for c in story["acceptanceCriteria"]):
                errors.append(f"{story_prefix}.acceptanceCriteria: all items must be strings")
            if not isinstance(story.get("priority"), int):
                errors.append(f"{story_prefix}.priority: must be an integer")
            if not isinstance(story.get("passes"), bool):
                errors.append(f"{story_prefix}.passes: must be a boolean")
            if not isinstance(story.get("notes"), str):
                errors.append(f"{story_prefix}.notes: must be a string")

        return errors

    def get_next_story(self) -> UserStory | None:
        """Get the highest-priority failing story."""
        failing = [s for s in self.user_stories if not s.passes]
        if not failing:
            return None
        return min(failing, key=lambda s: s.priority)

    def save(self, path: Path) -> None:
        """Save PRD back to JSON file."""
        data = {
            "branchName": self.branch_name,
            "userStories": [
                {
                    "id": s.id,
                    "title": s.title,
                    "acceptanceCriteria": s.acceptance_criteria,
                    "priority": s.priority,
                    "passes": s.passes,
                    "notes": s.notes,
                }
                for s in self.user_stories
            ],
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
