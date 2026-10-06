"""The Phase 3 outer loop and the integration review after it (#483; design #480 3.4, 3.5).

``_run_factory_locked`` iterates :meth:`IntegrationLoop.rounds`. Each round
is one scheduling pass plus Phase 3. After it, the loop re-enters
scheduling when a contract breaker was reset, and otherwise runs the
integration review once and stops. Only when ``[factory]
integration_blocking`` is true does a stop that is not clean fail the run.
No fix component is built: kstrl cannot tell which paths hold tests (#696
decision 6), so every open code finding is handed off and the round stops
red. The rules live here so the factory's own function does not grow.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING, Any

from kstrl.integration import STATUS_CLOSED, STATUS_HANDOFF, STATUS_OPEN
from kstrl.integration_fix import handoff_reason
from kstrl.integration_phase import (
    IntegrationRecord,
    IntegrationRun,
    Phase3Round,
    run_integration_review,
)
from kstrl.integration_state import (
    OUTCOME_CLEAN,
    OUTCOME_NOT_RUN,
    OUTCOME_OPEN_FINDINGS,
    OUTCOME_RED,
    add_stop,
    hand_off,
    write_state,
)

if TYPE_CHECKING:
    from kstrl.shutdown import StopController


class IntegrationLoop:
    """The rounds of the factory's outer loop, and the review after the last."""

    def __init__(self, run: IntegrationRun, stop: StopController | None) -> None:
        self._run = run
        self._stop = stop
        self.record = IntegrationRecord(OUTCOME_NOT_RUN, "the integration review was not reached")

    def rounds(self) -> Iterator[Phase3Round]:
        """Yield one Phase3Round per scheduling pass until the loop stops.

        The factory fills the yielded round and must not replace it: this
        generator reads the same object back after the ``yield``.
        """
        while True:
            phase3 = Phase3Round()
            yield phase3
            if phase3.breaker_reset:
                continue
            self.record = run_integration_review(self._run, phase3, self._stop)
            if self._run.pipeline.factory_config.integration_blocking:
                self.record = decide_round(self._run, self.record)
                project_stop(self._run, self.record)
            return


def decide_round(run: IntegrationRun, record: IntegrationRecord) -> IntegrationRecord:
    """Blocking only: hand off every open code finding and stop red (#696
    decision 6). A round that was red or not run is its own stop, already
    recorded. A clean round is one too, unless this run handed a finding
    off (#497)."""
    if record.state is None:
        return record
    if record.outcome == OUTCOME_CLEAN:
        handed = _handed_off_this_run(record.state, run.run_id)
        if handed:
            return _stop(run, record, OUTCOME_RED, _handoff_reason(handed))
        return record
    if record.outcome != OUTCOME_OPEN_FINDINGS:
        return record
    findings = _loop_findings(record.state, record)
    for finding in findings:
        if finding["status"] == STATUS_OPEN:
            reason = handoff_reason(finding["locations"])
            hand_off(record.state, finding["id"], reason, run.run_id, record.sha)
    return _stop(run, record, OUTCOME_RED, _handoff_reason([f["id"] for f in findings]))


def _loop_findings(state: dict[str, Any], record: IntegrationRecord) -> list[dict[str, Any]]:
    """This round's opened findings and the carried ones still open."""
    wanted = {*record.opened, *record.still_open}
    return [f for f in state["findings"] if f["id"] in wanted]


def _handoff_reason(handed: Sequence[str]) -> str:
    return (
        f"{len(handed)} findings cannot become a fix story (register or unscoped): "
        f"{', '.join(handed)}"
    )


def _handed_off_this_run(state: dict[str, Any], run_id: str) -> list[str]:
    """The findings this run handed off (#497). A finding is handed off by
    the run that wrote its last history entry: add_findings opens a
    register finding as handed off, hand_off appends one."""
    return [
        f["id"]
        for f in state["findings"]
        if f["status"] == STATUS_HANDOFF and f["history"][-1]["runId"] == run_id
    ]


def _stop(
    run: IntegrationRun, record: IntegrationRecord, outcome: str, reason: str
) -> IntegrationRecord:
    """Record a loop stop in the state before anything projects it."""
    assert record.state is not None
    add_stop(record.state, run.run_id, record.sha, outcome, reason, record.evidence, gates=True)
    error = write_state(run.root_dir, record.state)
    if error:
        run.ui.err(f"  Integration state write failed: {error}")
    run.ui.warn(f"  Integration loop stopped: {outcome}: {reason}")
    return dataclasses.replace(record, outcome=outcome, reason=reason)


def project_stop(run: IntegrationRun, record: IntegrationRecord) -> None:
    """Blocking only: a stop that is not clean fails the run through
    contract_failures, which the exit code already reads, and leaves one
    HALTED_RUN inbox item. The state entry was written first."""
    if record.outcome == OUTCOME_CLEAN:
        return
    open_ids = _open_ids(record)
    handed = [] if record.state is None else _handed_off_this_run(record.state, run.run_id)
    ids = [*open_ids, *(fid for fid in handed if fid not in open_ids)]
    lines = [] if open_ids else [f"integration {record.outcome}: {record.reason}"]
    lines.extend(f"integration {fid}: {_summary(record, fid)}" for fid in ids)
    run.pipeline.factory_result.contract_failures.extend(lines)
    run.pipeline.record_integration_halt(
        f"{record.outcome}: {record.reason}", ids, str(record.evidence or "")
    )


def _open_ids(record: IntegrationRecord) -> list[str]:
    """This round's findings and carried findings not closed at the stop."""
    if record.state is None:
        return []
    wanted = {*record.opened, *record.still_open}
    return [
        f["id"]
        for f in record.state["findings"]
        if f["id"] in wanted and f["status"] != STATUS_CLOSED
    ]


def _summary(record: IntegrationRecord, finding_id: str) -> str:
    assert record.state is not None
    finding = next(f for f in record.state["findings"] if f["id"] == finding_id)
    first = next((line for line in str(finding["text"]).splitlines() if line.strip()), "")
    return f"{finding['status']}: {first.strip()[:200]}"
