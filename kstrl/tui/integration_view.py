"""The merged-feature integration review of one run, as the TUI shows it (#433 F9, M4).

Two files hold it, and neither is folded into ``events.jsonl``:

- ``.kstrl/runs/<run>/integration/review-<n>.json``: one per round of
  the review in that run (``integration_state.write_evidence``). The
  verdict per criterion (IC1 to IC5) is recorded ONLY here, under
  ``review.criteria``. A round that did not run carries its outcome and
  reason and no verdicts.
- ``.kstrl/integration/state.json``: per feature, rewritten in place
  across runs (``integration_state.write_state``). It holds every IF
  finding's CURRENT status (open, handoff, closed) and the fixes the
  loop built, each naming the IF ids it carries.

A finding's disposition is joined by its id, never by its text: the
finding's ``status`` in state.json, and for a closed or open finding the
LAST fix whose ``findings`` list names that id. There is no dismissal
record anywhere in kstrl, so "dismissed" is never shown. A finding this
run opened that state.json no longer carries (a later feature replaced
the binding) is shown as ``unknown`` with the reason, never as open.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kstrl.integration import FINDING_ID_PREFIX
from kstrl.jsonread import read_json

#: Dispositions, one per IF finding. "dismissed" is absent on purpose:
#: kstrl records no dismissal.
FIXED = "fixed"
HANDED_OFF = "handed off"
OPEN = "open"
UNKNOWN = "unknown"

#: state.json's status word for an open finding is the disposition word.
_STATUS_TO_DISPOSITION = {"closed": FIXED, "handoff": HANDED_OFF, OPEN: OPEN}

#: Largest evidence file read; a review file is a few kilobytes.
_MAX_FILE_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class CriterionVerdict:
    story_id: str
    title: str
    verdict: str
    explanation: str = ""


@dataclass(frozen=True)
class ReviewRound:
    number: int
    outcome: str
    reason: str


@dataclass(frozen=True)
class FindingDisposition:
    finding_id: str
    kind: str
    story_id: str
    text: str
    locations: tuple[str, ...]
    #: FIXED | HANDED_OFF | OPEN | UNKNOWN.
    disposition: str
    #: The rest of the sentence: "by integration-fix-1", the hand-off
    #: reason, the fix that carries an open finding and how it ended.
    detail: str = ""


@dataclass(frozen=True)
class IntegrationReview:
    rounds: tuple[ReviewRound, ...]
    #: From the newest round that recorded verdicts; () when none did.
    criteria: tuple[CriterionVerdict, ...]
    findings: tuple[FindingDisposition, ...]
    #: Why dispositions could not be read, "" when they could.
    state_problem: str = ""
    #: Files that could not be read, one line each.
    unreadable: tuple[str, ...] = field(default_factory=tuple)

    @property
    def latest(self) -> ReviewRound | None:
        return self.rounds[-1] if self.rounds else None

    def count(self, disposition: str) -> int:
        return sum(1 for finding in self.findings if finding.disposition == disposition)


def _read_object(path: Path) -> tuple[dict[str, Any] | None, str]:
    """A JSON object from ``path``, or None and why not. Bounded read."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(_MAX_FILE_BYTES + 1)
    except OSError as exc:
        return None, f"{path.name}: {exc.strerror or exc}"
    if len(raw) > _MAX_FILE_BYTES:
        return None, f"{path.name}: larger than {_MAX_FILE_BYTES} bytes"
    try:
        data = read_json(raw)
    except ValueError as exc:
        return None, f"{path.name}: {exc}"
    if not isinstance(data, dict):
        return None, f"{path.name}: not a JSON object"
    return data, ""


def _review_number(path: Path) -> int:
    stem = path.stem.removeprefix("review-")
    return int(stem) if stem.isdigit() else 0


def review_files(run_dir: Path) -> list[Path]:
    """The run's review rounds in order; [] when the review never wrote one."""
    directory = run_dir / "integration"
    try:
        found = [path for path in directory.glob("review-*.json") if _review_number(path)]
    except OSError:
        return []
    return sorted(found, key=_review_number)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _criteria(payload: Mapping[str, Any]) -> tuple[CriterionVerdict, ...]:
    titles = {
        _text(story.get("id")): _text(story.get("title"))
        for story in payload.get("stories") or []
        if isinstance(story, Mapping)
    }
    review = payload.get("review")
    if not isinstance(review, Mapping):
        return ()
    verdicts = []
    for entry in review.get("criteria") or []:
        if not isinstance(entry, Mapping):
            continue
        story_id = _text(entry.get("storyId"))
        # A carried IF story's verdict is how that finding closed, which
        # its disposition already says; the criteria are IC1 to IC5.
        if not story_id or story_id.startswith(FINDING_ID_PREFIX):
            continue
        verdicts.append(
            CriterionVerdict(
                story_id=story_id,
                title=titles.get(story_id, ""),
                verdict=_text(entry.get("verdict")) or UNKNOWN,
                explanation=_text(entry.get("explanation")),
            )
        )
    return tuple(verdicts)


def _opened_ids(payload: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    """(id, entry) for every IF this round opened, closed or kept open."""
    found: list[tuple[str, Mapping[str, Any]]] = []
    for entry in payload.get("opened") or []:
        if isinstance(entry, Mapping) and _text(entry.get("id")):
            found.append((_text(entry.get("id")), entry))
    for key in ("closed", "stillOpen"):
        for finding_id in payload.get(key) or []:
            if isinstance(finding_id, str) and finding_id:
                found.append((finding_id, {}))
    return found


def _state_findings(
    state: Mapping[str, Any] | None,
) -> dict[str, Mapping[str, Any]]:
    if state is None:
        return {}
    return {
        _text(entry.get("id")): entry
        for entry in state.get("findings") or []
        if isinstance(entry, Mapping) and _text(entry.get("id"))
    }


def _last_fix_for(state: Mapping[str, Any] | None, finding_id: str) -> str:
    """The id of the LAST fix whose ``findings`` names ``finding_id``, or ""."""
    if state is None:
        return ""
    fix_id = ""
    for fix in state.get("fixes") or []:
        if not isinstance(fix, Mapping):
            continue
        carried = fix.get("findings") or []
        if isinstance(carried, list) and finding_id in carried:
            fix_id = _text(fix.get("id"))
    return fix_id


def _history_names_run(entry: Mapping[str, Any], run_id: str) -> bool:
    return any(
        isinstance(item, Mapping) and item.get("runId") == run_id
        for item in entry.get("history") or []
    )


def _disposition(
    finding_id: str,
    recorded: Mapping[str, Any] | None,
    state: Mapping[str, Any] | None,
    state_problem: str,
    fix_status: Mapping[str, str],
) -> tuple[str, str]:
    if recorded is None:
        why = state_problem or "state.json does not record it"
        return UNKNOWN, why
    disposition = _STATUS_TO_DISPOSITION.get(_text(recorded.get("status")), UNKNOWN)
    fix_id = _last_fix_for(state, finding_id)
    if disposition == FIXED:
        return FIXED, f"by {fix_id}" if fix_id else "closed by a later review; no fix recorded"
    if disposition == HANDED_OFF:
        reason = _text(recorded.get("handoffReason"))
        return HANDED_OFF, reason or "outside what a fix component can change"
    if disposition == OPEN and fix_id:
        ended = fix_status.get(fix_id, "")
        return OPEN, f"{fix_id} carries it ({ended})" if ended else f"{fix_id} carries it"
    if disposition == OPEN:
        return OPEN, "no fix built yet"
    return UNKNOWN, f"status {recorded.get('status')!r} is not one kstrl writes"


def read_integration_review(
    root_dir: Path,
    run_dir: Path,
    fix_status: Mapping[str, str] | None = None,
) -> IntegrationReview | None:
    """The run's integration review, or None when it wrote no round.

    ``fix_status`` maps a fix component id to its status (from the run's
    folded state), so an open finding can say how its fix ended.
    """
    files = review_files(run_dir)
    if not files:
        return None
    rounds, criteria, unreadable, seen = _read_rounds(files)
    from kstrl.integration_state import state_path as integration_state_path

    state_path = integration_state_path(root_dir)
    state, state_problem = _read_object(state_path)
    if state is None and not state_path.exists():
        state_problem = "no .kstrl/integration/state.json"
    recorded = _state_findings(state)
    for finding_id, entry in recorded.items():
        if finding_id not in seen and _history_names_run(entry, run_dir.name):
            seen[finding_id] = entry
    findings = tuple(
        _finding(
            finding_id,
            recorded.get(finding_id) or seen[finding_id],
            _disposition(
                finding_id, recorded.get(finding_id), state, state_problem, fix_status or {}
            ),
        )
        for finding_id in sorted(seen, key=_finding_order)
    )
    return IntegrationReview(
        rounds=tuple(rounds),
        criteria=criteria,
        findings=findings,
        state_problem=state_problem,
        unreadable=tuple(unreadable),
    )


def _read_rounds(
    files: list[Path],
) -> tuple[
    list[ReviewRound],
    tuple[CriterionVerdict, ...],
    list[str],
    dict[str, Mapping[str, Any]],
]:
    """Each round's outcome, the newest verdicts, and every IF id named."""
    rounds: list[ReviewRound] = []
    criteria: tuple[CriterionVerdict, ...] = ()
    unreadable: list[str] = []
    seen: dict[str, Mapping[str, Any]] = {}
    for path in files:
        payload, problem = _read_object(path)
        if payload is None:
            unreadable.append(problem)
            rounds.append(ReviewRound(_review_number(path), UNKNOWN, problem))
            continue
        outcome = _text(payload.get("outcome")) or UNKNOWN
        rounds.append(ReviewRound(_review_number(path), outcome, _text(payload.get("reason"))))
        criteria = _criteria(payload) or criteria
        for finding_id, entry in _opened_ids(payload):
            # A bare id (closed, stillOpen) never replaces a full entry.
            seen[finding_id] = entry or seen.get(finding_id, {})
    return rounds, criteria, unreadable, seen


def _finding(
    finding_id: str, entry: Mapping[str, Any], disposition: tuple[str, str]
) -> FindingDisposition:
    locations = entry.get("locations") or []
    return FindingDisposition(
        finding_id=finding_id,
        kind=_text(entry.get("kind")),
        story_id=_text(entry.get("storyId")) or _text(entry.get("story_id")),
        text=_text(entry.get("text")),
        locations=tuple(str(item) for item in locations if isinstance(item, str)),
        disposition=disposition[0],
        detail=disposition[1],
    )


def _finding_order(finding_id: str) -> tuple[int, str]:
    number = finding_id.rsplit("-", 1)[-1]
    return (int(number) if number.isdigit() else 1 << 30, finding_id)
