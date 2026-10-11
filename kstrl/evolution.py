"""Continuous learning - evolution journal, experiment tracking, and pattern routing.

Records factory run outcomes and extracts recurring failure patterns across runs.
Inspired by AutoResearchClaw's evolution directory and autoresearch-agents' results.tsv.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kstrl.appendio import JOURNAL_REPAIR_EVENT, REPAIR_DETAIL, append_records
from kstrl.evolution_config import EvolutionConfig
from kstrl.experiments_tsv import EXPERIMENTS_HEADER, ExperimentsDialect, experiment_rows
from kstrl.failure_signatures import (
    FailurePattern,
    _classify_check,
    _normalize_error,
    category_for_check,
    signature_for_error,
    split_signature,
)
from kstrl.journal_rows import (
    FINDINGS_SUPERSEDED_EVENT,
    JOURNAL_SCHEMA_VERSION,
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
from kstrl.version import kstrl_version

if TYPE_CHECKING:
    from kstrl.factory_result import FactoryResult
    from kstrl.findings import Finding
    from kstrl.manifest import Component, Manifest

logger = logging.getLogger("kstrl.evolution")

# #312 declared JOURNAL_REPAIR_EVENT here: the event_type of the row an
# append writes when it finds the file not newline-terminated. Its own
# type rather than a synthetic component_result, for the reason
# _role_usage_entries gives: every aggregate in this module selects on
# event_type, so a row of this type counts towards nothing and cannot
# invent an outcome. It exists to be grepped: it is the only durable
# trace that a crash tore the file.
#
# #331 moved the declaration to ``kstrl.appendio``, where the six
# appenders that can write it can all reach it. The import above is
# what binds the name in this module, for _repair_entry which writes
# the row and get_repair_count which counts it.
# #191: what a component_result entry records when no fact-utilization
# measurement reached the journal - the component never got past the
# gates to the distill phase, knowledge was off, or the measurement
# itself failed. Deliberately NOT the same value as a measured zero:
# `measured=False` is "no evidence", `measured=True, referenced=0` is
# evidence that injected facts went unused. Not version-gated, because
# the key is written on every entry from here on; a pre-#191 entry has
# no key at all, which is a third, distinguishable state.
_UNMEASURED_UTILIZATION: dict[str, Any] = {
    "measured": False,
    "injected": 0,
    "referenced": 0,
    "reason": "not measured",
    # Same shape as a measured entry, so a consumer reads `measured`
    # rather than probing for key presence.
    "by_tier": {
        "core": {"injected": 0, "referenced": 0},
        "dependency": {"injected": 0, "referenced": 0},
        "sibling": {"injected": 0, "referenced": 0},
    },
}


def _timestamp_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _journal_line(entry: dict[str, Any]) -> str:
    """One JSONL line, terminator included. The journal's line format.

    Stamps the kstrl that wrote the row (#451), so every row carries it
    whichever of the journal's writers built the dict.
    """
    return json.dumps({**entry, "kstrl_version": kstrl_version()}, separators=(",", ":")) + "\n"


def _log_experiments_repair(repaired: bool, path: Path) -> None:
    """Say that a torn experiments.tsv was padded, since the file cannot.

    The other five appenders record a repair as a row their own reader
    returns. This one pads bare, because every marker a TSV can carry is
    a field and a row of fields is a run, so the log is the only place
    the incident can go. Without it a crash that tore this file and cost
    a run is something nothing in the product ever says: the pad leaves
    a short fragment, ``experiment_rows`` drops it on width, and there
    is no counter on that path either.

    A function rather than an ``if`` inside :meth:`record_run` because
    that method is at the cyclomatic ratchet's grandfathered value and
    one more branch would push it up. It also gives the sentence one
    home.
    """
    if repaired:
        logger.warning(
            "experiments.tsv did not end in a newline, so a crash tore it: %s. "
            "The tail was padded onto a line of its own, so the row above this "
            "run may be a fragment and the run it belonged to was lost.",
            path,
        )


def _repair_entry() -> dict[str, Any]:
    """The row :meth:`EvolutionJournal.append_entries` writes on finding
    an unterminated tail.

    Carries no ``run_id`` on purpose: ``_read_journal_entries`` keeps the
    last N distinct run_ids, so a repair row with one of its own would be
    one of the N and a single tear would shorten the history every
    aggregate reads by a whole run.
    """
    return {
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "timestamp": _timestamp_now(),
        "event_type": JOURNAL_REPAIR_EVENT,
        "detail": REPAIR_DETAIL,
    }


def _summarize_findings(findings: list[Finding]) -> dict[str, Any]:
    """Aggregate counts grouped by phase, severity, category, and OWASP
    bucket for the evolution journal. Lets dashboards query trends
    without re-walking every Finding."""
    summary: dict[str, Any] = {
        "total": len(findings),
        "by_phase": {},
        "by_severity": {},
        "by_category": {},
        "by_owasp": {},
        "infrastructure_errors": 0,
    }
    for f in findings:
        if f.is_infrastructure_error:
            summary["infrastructure_errors"] += 1
        summary["by_phase"][f.phase] = summary["by_phase"].get(f.phase, 0) + 1
        summary["by_severity"][f.severity] = summary["by_severity"].get(f.severity, 0) + 1
        summary["by_category"][f.category] = summary["by_category"].get(f.category, 0) + 1
        if f.owasp:
            summary["by_owasp"][f.owasp] = summary["by_owasp"].get(f.owasp, 0) + 1
    return summary


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------


def _components_this_run(manifest: Manifest, factory_result: FactoryResult) -> list[Component]:
    """The manifest components this run touched, in manifest order (#447).

    ``scheduled`` is every component handed to the executor, which covers
    one that ended MERGE_PENDING or was stopped mid-attempt; ``failed`` and
    ``skipped`` add the failures and cascade skips made without a launch
    (a merge re-poll that finds the PR closed, a contract breaker, a
    cascade skip). ``completed`` is left out on purpose: every completion
    this run launched is already in ``scheduled``, and the only other one
    is a merge the re-poll confirms, whose iterations, duration and
    findings belong to the earlier run that did the work. A component in
    none of the three carried its state from an earlier run and gets no row.

    A component can also be in ``failed`` or ``skipped`` without ever
    being in ``scheduled``: a merge re-poll that finds the parked PR
    closed moves it straight to FAILED (kstrl/pipeline.py
    ``repoll_merge_pending``), and its cascade skips follow the same way.
    That component is admitted here (its FAILED transition is real and
    must be recorded), but it never ran in THIS run, so its row's
    iteration_count, duration_seconds and retries must not be its
    earlier run's numbers under a new run id - :func:`record_run` zeroes
    those three fields for it via :func:`_effective_result_fields`.
    """
    touched = {
        *factory_result.scheduled,
        *factory_result.failed,
        *factory_result.skipped,
    }
    return [comp for comp in manifest.components if comp.id in touched]


def _effective_result_fields(comp: Component, launched: set[str]) -> tuple[int, int, int, float]:
    """(retries, first_attempt, iteration_count, duration_seconds) for ``comp``'s row.

    A component this run moved to FAILED or skipped without launching it
    (a merge re-poll that finds the parked PR closed, and the cascade
    skips that follow) keeps whatever iteration_count, duration_seconds
    and retries an EARLIER run left on the manifest. Reporting those
    under this run's id would double-count that earlier run's work
    (#447, one path over: the same phantom-copy defect the writer fix
    above removes, reachable through ``failed``/``skipped`` instead of a
    carried manifest state). Only a component this run actually launched
    reports its own numbers; a component admitted without a launch
    reports zero for all three. ``status``, ``error`` and
    ``failure_signatures`` are untouched: the FAILED transition and its
    ``pr:closed-without-merge`` signature are real.

    The one exception (#463): a component whose earlier attempts this run
    took over from a run it resumed (``first_attempt <= retries``) keeps its
    retries and first attempt, because those attempts' readings are this
    run's rows now; only its iteration count and duration are zero.
    """
    if comp.id in launched:
        return comp.retries, comp.first_attempt, comp.iteration_count, comp.duration_seconds
    if comp.first_attempt <= comp.retries:
        return comp.retries, comp.first_attempt, 0, 0.0
    return 0, 1, 0, 0.0


def _role_usage_entries(
    usage_by_component: dict[str, dict[str, dict[str, Any]]],
    *,
    journaled: set[str],
    manifest: Manifest,
    run_id: str,
    timestamp: str,
) -> list[dict[str, Any]]:
    """Journal rows for spend that belongs to no manifest component.

    ``record_run`` builds its rows by walking the MANIFEST, so a usage
    key with no component was dropped on the floor while ``run_usage`` -
    which does include it - fed the TSV's ``total_cost_usd``. The
    journal's per-component rows then did not sum to its own run total
    (#257 review). The architect is what made that reachable: a one-role
    pseudo-component that spends before any component exists and never
    appears in a manifest.

    "Never" became structural in #281. This split is a set difference
    against the manifest's ids, so while role keys were bare words a
    component genuinely named `architect` swallowed the role row: the
    difference was empty, the spend was attributed to the component's
    ``usage`` field, and no ``role_usage`` row was written at all.
    ``names.role_component_key`` puts role keys where no component id can
    be spelled, so the two sets are now disjoint by construction rather
    than by what the architect happened to name things.

    Since #463 the difference is against ``journaled``, the components this
    run writes a ``component_result`` row for, not every manifest id: a run
    that resumes a killed one takes over the killed run's spend, including a
    component the killed run finished and this run does not run again, and
    that component's spend has no row of its own to sit on.

    A distinct ``event_type`` rather than a synthetic
    ``component_result``, because every field that row carries - status,
    retries, findings, failed_phase - is meaningless for something that
    is not a component, and three readers in this module aggregate over
    ``component_result`` specifically. They ignore this type, which is
    the point: the row records spend without inventing an outcome.
    """
    return [
        {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "timestamp": timestamp,
            "run_id": run_id,
            "project": manifest.project_name,
            "component_id": role,
            "event_type": "role_usage",
            "usage": usage_by_component[role],
        }
        for role in sorted(set(usage_by_component) - journaled)
    ]


def _merge_columns(manifest: Manifest, factory_result: FactoryResult) -> tuple[str, str]:
    """The experiments.tsv ``merged``/``clean_merged`` columns for one run (#601).

    ``merged`` counts only merges this run confirmed (``factory_result.merged``),
    never ``completed`` - a part completed with no PR merged nothing.
    ``clean_merged`` is the subset whose PR head GitHub reported equals the
    commit the diff phase judged. Deferred import: ``kstrl.factory`` imports
    this module lazily for the same reason (avoids a module-load cycle), and
    ``_classify_merges`` is the one place this comparison is made, so the TSV
    agrees with what the live ladder counted.
    """
    from kstrl.factory import _classify_merges

    verdicts = _classify_merges(manifest, factory_result.merged)
    clean = sum(1 for v in verdicts.values() if v == "clean")
    return str(len(verdicts)), str(clean)


class EvolutionJournal:
    def __init__(self, config: EvolutionConfig) -> None:
        self.config = config

    @classmethod
    def open(
        cls,
        root_dir: Path,
        warn: Callable[[str], None],
    ) -> EvolutionJournal | None:
        """The journal for ``root_dir``, or None when it is unusable.

        Every writer asks the same two questions in the same order -
        does the config parse, and is the journal switched on - and does
        the same thing on either No. Asking them here rather than at each
        site is what stops the pair drifting: measured, the four writers
        that existed before #257 disagreed, with two of them omitting the
        parse guard entirely and raising a config typo into work that had
        already been paid for.

        Which exceptions "does not parse" covers is ``load_or_none``'s to
        know, not a call site's. Degrades loudly through ``warn``.
        """
        config = EvolutionConfig.load_or_none(root_dir, warn=warn)
        if config is None or not config.enabled:
            return None
        return cls(config)

    # ------------------------------------------------------------------
    # record_run
    # ------------------------------------------------------------------

    def record_run(
        self,
        run_id: str,
        manifest: Manifest,
        factory_result: FactoryResult,
        usage_by_component: dict[str, dict[str, dict[str, Any]]] | None = None,
        run_usage: dict[str, Any] | None = None,
        failure_signatures: dict[str, list[str]] | None = None,
        fact_utilization: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """Record a completed factory run to the journal.

        Writes individual component outcomes as JSONL entries.
        Also appends a summary line to experiments.tsv.

        #447: both describe only the components this run touched
        (:func:`_components_this_run`). A component that carried its state
        from an earlier run gets no row: the row would repeat that run's
        result under this run's id, and every reader would count it again.

        R3.1: ``usage_by_component`` maps component id -> phase ->
        UsageTotals.to_dict() and lands on each component's journal
        entry; ``run_usage`` is the run-level UsageTotals.to_dict() and
        feeds the TSV totals columns. Both optional so pre-R3.1 callers
        keep working; token/cost figures are CLI self-reports and are
        lower bounds whenever ``unreported_calls`` > 0.

        R6.1: ``failure_signatures`` maps component id -> the structured
        "<check>:<code>" signatures the factory recorded when the
        component's last attempt failed (e.g. "linter:E501",
        "review:scope_creep"). When absent for a failed component, the
        legacy flattened-string classification is the fallback so
        journal entries never lose the signature fields entirely.

        #191: ``fact_utilization`` maps component id -> ``{"measured",
        "injected", "referenced", "reason"}``. The key is written for
        EVERY component, present in the map or not. ``measured=False``
        means the run could not measure, which is not the same as a
        measured ``referenced=0``; reading a missing or false
        ``measured`` as a real zero is what made the L2+
        fact-utilization gate un-evidenceable. Only ``measured=True``
        entries are evidence.
        """
        from kstrl.manifest import ComponentStatus

        timestamp = _timestamp_now()
        usage_by_component = usage_by_component or {}
        failure_signatures = failure_signatures or {}
        fact_utilization = fact_utilization or {}

        # --- JSONL entries per component ---
        ran = _components_this_run(manifest, factory_result)
        launched = set(factory_result.scheduled)
        effective_by_comp: dict[str, tuple[int, int, float]] = {}
        entries: list[dict[str, Any]] = []
        for comp in ran:
            eff_retries, eff_first, eff_iteration_count, eff_duration = _effective_result_fields(
                comp, launched
            )
            # The TSV counts the retries this run answers for (#463).
            effective_by_comp[comp.id] = (
                eff_retries - eff_first + 1,
                eff_iteration_count,
                eff_duration,
            )
            has_error = bool(comp.error) and comp.status in (
                ComponentStatus.FAILED.value,
                ComponentStatus.PENDING.value,  # retried components reset to pending
            )
            comp_signatures: list[str] = []
            check_name = ""
            error_sig = ""
            if has_error:
                comp_signatures = list(failure_signatures.get(comp.id) or [])
                if not comp_signatures:
                    # Legacy fallback: classify the flattened string.
                    legacy_check = _classify_check(comp.error)
                    legacy_sig = _normalize_error(comp.error)
                    if legacy_sig:
                        comp_signatures = [f"{legacy_check}:{legacy_sig}"]
                if comp_signatures:
                    check_name, error_sig = split_signature(comp_signatures[0])
            # E3-consume: include typed findings in the journal so
            # downstream aggregations (concern hit-rate, OWASP-bucket
            # frequency, infrastructure_error rate) can query the
            # structured stream directly rather than re-parsing the
            # rendered string.
            findings_serialized = [f.to_dict() for f in comp.findings]
            findings_summary = _summarize_findings(comp.findings)
            entry = {
                "schema_version": JOURNAL_SCHEMA_VERSION,
                "timestamp": timestamp,
                "run_id": run_id,
                "project": manifest.project_name,
                "component_id": comp.id,
                "event_type": "component_result",
                "status": comp.status,
                "retries": eff_retries,
                "first_attempt": eff_first,
                "error": comp.error,
                "check_name": check_name,
                "error_signature": error_sig,
                "failure_signatures": comp_signatures,
                "failed_phase": comp.failed_phase,
                "failed_check": comp.failed_check,
                "duration_seconds": eff_duration,
                "iteration_count": eff_iteration_count,
                "findings": findings_serialized,
                "findings_summary": findings_summary,
                "usage": usage_by_component.get(comp.id, {}),
                # #191: always present, so a pre-#191 journal (key
                # missing) is distinguishable from "measured=false, we
                # could not measure" and from "measured=true,
                # referenced=0", which is real evidence of unused facts.
                "knowledge_utilization": (
                    fact_utilization.get(comp.id) or dict(_UNMEASURED_UTILIZATION)
                ),
            }
            entries.append(entry)

        entries.extend(
            _role_usage_entries(
                usage_by_component,
                journaled={comp.id for comp in ran},
                manifest=manifest,
                run_id=run_id,
                timestamp=timestamp,
            )
        )

        try:
            self.append_entries(entries)
        except OSError as exc:
            logger.warning(
                "evolution journal write failed (non-fatal): %s: %s",
                self.config.journal_path,
                exc,
            )

        # --- Experiments TSV summary line ---
        total = len(ran)
        completed = len(factory_result.completed)
        failed = len(factory_result.failed)
        skipped = len(factory_result.skipped)

        # #447: a component admitted here without a launch (a merge
        # re-poll that finds the PR closed, and its cascade skips) has
        # its iteration_count, duration_seconds and retries zeroed in
        # ``effective_by_comp``, so its earlier run's numbers are not
        # averaged into THIS run's row under this run's id.
        iteration_counts = [
            effective_by_comp[c.id][1] for c in ran if effective_by_comp[c.id][1] > 0
        ]
        avg_iterations = sum(iteration_counts) / len(iteration_counts) if iteration_counts else 0.0

        durations = [effective_by_comp[c.id][2] for c in ran if effective_by_comp[c.id][2] > 0]
        avg_duration = sum(durations) / len(durations) if durations else 0.0

        retry_total = sum(effective_by_comp[c.id][0] for c in ran)
        retry_rate = retry_total / total if total > 0 else 0.0

        # Most common failure signature (full "<check>:<code>" form).
        failure_sigs: dict[str, int] = {}
        for comp in ran:
            if comp.status != ComponentStatus.FAILED.value or not comp.error:
                continue
            sigs = list(failure_signatures.get(comp.id) or [])
            if not sigs:
                sigs = [signature_for_error(_classify_check(comp.error), comp.error)]
            for sig in sigs:
                failure_sigs[sig] = failure_sigs.get(sig, 0) + 1
        common_failure = max(failure_sigs, key=failure_sigs.get, default="") if failure_sigs else ""  # type: ignore[arg-type]

        # R3.1 totals columns. Empty string (not 0) when no usage was
        # tracked for the run - zero would misread as "measured, free".
        # unreported_calls > 0 marks the token/cost figures as lower
        # bounds. Files written before R3.1 keep their shorter header;
        # experiment_rows keeps such a row while the file's header is a
        # PREFIX of the current one and reads it with the current header's
        # names, which is its second legal width. The reader has not been a bare
        # csv.DictReader since #331 and this comment named one until
        # #352 round 2.
        if run_usage:
            total_tokens_col = str(run_usage.get("total_tokens", ""))
            total_cost_col = str(run_usage.get("cost_usd", ""))
            unreported_col = str(run_usage.get("unreported_calls", ""))
        else:
            total_tokens_col = total_cost_col = unreported_col = ""

        merged_col, clean_merged_col = _merge_columns(manifest, factory_result)

        # Joined on ExperimentsDialect's delimiter rather than on a
        # literal tab, so the one constant the reader parses with is the
        # one this row is built with. Identical bytes: the delimiter IS
        # "\t". Not through a csv.writer, because under QUOTE_NONE with
        # no escapechar a writer RAISES on a field holding a tab, and
        # record_run's only handler is `except OSError`; that would turn
        # residual 1 on experiment_rows from a dropped row into a lost
        # run, which is the worse of the two.
        row = ExperimentsDialect.delimiter.join(
            (
                run_id,
                timestamp,
                manifest.project_name,
                str(total),
                str(completed),
                str(failed),
                str(skipped),
                f"{avg_iterations:.2f}",
                f"{avg_duration:.1f}",
                f"{retry_rate:.2f}",
                common_failure,
                total_tokens_col,
                total_cost_col,
                unreported_col,
                kstrl_version(),
                merged_col,
                clean_merged_col,
            )
        )

        try:
            self.config.experiments_path.parent.mkdir(parents=True, exist_ok=True)
            needs_header = (
                not self.config.experiments_path.exists()
                or self.config.experiments_path.stat().st_size == 0
            )
            # #331: through appendio, which repairs an unterminated
            # tail before appending onto it. This file is the WORST of
            # the seven, because its reader RENDERS the corruption
            # instead of dropping it: measured, a torn row plus this
            # append put a run into `ks evolve --status` and the TUI
            # trends tab whose "completed" column held a timestamp,
            # while the run being recorded here vanished.
            #
            # A BARE PAD, no marker row. Every marker a TSV can carry is
            # a field, and a row of fields is a RUN to this file's
            # reader; there is no shape here that reads as "not a run"
            # the way JOURNAL_REPAIR_EVENT does in a JSONL file.
            #
            # So the incident is recorded in the log rather than in the
            # file. The pad means there is no concatenated row left for
            # get_experiment_trends to drop, only a short fragment it
            # drops silently, and a crash that tore this file and cost a
            # run would otherwise be something nothing in the product
            # ever says. That is the same argument #312 made for the
            # journal_repair row, on the one file that cannot carry one.
            #
            # needs_header is unchanged and still asked before the
            # append: a file that is missing or empty cannot have a torn
            # tail, so the two never both fire.
            #
            # The other side of the two-sided contract: utf-8 is pinned
            # in the helper, and this is the file get_experiment_trends
            # decodes as utf-8.
            payload = (EXPERIMENTS_HEADER + "\n" if needs_header else "") + row + "\n"
            _log_experiments_repair(
                append_records(self.config.experiments_path, payload, repair=""),
                self.config.experiments_path,
            )
        except OSError as exc:
            logger.warning(
                "experiments.tsv write failed (non-fatal): %s: %s",
                self.config.experiments_path,
                exc,
            )

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
                legacy_check = _classify_check(comp.error)
                legacy_sig = _normalize_error(comp.error)
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
        experiments.tsv raised straight out of ``EvolveScreen.on_mount``
        two lines after the #289 config banner, which is that issue's
        own crash from that issue's own screen.

        The PARSE is guarded separately, and differently (#352 round 2).
        A read failure here is silent because a missing file is the
        ordinary state of a project that has recorded no runs, and
        because ``experiment_rows`` already documents ``[]`` as meaning
        three things. A parse refusal is not one of those three: it can
        only come from a file that exists and decoded, so it is an
        incident and it is logged with the path and the cause. The
        return stays ``[]`` rather than propagating, because this is
        called from ``EvolveScreen.reload`` on the Textual event loop,
        where a raise is the #289 crash again.
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
        ``cli._echo_journal_repairs`` and ``EvolveScreen._show_repairs``
        each held their own copy and each said the wording was
        deliberately the same on both, with nothing keeping it so. That
        is the same argument this PR made for hoisting
        ``REPAIR_DETAIL``, where three copies had already drifted.

        ``None`` at zero rather than a sentence saying zero, which is
        what both callers already did separately and for the same
        reason: a healthy journal that prints "0 repairs" every time
        teaches an operator to skip the line that matters. Deciding it
        here also keeps the file read ONCE. What stays per-surface is
        the prefix, two spaces for the CLI and an alert glyph for the
        TUI, and the widget or stream it goes to.
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
        only by an operator who already suspects the problem, and the
        logger warning goes to orchestrator.log under the TUI. ``ks
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
    # append_entries
    # ------------------------------------------------------------------

    def carry_superseded(self, from_run_id: str, to_run_id: str, owed: Mapping[str, range]) -> int:
        """Write ``from_run_id``'s superseded attempts again under
        ``to_run_id`` (#463), for the attempts ``owed`` names per component;
        returns how many.

        ``carried_from_run`` names the run the attempt actually ran in, and a
        row carried twice keeps the first run's name.
        """
        rows = [
            {
                **entry,
                "run_id": to_run_id,
                "carried_from_run": entry.get("carried_from_run") or from_run_id,
            }
            for entry in self._read_all_entries()
            if entry.get("event_type") == FINDINGS_SUPERSEDED_EVENT
            and entry.get("run_id") == from_run_id
            and entry.get("attempt") in owed.get(str(entry.get("component_id", "")), range(0))
        ]
        if rows:
            self.append_entries(rows)
        return len(rows)

    def append_entries(self, entries: list[dict[str, Any]]) -> None:
        """Append entries to the journal in JSONL form.

        The one writer of the journal's line format, enforced by
        ``tests/test_journal_one_writer.py``. Raises ``OSError`` rather
        than handling it, because the three callers surface a failed
        write differently: :meth:`record_run` logs it, decompose warns
        through the run's UI, and ``autonomy.commit_transition`` warns.

        #312: a crash mid-write leaves a tail with no newline, and an
        append onto that tail concatenates the two into one unparseable
        line, so the tolerant reader drops the NEW entry as well. The
        mechanism and the repair now live in ``kstrl.appendio``, which
        #331 hoisted them into when five more appenders were measured
        with the same defect; the ``"a+b"`` open, the probe, the single
        write and the encoding are all described there.

        What stays here is the POLICY, which is what differs per file.
        This journal repairs with a ``JOURNAL_REPAIR_EVENT`` row rather
        than a bare pad, per "if it's worth deciding, it's worth
        recording": the row is what ``ks evolve --status`` counts and an
        operator greps months later, and it is durable where the logger
        warning below is not, because the process that tore the file is
        exactly the process whose stderr nobody kept.

        An empty append writes nothing, so it repairs nothing: there is
        no entry to protect and the next real append will do it.

        Both are enforced by ``tests/test_journal_write_boundary.py``,
        which counts descriptors and writes. Round 2 of review on #327
        found that neither was, and the measurement here agrees: a
        version that reopens the file for the append, and a version
        that writes the newline, the marker and the batch separately,
        each pass 284 tests and 1 xfail across
        ``test_journal_torn_tail``, ``test_journal_one_writer``,
        ``test_decompose``, ``test_autonomy_ladder`` and
        ``test_config_control_plane``. An argument in a docstring is
        not a mechanism.

        WHAT IS STILL NOT ATOMIC, precisely, because a docstring that
        implied otherwise would be worse than no docstring.

        #330 listed three residuals of the unlocked probe-and-append.
        This call now passes ``lock=True``, so ``appendio.appending``
        holds ``fcntl.LOCK_EX`` on the journal's OWN descriptor across
        the probe and the write, and all three are gone WHERE ``fcntl``
        imports, which is POSIX. On a platform without it the helper
        yields no exclusion and all three are exactly as listed. The
        same degradation ``control_lock``, ``queue_lock`` and the
        run-level factory lock already take.

        The two-process measurement that decided this is on
        :func:`appendio.appending`, where the lock lives, rather than
        copied here: two copies of one measurement drift, and this
        docstring is about which residuals close, not about the numbers
        that showed they do.

        1. A STALE PROBE. Between this process's tail read and its
           write, another process could append; if that other write
           crashed mid-line inside the window, this append landed on the
           fragment and the pair was unreadable, which is the #312
           outcome in a narrower window. The lock closes the window: no
           other locked writer is between the probe and the write.
        2. A DOUBLED REPAIR ROW. Two processes repairing one tear each
           wrote a newline and a repair row, so one incident was
           recorded twice. Both the blank line and the extra row are
           skipped by every reader and counted by none, so this cost
           :meth:`get_repair_count` accuracy and nothing else. The lock
           makes the count exact: the second writer probes AFTER the
           first has written, and finds a terminated file.
        3. INTERLEAVED LINES. O_APPEND makes each ``write`` land at the
           end, and the repair row plus the whole batch go in ONE
           ``write`` so that another appender cannot land between them.
           That was not a guarantee, but not for the reason it is
           tempting to write down. Measured on this interpreter:
           ``BufferedWriter`` hands a payload of ANY size to the raw
           layer in one ``write(2)`` (100 bytes to 5 MB, one raw call
           each), so the split is not size-driven and
           ``io.DEFAULT_BUFFER_SIZE`` is not the threshold. It loops
           only when the OS returns a SHORT write, which on a regular
           file means a signal or ENOSPC. Under the lock no other kstrl
           writer can land in that loop.

        WHAT THE LOCK DOES NOT COVER, in any case: a SHORT write inside
        the one write (residual 4 below), an appender that does not take
        the lock (a foreign process, or this one on a platform without
        ``fcntl``), and the readers, which are tolerant and unlocked and
        unchanged by this.

        Deadlock ordering: this lock is a LEAF. ``append_entries`` calls
        nothing that takes another kstrl lock. ``commit_transition``
        calls ``state.save()``, which takes and RELEASES ``control_lock``
        BEFORE the append rather than around it; ``record_run`` and
        ``_record_contract_event`` run under the run-level factory lock,
        which is a different file that nothing acquires while holding
        this one. ``flock`` is per open file description and
        :func:`appendio.appending` opens its own, so two threads in one
        process serialize against each other too.

        4. The repair is not two-phase-safe either. There is no gap
           BETWEEN two calls, because there is only one call: the
           newline that isolates the fragment, the marker and the batch
           are one ``write``. What is left is a partial write INSIDE it,
           which is residual 3's short write, landing the newline and
           not the marker. That leaves a file that is terminated,
           malformed one line up and carrying no repair row, and the
           next append reads the last BYTE, finds a newline and adds
           none. Nothing is at risk by then - the fragment is isolated,
           which was the point - so this is an audit gap, not data loss,
           and the isolated fragment line is still on disk to be read.
           Closing it means parsing the last LINE on every append, which
           would fire on any malformed tail rather than a torn one: a
           different contract, not a bug fix.
           ``test_a_terminated_but_malformed_tail_is_not_a_tear`` pins
           it, so it cannot quietly stop being true.

        Neither 1 nor 3 was made worse by the probe: at cbdff7c the
        same two writers produced the same interleaving with no probe at
        all.
        """
        path = self.config.journal_path
        path.parent.mkdir(parents=True, exist_ok=True)
        repairing = append_records(
            path,
            "".join(_journal_line(entry) for entry in entries),
            repair=_journal_line(_repair_entry()),
            lock=True,
        )
        if repairing:
            logger.warning(
                "evolution journal did not end in a newline, so a crash tore it: "
                "%s. A newline and a %s row were written before this append, so "
                "the unterminated tail cannot swallow the entries after it.",
                path,
                JOURNAL_REPAIR_EVENT,
            )

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
