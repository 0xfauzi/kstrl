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


# --- Fixture entry validation (R7.2) -----------------------------------
# Fixture definitions are LLM-emitted and Phase 1 EXECUTES them, so
# validation is strict: unknown keys are rejected rather than ignored,
# because an ignored expectation key (a misspelled "stdout_contains",
# say) silently weakens the oracle to a vacuous pass.

_FIXTURE_ENTRY_KEYS = {"description", "fixture_type", "input_data", "expected"}

# fixture_type -> {input_data key -> required}
_FIXTURE_INPUT_KEYS: dict[str, dict[str, bool]] = {
    "cli": {"command": True},
    "function": {"module": True, "function": True, "args": False, "kwargs": False},
    "file": {"path": True},
}

_FIXTURE_EXPECTED_KEYS: dict[str, set[str]] = {
    "cli": {"exit_code", "stdout_contains", "stdout_not_contains"},
    "function": {"returns", "raises"},
    "file": {"exists", "contains", "not_contains"},
}


def _validate_string_list(prefix: str, value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(s, str) for s in value):
        return [f"{prefix}: must be an array of strings"]
    return []


def _validate_fixture_entry(prefix: str, entry: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(entry, dict):
        return [f"{prefix}: must be an object"]

    actual_keys = set(entry.keys())
    missing = _FIXTURE_ENTRY_KEYS - actual_keys
    extra = actual_keys - _FIXTURE_ENTRY_KEYS
    if missing:
        errors.append(f"{prefix}: missing keys: {', '.join(sorted(missing))}")
    if extra:
        errors.append(f"{prefix}: unexpected keys: {', '.join(sorted(extra))}")
    if missing or extra:
        return errors

    description = entry["description"]
    if not isinstance(description, str) or not description:
        errors.append(f"{prefix}.description: must be a non-empty string")
    fixture_type = entry["fixture_type"]
    if fixture_type not in _FIXTURE_INPUT_KEYS:
        errors.append(
            f"{prefix}.fixture_type: must be one of "
            f"{sorted(_FIXTURE_INPUT_KEYS)} (got: {fixture_type!r})"
        )
        return errors
    input_data = entry["input_data"]
    expected = entry["expected"]
    if not isinstance(input_data, dict):
        errors.append(f"{prefix}.input_data: must be an object")
    if not isinstance(expected, dict):
        errors.append(f"{prefix}.expected: must be an object")
    if errors:
        return errors

    key_spec = _FIXTURE_INPUT_KEYS[fixture_type]
    unknown = set(input_data) - set(key_spec)
    if unknown:
        errors.append(
            f"{prefix}.input_data: unexpected keys for {fixture_type} "
            f"fixture: {', '.join(sorted(unknown))}"
        )
    for key, required in key_spec.items():
        if required and key not in input_data:
            errors.append(f"{prefix}.input_data: missing required key: {key}")

    allowed_expected = _FIXTURE_EXPECTED_KEYS[fixture_type]
    unknown_expected = set(expected) - allowed_expected
    if unknown_expected:
        errors.append(
            f"{prefix}.expected: unexpected keys for {fixture_type} fixture: "
            f"{', '.join(sorted(unknown_expected))} "
            f"(allowed: {', '.join(sorted(allowed_expected))})"
        )
    if not expected:
        errors.append(
            f"{prefix}.expected: must not be empty - a fixture with no "
            "expectations verifies nothing"
        )
    if errors:
        return errors

    if fixture_type == "cli":
        command = input_data.get("command")
        if not isinstance(command, str) or not command.strip():
            errors.append(
                f"{prefix}.input_data.command: must be a non-empty string"
            )
        if "exit_code" in expected and (
            isinstance(expected["exit_code"], bool)
            or not isinstance(expected["exit_code"], int)
        ):
            errors.append(f"{prefix}.expected.exit_code: must be an integer")
        for key in ("stdout_contains", "stdout_not_contains"):
            if key in expected:
                errors.extend(
                    _validate_string_list(
                        f"{prefix}.expected.{key}", expected[key],
                    )
                )
    elif fixture_type == "function":
        for key in ("module", "function"):
            value = input_data.get(key)
            if not isinstance(value, str) or not value:
                errors.append(
                    f"{prefix}.input_data.{key}: must be a non-empty string"
                )
        if "args" in input_data and not isinstance(input_data["args"], list):
            errors.append(f"{prefix}.input_data.args: must be an array")
        if "kwargs" in input_data and not isinstance(input_data["kwargs"], dict):
            errors.append(f"{prefix}.input_data.kwargs: must be an object")
        if "raises" in expected:
            if not isinstance(expected["raises"], str) or not expected["raises"]:
                errors.append(
                    f"{prefix}.expected.raises: must be a non-empty string"
                )
            if "returns" in expected:
                errors.append(
                    f"{prefix}.expected: 'returns' and 'raises' are "
                    "mutually exclusive"
                )
    elif fixture_type == "file":
        path_value = input_data.get("path")
        if not isinstance(path_value, str) or not path_value:
            errors.append(
                f"{prefix}.input_data.path: must be a non-empty string"
            )
        elif Path(path_value).is_absolute() or ".." in Path(path_value).parts:
            # The PRD is untrusted input; a path outside the worktree
            # would leak file content into retry prompts and PR bodies.
            errors.append(
                f"{prefix}.input_data.path: must be relative to the "
                "worktree with no '..' components"
            )
        if "exists" in expected and not isinstance(expected["exists"], bool):
            errors.append(f"{prefix}.expected.exists: must be a boolean")
        for key in ("contains", "not_contains"):
            if key in expected:
                errors.extend(
                    _validate_string_list(
                        f"{prefix}.expected.{key}", expected[key],
                    )
                )
    return errors


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
    # Approved fixtures (R7.2): behavioral input/output pairs run during
    # Phase 1 when [fixtures].enabled. Kept as the raw validated JSON
    # entries - parsing into runner objects lives in kstrl.fixtures
    # (which imports this module; the reverse import would be a cycle).
    fixtures: list[dict[str, Any]] | None = None

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
            fixtures=data.get("fixtures"),
        )

    @classmethod
    def validate_schema(cls, data: Any) -> list[str]:
        """Validate PRD JSON schema, returning list of errors.

        Schema requirements:
        - Top-level must be dict with ``branchName`` and ``userStories``,
          optionally ``allowedPaths`` and ``fixtures``.
        - branchName: non-empty string.
        - userStories: array of story objects, each with exactly 6 keys
          (id, title, acceptanceCriteria, priority, passes, notes).
        - allowedPaths (optional): non-empty array of non-empty strings
          when present. An empty array is rejected because it silently
          disables diff-scope enforcement -- omit the field entirely
          to mean "no constraint".
        - fixtures (optional): non-empty array of fixture entries, each
          with exactly the keys description / fixture_type / input_data /
          expected, validated strictly per type (see
          ``_validate_fixture_entry``; R7.2).
        - Field types are strictly enforced.
        """
        errors: list[str] = []

        if not isinstance(data, dict):
            errors.append("PRD must be a JSON object")
            return errors

        required_keys = {"branchName", "userStories"}
        optional_keys = {"allowedPaths", "fixtures"}
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

        # Validate optional fixtures shape (strict; R7.2)
        if "fixtures" in data:
            fixtures = data["fixtures"]
            if not isinstance(fixtures, list):
                errors.append("fixtures must be an array")
            elif not fixtures:
                errors.append(
                    "fixtures must be non-empty when present "
                    "(omit the field entirely when there are none)"
                )
            else:
                for i, entry in enumerate(fixtures):
                    errors.extend(
                        _validate_fixture_entry(f"fixtures[{i}]", entry)
                    )

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
        """Save PRD back to JSON file.

        Round-trips the optional fields: dropping ``allowedPaths`` on a
        save would silently unbind the component's diff scope, and
        dropping ``fixtures`` would silently disable the behavioral
        oracle (R7.2).
        """
        data: dict[str, Any] = {
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
        if self.allowed_paths is not None:
            data["allowedPaths"] = self.allowed_paths
        if self.fixtures is not None:
            data["fixtures"] = self.fixtures
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
