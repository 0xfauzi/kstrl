"""The read side of the evolution journal (the queries that EvolutionJournal inherits)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from kstrl.appendio import JOURNAL_REPAIR_EVENT
from kstrl.evolution_config import EvolutionConfig
from kstrl.experiments_tsv import experiment_rows
from kstrl.failure_signatures import (
    FailurePattern,
    category_for_check,
    classify_check,
    normalize_error,
    split_signature,
)
from kstrl.journal_rows import (
    SPEC_ISSUES_EVENT,
    IterationReading,
    _refused,
    _signature_rows,
    entry_str,
    iteration_criterion_verdict,
    read_attempt_iterations,
    without_carried_results,
)
from kstrl.observability import read_progress_events

if TYPE_CHECKING:
    from kstrl.manifest import Component, Manifest

logger = logging.getLogger("kstrl.evolution")


class JournalReads:
    config: EvolutionConfig

    # ------------------------------------------------------------------
    # extract_failure_patterns (single run)
    # ------------------------------------------------------------------

    def extract_failure_patterns(
        self,
        manifest: Manifest,
        min_frequency: int = 2,
        signatures_by_component: dict[str, list[str]] | None = None,
    ) -> list[FailurePattern]:
        """Extract recurring failure patterns from a single run.

        Looks at failed/retried components to find common failure
        signatures, grouped by the full "<check>:<code>" signature.
        ``signatures_by_component`` carries the factory's structured
        signatures (R6.1); components absent from it fall back to
        classifying their flattened error string.
        """
        from kstrl.manifest import ComponentStatus

        signatures_by_component = signatures_by_component or {}

        # Collect components that failed or were retried.
        troubled: list[Component] = [
            c
            for c in manifest.components
            if c.status == ComponentStatus.FAILED.value or c.retries > 0
        ]

        if not troubled:
            return []

        # Group by full signature string.
        groups: dict[str, list[str]] = {}
        for comp in troubled:
            if not comp.error:
                continue
            sigs = list(signatures_by_component.get(comp.id) or [])
            if not sigs:
                legacy_check = classify_check(comp.error)
                legacy_sig = normalize_error(comp.error)
                if not legacy_sig:
                    continue
                sigs = [f"{legacy_check}:{legacy_sig}"]
            for sig in sigs:
                groups.setdefault(sig, []).append(comp.id)

        total = len(manifest.components)
        patterns: list[FailurePattern] = []
        for full_sig, comp_ids in groups.items():
            if len(comp_ids) < min_frequency:
                continue
            check_name, code = split_signature(full_sig)
            patterns.append(
                FailurePattern(
                    description=(
                        f"{check_name} failure '{code}' in {len(comp_ids)}/{total} components"
                    ),
                    frequency=len(comp_ids),
                    total_components=total,
                    affected_components=comp_ids,
                    check_name=check_name,
                    error_signature=code,
                    category=category_for_check(check_name),
                )
            )

        patterns.sort(key=lambda p: p.frequency, reverse=True)
        return patterns

    # ------------------------------------------------------------------
    # get_cross_run_patterns
    # ------------------------------------------------------------------

    def get_cross_run_patterns(
        self,
        lookback_runs: int = 10,
    ) -> list[FailurePattern]:
        """Get patterns that recur across multiple factory runs.

        Reads the journal, groups entries by their structured failure
        signatures ("<check>:<code>", R6.1), and returns patterns that
        appear in >= min_pattern_frequency distinct runs. Legacy v1
        entries without ``failure_signatures`` fall back to composing
        the signature from their check_name/error_signature fields.

        Since #496 the rows counted include every attempt a retry
        superseded (:func:`_signature_rows`). ``frequency`` is a count of
        distinct runs, so a signature on several attempts of one run
        counts once, and ``superseded_only`` marks a signature that was
        never on a component's final attempt.
        """
        entries = self._read_journal_entries(lookback_runs)
        if not entries:
            return []

        # Group by full signature across distinct run_ids.
        runs: set[str] = set()
        sig_runs: dict[str, set[str]] = {}
        sig_components: dict[str, list[str]] = {}
        on_a_final_attempt: set[str] = set()
        for run_id, comp_id, sigs, superseded in _signature_rows(entries):
            runs.add(run_id)
            for sig in sigs:
                sig_runs.setdefault(sig, set()).add(run_id)
                sig_components.setdefault(sig, []).append(comp_id)
                if not superseded:
                    on_a_final_attempt.add(sig)

        total_runs = len(runs)
        patterns: list[FailurePattern] = []

        for sig, run_ids in sig_runs.items():
            if len(run_ids) < self.config.min_pattern_frequency:
                continue
            check_name, code = split_signature(sig)
            unique_comps = list(dict.fromkeys(sig_components.get(sig, [])))
            patterns.append(
                FailurePattern(
                    description=(
                        f"'{sig}' appeared in {len(run_ids)}/{total_runs} runs "
                        f"across {len(unique_comps)} components"
                    ),
                    frequency=len(run_ids),
                    total_components=total_runs,
                    affected_components=unique_comps,
                    check_name=check_name,
                    error_signature=code,
                    category=category_for_check(check_name),
                    superseded_only=sig not in on_a_final_attempt,
                )
            )

        patterns.sort(key=lambda p: p.frequency, reverse=True)
        return patterns

    # ------------------------------------------------------------------
    # get_concern_hit_rate (D8)
    # ------------------------------------------------------------------

    def get_concern_hit_rate(self, lookback_runs: int = 10) -> dict[str, Any]:
        """Aggregate reviewer/security finding signal across recent runs.

        Returns ``{"runs": N, "components": M, "with_concern": K,
        "by_category": {...}}`` so dashboards can ask "did the
        adversarial reviewers surface anything across the last N runs?"

        R6.2: consumes the typed ``findings_summary`` that record_run
        writes on every component_result entry (E3 stream), replacing
        the old error-string scan that was structurally zero (concern
        categories never appeared in ``component.error``). A component
        counts as "with concern" when its summary has at least one
        finding in a real category - the synthetic
        ``infrastructure_error`` and ``phase_skipped`` categories mark
        non-execution, not adversarial signal, and are excluded.
        """
        entries = [
            e
            for e in self._read_journal_entries(lookback_runs)
            if e.get("event_type", "component_result") == "component_result"
        ]
        runs = len({e.get("run_id", "") for e in entries})
        components = len(entries)
        with_concern = 0
        by_category: dict[str, int] = {}
        for entry in entries:
            summary = entry.get("findings_summary") or {}
            cat_counts = summary.get("by_category") or {}
            if not isinstance(cat_counts, dict):
                continue
            hit = False
            for category, count in cat_counts.items():
                if category in ("infrastructure_error", "phase_skipped"):
                    continue
                try:
                    n = int(count)
                except (TypeError, ValueError):
                    continue
                if n <= 0:
                    continue
                hit = True
                by_category[category] = by_category.get(category, 0) + n
            if hit:
                with_concern += 1
        return {
            "runs": runs,
            "components": components,
            "with_concern": with_concern,
            "by_category": by_category,
        }

    def get_fact_utilization(self, lookback_runs: int = 10) -> dict[str, Any]:
        """Aggregate knowledge fact-utilization across recent runs (#191).

        Returns ``{"runs", "components", "measured", "unmeasured",
        "injected", "referenced", "runs_with_referenced"}``.

        Only ``measured=True`` entries contribute to ``injected`` and
        ``referenced``. An unmeasured component is counted under
        ``unmeasured`` and NEVER as a zero - that conflation is the
        defect this field exists to prevent. Entries written before
        #191 have no ``knowledge_utilization`` key and count as
        unmeasured.

        This is the query behind the L2+ cATO gate in
        ``docs/remediation-roadmap.md``: "two real factory runs with
        nonzero fact-utilization" is ``runs_with_referenced >= 2``.
        ``injected``/``referenced`` are lower bounds - see
        ``knowledge.measure_fact_utilization``.
        """
        entries = [
            e
            for e in self._read_journal_entries(lookback_runs)
            if e.get("event_type", "component_result") == "component_result"
        ]
        measured = unmeasured = 0
        injected = referenced = 0
        runs_with_referenced: set[str] = set()
        for entry in entries:
            util = entry.get("knowledge_utilization")
            if not isinstance(util, dict) or not util.get("measured"):
                unmeasured += 1
                continue
            try:
                n_injected = int(util.get("injected", 0))
                n_referenced = int(util.get("referenced", 0))
            except (TypeError, ValueError):
                # Present but unreadable is not evidence either.
                unmeasured += 1
                continue
            measured += 1
            injected += n_injected
            referenced += n_referenced
            if n_referenced > 0:
                runs_with_referenced.add(str(entry.get("run_id", "")))
        return {
            "runs": len({e.get("run_id", "") for e in entries}),
            "components": len(entries),
            "measured": measured,
            "unmeasured": unmeasured,
            "injected": injected,
            "referenced": referenced,
            "runs_with_referenced": len(runs_with_referenced),
        }

    # ------------------------------------------------------------------
    # get_experiment_trends
    # ------------------------------------------------------------------

    def get_experiment_trends(self, last_n: int = 10) -> list[dict[str, Any]]:
        """Read experiments.tsv and return the last N entries as dicts.

        Encoding is named and ``ValueError`` is caught beside
        ``OSError``, which is the house rule for a reader of any file
        kstrl writes (CLAUDE.md): ``UnicodeDecodeError`` IS a
        ``ValueError`` and escapes a fail-closed ``except OSError``.
        Measured before this line: a single non-utf-8 byte in
        experiments.tsv raised straight out of the reader.

        The PARSE is guarded separately, and differently (#352 round 2).
        A read failure here is silent because a missing file is the
        ordinary state of a project that has recorded no runs, and
        because ``experiment_rows`` already documents ``[]`` as meaning
        three things. A parse refusal is not one of those three: it can
        only come from a file that exists and decoded, so it is an
        incident and it is logged with the path and the cause. The
        return stays ``[]`` rather than propagating, because a raise here
        would take down the caller that only wanted the rows.
        """
        try:
            text = self.config.experiments_path.read_text(encoding="utf-8")
        except (OSError, ValueError):
            return []

        try:
            return experiment_rows(text)[-last_n:]
        except ValueError as exc:
            logger.warning(
                "experiments.tsv could not be parsed, so no runs are shown: %s: %s",
                self.config.experiments_path,
                exc,
            )
            return []

    # ------------------------------------------------------------------
    # #233: per-attempt iteration readings
    # ------------------------------------------------------------------

    def read_iteration_readings(self, rows: list[dict[str, Any]]) -> list[IterationReading]:
        """One reading per experiments.tsv row, in the rows' order."""
        entries = self._read_all_entries()
        readings: list[IterationReading] = []
        for row in rows:
            run_id = str(row.get("run_id", ""))
            raw = str(row.get("components_total", "")).strip()
            try:
                total = int(raw)
            except ValueError:
                readings.append(_refused(run_id, f"components_total is not an integer: {raw!r}"))
                continue
            readings.append(read_attempt_iterations(entries, run_id, total))
        return readings

    def iteration_criterion_lines(self, rows: list[dict[str, Any]]) -> list[str]:
        """The operator-facing #233 block, rendered where the path lives.

        ONE HOME, for the reason :meth:`repair_summary` gives: #352 round 2
        moved the repair sentence here so the journal path stopped escaping
        into the click module and the TUI, and ``EXPECTED_JOURNAL_PATH_SITES``
        in ``tests/test_journal_one_writer.py`` pins that. A display line in
        ``cli.py`` that reads ``config.journal_path`` would put the row back.
        """
        readings = self.read_iteration_readings(rows)
        verdict = iteration_criterion_verdict(rows, readings)
        lines = [
            f"  journal: {self.config.journal_path}",
            f"  window: last {verdict.window} run(s)",
        ]
        for reading in readings:
            if reading.measured:
                lines.append(
                    f"  {reading.run_id} | "
                    f"iterations_all_attempts={reading.avg_all_attempts:.2f} "
                    f"({reading.reason})"
                )
            else:
                lines.append(
                    f"  {reading.run_id} | iterations_all_attempts=REFUSED: {reading.reason}"
                )
        lines.append(
            "  clause 1 (journal per-attempt iterations > 1.00 in a majority "
            f"of the window): {verdict.clause1} - {verdict.clause1_detail}"
        )
        lines.append(
            "  clause 2 (at least two distinct project values): "
            f"{verdict.clause2} - {verdict.clause2_detail}"
        )
        lines.append(
            "  note: the avg_iterations column above is the LAST attempt's "
            "count per component, a lower bound, and it is the ruler #233's "
            "own text names. The clause 1 verdict here is computed from the "
            "journal's per-attempt readings instead, which is a larger number, "
            "and it never reads that column."
        )
        lines.append(
            "  note: this reads one project root, and it cannot tell a smoke "
            "run from a real project. The criterion excludes smoke runs and "
            "spans projects, so run it per root and pool the rows."
        )
        return lines

    # ------------------------------------------------------------------
    # get_repair_count
    # ------------------------------------------------------------------

    def repair_summary(self) -> str | None:
        """The operator-facing sentence about this journal's repairs, or None.

        ONE HOME for prose two surfaces render (#352 round 2, N4).
        ``cli._echo_journal_repairs`` and a second surface
        each held their own copy and each said the wording was
        deliberately the same on both, with nothing keeping it so. That
        is the same argument this PR made for hoisting
        ``REPAIR_DETAIL``, where three copies had already drifted.

        ``None`` at zero rather than a sentence saying zero, which is
        what both callers already did separately and for the same
        reason: a healthy journal that prints "0 repairs" every time
        teaches an operator to skip the line that matters. Deciding it
        here also keeps the file read ONCE. What stays per-surface is
        the prefix and the stream it goes to.
        """
        repairs = self.get_repair_count()
        if not repairs:
            return None
        return (
            f"journal: {repairs} interrupted write(s) repaired. "
            f"A crash left {self.config.journal_path} without a trailing newline. "
            "The line above each journal_repair row is what that write left behind: "
            "either a torn fragment, which is lost, or a whole record that lost only "
            "its newline, which is readable again. Read it to tell which."
        )

    def get_repair_count(self) -> int:
        """How many interrupted writes this journal has been repaired from.

        The read surface for ``JOURNAL_REPAIR_EVENT`` (#327 round 1,
        F5). Writing the row was only half of "if it's worth deciding,
        it's worth recording": a row no command reports is reachable
        only by an operator who already suspects the problem. ``ks
        evolve --status`` prints this when it is non-zero.

        Counts rows, not incidents. Residual 4 of
        :meth:`append_entries` is how a repair can happen and not be
        counted, so this is a lower bound. It is no longer an UPPER
        bound too: #330's residual 2, two processes each writing a row
        for one tear, is closed under ``fcntl`` by the lock the append
        takes, so one row is one tear on POSIX and two rows for one tear
        remain possible only on a platform with no ``fcntl``. A
        non-zero count means at least one crash left an unterminated
        tail. It does NOT mean a record was lost: the line above each
        row is either a fragment that was never readable or a whole
        record that lost only its newline and is readable again, which
        is the distinction ``docs/evolution-metrics.md`` and the status
        line both draw.
        """
        return sum(
            1 for e in self._read_all_entries() if e.get("event_type") == JOURNAL_REPAIR_EVENT
        )

    # ------------------------------------------------------------------
    # get_spec_audits / get_spec_issue_runs
    # ------------------------------------------------------------------

    def get_spec_audits(self) -> list[dict[str, Any]]:
        """Every recorded spec audit in the journal, oldest first (#314).

        The whole set, across every project, because the caller that
        accounts for the history a windowed trend leaves out needs
        exactly what the window drops: filtering by project here would
        hide the thing it is asking for.

        This is the read surface a caller uses INSTEAD of opening
        ``config.journal_path`` for itself. The difference is not
        cosmetic: if the journal ever compacts, rotates or gains a
        second segment, this method is what changes, while a caller
        holding the path would quietly return less than the journal
        holds - and silent loss of the excluded-history accounting is
        the defect #280 exists to fix.

        Deliberately NOT routed through :meth:`_read_journal_entries`:
        that reader keeps only entries whose ``run_id`` is among the
        last N distinct run ids, and a spec audit carries no ``run_id``
        at all (decompose runs before a factory run id exists), so
        every one of them is dropped there. Reading the raw entries is
        what makes the architect's own history readable.
        """
        return [e for e in self._read_all_entries() if e.get("event_type") == SPEC_ISSUES_EVENT]

    def get_spec_issue_runs(
        self,
        project: str,
        last_n: int = 10,
        audits: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """The last N recorded spec audits for ``project``, oldest first (#260).

        The one place the window rule lives (#314). ``decompose`` had a
        second copy of it, and two copies of one rule can drift: if
        they had, the convergence trend and the accounting printed
        under it would have disagreed about the same journal.

        ``last_n`` counts spec audits, not factory runs - a spec audit
        happens once per decompose, whether or not a factory run
        follows. Windowed here rather than by the caller, matching
        :meth:`get_experiment_trends`.

        ``audits`` lets a caller that has already called
        :meth:`get_spec_audits` window that snapshot instead of reading
        the file a second time, so the window and any accounting over
        the same entries cannot disagree because the file moved between
        two reads. The event-type filter is applied either way, so
        passing raw entries answers the same as passing audits.

        Nothing is assumed about an entry beyond it being a JSON
        object, so journals written by older versions read cleanly. A
        project is matched by :func:`entry_str`, so a null or
        non-string ``project`` field is an unattributed audit rather
        than a project named "None".
        """
        source = self.get_spec_audits() if audits is None else audits
        runs = [
            entry
            for entry in source
            if entry.get("event_type") == SPEC_ISSUES_EVENT
            and entry_str(entry, "project") == project
        ]
        return runs[-last_n:] if last_n > 0 else []

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read_all_entries(self) -> list[dict[str, Any]]:
        """Every well-formed JSON object in the journal, in file order.

        Delegates to ``observability.read_progress_events``: the
        journal and the progress log are the same JSONL-of-objects
        convention, and the tolerant-read policy (missing file, blank
        line, torn line, non-object line - all skipped) should be one
        policy rather than two. One unreadable line must not cost the
        reader the rest of the history.
        """
        return read_progress_events(self.config.journal_path)

    def _read_journal_entries(self, lookback_runs: int = 10) -> list[dict[str, Any]]:
        """Read JSONL journal and return entries from the last N distinct runs.

        Entries without a ``run_id`` are dropped, because the window is
        defined in terms of runs. Spec audits are exactly that case;
        :meth:`get_spec_audits` reads those instead.
        """
        entries = without_carried_results(self._read_all_entries())
        if not entries:
            return []

        # Determine the last N distinct run_ids (preserving order of appearance).
        seen_runs: list[str] = []
        seen_set: set[str] = set()
        for entry in reversed(entries):
            rid = entry.get("run_id", "")
            if rid and rid not in seen_set:
                seen_set.add(rid)
                seen_runs.append(rid)
            if len(seen_runs) >= lookback_runs:
                break

        allowed = set(seen_runs)
        return [e for e in entries if e.get("run_id", "") in allowed]
