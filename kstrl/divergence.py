"""Across-attempt divergence detector for the factory retry loop (#265).

:class:`~kstrl.breaker.NoProgressBreaker` watches ONE engineer loop and
halts when consecutive iterations change nothing. This module is its
across-attempt analogue, and the failure it watches for is the mirror
image: every attempt changes a great deal. The reviewer fails a
component, the engineer answers the findings by writing more code, the
change grows, and the reviewer comes back no happier. That is positive
feedback, and every turn of it costs a full engineer run.

Measured on a real run (#265): three attempts, a test file that grew
from 914 to 2221 lines, review failures that went 6, then 1, then 10,
and a fourth attempt whose change nothing could review. $21.44 and 71
minutes, zero components completed. Nothing in the loop was watching the
trend.

The predicate
-------------
Over the last ``growth_steps + 1`` CONSECUTIVE attempts in which the
reviewer ran and failed the component:

* the change grew strictly at every step, and
* at no step did the reviewer's blocking findings become a proper subset
  of the previous attempt's.

The second half is what keeps a converging component alive. Answering a
review finding almost always means writing code, so growth on its own
says nothing at all. What separates converging from diverging is whether
the growth BOUGHT anything: an attempt that retired findings and raised
none of its own is progress, however much larger it made the change, and
it resets the streak.

Finding COUNT cannot make that distinction, which is why identity is
used. The run above went 6 findings, then 1, then 10. A count rule reads
attempt 2 as a large improvement without knowing whether its single
remaining failure was one of the original six or something new that the
attempt had just introduced. Set containment answers exactly that.

Why lines changed, and not hunk size
------------------------------------
The obvious measurement for #265 is the largest single hunk, because
that is the quantity the reviewer's chunker fails on. It is also the
wrong one. #266 proposes dropping the pasted diff entirely - the
reviewer already runs inside the worktree with git on its path - and
chunking, hunks and the prompt cap all disappear with it. A detector
built on hunk size would then be measuring something nothing computes.

Lines changed against the base survives that. It is what ``git diff
--numstat`` reports, it is the unit #265 itself reasons in (914 to 2221
lines), and it needs no diff string to exist: the reading is taken from
the branch, not from a prompt. Files touched is recorded alongside it as
evidence for the operator but is deliberately NOT part of the predicate -
a component that keeps growing one file never touches a second one.
Diff byte size was the other candidate; it moves almost proportionally
with lines while also moving with formatting and line length, so it adds
noise without adding signal.

What this cannot see
--------------------
Stated here rather than discovered later, because the detector BLOCKS
and its case for blocking rests on being mechanical.

The growth half is exact: ``git diff --numstat``, counted through
``policy.count_diff_size`` so it agrees with the R8.1 size caps and
inherits their exclusion of machine-generated lockfiles.

The retirement half is a deterministic identity match over the
reviewer's structured output, and identity is not exactness. A concern's
key over-collapses (two findings in one file share a key), which reads
as improvement and holds the detector OFF - the safe direction. A
criterion's key is the story id plus the criterion text the reviewer
echoed, and that fails the other way: a reviewer that rewords a
still-failing criterion between attempts produces a key the previous
attempt did not have, so a genuine retirement can read as a new finding
and the streak survives. ``review.py`` already treats ``cr.criterion``
as an identity inside one result (``judged_criterion_count``); this
normalises whitespace and case on top, which makes it more stable than
that, not less. It is still the known false-positive channel, and it is
the first thing to look at if an operator reports a wrong trip.

In ``single_pr`` mode every component shares one branch, so the numbers
include components that already landed. The predicate survives it -
``max_parallel`` is forced to 1 there, so the offset is constant across
one component's attempts and strict inequality is offset-invariant - but
the numbers in the message are the branch's, not the component's.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from kstrl.review import ReviewResult, ReviewVerdict, normalize_story_id

#: Distinct, greppable prefix for the failure message, mirroring
#: ``breaker.NO_PROGRESS_MESSAGE_PREFIX``. Humans and logs read this;
#: the pipeline routes on the typed verdict, never on the string.
DIVERGENCE_MESSAGE_PREFIX = "divergence detector tripped"

_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class DivergenceConfig:
    """``[divergence]`` config for the #265 detector.

    ``growth_steps`` is the number of consecutive growth STEPS required,
    so the detector needs ``growth_steps + 1`` measured attempts before
    it can fire at all.

    The default of 2 is a structural minimum, not a measured number, and
    is recorded here as unmeasured. One step is the ORDINARY shape of a
    converging retry: a finding is answered by writing code, and the new
    code draws a finding of its own. A single step therefore cannot tell
    a trend from a step. Two consecutive steps is the smallest window in
    which "monotonic" carries information beyond "changed". Lower it to
    1 to save one more attempt at the cost of false positives, raise it
    to be more patient.

    ``enabled`` is the kill switch. With the factory's default
    ``max_retries`` of 3 and the default streak, a trip forecloses
    exactly one remaining attempt, and ``ks retry`` starts a fresh run
    with an empty history, so an operator who disagrees pays one command.
    """

    enabled: bool = True
    growth_steps: int = 2

    @classmethod
    def from_env(cls) -> DivergenceConfig:
        """Load divergence config from environment variables only."""
        defaults = cls()
        enabled = os.environ.get("KSTRL_DIVERGENCE_ENABLED")
        return cls(
            enabled=defaults.enabled if enabled is None else enabled == "1",
            growth_steps=int(
                os.environ.get("KSTRL_DIVERGENCE_GROWTH_STEPS", str(defaults.growth_steps))
            ),
        )

    @classmethod
    def load(cls, root_dir: Path | None = None) -> DivergenceConfig:
        """Precedence: env > toml > defaults; reads ``[divergence]``."""
        from kstrl.config import load_toml_section, resolve_config_file

        base = root_dir if root_dir is not None else Path.cwd()
        section = load_toml_section(resolve_config_file(base), "divergence")
        defaults = cls()
        enabled = bool(section["enabled"]) if "enabled" in section else defaults.enabled
        growth_steps = (
            int(section["growth_steps"]) if "growth_steps" in section else defaults.growth_steps
        )
        if "KSTRL_DIVERGENCE_ENABLED" in os.environ:
            enabled = os.environ["KSTRL_DIVERGENCE_ENABLED"] == "1"
        if "KSTRL_DIVERGENCE_GROWTH_STEPS" in os.environ:
            growth_steps = int(os.environ["KSTRL_DIVERGENCE_GROWTH_STEPS"])
        return cls(enabled=enabled, growth_steps=growth_steps)


@dataclass(frozen=True)
class AttemptReading:
    """One attempt's reading of the change and of the review verdict.

    Recorded only for an attempt whose reviewer actually RAN and failed
    the component. A crashed reviewer produced no verdict, and a change
    whose size could not be measured produced no size, so neither yields
    a reading: the gap breaks the consecutive run the predicate needs,
    which is the fail-open direction.
    """

    attempt: int
    lines_changed: int
    files_changed: int
    #: Stable identities of the reviewer's BLOCKING findings. Advisories
    #: never failed the component, so they must not decide whether it
    #: improved.
    finding_keys: frozenset[str]


@dataclass(frozen=True)
class DivergenceVerdict:
    """The evidence behind a trip, in the operator's own terms."""

    readings: tuple[AttemptReading, ...]

    @property
    def message(self) -> str:
        """What happened, with the numbers, and what to do about it."""
        attempts = ", ".join(str(r.attempt) for r in self.readings)
        lines = " -> ".join(f"{r.lines_changed}" for r in self.readings)
        files = " -> ".join(f"{r.files_changed}" for r in self.readings)
        findings = " -> ".join(f"{len(r.finding_keys)}" for r in self.readings)
        return (
            f"{DIVERGENCE_MESSAGE_PREFIX}: the change is outgrowing the "
            f"reviewer. Across attempts {attempts} the review failed every "
            f"time, the change grew at every step ({lines} lines changed "
            f"across {files} files), and at no step did the reviewer retire "
            f"findings without raising new ones ({findings} blocking "
            "findings). Another retry buys another engineer run on a change "
            "that is getting larger and no closer to passing. Split this "
            "component into smaller ones, or narrow its PRD, then run it "
            "again."
        )


def _normalize(text: str) -> str:
    """Collapse whitespace and case so cosmetic rewording is not identity."""
    return _WHITESPACE_RE.sub(" ", text.strip()).lower()


def _location_key(location: str) -> str:
    """The file part of a reviewer location, without the line number.

    Reviewers emit ``path:line`` (that is the shape the PR body renders),
    and line numbers move whenever the change grows. Keeping them would
    make every finding look new on every attempt, which is exactly the
    direction that manufactures false positives. Dropping everything
    after the first colon over-collapses instead: two distinct findings
    in one file share a key, the newer set reads as a subset, the streak
    resets and the detector stays quiet. That is the fail-open direction.
    """
    return _normalize(location).split(":", 1)[0].strip()


def review_finding_keys(result: ReviewResult) -> frozenset[str]:
    """Stable identities for the blocking findings of one review.

    Built from the reviewer's own structure rather than from
    ``as_findings()``, which folds the criterion text into free-form
    explanation prose and drops the story id. Identity has to survive
    the reviewer rewording itself between attempts, so it is keyed on
    the two things the PRD pins down (story id and criterion text) and,
    for a concern, on its category and the file it names.

    Only ``fail`` items are keyed. An advisory did not fail the
    component, so its arrival or departure is not the component getting
    better or worse at the thing being gated on.
    """
    keys: set[str] = set()
    for criterion in result.criteria:
        if criterion.verdict != ReviewVerdict.FAIL.value:
            continue
        story = normalize_story_id(criterion.story_id)
        keys.add(f"criterion:{story}:{_normalize(criterion.criterion)}")
    for concern in result.concerns:
        if concern.severity != "fail":
            continue
        keys.add(f"concern:{_normalize(concern.category)}:{_location_key(concern.location)}")
    return frozenset(keys)


def detect_divergence(
    readings: Sequence[AttemptReading],
    config: DivergenceConfig,
) -> DivergenceVerdict | None:
    """The predicate. ``None`` means "not diverging, or cannot tell".

    Every early return is a fail-open: too few readings, a gap in the
    attempt numbers, a step where the change did not grow, a step where
    the reviewer's findings became a proper subset of the previous
    attempt's, or an attempt whose blocking findings could not be keyed
    at all.

    ``config.enabled`` is deliberately NOT read here. The kill switch is
    the caller's, checked before a reading is even measured, so this
    function stays what its name says: the predicate.
    """
    if config.growth_steps < 1:
        return None
    window = config.growth_steps + 1
    if len(readings) < window:
        return None
    tail = tuple(readings[-window:])
    if any(not reading.finding_keys for reading in tail):
        # A failed review with no keyable blocking finding is not a
        # verdict this predicate can compare. Nothing is inferred from it.
        return None
    for previous, current in zip(tail, tail[1:], strict=False):
        if current.attempt != previous.attempt + 1:
            return None
        if current.lines_changed <= previous.lines_changed:
            return None
        if current.finding_keys < previous.finding_keys:
            # A proper subset: findings were retired and none were added.
            # The growth bought something, so the streak restarts.
            return None
    return DivergenceVerdict(readings=tail)
