"""Continuous learning - evolution journal, experiment tracking, and pattern routing.

Records factory run outcomes and extracts recurring failure patterns across runs.
Inspired by AutoResearchClaw's evolution directory and autoresearch-agents' results.tsv.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from kstrl.appendio import JOURNAL_REPAIR_EVENT, REPAIR_DETAIL, append_records
from kstrl.evolution_config import EvolutionConfig
from kstrl.experiments_tsv import EXPERIMENTS_HEADER, ExperimentsDialect
from kstrl.failure_signatures import (
    classify_check,
    normalize_error,
    signature_for_error,
    split_signature,
)
from kstrl.journal_reads import JournalReads
from kstrl.journal_rows import (
    FINDINGS_SUPERSEDED_EVENT,
    JOURNAL_SCHEMA_VERSION,
)
from kstrl.version import kstrl_version

if TYPE_CHECKING:
    from kstrl.findings import Finding
    from kstrl.manifest import Component, Manifest


class RunOutcome(Protocol):
    """The fields of a factory result that the journal reads."""

    @property
    def completed(self) -> Sequence[str]: ...

    @property
    def failed(self) -> Sequence[str]: ...

    @property
    def skipped(self) -> Sequence[str]: ...

    @property
    def scheduled(self) -> Sequence[str]: ...

    @property
    def merged(self) -> dict[str, str]: ...


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


def _components_this_run(manifest: Manifest, factory_result: RunOutcome) -> list[Component]:
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


def classify_merges(manifest: Manifest, merged: dict[str, str]) -> dict[str, str]:
    """One verdict per merged component id: ``"clean"``, ``"edited"`` or ``"head unknown"``.

    A merge is clean only when the PR head GitHub merged is the commit
    the diff phase judged (``Component.judged_sha``). A commit pushed to
    the PR, or made in the worktree while the checkpoint prompt was open,
    makes them differ. When either commit is unknown the merge counts as
    edited: a promotion input must not be satisfied by what kstrl could
    not observe (#601).

    Pure (no state mutation) so the journal writer of ``kstrl.evolution`` can
    call it to size the ``merged``/``clean_merged`` TSV columns from the
    same classification the live ladder uses, rather than a second
    definition of "clean" that could drift from this one.
    """
    judged = {comp.id: comp.judged_sha for comp in manifest.components}
    result: dict[str, str] = {}
    for comp_id, head_sha in merged.items():
        judged_sha = judged.get(comp_id, "")
        if not judged_sha or not head_sha:
            result[comp_id] = "head unknown"
        elif judged_sha == head_sha:
            result[comp_id] = "clean"
        else:
            result[comp_id] = "edited"
    return result


def _merge_columns(manifest: Manifest, factory_result: RunOutcome) -> tuple[str, str]:
    """The experiments.tsv ``merged``/``clean_merged`` columns for one run (#601).

    ``merged`` counts only merges this run confirmed (``factory_result.merged``),
    never ``completed`` - a part completed with no PR merged nothing.
    ``clean_merged`` is the subset whose PR head GitHub reported equals the
    commit the diff phase judged. ``classify_merges`` is the one place this
    comparison is made, so the TSV agrees with what the live ladder counted.
    """
    verdicts = classify_merges(manifest, factory_result.merged)
    clean = sum(1 for v in verdicts.values() if v == "clean")
    return str(len(verdicts)), str(clean)


class EvolutionJournal(JournalReads):
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
        factory_result: RunOutcome,
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
                    legacy_check = classify_check(comp.error)
                    legacy_sig = normalize_error(comp.error)
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
                sigs = [signature_for_error(classify_check(comp.error), comp.error)]
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
