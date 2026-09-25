"""The integration review's rules (#482; design #480 sections 3.2 and 3.3).

Pure data: nothing here runs git or an agent. The one writer is the PRD
file the reviewer is handed. The factory-facing half is
``kstrl/integration_phase.py``.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path

from kstrl.contract import ContractResult
from kstrl.prd import PRD, UserStory
from kstrl.review import (
    CriterionReview,
    ReviewConcern,
    ReviewMode,
    ReviewResult,
    ReviewVerdict,
    normalize_story_id,
)

INTEGRATION_CRITERIA_PROMPT_VERSION = "1.1.0"

# One story per line: "id | title | criterion". Instruction to the reviewer
# LLM, and the detector itself, so it is enrolled (H3). Its H2 roles are
# `integration` and `integration_clean`, scored on the #480 section 8
# fixtures in tests/adversarial_fixtures/integration/.
INTEGRATION_CRITERIA_PROMPT = """\
IC1 | Calls across component boundaries | Every call from one component into another passes inputs the callee documents as valid and handles every outcome the callee documents, errors and empty results included, and every such callee's docstring describes what its callers actually do.
IC2 | Stored data read back | Data that one component writes and another component reads back is read under rules that still accept everything written before, so a validation rule tightened for new input cannot make stored data unreadable.
IC3 | One definition per shared rule | A value or rule that more than one component depends on is defined once and imported by the others, unless the specification states them as separate rules that share a value, in which case separate definitions are correct and are not a defect.
IC4 | Calls into code that predates the feature | Every call the feature makes into code that already existed at commit {feature_base_sha} uses that code as its docstring and its tests state, including argument formats, return values and raised errors.
IC5 | Decisions agree with criteria | Each decision in scripts/kstrl/decisions.json agrees with every other decision in that file and with the acceptance criteria of the component it binds, which are in the prd.json file in the directory of that component under scripts/kstrl/feature/. If decisions.json does not exist at this commit, this criterion passes and the explanation says the file is absent.
"""

INTEGRATION_CARRIED_PROMPT_VERSION = "1.0.0"

# The criterion of the story that carries one open finding into the next
# review (#483; design #480 section 3.2, IF-n). Instruction to the reviewer
# LLM, so it is enrolled (H3). Its H2 role is `integration`, which has no
# fixture yet.
INTEGRATION_CARRIED_PROMPT = "The defect {text} at {locations} no longer holds."

#: A finding's id, and the id of the story that carries it (#483).
FINDING_ID_PREFIX = "IF-"

#: The criterion about the decision register. A fail here is a register
#: finding: recorded and handed off, never a code finding (design 3.3, 3.4).
REGISTER_STORY_ID = "IC5"

#: Concern categories defined relative to the PRD (REVIEWER_PROMPT), so a
#: fail in one is recorded and opens nothing. Must stay a subset of
#: review.VALID_CONCERN_CATEGORIES; a test pins that.
RECORD_ONLY_CONCERN_CATEGORIES = frozenset({"scope_creep", "unrelated_change"})

KIND_TEST = "test"
KIND_CRITERION = "criterion"
KIND_CONCERN = "concern"
KIND_REGISTER = "register"
FINDING_KINDS = (KIND_TEST, KIND_CRITERION, KIND_CONCERN, KIND_REGISTER)

STATUS_OPEN = "open"
STATUS_HANDOFF = "handoff"
STATUS_CLOSED = "closed"
FINDING_STATUSES = (STATUS_OPEN, STATUS_HANDOFF, STATUS_CLOSED)

_PATH_TOKEN = re.compile(r"[\w./-]*\w\.[A-Za-z][A-Za-z0-9]{0,4}\b")


@dataclass(frozen=True)
class ExpectedStory:
    story_id: str
    title: str
    criterion: str


@dataclass(frozen=True)
class OpenedFinding:
    kind: str
    story_id: str
    category: str
    text: str
    suggestion: str
    locations: tuple[str, ...]
    missing_locations: tuple[str, ...]
    status: str


@dataclass(frozen=True)
class RecordedOnly:
    kind: str
    story_id: str
    category: str
    severity: str
    text: str


@dataclass(frozen=True)
class IntegrationOutcome:
    """A round's reading. Non-empty ``errors`` means red: nothing opened."""

    errors: tuple[str, ...] = ()
    opened: tuple[OpenedFinding, ...] = ()
    recorded: tuple[RecordedOnly, ...] = ()
    #: Carried findings (#483) whose story got its single pass verdict.
    closed: tuple[str, ...] = ()
    #: Carried findings whose story got fail or advisory: still open.
    still_open: tuple[str, ...] = ()


def render_integration_criteria(feature_base_sha: str) -> str:
    """The enrolled template with this feature's base commit filled in."""
    return INTEGRATION_CRITERIA_PROMPT.format(feature_base_sha=feature_base_sha)


def integration_stories(feature_base_sha: str) -> tuple[ExpectedStory, ...]:
    """IC1 to IC5 from the enrolled template. Raises ValueError on a line
    that is not ``id | title | criterion``."""
    stories: list[ExpectedStory] = []
    for number, line in enumerate(
        render_integration_criteria(feature_base_sha).splitlines(), start=1
    ):
        if not line.strip():
            continue
        parts = [part.strip() for part in line.split(" | ")]
        if len(parts) != 3 or not all(parts):
            raise ValueError(
                f"INTEGRATION_CRITERIA_PROMPT line {number} is not 'id | title | criterion': {line!r}"
            )
        stories.append(ExpectedStory(story_id=parts[0], title=parts[1], criterion=parts[2]))
    return tuple(stories)


def carried_story(finding_id: str, text: str, locations: Sequence[str]) -> ExpectedStory:
    """The story that carries one open finding into the next review (#483).

    The story id is the finding id, and it is what the verdict is joined by
    (``_story_errors``). The criterion is one line, so the PRD the reviewer
    reads shows it as one bullet.
    """
    return ExpectedStory(
        story_id=finding_id,
        title=finding_id,
        criterion=INTEGRATION_CARRIED_PROMPT.format(
            text=" ".join(text.split()), locations=", ".join(locations)
        ),
    )


def write_integration_prd(path: Path, stories: Sequence[ExpectedStory]) -> None:
    """The PRD the reviewer is handed: one criterion per story. The parent must exist."""
    PRD(
        branch_name="kstrl/integration-review",
        user_stories=[
            UserStory(
                id=story.story_id,
                title=story.title,
                acceptance_criteria=[story.criterion],
                priority=number,
                passes=False,
                notes="",
            )
            for number, story in enumerate(stories, start=1)
        ],
    ).save(path)


def cited_paths(text: str) -> tuple[str, ...]:
    """Repository-relative file paths named in ``text``, in order, once each.
    Candidates only: :func:`scope_locations` resolves them against the files
    tracked at the reviewed commit."""
    found: list[str] = []
    for token in _PATH_TOKEN.findall(text):
        path = token.removeprefix("./")
        if path.startswith("/") or ".." in path.split("/") or path in found:
            continue
        found.append(path)
    return tuple(found)


def scope_locations(text: str, tracked: Collection[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(located, missing)`` for the paths ``text`` cites (#500).

    A cited path tracked as written is itself. Otherwise it names the one
    tracked file whose trailing whole path segments equal it: ``storage.py``
    names ``src/snippetvault/storage.py`` when no other tracked file ends in
    ``/storage.py``. A path matching no tracked file, or more than one, is
    missing: two candidates are never chosen between. ``located`` holds each
    resolved file once, in citation order; ``missing`` holds the cited
    spelling of each path that resolved to nothing.
    """
    located: list[str] = []
    missing: list[str] = []
    for path in cited_paths(text):
        resolved = _resolve_location(path, tracked)
        if not resolved:
            missing.append(path)
        elif resolved not in located:
            located.append(resolved)
    return tuple(located), tuple(missing)


def _resolve_location(path: str, tracked: Collection[str]) -> str:
    if path in tracked:
        return path
    candidates = [name for name in tracked if name.endswith("/" + path)]
    return candidates[0] if len(candidates) == 1 else ""


def integration_outcome(
    test_result: ContractResult | None,
    review_result: ReviewResult,
    expected: Sequence[ExpectedStory],
    *,
    tracked: Collection[str],
) -> IntegrationOutcome:
    """The only place the design 3.3 rules are written.

    ``test_result`` is the round's integrated check, or None when the tests
    did not run. ``tracked`` is every file tracked at the reviewed commit,
    which each cited path is resolved against (:func:`scope_locations`).
    The verdict is read from the validated verdicts and never
    from ``review_result.passed``, which counts every fail concern,
    including the record-only ones.
    """
    errors = review_errors(review_result, expected)
    if errors:
        return IntegrationOutcome(errors=tuple(errors))
    opened: list[OpenedFinding] = []
    recorded: list[RecordedOnly] = []
    if test_result is not None and not test_result.passed:
        output = test_result.test_output.strip()
        opened.append(
            _scoped(
                KIND_TEST,
                "",
                "",
                output or "the integrated tests failed with no output",
                "",
                output,
                tracked,
            )
        )
    by_story = {normalize_story_id(cr.story_id): cr for cr in review_result.criteria}
    closed: list[str] = []
    still_open: list[str] = []
    for story in expected:
        verdict = by_story[normalize_story_id(story.story_id)]
        if story.story_id.startswith(FINDING_ID_PREFIX):
            _read_carried(story, verdict, closed, still_open)
            continue
        _read_verdict(story, verdict, opened, recorded, tracked)
    for concern in review_result.concerns:
        _read_concern(concern, opened, recorded, tracked)
    return IntegrationOutcome(
        opened=tuple(opened),
        recorded=tuple(recorded),
        closed=tuple(closed),
        still_open=tuple(still_open),
    )


def review_errors(result: ReviewResult, expected: Sequence[ExpectedStory]) -> list[str]:
    """Everything that makes a review unreadable. Empty means valid."""
    errors = _result_errors(result)
    errors.extend(_verdict_errors(result.criteria, expected))
    return errors


def _result_errors(result: ReviewResult) -> list[str]:
    errors: list[str] = []
    if result.infrastructure_error:
        errors.append(f"review infrastructure error: {result.overall_notes or 'no detail'}")
    if result.coverage_refused:
        errors.append(f"review coverage refused: {result.diffstat_disagreement}")
    if result.mode != ReviewMode.HARD.value:
        errors.append(f"review ran in {result.mode!r} mode; only a hard-mode review can be read")
    if result.concerns_not_list:
        errors.append("review output: 'concerns' is missing or is not a list")
    if result.dropped_concerns:
        errors.append(
            f"review output: {result.dropped_concerns} malformed concern entries were dropped"
        )
    return errors


def _verdict_errors(
    criteria: Sequence[CriterionReview], expected: Sequence[ExpectedStory]
) -> list[str]:
    wanted = {normalize_story_id(story.story_id): story for story in expected}
    errors = [
        f"verdict for unexpected story id {cr.story_id!r}"
        for cr in criteria
        if normalize_story_id(cr.story_id) not in wanted
    ]
    for key, story in wanted.items():
        errors.extend(
            _story_errors(story, [cr for cr in criteria if normalize_story_id(cr.story_id) == key])
        )
    return errors


def _story_errors(story: ExpectedStory, verdicts: Sequence[CriterionReview]) -> list[str]:
    """A story is judged when exactly one verdict names its id and that
    verdict has an explanation. The story id is the join (#518). The
    criterion text the reviewer echoes is recorded in the evidence and never
    compared: a reviewer that rewords or shortens it has still judged the
    story its id names."""
    if len(verdicts) != 1:
        return [f"story {story.story_id}: {len(verdicts)} verdicts; exactly one is required"]
    if not verdicts[0].explanation.strip():
        return [f"story {story.story_id}: empty explanation"]
    return []


def _read_carried(
    story: ExpectedStory,
    verdict: CriterionReview,
    closed: list[str],
    still_open: list[str],
) -> None:
    """A carried finding closes only on its single pass verdict (#483). Fail
    and advisory keep it open, and it never opens a second finding."""
    if verdict.verdict == ReviewVerdict.PASS.value:
        closed.append(story.story_id)
    else:
        still_open.append(story.story_id)


def _read_verdict(
    story: ExpectedStory,
    verdict: CriterionReview,
    opened: list[OpenedFinding],
    recorded: list[RecordedOnly],
    tracked: Collection[str],
) -> None:
    if verdict.verdict == ReviewVerdict.ADVISORY.value:
        recorded.append(
            RecordedOnly(
                KIND_CRITERION,
                story.story_id,
                "",
                "advisory",
                f"{story.story_id}: {verdict.explanation}",
            )
        )
        return
    if verdict.verdict != ReviewVerdict.FAIL.value:
        return
    text = f"{story.story_id} failed: {verdict.explanation}"
    if normalize_story_id(story.story_id) == normalize_story_id(REGISTER_STORY_ID):
        opened.append(
            OpenedFinding(
                KIND_REGISTER, story.story_id, "", text, verdict.suggestion, (), (), STATUS_HANDOFF
            )
        )
        return
    opened.append(
        _scoped(
            KIND_CRITERION,
            story.story_id,
            "",
            text,
            verdict.suggestion,
            f"{verdict.explanation} {verdict.suggestion}",
            tracked,
        )
    )


def _read_concern(
    concern: ReviewConcern,
    opened: list[OpenedFinding],
    recorded: list[RecordedOnly],
    tracked: Collection[str],
) -> None:
    if concern.severity != "fail" or concern.category in RECORD_ONLY_CONCERN_CATEGORIES:
        recorded.append(
            RecordedOnly(
                KIND_CONCERN,
                "",
                concern.category,
                concern.severity,
                f"{concern.location}: {concern.explanation}",
            )
        )
        return
    opened.append(
        _scoped(
            KIND_CONCERN,
            "",
            concern.category,
            concern.explanation,
            concern.suggestion,
            concern.location,
            tracked,
        )
    )


def _scoped(
    kind: str,
    story_id: str,
    category: str,
    text: str,
    suggestion: str,
    cited_in: str,
    tracked: Collection[str],
) -> OpenedFinding:
    """A finding that cites a file tracked at the reviewed commit is open; one
    citing none cannot be scoped and is handed off, never widened."""
    present, missing = scope_locations(cited_in, tracked)
    status = STATUS_OPEN if present else STATUS_HANDOFF
    return OpenedFinding(kind, story_id, category, text, suggestion, present, missing, status)
