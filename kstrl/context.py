"""Context accumulation for retry prompts.

R10.2 (issue #223) makes this level-triggered. An edge-triggered system
acts on transitions ("this failure happened"); a level-triggered one acts
on current state ("this failure is happening now"). Before R10.2 the
object was an integrator with no discharge: three append-only lists, no
clear and no expiry, re-rendered in full on every retry under the line
"Fix ALL issues listed above before completing." An agent on attempt 3
was therefore handed attempt 1's failures - which it may well have fixed
on attempt 2 - unmarked as fixed, and told to fix them.

Each failure now carries the attempt it was measured in and the phase
that measured it, and the renderer sorts them into three buckets. See
``_buckets`` for the rule.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# The phases run in this fixed order inside one attempt. The rank is
# what lets the renderer tell "this sensor ran again in the latest
# attempt, so the older reading is stale" from "this sensor never ran in
# the latest attempt, so its older reading still stands".
#
# "engineer" covers everything the engineer loop and the PR flow raise
# before Phase 1 measures anything: guard violations that abort the
# loop, breaker trips, merge-conflict restarts, and human retry requests
# at the E6 checkpoint.
PHASE_RANK: dict[str, int] = {
    "engineer": 0,
    "verification": 1,
    "review": 2,
    "security": 3,
    "contract": 4,
}

#: Attempt number carried by entries recovered from a context serialised
#: before entries existed. Their real age is unknown.
LEGACY_ATTEMPT = 0

#: Which phases feed each backward-compatible string view. Mirrors the
#: list each phase's text landed in before R10.2.
_VIEW_PHASES: dict[str, tuple[str, ...]] = {
    "review_findings": ("engineer", "review", "security"),
    "verification_failures": ("verification",),
    "contract_failures": ("contract",),
}

#: Phase assigned to each legacy list when reading pre-R10.2 JSON. The
#: three lists cannot distinguish engineer/review/security, so
#: review_findings collapses to "review"; the attempt is unknown either
#: way, which is what actually decides where those entries render.
_LEGACY_LIST_PHASE: dict[str, str] = {
    "review_findings": "review",
    "verification_failures": "verification",
    "contract_failures": "contract",
}


@dataclass
class IterationRecord:
    """Record of a single iteration attempt.

    ``iteration`` is the engineer loop's own iteration counter, which is
    not the attempt number: one attempt runs many iterations. ``attempt``
    carries the attempt number so the history renders honestly; it is
    ``LEGACY_ATTEMPT`` on records deserialised from pre-R10.2 JSON, and
    the renderer then falls back to the record's position.
    """

    iteration: int
    success: bool
    error: str | None = None
    summary: str = ""
    attempt: int = LEGACY_ATTEMPT


@dataclass(frozen=True)
class FailureEntry:
    """One sensor reading: a failure, the attempt it was measured in, and
    the phase that measured it."""

    attempt: int
    phase: str
    text: str


@dataclass(frozen=True)
class _Buckets:
    """The three groups ``format_for_prompt`` renders, plus the latest
    attempt any of them was measured in."""

    current: list[FailureEntry]
    not_remeasured: list[FailureEntry]
    resolved: list[FailureEntry]
    measured_attempt: int


@dataclass
class IterationContext:
    """Accumulated context across retries for a component.

    Serializable to JSON for transport across process boundaries.
    """

    records: list[IterationRecord] = field(default_factory=list)
    entries: list[FailureEntry] = field(default_factory=list)

    # Backward-compatible read-only views. Nothing in kstrl/ reads these
    # any more, but they keep the shape the pre-R10.2 object exposed.
    @property
    def review_findings(self) -> list[str]:
        return self._texts("review_findings")

    @property
    def verification_failures(self) -> list[str]:
        return self._texts("verification_failures")

    @property
    def contract_failures(self) -> list[str]:
        return self._texts("contract_failures")

    def _texts(self, view: str) -> list[str]:
        phases = _VIEW_PHASES[view]
        return [e.text for e in self.entries if e.phase in phases]

    def add_iteration(self, record: IterationRecord) -> None:
        self.records.append(record)

    def add_review_finding(
        self, finding: str, *, attempt: int, phase: str,
    ) -> None:
        """``phase`` is explicit: the review, security, engineer-guard and
        checkpoint call sites all route their text through here."""
        self._add(finding, attempt, phase)

    def add_verification_failure(self, failure: str, *, attempt: int) -> None:
        self._add(failure, attempt, "verification")

    def add_contract_failure(self, failure: str, *, attempt: int) -> None:
        self._add(failure, attempt, "contract")

    def _add(self, text: str, attempt: int, phase: str) -> None:
        if not text:
            return
        if phase not in PHASE_RANK:
            raise ValueError(
                f"unknown phase {phase!r}; expected one of {sorted(PHASE_RANK)}"
            )
        self.entries.append(
            FailureEntry(attempt=attempt, phase=phase, text=text),
        )

    def _latest_attempt(self) -> int:
        """The latest attempt any evidence came from.

        Failure entries and records both carry the attempt. Records
        deserialised from pre-R10.2 JSON do not, so their count is the
        floor: at most one record is appended per attempt.
        """
        return max(
            max((e.attempt for e in self.entries), default=0),
            max((r.attempt for r in self.records), default=0),
            len(self.records),
        )

    def _buckets(self) -> _Buckets:
        """Sort the entries into current, not re-measured, and resolved.

        Let ``N`` be the latest attempt with any entry and ``Q`` the rank
        of the highest-ranked phase with an entry from ``N`` (an attempt
        stops at its first failing gate, so in practice that is the gate
        that fired; the max is the safe general form).

        - attempt ``N``: current, rendered in full.
        - rank above ``Q``: that sensor never ran in attempt ``N``, so
          its reading is un-re-measured, not stale. Rendered in full.
        - rank at or below ``Q``: rank below ``Q`` means the phase ran in
          attempt ``N`` and passed, or ``Q`` would be lower; rank equal
          to ``Q`` means the same sensor produced a fresh reading that
          supersedes the old one. Counted, not rendered.
        """
        current: list[FailureEntry] = []
        not_remeasured: list[FailureEntry] = []
        resolved: list[FailureEntry] = []

        live = [e for e in self.entries if e.attempt > LEGACY_ATTEMPT]
        if not live:
            not_remeasured.extend(self.entries)
            return _Buckets(current, not_remeasured, resolved, 0)

        n = max(e.attempt for e in live)
        q = max(PHASE_RANK[e.phase] for e in live if e.attempt == n)
        for entry in self.entries:
            if entry.attempt == LEGACY_ATTEMPT:
                # Special-cased AHEAD of the rank comparison. The rank
                # rule infers "this phase ran in attempt N and passed"
                # from an entry being older than N; an entry of unknown
                # age supports no such inference. Without this branch the
                # rank rule files a legacy review entry under Resolved
                # whenever the latest failure ranks at or above review,
                # silently dropping a finding whose age nobody knows.
                not_remeasured.append(entry)
            elif entry.attempt == n:
                current.append(entry)
            elif PHASE_RANK[entry.phase] > q:
                not_remeasured.append(entry)
            else:
                resolved.append(entry)
        return _Buckets(current, not_remeasured, resolved, n)

    def format_for_prompt(self) -> str:
        """Format accumulated context as text to prepend to the agent prompt."""
        sections: list[str] = []
        latest = self._latest_attempt()
        sections.append(f"=== PREVIOUS ATTEMPT CONTEXT (Attempt {latest + 1}) ===")

        buckets = self._buckets()
        measured = buckets.measured_attempt

        if buckets.current:
            gate = max(
                buckets.current, key=lambda e: PHASE_RANK[e.phase],
            ).phase
            sections.append("")
            sections.append(
                f"## Current failures (measured in attempt {measured}, {gate})"
            )
            sections.extend(e.text for e in buckets.current)

        if buckets.not_remeasured:
            dated = [
                e.attempt for e in buckets.not_remeasured
                if e.attempt > LEGACY_ATTEMPT
            ]
            heading = "## Not re-measured"
            if dated:
                heading += f" since attempt {min(dated)}"
            sections.append("")
            sections.append(heading)
            for entry in buckets.not_remeasured:
                label = (
                    "attempt unknown" if entry.attempt == LEGACY_ATTEMPT
                    else f"attempt {entry.attempt}"
                )
                sections.append(f"({label}, {entry.phase}) {entry.text}")

        if buckets.resolved:
            names = ", ".join(sorted(
                {e.phase for e in buckets.resolved},
                key=lambda p: PHASE_RANK[p],
            ))
            sections.append("")
            sections.append("## Resolved or superseded")
            sections.append(
                f"{len(buckets.resolved)} earlier finding(s) from {names} "
                f"passed or were re-measured in attempt {measured} and are "
                f"omitted."
            )

        if self.records:
            measured_attempts = {
                e.attempt for e in self.entries if e.attempt > LEGACY_ATTEMPT
            }
            sections.append("")
            sections.append("## Attempt history")
            for position, rec in enumerate(self.records, start=1):
                attempt = (
                    rec.attempt if rec.attempt > LEGACY_ATTEMPT else position
                )
                status = "completed" if rec.success else "FAILED"
                line = f"- Attempt {attempt}: {status}"
                # The record's error is the failure text itself on the
                # paths that raise one (the in-loop guard sets it to the
                # scope violation, which is also a dated entry). Printing
                # it here would smuggle a superseded failure back into
                # the prompt through the history, undoing the bucketing.
                # It is the only account of the attempt where no entry
                # was recorded - a plain engineer-loop failure, a
                # merge-conflict restart - so it renders there.
                if rec.error and attempt not in measured_attempts:
                    line += f" - {rec.error}"
                if rec.summary:
                    line += f" ({rec.summary})"
                sections.append(line)

        sections.append("")
        sections.append(
            "Fix the current failures. Re-check the not-re-measured items "
            "yourself; do not assume they still apply."
        )
        sections.append("=== END PREVIOUS CONTEXT ===")

        return "\n".join(sections)

    def to_json(self) -> str:
        """Serialize to JSON string for ProcessPoolExecutor transport."""
        data: dict[str, Any] = {
            "records": [
                {
                    "iteration": r.iteration,
                    "success": r.success,
                    "error": r.error,
                    "summary": r.summary,
                    "attempt": r.attempt,
                }
                for r in self.records
            ],
            "entries": [
                {"attempt": e.attempt, "phase": e.phase, "text": e.text}
                for e in self.entries
            ],
        }
        return json.dumps(data)

    @classmethod
    def from_json(cls, data: str) -> IterationContext:
        """Deserialize from JSON string, in either the current shape or the
        pre-R10.2 shape (three undated string lists)."""
        if not data or data == "{}":
            return cls()
        parsed = json.loads(data)
        ctx = cls()
        for rec_data in parsed.get("records", []):
            ctx.records.append(IterationRecord(
                iteration=rec_data["iteration"],
                success=rec_data["success"],
                error=rec_data.get("error"),
                summary=rec_data.get("summary", ""),
                attempt=rec_data.get("attempt", LEGACY_ATTEMPT),
            ))
        if "entries" in parsed:
            for entry_data in parsed["entries"]:
                ctx.entries.append(FailureEntry(
                    attempt=entry_data["attempt"],
                    phase=entry_data["phase"],
                    text=entry_data["text"],
                ))
            return ctx
        for list_name, phase in _LEGACY_LIST_PHASE.items():
            for text in parsed.get(list_name, []):
                ctx.entries.append(FailureEntry(
                    attempt=LEGACY_ATTEMPT, phase=phase, text=text,
                ))
        return ctx
