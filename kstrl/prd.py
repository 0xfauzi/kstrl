"""PRD (Product Requirements Document) loading and validation."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any

from kstrl.atomicio import atomic_write_json


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


# --- The routed spec findings (#260) -----------------------------------

_SPEC_ISSUES_KEY = "specIssues"

# The block is validated as an array and no further, deliberately
# (#260 review F2), which is the opposite of how ``fixtures`` is
# treated below.
#
# ``fixtures`` earns its strictness twice over: Phase 1 EXECUTES those
# entries as an oracle, and ``tamper_changes`` pins them, so an ignored
# key really would weaken a gate the component is judged by.
# ``specIssues`` is read by no gate at all.
#
# A first version copied the fixture rules anyway: closed key set,
# every value a string, ``appliesTo`` from a two-value enum, no empty
# array. Five plausible engineer edits hard-failed the run under it -
# resolving them all to ``[]``, adding a ``"resolved"`` key, writing
# ``appliesTo: "resolved"``, dropping ``suggestion`` as noise, and
# collapsing the block to a list of strings. All five failed inside
# ``PRD.load``, so ``verify.check_prd_stories`` reported "Failed to load
# PRD" and never reached ``_tamper_changes``: the operator got a schema
# error, after paying for the whole component, for annotating a comment.
#
# Strictness that protects no gate only manufactures that, so the field
# is lenient here and unpinned in ``tamper_changes``, whose docstring
# states what that costs.

# Keys stripped out of a PRD before it is pasted into an adversarial
# prompt (#260 review F1).
#
# Two enrolled prompts paste this file verbatim and untruncated:
# ``SECURITY_PROMPT`` under "BEGIN PRD (what the implementer was asked
# to build)" and ``DISTILL_PROMPT`` under "BEGIN ACCEPTANCE CRITERIA
# (from PRD)". Neither framing covers unresolved spec questions, and
# neither role acts on them: the security reviewer hunts vulnerabilities
# in a diff, and the distiller records what was built. The engineer is
# the only reader ``specIssues`` was routed for.
#
# Measured on the real document-format PRD carrying run 4's 15 routed
# findings: leaving the block in grew SECURITY_PROMPT by 60.2 percent
# and took the PRD from 50 to 69 percent of the whole prompt. That is a
# change to what an enrolled adversarial role READS, which is the
# quantity H2 governs, reached without editing any prompt constant. The
# fix is to not deliver it rather than to pay for a calibration run.
#
# For this key a strip beats a cap: a cap would still hand the reviewer
# a truncated block it has no use for, and the growth is unbounded in
# the number of findings, so the honest answer is zero. That reasoning
# is about a key no prompt asks for and does not generalise. ``notes``,
# for one, is unbounded, engineer-written and genuinely wanted by both
# prompts, so it stays whole and stays uncapped here.
PROMPT_EXCLUDED_KEYS: frozenset[str] = frozenset({_SPEC_ISSUES_KEY})


def prd_text_for_prompt(text: str) -> str:
    """``text`` without the keys no reviewer prompt asks for.

    Takes the already-read bytes rather than a path, so it adds no new
    failure mode to either call site and leaves their existing error
    handling exactly as it was.

    Text that is not a JSON object comes back untouched: this is a
    filter, not a validator, and both callers already paste whatever
    they read. Nothing routed can reach them that way in practice,
    because Phase 1 ``check_prd_stories`` loads the same file and fails
    the component before either role runs.
    """
    try:
        data = json.loads(text)
    except ValueError:
        return text
    if not isinstance(data, dict):
        return text
    if not PROMPT_EXCLUDED_KEYS & set(data):
        return text
    kept = {k: v for k, v in data.items() if k not in PROMPT_EXCLUDED_KEYS}
    return json.dumps(kept, indent=2) + "\n"


def _key_set_errors(prefix: str, actual: set[str], expected: set[str]) -> list[str]:
    """How ``actual`` differs from the closed key set ``expected``.

    The fixture validator and the user-story loop ask the same question
    and phrase the answer the same way; this is the one writer of both
    messages. Callers decide what a difference means: both stop, because
    the per-field checks below them index keys they can no longer trust.
    """
    errors: list[str] = []
    missing = expected - actual
    if missing:
        errors.append(f"{prefix}: missing keys: {', '.join(sorted(missing))}")
    extra = actual - expected
    if extra:
        errors.append(f"{prefix}: unexpected keys: {', '.join(sorted(extra))}")
    return errors


def _validate_string_list(prefix: str, value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(s, str) for s in value):
        return [f"{prefix}: must be an array of strings"]
    return []


def _validate_fixture_entry(prefix: str, entry: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(entry, dict):
        return [f"{prefix}: must be an object"]

    errors.extend(_key_set_errors(prefix, set(entry.keys()), _FIXTURE_ENTRY_KEYS))
    if errors:
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
            errors.append(f"{prefix}.input_data.command: must be a non-empty string")
        if "exit_code" in expected and (
            isinstance(expected["exit_code"], bool) or not isinstance(expected["exit_code"], int)
        ):
            errors.append(f"{prefix}.expected.exit_code: must be an integer")
        for key in ("stdout_contains", "stdout_not_contains"):
            if key in expected:
                errors.extend(
                    _validate_string_list(
                        f"{prefix}.expected.{key}",
                        expected[key],
                    )
                )
    elif fixture_type == "function":
        for key in ("module", "function"):
            value = input_data.get(key)
            if not isinstance(value, str) or not value:
                errors.append(f"{prefix}.input_data.{key}: must be a non-empty string")
        if "args" in input_data and not isinstance(input_data["args"], list):
            errors.append(f"{prefix}.input_data.args: must be an array")
        if "kwargs" in input_data and not isinstance(input_data["kwargs"], dict):
            errors.append(f"{prefix}.input_data.kwargs: must be an object")
        if "raises" in expected:
            if not isinstance(expected["raises"], str) or not expected["raises"]:
                errors.append(f"{prefix}.expected.raises: must be a non-empty string")
            if "returns" in expected:
                errors.append(f"{prefix}.expected: 'returns' and 'raises' are mutually exclusive")
    elif fixture_type == "file":
        path_value = input_data.get("path")
        if not isinstance(path_value, str) or not path_value:
            errors.append(f"{prefix}.input_data.path: must be a non-empty string")
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
                        f"{prefix}.expected.{key}",
                        expected[key],
                    )
                )
    return errors


def _validate_allowed_path_items(key: str, value: list[Any]) -> list[str]:
    if not all(isinstance(p, str) and p for p in value):
        return [f"{key}: all items must be non-empty strings"]
    return []


def _validate_fixture_entries(key: str, value: list[Any]) -> list[str]:
    errors: list[str] = []
    for i, entry in enumerate(value):
        errors.extend(_validate_fixture_entry(f"{key}[{i}]", entry))
    return errors


# Every optional top-level PRD key, and how far its contents are
# checked. One row per key is the whole story: the set of optional keys
# is derived from it below, so a key cannot be added here and forgotten
# in ``validate_schema``, or the other way round.
#
# ``empty_hint`` None means an empty array is allowed; a string is the
# advice printed when the array is present and empty.
# ``item_validator`` None means the items are not inspected at all.
# ``specIssues`` takes None for both because no gate reads it: see its
# comment above for why strictness there only manufactures failures.
_OPTIONAL_ARRAYS: tuple[
    tuple[str, str | None, Callable[[str, list[Any]], list[str]] | None], ...
] = (
    (
        "allowedPaths",
        "omit the field entirely to leave scope unconstrained",
        _validate_allowed_path_items,
    ),
    (
        "fixtures",
        "omit the field entirely when there are none",
        _validate_fixture_entries,
    ),
    (_SPEC_ISSUES_KEY, None, None),
)

_OPTIONAL_KEYS = frozenset(key for key, _, _ in _OPTIONAL_ARRAYS)


def _validate_optional_arrays(data: dict[str, Any]) -> list[str]:
    """Shape errors for the optional array-valued PRD keys."""
    errors: list[str] = []
    for key, empty_hint, item_validator in _OPTIONAL_ARRAYS:
        if key not in data:
            continue
        value = data[key]
        if not isinstance(value, list):
            errors.append(f"{key} must be an array")
        elif not value:
            if empty_hint is not None:
                errors.append(f"{key} must be non-empty when present ({empty_hint})")
        elif item_validator is not None:
            errors.extend(item_validator(key, value))
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
    # The architect's non-blocker spec findings on this component's
    # surface (#260), routed here by ``decompose.route_spec_issues``.
    # Informational: no gate reads them. They are here because the
    # engineer's first instruction is to read this file, and before
    # this field the majors and minors were written to
    # spec-issues.json, which nothing in kstrl opens.
    #
    # ``list[Any]``, not ``list[dict[str, str]]``: the harness writes
    # that shape but the schema only requires an array, so an engineer
    # that annotated the block loads back as whatever it wrote. Typing
    # it tighter would be a claim the validator does not enforce.
    spec_issues: list[Any] | None = None

    @classmethod
    def load(cls, path: Path) -> PRD:
        """Load PRD from JSON file.

        utf-8 pinned to match ``save`` (#291): the read side has to name
        the same encoding as the write side or the file is only readable
        in the locale that happened to write it.
        """
        with open(path, encoding="utf-8") as f:
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

        # No coercion of a non-list allowedPaths (#293 review):
        # validate_schema above rejects one outright, so this was
        # unreachable, and reading a malformed scope as "no scope" is
        # exactly the conflation #269/#293 removed elsewhere.
        allowed_paths = data.get("allowedPaths")
        return cls(
            branch_name=data["branchName"],
            user_stories=stories,
            allowed_paths=allowed_paths,
            fixtures=data.get("fixtures"),
            spec_issues=data.get(_SPEC_ISSUES_KEY),
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
        - specIssues (optional): an array, and nothing further. The
          routed spec audit is a note to the engineer that no gate
          reads, so it is lenient where ``fixtures`` is strict and an
          empty array is accepted (#260).
        - Field types are strictly enforced.
        """
        errors: list[str] = []

        if not isinstance(data, dict):
            errors.append("PRD must be a JSON object")
            return errors

        required_keys = {"branchName", "userStories"}
        actual_keys = set(data.keys())
        missing = required_keys - actual_keys
        extra = actual_keys - required_keys - _OPTIONAL_KEYS

        if missing or extra:
            if missing:
                errors.append(f"Missing required keys: {', '.join(sorted(missing))}")
            if extra:
                errors.append(f"Unexpected keys: {', '.join(sorted(extra))}")
            return errors

        errors.extend(_validate_optional_arrays(data))

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
            key_errors = _key_set_errors(story_prefix, set(story.keys()), story_keys)
            if key_errors:
                errors.extend(key_errors)
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

    def _pinned_stories(self) -> dict[str, UserStory]:
        """Each story with the engineer-writable fields blanked out.

        ``passes`` and ``notes`` are the ONLY two an engineer may
        rewrite: setting ``passes`` is the whole job, ``notes`` is where
        it records what it did, and they are also the only fields
        ``review.revert_unconfirmed_stories`` touches, so the harness's
        own set-point write cannot look like tampering.

        Blanking them and comparing whole stories through
        ``UserStory``'s generated ``__eq__`` means a field added to the
        dataclass later is pinned BY DEFAULT rather than silently
        exempt. That is the fail-closed direction: forgetting to pin a
        new field would let an agent edit it unnoticed, while forgetting
        to exempt one produces a loud, fixable refusal.
        """
        return {s.id: replace(s, passes=False, notes="") for s in self.user_stories}

    def tamper_changes(self, pre_run: PRD) -> list[str]:
        """How ``self`` differs from ``pre_run`` in ways no engineer may.

        The field policy for #264's carve-out: the component PRD is
        inside every component's write scope by design, so the file
        Phase 1 trusts is a file the agent edits. ``check_prd_stories``
        re-reads the stories from it and ``check_fixtures_from_prd``
        re-reads the fixtures, so an unpinned PRD lets an agent delete a
        criterion or neuter an executable oracle and pass a gate it
        authored. Returns one clause per change, empty when the PRD is
        untouched in every pinned respect.

        ``allowedPaths`` is deliberately NOT compared (#269), and that
        comparison is gone rather than relaxed: the scope both guards
        enforce is resolved before the run starts, so editing this field
        changes nothing and refusing an edit to it could only ever be a
        false positive. ``kstrl.scope`` records why.

        ``specIssues`` is not compared either (#260), and the honest
        statement of that is stronger than "safe to edit": this field
        fails OPEN. An engineer may reword the findings, resolve them,
        or delete the block, and nothing here or anywhere else notices.
        A deletion also persists, because ``factory._run_component``
        seeds the worktree copy only when it does not already exist, so
        iteration 2 reads what iteration 1 left. What that costs is
        bounded by what the field is: a note nothing is judged against.
        The audit itself is not at risk, because spec-issues.json is
        written by the architect before any worktree exists and no
        component can reach it.

        Everything that IS compared is compared for equality, ORDER
        INCLUDED, because the engineer is not meant to touch these
        fields at all: any difference is a rewrite, and the remedy
        ("restore the file") is always available.
        """
        changes: list[str] = []
        if pre_run.branch_name != self.branch_name:
            changes.append(
                f"changed branchName from {pre_run.branch_name!r} to {self.branch_name!r}"
            )
        before = pre_run._pinned_stories()
        after = self._pinned_stories()
        if set(before) != set(after):
            changes.append(
                f"changed the story set from {', '.join(sorted(before)) or '(none)'} "
                f"to {', '.join(sorted(after)) or '(none)'}"
            )
        else:
            for story_id in sorted(before):
                moved = [
                    f.name
                    for f in fields(UserStory)
                    if getattr(before[story_id], f.name) != getattr(after[story_id], f.name)
                ]
                if moved:
                    changes.append(f"rewrote {', '.join(moved)} on story {story_id}")
        if pre_run.fixtures != self.fixtures:
            changes.append("changed the approved fixtures")
        return changes

    def get_next_story(self) -> UserStory | None:
        """Get the highest-priority failing story."""
        failing = [s for s in self.user_stories if not s.passes]
        if not failing:
            return None
        return min(failing, key=lambda s: s.priority)

    def save(self, path: Path) -> None:
        """Save PRD back to JSON file.

        Round-trips the optional fields: dropping ``allowedPaths`` on a
        save would silently unbind the component's diff scope, dropping
        ``fixtures`` would silently disable the behavioral oracle
        (R7.2), and dropping ``specIssues`` would take the architect's
        findings back off the engineer's desk (#260).
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
        if self.spec_issues is not None:
            data[_SPEC_ISSUES_KEY] = self.spec_issues
        # R10.3: written atomically, through the shared helper that owns
        # that pattern for every file kstrl must not leave half-written
        # (#291; manifest.save, knowledge.write_facts, decompose's PRD
        # writer go through the same one). Until R10.3 nothing in kstrl called
        # this method: the PRD was written once by the architect and
        # then edited only by the agent. The set-point check made the
        # harness a writer of a file the next attempt reads, and a torn
        # write there costs the run. The bytes are unchanged - two-space
        # indent, one trailing newline - so a save of an unmodified PRD
        # is byte-identical to what the architect wrote.
        atomic_write_json(path, data)
