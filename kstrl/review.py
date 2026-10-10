"""Phase 2: Second-opinion review - a separate agent reviews the diff against the spec."""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from kstrl import git
from kstrl.decompose import (
    AgentOutputTooLarge,
    _extract_json,
    _select_agent_output,
    collect_agent_output,
)
from kstrl.findings import (
    Finding,
    dump_raw_debug,
    tag_finding_with_model,
)
from kstrl.prd import PRD
from kstrl.review_prompt import build_review_prompt
from kstrl.verify_model import VerificationResult

if TYPE_CHECKING:
    from kstrl.agents.base import Agent
    from kstrl.ui.base import UI


class ReviewVerdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    ADVISORY = "advisory"


class ReviewMode(StrEnum):
    HARD = "hard"
    ADVISORY = "advisory"
    SKIP = "skip"


VALID_CONCERN_CATEGORIES = frozenset(
    {
        "scope_creep",
        "security_concern",
        "test_quality",
        "test_weakening",
        "unrelated_change",
        "dead_code",
        "error_handling",
        "copy_paste",
        "other",
    }
)

# The two severities a reviewer concern carries. The parser drops a
# concern with any other, and the calibration scorer reads a fixture's
# ``severity_at_least`` against this same set (#564).
VALID_CONCERN_SEVERITIES = frozenset({ReviewVerdict.FAIL.value, ReviewVerdict.ADVISORY.value})

# R1.1: whitelist of criterion verdicts accepted from the reviewer,
# compared case-insensitively after stripping. The prompt schema
# promises pass|fail|advisory; anything else ("Blocked", "n/a", a
# missing key) is a parse failure - infrastructure error, never a
# silently non-blocking advisory.
VALID_CRITERION_VERDICTS = frozenset(
    {
        ReviewVerdict.PASS.value,
        ReviewVerdict.FAIL.value,
        ReviewVerdict.ADVISORY.value,
    }
)


# R10.3: story ids are compared case-insensitively after stripping.
# parse_review_output's coverage gate has matched ids this way since
# R1.1; the claim check compares the same ids against the PRD, so
# the rule lives in one place rather than being spelled out at each
# comparison. Matching is by id and never by criterion text.
def normalize_story_id(value: str) -> str:
    """The comparison form of a story id: stripped and lowercased."""
    return value.strip().lower()


# R10.3: severity order for rolling a story's criterion verdicts up into
# one story verdict. Fail dominates advisory dominates pass, so the
# story's verdict is the worst any of its criteria received.
_VERDICT_RANK: dict[str, int] = {
    ReviewVerdict.PASS.value: 0,
    ReviewVerdict.ADVISORY.value: 1,
    ReviewVerdict.FAIL.value: 2,
}
_RANK_VERDICT: dict[int, str] = {v: k for k, v in _VERDICT_RANK.items()}


@dataclass
class CriterionReview:
    """Review verdict for a single acceptance criterion.

    R10.3: ``story_id`` is the ``storyId`` the reviewer emitted for the
    story this criterion belongs to, stripped but otherwise verbatim.
    It is stored so the reviewer's verdicts can be compared against the
    engineer's per-story ``passes`` flag (claim agreement); before
    R10.3 the parser read the id and discarded it. Normalisation for
    comparison happens at lookup time, not here, so the raw value stays
    inspectable. Empty when the reviewer omitted the id, in which case
    the verdict cannot be attributed to a story.
    """

    criterion: str
    verdict: str
    explanation: str
    suggestion: str = ""
    story_id: str = ""


@dataclass
class ReviewConcern:
    """A cross-cutting concern the reviewer surfaced beyond the PRD criteria.

    The PRD lists what the implementer was supposed to do. Concerns are
    what they should NOT have done (scope creep, dead code, security
    smells) or did sloppily (tautological tests, swallowed errors). These
    are the bugs a real code reviewer catches that the PRD never asked
    about.
    """

    category: str
    severity: str  # "fail" or "advisory"
    location: str
    explanation: str
    suggestion: str = ""


@dataclass
class ReviewResult:
    """Aggregated review result across all stories."""

    passed: bool
    mode: str
    criteria: list[CriterionReview] = field(default_factory=list)
    concerns: list[ReviewConcern] = field(default_factory=list)
    overall_notes: str = ""
    raw_output: str = ""
    duration_seconds: float = 0.0
    # Self-reported claim that the reviewer searched thoroughly. Useful
    # as a hint when investigating reviews but DO NOT gate on it - it
    # cannot be verified at runtime. The trustworthy verification path
    # is the planted-bug calibration suite at tests/test_calibration.py
    # (runs with KSTRL_RUN_CALIBRATION=1) which catches reviewers that
    # claim exhaustive coverage but miss known bugs.
    exhaustively_searched: bool = False
    # E9: parallel to SecurityResult.infrastructure_error - True when
    # the agent failed to run or returned unparseable output, so
    # downstream callers can distinguish "clean review found nothing"
    # from "review never actually happened".
    infrastructure_error: bool = False
    # #266: the diffstat the reviewer says it saw, parsed from its
    # output. None when it reported none. Compared against git's own
    # numstat by ``run_review``; see ``git.diffstat_disagreement`` for
    # what that comparison does and does not prove.
    observed_diffstat: git.DiffStat | None = None
    # #266: set when the reviewer's diffstat did not match git's - i.e.
    # the verdict was reached without the whole change in hand. Carries
    # the human-readable disagreement. Hard mode treats it as an
    # infrastructure error (the review did not happen over the change);
    # advisory mode records it and continues, visibly.
    diffstat_disagreement: str = ""
    # R7.1: identity of the model that produced this review (the
    # agent's ``name``, e.g. "codex (gpt-5)"). Stamped by run_review;
    # empty when no reviewer ran (mode=skip). Flows onto every Finding
    # as a ``model:<id>`` tag and into the PR body, so same-family vs
    # cross-family review outcomes stay attributable.
    reviewer_model: str = ""
    # #482: concern entries parse_review_output dropped as malformed, and
    # whether ``concerns`` was missing or not a list. The per-component path
    # still ignores both, as it always has; the integration review refuses
    # a result carrying either (integration.integration_outcome).
    dropped_concerns: int = 0
    concerns_not_list: bool = False
    # #480: set by parse_review_output on a reply it refused that states
    # nothing a second reply could replace (``_nothing_to_discard``). Only
    # the integration review reads it, to ask the reviewer once more.
    reply_unread: bool = False
    # #480: the refused reading a re-ask replaced, kept for the audit
    # trail. None when the reviewer was asked once.
    replaced: ReviewResult | None = None

    @property
    def coverage_refused(self) -> bool:
        """#266: the coverage check REFUSED this review, not merely
        recorded a disagreement about it.

        ``apply_coverage_check`` records ``diffstat_disagreement`` in
        EVERY mode - that is how an advisory pass stays visibly
        unverified - and forces ``passed=False`` only in hard mode. The
        pipeline's wall has to key on the refusal, or advisory mode
        starts blocking, which is the one thing it promises never to do.

        A property rather than the predicate spelled out at each wall,
        because the two walls were not spelled the same: Phase 2's got
        the ``passed`` half for free by living inside ``_review_failure``
        (a passing review never reaches it) while Phase 2.5's sat in the
        open and had to state it. One of them was therefore relying on
        an invariant proved three functions away.
        """
        return bool(self.diffstat_disagreement) and not self.passed

    def as_retry_context(self) -> str:
        """Format failing/advisory findings for injection into retry prompt."""
        lines: list[str] = []
        for cr in self.criteria:
            if cr.verdict != ReviewVerdict.PASS.value:
                lines.append(f'- {cr.verdict.upper()}: "{cr.criterion}"')
                lines.append(f"  Explanation: {cr.explanation}")
                if cr.suggestion:
                    lines.append(f"  Suggestion: {cr.suggestion}")
        for concern in self.concerns:
            lines.append(f"- {concern.severity.upper()} {concern.category}: {concern.location}")
            lines.append(f"  Explanation: {concern.explanation}")
            if concern.suggestion:
                lines.append(f"  Suggestion: {concern.suggestion}")
        if self.overall_notes:
            lines.append(f"Overall: {self.overall_notes}")
        return "\n".join(lines)

    def as_pr_body_section(self) -> str:
        """Format all findings for PR description."""
        lines: list[str] = ["## Review Findings", ""]
        # Locals are explicitly criterion-only to avoid colliding with
        # the same-named instance properties (which sum criteria +
        # concerns). The header line below describes criteria; concerns
        # are summarized separately as "additional concerns".
        criterion_pass = sum(1 for c in self.criteria if c.verdict == ReviewVerdict.PASS.value)
        criterion_fail = sum(1 for c in self.criteria if c.verdict == ReviewVerdict.FAIL.value)
        criterion_adv = sum(1 for c in self.criteria if c.verdict == ReviewVerdict.ADVISORY.value)
        lines.append(
            f"**{criterion_pass} criteria passed, {criterion_fail} failed, "
            f"{criterion_adv} advisory; "
            f"{len(self.concerns)} additional concerns**"
        )
        if self.reviewer_model:
            lines.append("")
            lines.append(f"**Reviewer model**: {self.reviewer_model}")
        if self.diffstat_disagreement:
            lines.append("")
            lines.append(f"**UNVERIFIED COVERAGE (#266): {self.diffstat_disagreement}**")
        lines.append("")

        for cr in self.criteria:
            if cr.verdict == ReviewVerdict.PASS.value:
                icon = "pass"
            elif cr.verdict == ReviewVerdict.FAIL.value:
                icon = "FAIL"
            else:
                icon = "advisory"
            lines.append(f"- [{icon}] {cr.criterion}")
            if cr.verdict != ReviewVerdict.PASS.value:
                lines.append(f"  - {cr.explanation}")
                if cr.suggestion:
                    lines.append(f"  - Suggestion: {cr.suggestion}")

        if self.concerns:
            lines.append("")
            lines.append("### Reviewer concerns (beyond PRD)")
            for concern in self.concerns:
                icon = "FAIL" if concern.severity == "fail" else "advisory"
                lines.append(f"- [{icon}] **{concern.category}** at `{concern.location}`")
                lines.append(f"  - {concern.explanation}")
                if concern.suggestion:
                    lines.append(f"  - Suggestion: {concern.suggestion}")

        if self.overall_notes:
            lines.append("")
            lines.append(f"**Notes**: {self.overall_notes}")

        return "\n".join(lines)

    @property
    def fail_count(self) -> int:
        """Failed criteria plus failing concerns: one per ``fail`` row
        ``as_findings`` produces. The one definition the log line, the
        ``review_result`` event, the merge-gate evidence and the
        divergence reading all read (#450)."""
        return sum(1 for c in self.criteria if c.verdict == ReviewVerdict.FAIL.value) + sum(
            1 for c in self.concerns if c.severity == "fail"
        )

    @property
    def advisory_count(self) -> int:
        """Advisory criteria plus advisory concerns: one per ``advisory``
        row ``as_findings`` produces. See ``fail_count``."""
        return sum(1 for c in self.criteria if c.verdict == ReviewVerdict.ADVISORY.value) + sum(
            1 for c in self.concerns if c.severity == "advisory"
        )

    def story_verdicts(self) -> dict[str, str]:
        """R10.3: per-story verdict derived from criterion verdicts.

        "fail" if any criterion for the story is fail; else "advisory"
        if any is advisory; else "pass". A story with no criterion
        verdict is ABSENT from the dict rather than present with a
        default - that absence is how "the reviewer did not cover this
        story" is expressed, and it is a different fact from "the
        reviewer looked and was unhappy".

        Keys are normalised by ``normalize_story_id`` (stripped,
        lowercased), matching how the coverage gate has compared ids
        since R1.1. Look up with ``normalize_story_id(story.id)``, never
        with the raw id, or a reviewer writing "us-001" for a PRD's
        "US-001" reads as uncovered.

        Criteria whose ``story_id`` is empty are ignored: a verdict that
        names no story cannot be attributed to one.

        Fail dominates when one criterion comes back with two
        verdicts, which resolves toward the stricter reading. Hard mode
        takes the same direction everywhere else it has a choice.
        """
        worst: dict[str, int] = {}
        for cr in self.criteria:
            key = normalize_story_id(cr.story_id)
            # A verdict outside the whitelist is not a reading. The
            # parser cannot produce one (an unrecognised verdict is an
            # infrastructure error), so this only guards a
            # hand-constructed result; skipping is the safe direction,
            # because a story left with no reading at all reads as
            # uncovered rather than as confirmed.
            if not key or cr.verdict not in _VERDICT_RANK:
                continue
            rank = _VERDICT_RANK[cr.verdict]
            worst[key] = max(worst.get(key, rank), rank)
        return {key: _RANK_VERDICT[rank] for key, rank in worst.items()}

    def criteria_for(self, story_id: str) -> list[CriterionReview]:
        """Every criterion verdict the reviewer returned for *story_id*.

        Order is the reviewer's own. Matching is by normalised id and
        never by criterion text, the rule ``parse_review_output`` has
        followed since R1.1.
        """
        key = normalize_story_id(story_id)
        return [cr for cr in self.criteria if normalize_story_id(cr.story_id) == key]

    def judged_criterion_count(self, story_id: str) -> int:
        """How many DISTINCT criteria the reviewer judged for a story.

        Distinct by criterion text, because a reviewer can return the
        same criterion more than once. Counting raw entries would then
        report a story as fully judged on one criterion judged twice.

        Used to tell "the reviewer passed this story" from "the reviewer
        passed the part of this story it looked at". It is a count, not
        a match: pairing reviewer text to PRD text would be matching by
        criterion text, which this module refuses to do.

        What a count therefore cannot catch, stated so nobody reads more
        into it than it carries: a reviewer that returns the right
        NUMBER of criteria while substituting one the PRD never asked
        for still satisfies this check, because two distinct texts came
        back for two acceptance criteria. Closing that would need text
        matching. The count catches the failure mode that actually
        occurs, a reviewer judging fewer criteria than the story has,
        and is silent about the one it cannot see.
        """
        return len({cr.criterion for cr in self.criteria_for(story_id)})

    def non_pass_criteria(self, story_id: str) -> list[CriterionReview]:
        """Every criterion for *story_id* the reviewer did not pass.

        Order is the reviewer's own. Used for the claim finding's
        suggestion text and for the note written into a reverted PRD
        story, so both read the same evidence.
        """
        return [cr for cr in self.criteria_for(story_id) if cr.verdict != ReviewVerdict.PASS.value]

    def as_findings(self) -> list[Finding]:
        """E3: typed representation of every non-PASS criterion + every
        concern. Used by factory to populate ``Component.findings``.

        Criteria with verdict=PASS are skipped (they're not findings).
        ADVISORY criteria carry severity="advisory"; FAIL criteria carry
        severity="fail". Concerns carry their native severity field
        (already "fail" or "advisory" -- see ReviewConcern).

        E3-infra: when this result has ``infrastructure_error=True``
        (review agent crashed, output unparseable, timeout) the list
        LEADS with a synthetic infrastructure_error Finding, so
        downstream consumers can still distinguish "clean review" (empty
        list) from "review did not fully happen" (an infra finding
        present).

        Anything the reviewer DID return follows it. Until #266 every
        infra path built a fresh, empty result, so returning the
        synthetic finding alone lost nothing. The coverage check breaks
        that: a reviewer can report real criteria and real concerns and
        still fail to prove it read the whole change. Dropping those
        would have made the typed findings stream and the PR body -
        which renders them either way - disagree about the same review,
        and would have deleted a critical finding because the reviewer
        miscounted its own diffstat. What an infra error costs is TRUST
        IN THE CLEAN VERDICTS, not the evidence that came back.

        R7.1: every returned Finding is tagged ``model:<reviewer_model>``
        when the reviewing model identity is known, so the journal can
        attribute findings (and misses) to the model family that
        reviewed the diff.
        """
        out: list[Finding] = []
        if self.infrastructure_error:
            out.append(
                Finding.infrastructure_error(
                    phase="review",
                    explanation=(
                        self.overall_notes or "Reviewer agent did not produce parseable output"
                    ),
                )
            )
        for cr in self.criteria:
            if cr.verdict == ReviewVerdict.PASS.value:
                continue
            sev = "fail" if cr.verdict == ReviewVerdict.FAIL.value else "advisory"
            out.append(
                Finding.from_review_concern(
                    category="prd_criterion",
                    severity=sev,
                    location="",
                    explanation=f"{cr.criterion}: {cr.explanation}",
                    suggestion=cr.suggestion,
                )
            )
        for concern in self.concerns:
            out.append(
                Finding.from_review_concern(
                    category=concern.category,
                    severity=concern.severity,
                    location=concern.location,
                    explanation=concern.explanation,
                    suggestion=concern.suggestion,
                )
            )
        return [tag_finding_with_model(f, self.reviewer_model) for f in out]


#: #480: the verdicts a refused reply may state and still be asked again.
_REASKABLE_VERDICTS = frozenset({ReviewVerdict.PASS.value, ReviewVerdict.ADVISORY.value})
#: #480: a "verdict" or "severity" key with a string value, as JSON writes it,
#: anywhere in a reply's text, inside the parsed object or outside it.
_JUDGEMENT_FIELD = re.compile(r'"(?:verdict|severity)"\s*:\s*"([^"]*)"', re.IGNORECASE)
#: #480: every spelling of those two words. One that _JUDGEMENT_FIELD did not
#: read is a judgement the rule cannot read, so it blocks the re-ask.
_JUDGEMENT_WORD = re.compile(r"verdict|severity", re.IGNORECASE)


def _nothing_to_discard(
    data: object, expected_story_ids: Sequence[str] | None, raw_output: str
) -> bool:
    """#480: whether a refused reply states nothing a re-ask could discard.

    Two layers, both required. The TEXT layer reads the whole reply, parsed
    or not: every "verdict" or "severity" in it must be a JSON string field
    whose value is pass or advisory. It exists because the parsed object can
    be part of the reply only (``_extract_json`` takes the first fenced block
    or the first balanced braces), and because a reply that did not parse can
    still state a fail: the kept 131430 int-d2 run-3 reply is invalid JSON
    holding ``"verdict": "fail"`` on the planted story. The OBJECT layer reads
    what the parser read: ``data`` is None (no JSON, or JSON null), or it is
    an object whose ``stories`` and ``concerns`` are lists (or absent), that
    names no expected story id, whose every verdict is pass or advisory and
    every concern advisory. Everything else is False: the re-ask replaces a
    refusal, so it may only replace one that holds no verdict it would drop.
    Prose ("IC1 fails") is not read as a verdict here, as the parser never
    reads it as one.
    """
    if not _text_states_no_fail(raw_output):
        return False
    if data is None:
        return True
    if not isinstance(data, dict):
        return False
    stories = data.get("stories", [])
    concerns = data.get("concerns", [])
    if not isinstance(stories, list) or not isinstance(concerns, list):
        return False
    wanted = {normalize_story_id(sid) for sid in expected_story_ids or ()}
    return all(_story_states_nothing(story, wanted) for story in stories) and all(
        _severity(concern) == ReviewVerdict.ADVISORY.value for concern in concerns
    )


def _text_states_no_fail(raw_output: str) -> bool:
    values = _JUDGEMENT_FIELD.findall(raw_output)
    if len(values) != len(_JUDGEMENT_WORD.findall(raw_output)):
        return False
    return all(value.strip().lower() in _REASKABLE_VERDICTS for value in values)


def _story_states_nothing(story: object, wanted: set[str]) -> bool:
    if not isinstance(story, dict):
        return False
    if normalize_story_id(str(story.get("storyId", ""))) in wanted:
        return False
    criteria = story.get("criteria", [])
    if not isinstance(criteria, list):
        return False
    return all(_verdict(entry) in _REASKABLE_VERDICTS for entry in criteria)


def _verdict(entry: object) -> str:
    return str(entry.get("verdict", "")).strip().lower() if isinstance(entry, dict) else ""


def _severity(entry: object) -> str:
    return str(entry.get("severity", "")).strip().lower() if isinstance(entry, dict) else ""


def parse_review_output(
    raw_output: str,
    expected_story_ids: Sequence[str] | None = None,
    *,
    debug_dir: Path | None = None,
) -> ReviewResult:
    """Parse structured JSON from reviewer agent output.

    ``expected_story_ids`` enables the R1.1 criterion-coverage gate:
    every PRD story id must receive at least one criterion verdict or
    the result is an infrastructure error - a partial or empty review
    (``{"stories": [], "concerns": []}``) is a review that did not
    happen, not a clean pass (CRIT-5). Matching is by story id
    (case-insensitive, whitespace-stripped), never by criterion text.
    ``None`` skips the check for callers that have no PRD.

    ``debug_dir`` enables a full raw-output dump on parse failure via
    :func:`kstrl.findings.dump_raw_debug`; the result's
    ``raw_output`` field stays truncated to 2000 chars to bound
    manifest/journal size.
    """

    def _infra(notes: str, label: str, data: object) -> ReviewResult:
        dump_path = dump_raw_debug(debug_dir, "review", raw_output, label)
        if dump_path:
            notes = f"{notes} [full raw output: {dump_path}]"
        return ReviewResult(
            passed=False,
            mode="",
            overall_notes=notes,
            raw_output=raw_output[:2000],
            infrastructure_error=True,
            reply_unread=_nothing_to_discard(data, expected_story_ids, raw_output),
        )

    try:
        data = _extract_json(raw_output)
    except ValueError:
        return _infra("Failed to parse reviewer output as JSON", "no_json", None)

    # R1.2: _extract_json returns whatever json.loads produced - null,
    # a list, a bare string. Anything but an object would crash the
    # .get() calls below with AttributeError.
    if not isinstance(data, dict):
        return _infra(
            f"Review output was not a JSON object (got {type(data).__name__})",
            "non_dict_json",
            data,
        )

    criteria: list[CriterionReview] = []

    stories = data.get("stories", [])
    if not isinstance(stories, list):
        return _infra(
            "Invalid review output: 'stories' is not an array",
            "stories_not_array",
            data,
        )

    covered_story_ids: set[str] = set()
    invalid_verdicts: list[str] = []
    for story in stories:
        if not isinstance(story, dict):
            continue
        story_id = str(story.get("storyId", "")).strip()
        raw_criteria = story.get("criteria", [])
        if not isinstance(raw_criteria, list):
            continue
        for crit_data in raw_criteria:
            if not isinstance(crit_data, dict):
                continue
            # R1.1: normalize then whitelist. Verbatim storage meant
            # "FAIL" matched neither the fail gate nor the pass gate
            # and became a non-blocking advisory-alike.
            verdict = str(crit_data.get("verdict", "")).strip().lower()
            if verdict not in VALID_CRITERION_VERDICTS:
                invalid_verdicts.append(str(crit_data.get("verdict", ""))[:40] or "<missing>")
                continue
            criteria.append(
                CriterionReview(
                    criterion=str(crit_data.get("criterion", "")),
                    verdict=verdict,
                    explanation=str(crit_data.get("explanation", "")),
                    suggestion=str(crit_data.get("suggestion", "")),
                    # R10.3: keep the story this verdict belongs to. The id
                    # was already read above and thrown away; the claim
                    # check needs it to compare the reviewer's verdict
                    # against the engineer's passes flag per story.
                    story_id=story_id,
                )
            )
            if story_id:
                covered_story_ids.add(normalize_story_id(story_id))

    if invalid_verdicts:
        return _infra(
            "Review output contained unrecognized verdicts: "
            + ", ".join(repr(v) for v in invalid_verdicts)
            + " (valid: pass/fail/advisory)",
            "invalid_verdict",
            data,
        )

    if expected_story_ids:
        missing = [
            sid for sid in expected_story_ids if normalize_story_id(sid) not in covered_story_ids
        ]
        if missing:
            return _infra(
                "Review coverage incomplete: no verdict for story ids "
                + ", ".join(missing)
                + " (CRIT-5: a partial or empty review cannot pass)",
                "coverage_gap",
                data,
            )

    concerns: list[ReviewConcern] = []
    dropped_concerns = 0
    concerns_not_list = not isinstance(data.get("concerns"), list)
    raw_concerns = data.get("concerns", [])
    if isinstance(raw_concerns, list):
        for c in raw_concerns:
            if not isinstance(c, dict):
                dropped_concerns += 1
                continue
            category = str(c.get("category", "")).strip()
            severity = str(c.get("severity", "")).strip()
            location = str(c.get("location", "")).strip()
            explanation = str(c.get("explanation", "")).strip()
            # Reject malformed entries instead of silently storing junk
            if category not in VALID_CONCERN_CATEGORIES:
                dropped_concerns += 1
                continue
            if severity not in VALID_CONCERN_SEVERITIES:
                dropped_concerns += 1
                continue
            if not explanation:
                dropped_concerns += 1
                continue
            concerns.append(
                ReviewConcern(
                    category=category,
                    severity=severity,
                    location=location,
                    explanation=explanation,
                    suggestion=str(c.get("suggestion", "")),
                )
            )

    exhaustively_searched = bool(data.get("exhaustively_searched", False))

    has_criterion_failures = any(c.verdict == ReviewVerdict.FAIL.value for c in criteria)
    has_concern_failures = any(c.severity == "fail" for c in concerns)
    overall_notes = str(data.get("overallNotes", ""))

    return ReviewResult(
        passed=not (has_criterion_failures or has_concern_failures),
        mode="",
        criteria=criteria,
        concerns=concerns,
        exhaustively_searched=exhaustively_searched,
        observed_diffstat=git.parse_observed_diffstat(data.get("observedDiffstat")),
        overall_notes=overall_notes,
        raw_output=raw_output[:2000],
        dropped_concerns=dropped_concerns,
        concerns_not_list=concerns_not_list,
    )


def apply_coverage_check(
    result: ReviewResult,
    actual: git.DiffStat,
    mode: ReviewMode,
) -> None:
    """#266: the anti-padding replacement, applied to *result* in place.

    The reviewer was told to run ``git diff --numstat <base>...HEAD``
    and report the totals; this is where its answer meets git's.

    A disagreement always leaves a visible trace - the flag, an advisory
    concern, and therefore a line in the PR body and in the retry
    context. Hard mode additionally REFUSES: it must not approve a
    change nothing is known to have read. It refuses as infrastructure
    rather than as a criterion failure because the reviewer's verdicts
    are not WRONG here, they are unattributable, and charging the
    engineer a retry for a reviewer that cannot reach the repository
    would spend engineer iterations on a harness fault.

    A no-op on an already-errored result: there is no reading to check.
    """
    if result.infrastructure_error:
        return
    disagreement = git.diffstat_disagreement(result.observed_diffstat, actual)
    if disagreement is None:
        return
    result.diffstat_disagreement = disagreement
    result.concerns.append(
        ReviewConcern(
            category="other",
            severity="advisory",
            location="",
            explanation=git.coverage_marker_text("review", disagreement),
            suggestion=git.COVERAGE_SUGGESTION,
        )
    )
    if mode != ReviewMode.HARD:
        return
    result.passed = False
    result.infrastructure_error = True
    result.overall_notes = (
        git.coverage_notes_prefix("review", disagreement) + " " + result.overall_notes
    ).strip()


def run_review(
    agent: Agent,
    prd_path: Path,
    worktree_path: Path,
    base_branch: str,
    verification_result: VerificationResult,
    mode: ReviewMode,
    ui: UI,
    timeout: float | None = None,
    *,
    debug_dir: Path | None = None,
    on_line: Callable[[str], None] | None = None,
) -> ReviewResult:
    """Run the full review: build prompt, run agent, parse output.

    In advisory mode, all FAILs are downgraded and passed=True is returned.

    #266: no diff is passed in or fetched for the prompt. The agent runs
    with ``cwd=worktree_path`` and reads the change from git itself. The
    harness measures the same range with ``git diff --numstat`` and
    checks the reviewer's reported diffstat against it - the replacement
    for the chunking guarantee, whose reach ``git.diffstat_disagreement``
    documents.

    Never raises: any agent/prompt failure degrades to a ReviewResult
    with ``infrastructure_error=True`` so one broken reviewer fails one
    component instead of aborting the whole factory run (R1.2, mirrors
    ``run_security_review``).
    """
    if mode == ReviewMode.SKIP:
        return ReviewResult(passed=True, mode=mode.value)

    ui.info("  Running second-opinion review...")
    start = time.monotonic()
    # R7.1: the agent's name IS the reviewing model identity ("codex
    # (gpt-5)", "claude-code (haiku)", "custom (<cmd>)"). Captured up
    # front so even crash/oversize results stay attributable.
    reviewer_model = getattr(agent, "name", "") or ""

    try:
        # A SHA, resolved ONCE, and used for BOTH the reviewer's
        # instructions and the harness's own measurement. A ref name
        # would not do: origin/<base> moves whenever a sibling
        # component's PR merges mid-run, and the two measurements are
        # taken minutes apart. See git.resolve_base_sha.
        base_sha = git.resolve_base_sha(base_branch, worktree_path)
        # Strict by construction (git.get_diff_stat): a failed
        # measurement raises into the handler below rather than folding
        # to a zero that would agree with a reviewer that read nothing.
        actual_diffstat = git.get_diff_stat(base_sha, worktree_path, resolved=True)
        prompt = build_review_prompt(
            prd_path,
            base_sha,
            verification_result,
        )
        # R1.1: the coverage gate needs the ground-truth story ids from
        # the PRD, not whatever ids the reviewer chose to mention.
        expected_story_ids = [story.id for story in PRD.load(prd_path).user_stories]
        output_lines = collect_agent_output(
            agent,
            prompt,
            cwd=worktree_path,
            timeout=timeout,
            on_line=on_line,
        )
    except AgentOutputTooLarge as exc:
        # Hostile/buggy agent flooding output. The review never
        # happened: infrastructure error (H-13), which hard mode blocks
        # on; advisory passes but the infra finding stays visible.
        ui.warn(f"  Reviewer agent output too large: {exc}")
        result = ReviewResult(
            passed=mode != ReviewMode.HARD,
            mode=mode.value,
            overall_notes=f"Reviewer agent output too large: {exc}",
            infrastructure_error=True,
            reviewer_model=reviewer_model,
        )
        result.duration_seconds = time.monotonic() - start
        return result
    except Exception as exc:  # noqa: BLE001
        # Reviewer agent crashed (or the PRD/diff could not be read).
        # Degrade to a per-component infrastructure failure - never
        # propagate and abort the run (R1.2).
        ui.warn(f"  Reviewer agent failed: {exc}")
        result = ReviewResult(
            passed=mode != ReviewMode.HARD,
            mode=mode.value,
            overall_notes=f"Reviewer agent failed: {exc}",
            infrastructure_error=True,
            reviewer_model=reviewer_model,
        )
        result.duration_seconds = time.monotonic() - start
        return result

    raw_output = _select_agent_output(agent, output_lines)
    result = parse_review_output(
        raw_output,
        expected_story_ids,
        debug_dir=debug_dir,
    )
    result.mode = mode.value
    result.reviewer_model = reviewer_model
    result.duration_seconds = time.monotonic() - start

    apply_coverage_check(result, actual_diffstat, mode)

    # In advisory mode, downgrade all FAILs and force pass
    if mode == ReviewMode.ADVISORY:
        for cr in result.criteria:
            if cr.verdict == ReviewVerdict.FAIL.value:
                cr.verdict = ReviewVerdict.ADVISORY.value
        for concern in result.concerns:
            if concern.severity == "fail":
                concern.severity = "advisory"
        result.passed = True

    status = "passed" if result.passed else "FAILED"
    coverage_note = " (UNVERIFIED COVERAGE)" if result.diffstat_disagreement else ""
    ui.info(
        f"  Review {status}{coverage_note}: "
        f"{result.fail_count} fail, {result.advisory_count} advisory"
    )

    return result
