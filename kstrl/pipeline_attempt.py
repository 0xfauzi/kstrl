"""The record of one attempt of one component.

``AttemptRecorder`` holds the start and the end of an attempt, the phase
events, the transcripts, the findings, the failure signatures, the phase
readings and the journal rows.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from kstrl import event_catalog
from kstrl.agents.prompt_record import AgentCall
from kstrl.context import IterationContext
from kstrl.findings import Finding, finding_model, tag_finding_with_attempt
from kstrl.manifest import Component, ComponentStatus
from kstrl.pipeline_state import PipelineState, _iso_now
from kstrl.worktree_sweep import WorktreeSweep, sweep_findings


class AttemptRecorder(PipelineState):
    """The record that one attempt leaves in the journal and in the run files."""

    def _journal_offset(self) -> int:
        """Current byte size of the v1 progress log; used to bracket one
        attempt's slice of events (R3.3). -1 when no real progress log
        is configured for this run. Deliberately pegged to the v1 compat
        file, NOT events.jsonl - the manifest's journal_offset_start/end
        semantics must not silently repoint (plan: explicit future
        schema decision)."""
        if self.journal_path is None:
            return -1
        try:
            return self.journal_path.stat().st_size if self.journal_path.exists() else 0
        except OSError:
            return -1

    @contextmanager
    def _phase_transcript(
        self,
        comp_id: str,
        phase: str,
    ) -> Iterator[Callable[[str], None] | None]:
        """Line writer onto RunPaths.phase_log for one phase invocation.

        Yields None when no run dir is configured (progress logging
        disabled) or the file cannot be opened - transcripts are
        observability and must never gate a phase. Repeated
        invocations (retries) append.
        """
        if self.run_paths is None:
            yield None
            return
        path = self.run_paths.phase_log(comp_id, phase)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(path, "a", buffering=1, encoding="utf-8")
        except OSError:
            yield None
            return

        def _write_line(line: str) -> None:
            fh.write(line + "\n")

        try:
            yield _write_line
        finally:
            try:
                fh.close()
            except OSError:
                pass

    def _agent_call(self, comp: Component, role: str) -> AgentCall:
        """Who one phase's agent prompts are recorded for (#532).

        Under ``usage_paths``, not ``run_paths``: the record is evidence a
        later reader scores, so it survives the progress-log opt-out the
        way the usage accounting does.
        """
        return AgentCall(
            run_root=self.usage_paths.root,
            run_id=self.run_id,
            component=comp.id,
            role=role,
            attempt=comp.retries + 1,
        )

    def _phase_started(self, comp: Component, phase: str) -> float:
        """Emit the authoritative phase bracket opener; returns the
        monotonic start for the matching _phase_completed."""
        self.bus.emit(
            event_catalog.PhaseStarted(
                component=comp.id,
                phase=phase,
                attempt=comp.retries + 1,
            )
        )
        return time.monotonic()

    def _phase_completed(
        self,
        comp: Component,
        phase: str,
        started: float,
        passed: bool,
        detail: str = "",
    ) -> None:
        self.bus.emit(
            event_catalog.PhaseCompleted(
                component=comp.id,
                phase=phase,
                passed=passed,
                detail=detail,
                duration_seconds=round(time.monotonic() - started, 2),
            )
        )

    def _debug_dir_for(self, comp_id: str) -> Path:
        """Forensic raw-output dir for this run's component (R1.2)."""
        return self.root_dir / ".kstrl" / "debug" / self.run_id / comp_id

    def _add_findings(
        self,
        comp: Component,
        new_findings: list[Finding],
    ) -> None:
        """Append findings tagged ``attempt:<n>`` for the attempt in
        flight (R3.3), so the journal can attribute every finding to the
        attempt that produced it."""
        attempt = comp.retries + 1
        comp.findings.extend(tag_finding_with_attempt(f, attempt) for f in new_findings)
        # Chunk 4: stream each finding as a typed event the moment it is
        # recorded (the manifest only carries them at transition time).
        for finding in new_findings:
            self.bus.emit(
                event_catalog.FindingRecorded(
                    component=comp.id,
                    phase=finding.phase,
                    category=finding.category,
                    severity=finding.severity,
                    location=finding.location,
                    explanation=finding.explanation,
                    attempt=attempt,
                    model=finding_model(finding) or "",
                )
            )

    def begin_attempt(self, comp: Component) -> None:
        """PENDING -> RUNNING transition for one attempt (R3.3).

        The prior attempt's findings were journaled when its retry was
        scheduled (or by record_run when a previous run ended), so the
        manifest carries only the current attempt's stream; the failure
        and evidence pointers likewise describe only the attempt in
        flight."""
        comp.findings = []
        comp.review_findings = ""
        comp.failed_phase = ""
        comp.failed_check = ""
        comp.completed_at = ""
        comp.evidence_worktree = ""
        comp.evidence_debug_dir = ""
        comp.journal_offset_start = self._journal_offset()
        comp.journal_offset_end = -1
        comp.status = ComponentStatus.RUNNING.value
        comp.started_at = _iso_now()
        self.component_failure_signatures.pop(comp.id, None)
        # #247: the readings describe the attempt in flight, so the
        # attempt boundary is where they are cleared. Anything the
        # previous attempt observed is already in the context's JSON.
        # What this pop does is BOUND the dict, not decide anything: the
        # attempt number is captured at record time, so a pair carried
        # over from an earlier attempt never matches the latest one and
        # `_buckets` ignores it. Deleting the pop is measured green
        # (round 1 review, mutation R3) and no test is owed for it.
        self._phase_readings.pop(comp.id, None)
        self._attempt_started_monotonic[comp.id] = time.monotonic()

    def _end_attempt(self, comp: Component) -> None:
        """Stamp the attempt's evidence pointers when it stops running:
        the progress-log slice end, and the debug dir when any phase
        dumped raw output there (R3.3). Also stamp the attempt's full
        wall-clock duration (R6.4): every terminal transition (retry,
        fail, merge-pending, completed, scheduler backstop) routes
        through here, so duration_seconds covers engineer + verify +
        review + security + PR instead of the engineer loop only."""
        comp.journal_offset_end = self._journal_offset()
        started = self._attempt_started_monotonic.get(comp.id)
        if started is not None:
            comp.duration_seconds = time.monotonic() - started
        debug_dir = self._debug_dir_for(comp.id)
        if debug_dir.exists():
            comp.evidence_debug_dir = str(debug_dir)

    def journal_integration_result(
        self,
        outcome: str,
        reason: str,
        reviewed_sha: str,
        opened: Sequence[str],
        errors: Sequence[str],
        *,
        gates: bool = False,
    ) -> None:
        """Journal one integration review round (#482). Non-fatal, never silent."""
        from kstrl.evolution import (
            INTEGRATION_RESULT_EVENT,
            JOURNAL_SCHEMA_VERSION,
            EvolutionJournal,
        )

        journal = EvolutionJournal.open(self.root_dir, warn=self.ui.warn)
        if journal is None:
            return
        entry = {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "timestamp": _iso_now(),
            "run_id": self.run_id,
            "project": self.manifest.project_name,
            "component_id": "",
            "event_type": INTEGRATION_RESULT_EVENT,
            "outcome": outcome,
            "reason": reason,
            "reviewed_sha": reviewed_sha,
            "opened": list(opened),
            "errors": list(errors),
            "gates": gates,
        }
        try:
            journal.append_entries([entry])
        except OSError as exc:
            self.ui.warn(f"  Evolution journal write failed (non-fatal): {exc}")

    def journal_superseded_findings(
        self, comp: Component, failure_count: int | None = None
    ) -> None:
        """A scheduled retry supersedes the current attempt. Record the
        attempt's findings and iteration count in the evolution journal
        (attempt-tagged) before the next attempt clears the manifest
        stream, so superseded and shipped findings stay distinguishable
        (R3.3). The final attempt's findings reach the journal via
        record_run instead. Non-fatal on I/O errors, matching
        _record_contract_event.

        Writes the row whether or not the attempt produced any
        ``Finding``: an attempt boundary is worth recording either way,
        and a guard here used to drop the row entirely on a clean
        attempt, which is the row #233's reader depends on
        (``read_attempt_iterations``) to see every attempt.

        ``iteration_count`` is the ENDING attempt's own count, not a
        running total. ``process_result`` assigns it at
        ``pipeline.py:2276`` before routing into any transition, and both
        callers of this method run BEFORE the matching ``retries``
        increment (``pipeline.py:1558``, ``factory.py:4517``), so
        ``comp.retries + 1`` names the attempt the count belongs to. That
        is the same expression ``PhaseStarted`` uses at
        ``factory.py:4225``: one definition of the attempt number.
        """
        from kstrl.evolution import (
            FINDINGS_SUPERSEDED_EVENT,
            JOURNAL_SCHEMA_VERSION,
            EvolutionJournal,
        )

        journal = EvolutionJournal.open(self.root_dir, warn=self.ui.warn)
        if journal is None:
            return
        entry = {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "timestamp": _iso_now(),
            "run_id": self.run_id,
            "project": self.manifest.project_name,
            "component_id": comp.id,
            "event_type": FINDINGS_SUPERSEDED_EVENT,
            "attempt": comp.retries + 1,
            "iteration_count": comp.iteration_count,
            "failure_signatures": self.component_failure_signatures.get(
                comp.id,
                [],
            ),
            "findings": [f.to_dict() for f in comp.findings],
            # #233: the gate's failure count for this attempt, null when the
            # failure was not a gate's. The distribution
            # [factory] convergence_attempts is set from.
            "failure_count": failure_count,
        }
        try:
            journal.append_entries([entry])
        except OSError as exc:
            # Evolution recording is non-fatal, but never silent (R6.1).
            self.ui.warn(f"  Evolution journal write failed (non-fatal): {exc}")

    def _record_failure_signatures(
        self,
        comp: Component,
        phase: str,
        error: str,
        signatures: list[str] | None,
    ) -> None:
        """R6.1: remember the structured signatures for this failure so
        record_run journals real "<check>:<code>" identifiers instead of
        re-deriving a degenerate slug from the flattened error string.
        Sites that pass no signatures fall back to a slug of the
        error text under the failing phase."""
        from kstrl.evolution import signature_for_error

        if signatures:
            self.component_failure_signatures[comp.id] = list(signatures)
        else:
            self.component_failure_signatures[comp.id] = [
                signature_for_error(phase or "unknown", error),
            ]

    def _note_phase_reading(self, comp: Component, phase: str, produced: bool) -> None:
        """Record that ``phase`` measured this attempt of ``comp`` (#247).

        The attempt number is captured HERE rather than at merge time,
        which is what stops an off-by-one: ``comp.retries + 1`` is the
        attempt convention every entry site uses, and ``retry_or_fail``
        increments ``retries`` before it stores the context.
        """
        if produced:
            self._phase_readings.setdefault(comp.id, set()).add((comp.retries + 1, phase))

    def _merge_phase_readings(self, comp_id: str, ctx: IterationContext) -> None:
        """Merge this attempt's readings into the context about to be
        stored for the next attempt.

        Takes the object rather than JSON so each writer parses and
        serialises once. A record carried forward from an older attempt
        is inert by construction: ``_buckets`` only consults readings
        whose attempt equals the latest one.
        """
        for attempt, phase in self._phase_readings.get(comp_id, set()):
            ctx.add_phase_reading(phase, attempt=attempt)

    def record_worktree_sweep(self, comp_id: str, sweep: WorktreeSweep, phase: str) -> None:
        """Record a worktree sweep's survivors as findings on the component (#461)."""
        comp = self.manifest.get_component(comp_id)
        if comp is not None:
            self._add_findings(comp, sweep_findings(sweep, phase))

    def _record_phase_skip(
        self,
        comp: Component,
        phase: str,
        reason: str,
    ) -> None:
        """R1.2: a phase that never ran must leave a trace in both
        the findings stream and the journal, so "ran clean" and
        "never ran" are distinguishable downstream."""
        self._add_findings(comp, [Finding.phase_skipped(phase, reason)])
        self.bus.emit(
            event_catalog.PhaseSkipped(
                component=comp.id,
                phase=phase,
                reason=reason,
            )
        )
