"""The architect's product spec: requirements and non-goals (#639 slice 2).

The architect returns ``requirements`` beside its components: each entry
says one thing the specification asks for (``requirement``) or rules out
(``non_goal``), and a requirement names the user stories that build it.
The field is required whenever components are returned, and every story
must be named by a requirement (owner decisions 4a and 5), so a plan
cannot hold work nobody asked for or leave a requirement unbuilt.

The rules of ``decisions.py`` hold here: the raw payload is validated
entry by entry with an indexed message before anything is parsed, the
validator and the parser read one vocabulary (``REQUIREMENT_KINDS``),
and stories join requirements by id, never by count.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

from kstrl.decisions import enum_field_error, required_field_error

KIND_REQUIREMENT = "requirement"
KIND_NON_GOAL = "non_goal"
REQUIREMENT_KINDS = frozenset((KIND_REQUIREMENT, KIND_NON_GOAL))

#: "R-1", "R-2", ...: what a PR body, an acceptance check and the
#: integration review cite. A story id such as "US-001" does not match.
_REQUIREMENT_ID = re.compile(r"R-[1-9][0-9]*")


@dataclass(frozen=True)
class SpecRequirement:
    """One requirement or non-goal, with the ids of the stories that build it."""

    id: str
    kind: str
    statement: str
    stories: tuple[str, ...]


def _entry_errors(index: int, entry: Any) -> list[str]:
    """Everything wrong with ONE raw entry, without the story join."""
    prefix = f"requirements[{index}]"
    if not isinstance(entry, dict):
        return [f"{prefix}: must be an object, got {type(entry).__name__}"]
    errors: list[str] = []
    raw_id = entry.get("id")
    error = required_field_error(prefix, "id", raw_id)
    if error is None and not _REQUIREMENT_ID.fullmatch(str(raw_id)):
        error = f"{prefix}.id: {raw_id!r} is not of the form 'R-<n>', for example 'R-1'"
    for found in (
        error,
        enum_field_error(prefix, "kind", entry.get("kind"), REQUIREMENT_KINDS),
        required_field_error(prefix, "statement", entry.get("statement")),
    ):
        if found is not None:
            errors.append(found)
    stories = entry.get("stories")
    if not isinstance(stories, list) or not all(isinstance(s, str) and s for s in stories):
        errors.append(f"{prefix}.stories: must be an array of story ids")
    elif entry.get("kind") == KIND_NON_GOAL and stories:
        errors.append(f"{prefix}.stories: a non_goal is built by no story, so it must be []")
    return errors


def _join_errors(raw: list[Any], story_ids: Collection[str]) -> list[str]:
    """Every requirement names known stories, and every story is named."""
    errors: list[str] = []
    named: set[str] = set()
    for index, entry in enumerate(raw):
        if entry["kind"] != KIND_REQUIREMENT:
            continue
        if not entry["stories"]:
            errors.append(
                f"requirements[{index}].stories: a requirement must name the "
                f"stories that build it, at least one"
            )
        for story in entry["stories"]:
            named.add(story)
            if story not in story_ids:
                errors.append(f"requirements[{index}].stories: unknown story id {story!r}")
    errors.extend(
        f"requirements: story {story!r} is named by no requirement; name it in "
        f"the 'stories' of the requirement it builds"
        for story in sorted(story_ids)
        if story not in named
    )
    return errors


def requirements_payload_errors(data: Any, story_ids: Collection[str] | None) -> list[str]:
    """Everything wrong with the raw ``requirements`` array, indexed.

    ``story_ids`` is every user story id in the payload. ``None`` skips
    the join and makes the array optional: a halt returns no components,
    and the register on disk holds no stories to join against.
    """
    if not isinstance(data, dict):
        return ["output must be a JSON object"]
    if "requirements" not in data:
        if story_ids is None:
            return []
        return ["'requirements' is required when 'components' is not empty"]
    raw = data["requirements"]
    if not isinstance(raw, list):
        return [f"'requirements' must be an array, got {type(raw).__name__}"]
    errors: list[str] = []
    ids = [entry.get("id") if isinstance(entry, dict) else None for entry in raw]
    for index, entry in enumerate(raw):
        errors.extend(_entry_errors(index, entry))
        if isinstance(ids[index], str) and ids[index] in ids[:index]:
            errors.append(f"requirements[{index}].id: duplicate id {ids[index]!r}")
    if errors or story_ids is None:
        return errors
    return _join_errors(raw, story_ids)


def parse_requirements(data: Any) -> tuple[SpecRequirement, ...]:
    """Typed requirements from a payload ``requirements_payload_errors`` passed."""
    raw = data.get("requirements", []) if isinstance(data, dict) else []
    return tuple(
        SpecRequirement(
            id=entry["id"],
            kind=entry["kind"],
            statement=entry["statement"].strip(),
            stories=tuple(entry["stories"]),
        )
        for entry in raw
    )
