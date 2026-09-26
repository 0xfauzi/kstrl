"""What a run delivered: integration review, merges, and each merge's CI (#433 F9, M3, G11).

"All components completed" is not "shipped". Three separate pieces of
evidence, each shown with its own state:

- the merged-feature integration review (``integration_view``);
- the merges: each ``pr_merged`` event's PR number and ``merge_sha``,
  and the run's ``release_ref`` from ``factory_completed`` (#442);
- the CI state of each merge commit, from the ledger ``ks ci poll``
  writes (#553, ``ci_state.read_ci_ledger``). The TUI never asks the
  network. A commit the ledger has not read says so and names the
  command that reads it; every reading says how long ago it was taken,
  so a stale one is visible. Only ``passed`` is green: unknown, absent
  and an unreadable ledger never are.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from rich.text import Text

from kstrl.ci_state import CiLedger, CiState, read_ci_ledger
from kstrl.tui import theme
from kstrl.tui.integration_view import (
    FIXED,
    HANDED_OFF,
    OPEN,
    UNKNOWN,
    IntegrationReview,
    read_integration_review,
)
from kstrl.tui.run_status import age_phrase

if TYPE_CHECKING:
    from kstrl.reducer import RunState

#: What a merge commit the ledger has no reading of says (#433 G11).
NO_CI_READING = "no CI reading - run ks ci poll"
#: Why a Delivery shows no CI until its ledger has been read.
CI_NOT_READ = "the CI ledger was not read"
#: The most merge commits listed one per line; the rest share one line.
MERGE_LINES = 4

#: CI state -> colour. Only passed is green (#433 G11).
CI_STYLE: dict[CiState, str] = {
    CiState.PASSED: theme.SUCCESS,
    CiState.FAILED: theme.ERROR,
    CiState.RUNNING: theme.ACCENT,
    CiState.UNKNOWN: theme.WARNING,
}

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
    #: The CI ledger as read (#553); None when it was not or could not be.
    ci: CiLedger | None = None
    #: Why ``ci`` is None.
    ci_problem: str = CI_NOT_READ


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


def read_ci(root_dir: Path) -> tuple[CiLedger | None, str]:
    """The CI ledger, or None and why it could not be read (worker thread).

    ``read_ci_ledger`` raises on an unreadable file so that it is never an
    empty ledger. Here that becomes every commit's ``unknown`` with the
    reason, and the rest of the delivery section still renders.
    """
    try:
        return read_ci_ledger(root_dir), ""
    except Exception as exc:  # noqa: BLE001 - shown as unknown with the reason
        return None, f"the CI ledger could not be read: {type(exc).__name__}: {exc}"


def read_delivery(root_dir: Path, run_dir: Path, state: RunState) -> Delivery:
    """File reads: the run's review rounds, the integration state file and the CI ledger."""
    ci, ci_problem = read_ci(root_dir)
    return Delivery(
        run_id=run_dir.name,
        merges=merges_of(state),
        release_ref=state.release_ref,
        release_withheld=state.release_withheld,
        integration=read_integration_review(root_dir, run_dir, fix_statuses(state)),
        ci=ci,
        ci_problem=ci_problem,
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
        text.append("no review recorded for this run", style=theme.MUTED)
        return text
    word, color, note = _outcome(review)
    text.append(word, style=f"bold {color}")
    if review.criteria:
        _verdicts(text, review, short)
    _disposition_counts(text, review)
    if note and not short:
        text.append(f" · {note}", style=theme.MUTED)
    return text


def release_note(delivery: Delivery) -> str:
    """`` · release ref 4c4706b`` for the section title, or ""."""
    return f" · release ref {delivery.release_ref[:7]}" if delivery.release_ref else ""


def _read_ago(observed_at: str, now: float) -> str:
    """``read 5m ago``; the recorded time itself when it cannot be parsed."""
    try:
        taken = datetime.strptime(observed_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return f"read at {observed_at}"
    return f"read {age_phrase(now - taken.timestamp())} ago"


def _ci_of(merge: Merge, delivery: Delivery, now: float) -> tuple[CiState | None, list[str]]:
    """(state, what to say after it) for one merge commit; None is "no reading"."""
    if not merge.merge_sha:
        return CiState.UNKNOWN, ["no merge commit recorded"]
    if delivery.ci is None:
        return CiState.UNKNOWN, [delivery.ci_problem]
    reading = delivery.ci.latest(merge.merge_sha)
    if reading is None:
        return None, []
    return reading.state, [_read_ago(reading.observed_at, now), reading.reason]


def _ci_word(state: CiState | None) -> str:
    return NO_CI_READING if state is None else f"CI {state}"


def ci_line(merge: Merge, delivery: Delivery, now: float) -> Text:
    """``merged PR #8 4ab99ae  CI passed · read 5m ago · 7 checks passed``.

    The reason is last, so a narrow screen cuts it and not the state or
    how old the reading is.
    """
    text = Text("merged ", style=f"bold {theme.MUTED}")
    text.append(f"PR #{merge.pr_number}" if merge.pr_number else merge.component_id)
    if merge.merge_sha:
        text.append(f" {merge.merge_sha[:7]}", style=theme.MUTED)
    state, detail = _ci_of(merge, delivery, now)
    style = theme.WARNING if state is None else f"bold {CI_STYLE[state]}"
    text.append(f"  {_ci_word(state)}", style=style)
    for part in detail:
        if part:
            text.append(f" · {part}", style=theme.MUTED)
    return text


def _more_line(rest: tuple[Merge, ...], delivery: Delivery, now: float) -> Text:
    """``+3 more merged: 2 CI passed, 1 CI failed``: counted, never dropped."""
    counts = Counter(_ci_word(_ci_of(merge, delivery, now)[0]) for merge in rest)
    text = Text(f"+{len(rest)} more merged: ", style=f"bold {theme.MUTED}")
    text.append(", ".join(f"{count} {word}" for word, count in counts.items()))
    return text


def merge_lines(delivery: Delivery, now: float, width: int) -> list[Text]:
    """One line per merge commit with its CI (#433 G11), each cut to
    ``width`` cells with an ellipsis. Past ``MERGE_LINES`` commits the
    rest are counted by state on the last line."""
    if not delivery.merges:
        lines = [Text("merged ", style=f"bold {theme.MUTED}")]
        lines[0].append("none recorded in this run", style=theme.MUTED)
    elif len(delivery.merges) > MERGE_LINES:
        shown, rest = delivery.merges[: MERGE_LINES - 1], delivery.merges[MERGE_LINES - 1 :]
        lines = [ci_line(m, delivery, now) for m in shown] + [_more_line(rest, delivery, now)]
    else:
        lines = [ci_line(merge, delivery, now) for merge in delivery.merges]
    for line in lines:
        line.truncate(max(1, width), overflow="ellipsis")
    return lines
