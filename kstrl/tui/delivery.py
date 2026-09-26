"""What a run delivered: integration review, merges, and main's CI (#433 F9, M3, Q7, Q8).

"All components completed" is not "shipped". Three separate pieces of
evidence, each shown with its own state:

- the merged-feature integration review (``integration_view``);
- the merges: each ``pr_merged`` event's PR number and ``merge_sha``,
  and the run's ``release_ref`` from ``factory_completed`` (#442);
- main's CI for that commit. kstrl records NO check state for any
  commit: ``pr_state`` asks ``gh`` for ``state`` and ``mergeCommit``
  only, ``gh pr merge --auto`` waits on checks without storing them, and
  the signals poller (#441) reads an error tracker, keyed by issue. So
  the state is always "unknown", with that reason, until a record
  exists. The TUI never asks the network.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from rich.text import Text

from kstrl.tui import theme
from kstrl.tui.integration_view import (
    FIXED,
    HANDED_OFF,
    OPEN,
    UNKNOWN,
    IntegrationReview,
    read_integration_review,
)

if TYPE_CHECKING:
    from kstrl.reducer import RunState

CI_UNKNOWN = "unknown"
CI_UNKNOWN_REASON = "kstrl records no CI check state"

#: Review outcome -> (word, colour).
OUTCOME_WORDS: dict[str, tuple[str, str]] = {
    "clean": ("passed", theme.SUCCESS),
    "open_findings": ("open findings", theme.ERROR),
    "red": ("red: gates failed", theme.ERROR),
    "not_run": ("not run", theme.WARNING),
}

#: Criterion verdict -> (glyph, colour).
VERDICT_STYLE: dict[str, tuple[str, str]] = {
    "pass": ("✓", theme.SUCCESS),
    "fail": ("✗", theme.ERROR),
    "advisory": ("▲", theme.WARNING),
}

DISPOSITION_STYLE: dict[str, str] = {
    FIXED: theme.SUCCESS,
    HANDED_OFF: theme.VIOLET,
    OPEN: theme.ERROR,
    UNKNOWN: theme.WARNING,
}


@dataclass(frozen=True)
class Merge:
    component_id: str
    pr_number: int
    merge_sha: str


@dataclass(frozen=True)
class Delivery:
    run_id: str
    merges: tuple[Merge, ...]
    release_ref: str
    release_withheld: str
    integration: IntegrationReview | None
    ci: str = CI_UNKNOWN
    ci_reason: str = CI_UNKNOWN_REASON


def merges_of(state: RunState) -> tuple[Merge, ...]:
    """This run's merges, in PR order."""
    merged = [
        Merge(comp.component_id, comp.pr_number, comp.merge_sha)
        for comp in state.components.values()
        if comp.pr_state == "merged" and not comp.carried
    ]
    return tuple(sorted(merged, key=lambda merge: (merge.pr_number, merge.component_id)))


def fix_statuses(state: RunState) -> dict[str, str]:
    return {cid: comp.status for cid, comp in state.components.items()}


def read_delivery(root_dir: Path, run_dir: Path, state: RunState) -> Delivery:
    """File reads: the run's review rounds and the integration state file."""
    return Delivery(
        run_id=run_dir.name,
        merges=merges_of(state),
        release_ref=state.release_ref,
        release_withheld=state.release_withheld,
        integration=read_integration_review(root_dir, run_dir, fix_statuses(state)),
    )


def _outcome(review: IntegrationReview) -> tuple[str, str, str]:
    """(word, colour, note) for the review: the newest round that ran."""
    ran = [r for r in review.rounds if r.outcome != "not_run"]
    latest = review.latest
    shown = ran[-1] if ran else latest
    word, color = OUTCOME_WORDS.get(shown.outcome if shown else "", (UNKNOWN, theme.WARNING))
    note = ""
    if latest is not None and shown is not latest:
        word_now = OUTCOME_WORDS.get(latest.outcome, (latest.outcome, ""))[0]
        note = f"round {latest.number} {word_now}" + (f": {latest.reason}" if latest.reason else "")
    return word, color, note


def _verdicts(text: Text, review: IntegrationReview, short: bool) -> None:
    text.append(" · ", style=theme.MUTED)
    for index, verdict in enumerate(review.criteria):
        glyph, color = VERDICT_STYLE.get(verdict.verdict, ("?", theme.WARNING))
        if index:
            text.append(" ")
        text.append(verdict.story_id, style=theme.MUTED)
        text.append(glyph if short else f"{glyph}{verdict.verdict}", style=color)


def _disposition_counts(text: Text, review: IntegrationReview) -> None:
    counts = [
        (review.count(OPEN), OPEN, theme.ERROR),
        (review.count(HANDED_OFF), "handed off", theme.VIOLET),
        (review.count(FIXED), "fixed", theme.SUCCESS),
        (review.count(UNKNOWN), "unknown", theme.WARNING),
    ]
    shown = [(n, label, c) for n, label, c in counts if n]
    for index, (count, label, style) in enumerate(shown):
        text.append(", " if index else " · IF ", style=theme.MUTED)
        text.append(f"{count} {label}", style=style)


def integration_summary(review: IntegrationReview | None, *, short: bool = False) -> Text:
    """``integration ✗ open findings · IC1✗fail ... · IF 4 open, 1 handed off``."""
    text = Text()
    text.append("integration ", style=f"bold {theme.MUTED}")
    if review is None:
        text.append("not recorded in this run", style=theme.MUTED)
        return text
    word, color, note = _outcome(review)
    text.append(word, style=f"bold {color}")
    if review.criteria:
        _verdicts(text, review, short)
    _disposition_counts(text, review)
    if note and not short:
        text.append(f" · {note}", style=theme.MUTED)
    return text


def _merges(text: Text, merges: tuple[Merge, ...]) -> None:
    for index, merge in enumerate(merges):
        if index:
            text.append(", ", style=theme.MUTED)
        text.append(f"PR #{merge.pr_number}" if merge.pr_number else merge.component_id)
        if merge.merge_sha:
            text.append(f" {merge.merge_sha[:7]}", style=theme.MUTED)


def merge_summary(delivery: Delivery, *, short: bool = False) -> Text:
    """``merges PR #8 1a2b3c4, PR #9 4c4706b · release ref 4c4706b · CI unknown (...)``."""
    text = Text()
    text.append("merges ", style=f"bold {theme.MUTED}")
    if delivery.merges:
        _merges(text, delivery.merges)
    else:
        text.append("none recorded in this run", style=theme.MUTED)
    if delivery.release_ref:
        text.append(" · release " if short else " · release ref ", style=theme.MUTED)
        text.append(delivery.release_ref[:7], style="bold")
    if not delivery.merges and not delivery.release_ref:
        return text
    text.append(" · ", style=theme.MUTED)
    text.append(f"CI {delivery.ci}", style=f"bold {theme.WARNING}")
    if not short:
        text.append(f" ({delivery.ci_reason})", style=theme.MUTED)
    return text
