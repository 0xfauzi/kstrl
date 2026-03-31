"""PRD (Product Requirements Document) loading, validation, and management."""

from __future__ import annotations

import json
import re
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


# -- Section names that should be treated as metadata, not stories --
_META_SECTIONS = {
    "overview",
    "background",
    "context",
    "introduction",
    "intro",
    "summary",
    "tech stack",
    "technology",
    "technologies",
    "stack",
    "architecture",
    "design",
    "notes",
    "references",
    "appendix",
    "glossary",
    "verification",
    "testing",
    "test plan",
    "non-functional requirements",
    "constraints",
    "assumptions",
    "dependencies",
}


@dataclass
class ParsedMarkdown:
    """Result of parsing a free-form markdown document into PRD components."""

    feature_overview: str
    stories: list[dict[str, str]]  # [{"title": ..., "criteria": ...}]
    tech_stack: str
    verification_commands: str


def parse_markdown_to_stories(content: str) -> ParsedMarkdown:
    """Parse a free-form markdown document into story components.

    Parsing strategy:
    - ## or ### headings become story titles (unless they match meta-section names)
    - Bullet items (- or * or 1.) under a heading become acceptance criteria
    - Meta-sections (Overview, Background, Tech Stack, etc.) are extracted as metadata
    - If no headings exist, paragraphs separated by blank lines become stories
    """
    lines = content.splitlines()
    feature_overview = ""
    tech_stack = ""
    verification_commands = ""
    stories: list[dict[str, str]] = []

    current_heading: str | None = None
    current_bullets: list[str] = []
    current_text: list[str] = []

    def _flush_section() -> None:
        nonlocal feature_overview, tech_stack, verification_commands
        if current_heading is None:
            return

        heading_lower = current_heading.lower().strip()

        if heading_lower in _META_SECTIONS or any(
            heading_lower.startswith(m) for m in _META_SECTIONS
        ):
            text_block = "\n".join(current_text).strip()
            bullets_block = "\n".join(current_bullets).strip()
            combined = (text_block + "\n" + bullets_block).strip()

            overview_keys = {
                "overview", "background", "context", "introduction", "intro", "summary",
            }
            tech_keys = {
                "tech stack", "technology", "technologies", "stack", "architecture",
            }
            verify_keys = {"verification", "testing", "test plan"}

            if heading_lower in overview_keys:
                feature_overview = (feature_overview + "\n" + combined).strip()
            elif heading_lower in tech_keys:
                tech_stack = (tech_stack + "\n" + combined).strip()
            elif heading_lower in verify_keys:
                verification_commands = (verification_commands + "\n" + combined).strip()
        else:
            criteria_text = (
                "\n".join(current_bullets) if current_bullets
                else "\n".join(current_text)
            )
            stories.append({
                "title": current_heading.strip(),
                "criteria": criteria_text.strip(),
            })

    for line in lines:
        stripped = line.strip()

        # Detect headings (## or ###, skip # which is usually document title)
        if stripped.startswith("## ") or stripped.startswith("### "):
            _flush_section()
            current_heading = stripped.lstrip("#").strip()
            current_bullets = []
            current_text = []
            continue

        # Detect # heading as document title -> feature overview
        if stripped.startswith("# ") and not stripped.startswith("## "):
            _flush_section()
            # Use the title text as part of feature overview if we have no heading yet
            title_text = stripped.lstrip("#").strip()
            if not feature_overview:
                feature_overview = title_text
            current_heading = None
            current_bullets = []
            current_text = []
            continue

        # Detect bullet items
        if stripped.startswith("- ") or stripped.startswith("* "):
            current_bullets.append(stripped[2:].strip())
            continue

        # Detect numbered items
        numbered = re.match(r"^\d+[\.\)]\s+(.+)", stripped)
        if numbered:
            current_bullets.append(numbered.group(1).strip())
            continue

        # Regular text
        if stripped:
            current_text.append(stripped)

    # Flush last section
    _flush_section()

    # Fallback: if no stories were found from headings, split by paragraphs
    if not stories:
        paragraphs: list[str] = []
        current_para: list[str] = []
        for line in lines:
            if line.strip():
                current_para.append(line.strip())
            elif current_para:
                paragraphs.append("\n".join(current_para))
                current_para = []
        if current_para:
            paragraphs.append("\n".join(current_para))

        for para in paragraphs:
            # Skip very short paragraphs (likely titles or separators)
            if len(para) < 10:
                continue
            # Use first line as title, rest as criteria
            para_lines = para.splitlines()
            title = para_lines[0][:80]
            criteria = "\n".join(para_lines[1:]) if len(para_lines) > 1 else ""
            stories.append({"title": title, "criteria": criteria})

    return ParsedMarkdown(
        feature_overview=feature_overview,
        stories=stories,
        tech_stack=tech_stack,
        verification_commands=verification_commands,
    )
