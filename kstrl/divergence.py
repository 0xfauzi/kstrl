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

* the change got larger at every step, and
* at no step was a single one of the reviewer's blocking findings
  retired.

The second half is what keeps a converging component alive. Answering a
review finding almost always means writing code, so growth on its own
says nothing at all. What separates converging from diverging is whether
the growth BOUGHT anything, and retiring even one blocking objection is
the cheapest honest evidence that it did.

That reset is deliberately weak, and it is weak because of what a
reviewer does on a changed diff: it raises something new almost every
time. A stricter test - "the new finding set is a proper SUBSET of the
old one" - reads the ordinary converging trajectory as failure. Retire
A, keep B, draw C; retire B, keep C, draw D. One objection answered
every attempt, and a subset test never once fires. The gate would
condemn exactly the component that was working.

So a trip now says something narrow and strong: the change keeps getting
larger and not one thing the reviewer objected to has gone away.

Identity rather than count, because a count cannot tell a genuinely
retired finding from a new one that replaced it, and "one objection was
answered" is the whole reset.

What this would NOT have caught, stated plainly
-----------------------------------------------
The #265 run motivated this module. The shipped predicate would not have
fired on it, and pretending otherwise would be the kind of claim this
project exists to stop making.

That run went 6 blocking findings, then 1, then 10. Attempt 2 retired at
least five of attempt 1's six no matter which identities they had, so the
streak resets there, and one bad step afterwards is not two. Checked by
exhaustive search rather than argued: over every trajectory of shape
6 -> 1 -> 10 across a 16-key universe, 128,128 of them, this predicate
trips on none.

That is the deliberate cost of not condemning the converging trajectory
above, and it is the strongest single reason the gate ships ADVISORY.
A gate that cannot be shown to fire on the run it was built from has not
earned the right to end a component; what it has earned is the right to
be measured on real runs, which is what advisory mode is for.

Which way the identity heuristic fails
--------------------------------------
The size half is exact: ``git diff --numstat``, counted through
:func:`kstrl.policy.count_diff_size` so it agrees with the R8.1 size
caps and inherits their exclusion of machine-generated lockfiles.

The retirement half is a heuristic, because a finding's identity has to
be reconstructed from what the reviewer wrote. The weak reset buys it
one saving property:

    A trip requires that EVERY previously-blocking key still be present.
    Any instability in a key - a reworded criterion, a moved line, a
    rephrased explanation - makes an old key vanish from the new set,
    which counts as a retirement and RESETS the streak.

So key instability can only produce misses, never false trips. Both
halves of the design therefore err the same way: quiet.

Why lines changed, and not hunk size
------------------------------------
The obvious measurement for #265 is the largest single hunk, because
that is the quantity the reviewer's chunker fails on. It is also the
wrong one. #266 dropped the pasted diff entirely - the reviewer
already runs inside the worktree with git on its path - and chunking,
hunks and the prompt cap went with it. A detector built on hunk size
would now be measuring something nothing computes.

Lines changed against the base survives that. It needs no diff string to
exist: the reading is taken from the branch, not from a prompt.

``lines_changed`` is git's own sense of the phrase - **lines added plus
lines removed** against the base, the quantity ``[policy]
max_lines_changed`` caps. It is churn, not file growth, and deleting
pre-existing code raises it. That is intended: a component that keeps
rewriting the same region without answering a single objection is
diverging exactly as much as one that keeps appending. It does mean the
number must never be described as the artifact getting bigger, and the
message below says "lines changed (added plus removed)" for that reason.

Files touched is recorded alongside as evidence for the operator but is
deliberately NOT part of the predicate: a component that keeps growing
one file never touches a second one. Diff byte size was the other
candidate; it moves almost proportionally with lines while also moving
with formatting and line length, so it adds noise without adding signal.

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
from enum import StrEnum
from pathlib import Path

from kstrl.review import ReviewResult, ReviewVerdict, normalize_story_id

#: Distinct, greppable prefix for the message, mirroring
#: ``breaker.NO_PROGRESS_MESSAGE_PREFIX``. Humans and logs read this; the
#: pipeline routes on the typed verdict, never on the string.
DIVERGENCE_MESSAGE_PREFIX = "divergence detector tripped"

_WHITESPACE_RE = re.compile(r"\s+")


class DivergenceMode(StrEnum):
    """What a trip does.

    ``advisory``/``block`` is the vocabulary of the other advisory-first
    gates, ``[adequacy] layer0`` and ``[factory] setpoint_agreement``,
    plus a skip. Deliberately NOT ``[security] mode``'s
    ``hard|advisory|skip``: "hard" describes how a reviewer reads, and
    this is not a reviewer.
    """

    #: Do not measure at all.
    SKIP = "skip"
    #: Measure, record the finding and the event, keep retrying.
    ADVISORY = "advisory"
    #: Fail the component instead of spending another engineer run.
    BLOCK = "block"


_VALID_MODES: frozenset[str] = frozenset(m.value for m in DivergenceMode)


@dataclass(frozen=True)
class DivergenceConfig:
    """``[divergence]`` config for the #265 detector.

    ``growth_steps`` is the number of consecutive steps required, so the
    detector needs ``growth_steps + 1`` measured attempts before it can
    fire at all.

    The default of 2 is a structural minimum, not a measured number, and
    is recorded here as unmeasured. One step is the ORDINARY shape of a
    converging retry: a finding is answered by writing code, and the new
    code draws a finding of its own. A single step therefore cannot tell
    a trend from a step. Two consecutive steps is the smallest window in
    which "monotonic" carries information beyond "changed". Lower it to
    1 to act a step sooner at the cost of false positives, raise it to be
    more patient. Non-positive values are rejected rather than quietly
    treated as off, because ``mode = "skip"`` is how you turn it off and
    a reader could reasonably expect 0 to mean "most aggressive".

    ``mode`` ships ``advisory``: the predicate is mechanical but its
    retirement half is a heuristic whose false-positive rate has not been
    measured on real runs, and the project's rule is that a gate
    graduates to blocking once an operator has seen its output and can
    name what it caught and what it flagged wrongly. Advisory mode is
    what produces that evidence.

    Unlike ``adequacy.layer0_blocks`` and ``review.setpoint_blocks``, the
    autonomy ladder deliberately does NOT harden this gate at L1+, and
    that asymmetry is a decision rather than an oversight. Those two
    gates ask "did an independent sensor confirm the claim", and a run
    spending less human attention should insist on that harder. This one
    forecasts, from a heuristic with no measured false-positive rate,
    that further retries are not worth buying. Auto-hardening it at the
    exact levels where nobody is watching is how an unattended run gets
    its components killed by a gate no operator has ever read the output
    of. It hardens when an operator sets ``mode = "block"``, and not
    before.
    """

    mode: str = DivergenceMode.ADVISORY.value
    growth_steps: int = 2

    def __post_init__(self) -> None:
        if self.mode not in _VALID_MODES:
            raise ValueError(
                f"invalid [divergence] mode {self.mode!r}; expected one of "
                f"{[m.value for m in DivergenceMode]}"
            )
        if self.growth_steps < 1:
            raise ValueError(
                f"invalid [divergence] growth_steps {self.growth_steps}; must be >= 1 "
                '(set mode = "skip" to turn the detector off)'
            )

    @property
    def measures(self) -> bool:
        """Whether to take readings at all."""
        return self.mode != DivergenceMode.SKIP

    @property
    def blocks(self) -> bool:
        """Whether a trip ends the component instead of reporting."""
        return self.mode == DivergenceMode.BLOCK

    @classmethod
    def from_env(cls) -> DivergenceConfig:
        """Load divergence config from environment variables only."""
        defaults = cls()
        return cls(
            mode=os.environ.get("KSTRL_DIVERGENCE_MODE", defaults.mode),
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
        mode = str(section["mode"]) if "mode" in section else defaults.mode
        growth_steps = (
            int(section["growth_steps"]) if "growth_steps" in section else defaults.growth_steps
        )
        if "KSTRL_DIVERGENCE_MODE" in os.environ:
            mode = os.environ["KSTRL_DIVERGENCE_MODE"]
        if "KSTRL_DIVERGENCE_GROWTH_STEPS" in os.environ:
            growth_steps = int(os.environ["KSTRL_DIVERGENCE_GROWTH_STEPS"])
        return cls(mode=mode, growth_steps=growth_steps)


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
    #: Lines ADDED PLUS REMOVED against the base, git's own sense of the
    #: phrase and the quantity ``[policy] max_lines_changed`` caps.
    lines_changed: int
    files_changed: int
    #: Stable identities of the reviewer's BLOCKING findings, for the
    #: predicate only. Advisories never failed the component, so they
    #: must not decide whether it improved.
    finding_keys: frozenset[str]
    #: ``ReviewResult.fail_count`` for this attempt. Carried separately
    #: because keys deduplicate and a count must not: the operator sees
    #: "Phase 2 FAILED: 10 failures" on the line above the message, and
    #: a dashboard joins this against ``ReviewResultEvent.fail_count``.
    blocking_count: int


@dataclass(frozen=True)
class DivergenceVerdict:
    """The evidence behind a trip, in the operator's own terms."""

    readings: tuple[AttemptReading, ...]

    @property
    def message(self) -> str:
        """What happened, with the numbers, and what to do about it."""
        attempts = ", ".join(str(r.attempt) for r in self.readings)
        lines = " -> ".join(str(r.lines_changed) for r in self.readings)
        files = " -> ".join(str(r.files_changed) for r in self.readings)
        findings = " -> ".join(str(r.blocking_count) for r in self.readings)
        return (
            f"{DIVERGENCE_MESSAGE_PREFIX}: the change is outgrowing the "
            f"reviewer. Across attempts {attempts} the review failed every "
            f"time, the change got larger at every step ({lines} lines "
            f"changed, added plus removed, across {files} files), and not "
            f"one of the reviewer's blocking findings was retired at any "
            f"step ({findings} blocking findings). Another retry buys "
            "another engineer run on a change that is getting larger and "
            "no closer to passing. Split this component into smaller ones, "
            "or narrow its PRD, then run it again."
        )


def _normalize(text: str) -> str:
    """Collapse whitespace and case so cosmetic rewording is not identity."""
    return _WHITESPACE_RE.sub(" ", text.strip()).lower()


def _location_key(location: str) -> str:
    """The file part of a reviewer location, without the line number.

    Reviewers emit ``path:line`` (that is the shape the PR body renders),
    and line numbers move whenever the change grows, so keeping them
    would make every finding look new on every attempt. Dropping the line
    is therefore about STABILITY, not about erring in a safe direction:
    the file alone does not identify a finding, which is why the concern
    key below also carries the explanation.
    """
    return _normalize(location).split(":", 1)[0].strip()


def review_finding_keys(result: ReviewResult) -> frozenset[str]:
    """Stable identities for the blocking findings of one review.

    Built from the reviewer's own structure rather than from
    ``as_findings()``, which folds the criterion text into free-form
    explanation prose and drops the story id.

    A criterion is keyed by story id plus criterion text: the PRD pins
    both down, and ``review.py`` already treats ``cr.criterion`` as an
    identity inside one result (``judged_criterion_count``), with this
    normalising whitespace and case on top.

    A concern is keyed by category, file, and explanation. The
    explanation is prose and the least stable part of the key, which is
    exactly why it is safe to include: an unstable key can only make an
    old finding look retired, and a retirement resets the streak.

    Leaving it out is what is NOT safe, and an earlier draft did. Keyed
    on ``(category, file)`` alone, two concerns of one category in one
    file collapse onto ONE key, so retiring both and raising a third
    leaves the sets EQUAL rather than smaller - and equal is not a
    retirement. On a component whose findings concentrate in a single
    test file, which is exactly the #265 workload, that made the
    retirement half of the predicate contribute nothing at all.

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
        keys.add(
            f"concern:{_normalize(concern.category)}:"
            f"{_location_key(concern.location)}:{_normalize(concern.explanation)}"
        )
    return frozenset(keys)


def detect_divergence(
    readings: Sequence[AttemptReading],
    config: DivergenceConfig,
) -> DivergenceVerdict | None:
    """The predicate. ``None`` means "not diverging, or cannot tell".

    Every early return is a fail-open: too few readings, a gap in the
    attempt numbers, a step where the change did not get larger, a step
    where the reviewer retired something, or an attempt whose blocking
    findings could not be keyed at all.

    ``config.mode`` is deliberately NOT read here. Whether to measure and
    what a trip does are the caller's, so this function stays what its
    name says: the predicate.
    """
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
        if not previous.finding_keys <= current.finding_keys:
            # At least one thing the reviewer objected to is gone. The
            # growth bought something, so the streak restarts.
            return None
    return DivergenceVerdict(readings=tail)
