"""The spec findings of the architect: parse, routing, audit file, journal row, display."""

from __future__ import annotations

import re
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kstrl.atomicio import atomic_write_json
from kstrl.decisions import DISPOSITION_ESCALATED, DISPOSITION_ORDER, SpecDecision
from kstrl.evolution import EvolutionJournal
from kstrl.evolution_config import EvolutionConfig
from kstrl.journal_rows import SPEC_ISSUES_EVENT

if TYPE_CHECKING:
    from kstrl.ui.base import UI


# What ``appliesTo`` says about a routed spec finding (#260). The
# vocabulary lives with the code that writes it: ``prd`` holds the field
# but validates nothing about its contents, so it has no use for these.
SPEC_ISSUE_APPLIES_COMPONENT = "component"
SPEC_ISSUE_APPLIES_SPEC = "spec"


@dataclass
class SpecIssue:
    """A red-team finding raised by the architect during decomposition.

    ``issue_id`` is the join key the ``decisions`` register closes each
    finding by (#260 round 2). Defaulted to "" so the twenty-odd
    in-repo constructions that predate it still build; the VALIDATOR is
    where a real architect payload is required to carry one, because
    that is where a missing id can still be rejected and retried.
    """

    severity: str  # "blocker" | "major" | "minor"
    kind: str
    summary: str
    location: str = ""
    suggestion: str = ""
    issue_id: str = ""


# Severities, worst first. The order ``_issue_counts`` counts in and
# the convergence report renders in. ``_surface_spec_issues`` below
# still enumerates its own three groups, because it pairs each with a
# different UI emitter and a label that is not the severity name.
_SEVERITY_ORDER = ("blocker", "major", "minor")
_VALID_SEVERITIES = frozenset(_SEVERITY_ORDER)
_VALID_KINDS = frozenset(
    {
        "ambiguity",
        "missing_detail",
        "contradiction",
        "unstated_assumption",
        "undefined_failure_mode",
        "out_of_scope_creep",
        "other",
    }
)


def _parse_spec_issues(data: Any) -> list[SpecIssue]:
    """Extract typed SpecIssue entries from raw decompose output.

    Invalid entries (unknown severity, unknown kind, missing summary)
    are skipped rather than crashing decomposition. We surface what the
    LLM produced honestly even if some entries are malformed.
    """
    if not isinstance(data, dict):
        return []
    raw = data.get("spec_issues")
    if not isinstance(raw, list):
        return []
    issues: list[SpecIssue] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        severity = str(entry.get("severity", "")).strip()
        kind = str(entry.get("kind", "")).strip()
        summary = str(entry.get("summary", "")).strip()
        if severity not in _VALID_SEVERITIES:
            continue
        if kind not in _VALID_KINDS:
            continue
        if not summary:
            continue
        issues.append(
            SpecIssue(
                severity=severity,
                kind=kind,
                summary=summary,
                location=str(entry.get("location", "")).strip(),
                suggestion=str(entry.get("suggestion", "")).strip(),
                issue_id=str(entry.get("id", "")).strip(),
            )
        )
    return issues


def _surface_spec_issues(issues: list[SpecIssue], ui: UI) -> None:
    """Render spec issues to the UI grouped by severity."""
    if not issues:
        ui.ok("Spec audit: no issues raised")
        return
    blockers = [i for i in issues if i.severity == "blocker"]
    majors = [i for i in issues if i.severity == "major"]
    minors = [i for i in issues if i.severity == "minor"]
    ui.section("Spec Audit Findings")
    for label, group, emit in (
        ("Blockers", blockers, ui.err),
        ("Major", majors, ui.warn),
        ("Minor", minors, ui.info),
    ):
        if not group:
            continue
        ui.kv(label, str(len(group)))
        for issue in group:
            emit(f"  [{issue.kind}] {issue.summary}")
            if issue.location:
                ui.info(f"    location: {issue.location}")
            if issue.suggestion:
                ui.info(f"    suggestion: {issue.suggestion}")


def _surface_one_decision(
    decision: SpecDecision,
    ui: UI,
    emit: Callable[[str], None],
) -> None:
    """One register entry as the operator reads it."""
    emit(f"  {decision.question}")
    ui.info(f"    resolution: {decision.resolution}")
    if decision.reason:
        ui.info(f"    because: {decision.reason}")
    if decision.alternative:
        ui.info(f"    rejected: {decision.alternative}")


def _surface_spec_decisions(decisions: list[SpecDecision], ui: UI) -> None:
    """Render the disposition register to the UI (#260).

    Escalations are errors because they stop the run; everything else is
    information. The operator seeing what the architect DECIDED is the
    point of the register: before this, 83 of 117 findings across five
    real runs were local defaults the architect could have settled, and
    it had nowhere to say so.
    """
    if not decisions:
        return
    ui.section("Architect Decisions")
    for disposition in DISPOSITION_ORDER:
        group = [d for d in decisions if d.disposition == disposition]
        if not group:
            continue
        ui.kv(disposition.capitalize(), str(len(group)))
        emit = ui.err if disposition == DISPOSITION_ESCALATED else ui.info
        for decision in group:
            _surface_one_decision(decision, ui, emit)


# Relative location of the persisted red-team artifact (R1.7). Lives
# next to manifest.json so one directory holds the decompose outputs.
SPEC_ISSUES_REL_PATH = Path("scripts") / "kstrl" / "spec-issues.json"


def _issue_dict(issue: SpecIssue) -> dict[str, str]:
    """One issue as the six JSON keys every artifact writes it under."""
    return {
        "id": issue.issue_id,
        "severity": issue.severity,
        "kind": issue.kind,
        "summary": issue.summary,
        "location": issue.location,
        "suggestion": issue.suggestion,
    }


def _issue_dicts(issues: list[SpecIssue]) -> list[dict[str, str]]:
    return [_issue_dict(i) for i in issues]


def _issue_counts(issues: list[SpecIssue]) -> dict[str, int]:
    """Per-severity counts, in ``_SEVERITY_ORDER``.

    Counts this run's issues and a previous run's (rehydrated from the
    journal by ``_stored_issues``) with the same code.
    """
    return {sev: sum(1 for i in issues if i.severity == sev) for sev in _SEVERITY_ORDER}


# --- routing the audit into the component PRDs (#260) -------------------
#
# Everything below exists because the architect's non-blocker findings
# had no reader. spec-issues.json is written on every decompose and
# NOTHING in kstrl opens it: the majors and minors were printed once and
# discarded, 91 of them across the five recorded writers-room runs. The
# engineer that could have acted on them never saw one.
#
# The attachment signal is the issue's own ``location`` field, and it is
# weaker than it sounds. Measured over those 117 real issues: the field
# is populated every time (117/117) but it is PROSE quoting the spec,
# never a repo path. Matching it against a component's ``allowedPaths``
# is worthless - the one real decomposed component on disk declares
# ``src/writers_room/``, ``tests/`` and ``scripts/kstrl/``, which every
# sibling component would declare too.
#
# What does carry signal is the component's NAME. 31 of the 117
# locations literally name the component ("Component 2, the claude
# adapter: ..."), which is a labelled sample the rule can be scored
# against. Measured on those 31, at the shipped word length of 5:
# requiring two distinctive name words scores precision 1.00, recall
# 0.53; requiring one scores precision 0.81, recall 0.81. Two wins
# because the two error kinds do not cost the same: a MISS still
# reaches every engineer through the spec-wide bucket below, while a
# MISATTACHMENT hides the finding from the one component that needed
# it. Precision is the number to protect.
#
# There is deliberately no stopword list. One was written and then
# deleted, because it could not be shown to do anything: emptying it
# left every score below and every distinctive-word set on the real data
# byte-identical, and not one of its 31 entries even appeared in a
# component name. The job it was meant to do is already done twice over,
# by the distinctiveness filter (a word two components share tells them
# apart for neither) and by the two-word threshold (one stray common
# word cannot attach anything on its own). A second unmeasured mechanism
# aimed at the same failure is a knob to maintain, not a guard.

# A name word shorter than this is noise. Measured on the labelled 31,
# holding the two-word threshold: four characters scores precision 0.90
# and recall 0.56, five scores 1.00 and 0.53, because four lets ordinary
# words a title happens to contain ("read", "file", "path") do the
# matching. A component named in four characters simply broadcasts
# instead, which is the safe direction.
_ROUTE_MIN_WORD_LEN = 5

# Distinctive name words an issue must share with a component before it
# attaches there. See the precision/recall numbers above for why 2.
_ROUTE_MIN_SHARED = 2

_ROUTE_WORD_RE = re.compile(r"[a-z0-9]+")


def _words(text: str) -> set[str]:
    """Every word in ``text``, lowercased."""
    return set(_ROUTE_WORD_RE.findall(text.lower()))


def _name_words(text: str) -> set[str]:
    """Words of a component's name long enough to identify it.

    The length rule belongs here and nowhere else. Filtering the issue
    text too would be inert: the only use of either set is their
    intersection, and every member of this one already passes, so a
    short word on the issue side can never match anything.
    """
    return {word for word in _words(text) if len(word) >= _ROUTE_MIN_WORD_LEN}


def _distinctive_name_words(components: Sequence[dict[str, Any]]) -> dict[str, set[str]]:
    """Per component id, the name words no sibling component shares.

    The name is the id (separators read as spaces) plus the title. A
    word two components share cannot tell them apart, so it is dropped
    from both rather than attaching the issue to each: that is the
    difference between "this finding is about the parser" and "this
    finding says the word document".
    """
    per_component: dict[str, set[str]] = {}
    for comp in components:
        comp_id = str(comp.get("id", ""))
        spaced = comp_id.replace("-", " ").replace("_", " ")
        per_component[comp_id] = _name_words(f"{spaced} {comp.get('title', '')}")
    shared: Counter[str] = Counter(word for words in per_component.values() for word in words)
    return {
        comp_id: {word for word in words if shared[word] == 1}
        for comp_id, words in per_component.items()
    }


def route_spec_issues(
    issues: Sequence[SpecIssue],
    components: Sequence[dict[str, Any]],
) -> dict[str, list[dict[str, str]]]:
    """Which findings belong in which component's PRD (#260).

    Returns one list of PRD-shaped entries per component id, in the
    order the engineer should read them: the findings attributed to
    that component first, then the ones that could not be attributed
    anywhere.

    Nothing is dropped. An issue that matches no component is not
    silently binned, it is handed to EVERY component, because a finding
    the rule cannot place is still a finding somebody has to answer.
    An issue may also match several components; it goes to each of
    them, which is the honest reading of a location naming two
    surfaces.

    Callers pass the non-blocker findings. Blockers halt before any PRD
    is written, so routing them would be dead code today, and deciding
    what a blocker means inside a PRD belongs with the change that
    moves the halt rather than with this one.
    """
    distinctive = _distinctive_name_words(components)
    attached: dict[str, list[dict[str, str]]] = {comp_id: [] for comp_id in distinctive}
    spec_entries: list[dict[str, str]] = []
    for issue in issues:
        words = _words(f"{issue.location} {issue.summary}")
        entry = _issue_dict(issue)
        hits = [
            comp_id
            for comp_id, name_words in distinctive.items()
            if len(name_words & words) >= _ROUTE_MIN_SHARED
        ]
        for comp_id in hits:
            attached[comp_id].append({**entry, "appliesTo": SPEC_ISSUE_APPLIES_COMPONENT})
        if not hits:
            spec_entries.append({**entry, "appliesTo": SPEC_ISSUE_APPLIES_SPEC})
    return {comp_id: own + spec_entries for comp_id, own in attached.items()}


def persist_spec_issues(
    issues: list[SpecIssue],
    root_dir: Path,
    project_name: str,
    spec_file: str,
    *,
    halted: bool,
) -> Path:
    """Persist the architect's red-team findings to a durable artifact (R1.7).

    Written on every decompose that produced parseable output, including
    a clean audit: an empty ``issues`` array is the record that the
    audit ran and found nothing, which is a different fact from "no
    record". Returns the artifact path; raises ``OSError`` on write
    failure so the caller can surface it loudly.
    """
    path = root_dir / SPEC_ISSUES_REL_PATH
    payload: dict[str, Any] = {
        "project": project_name,
        "specFile": spec_file,
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "halted": halted,
        "counts": _issue_counts(issues),
        "issues": _issue_dicts(issues),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, payload)
    return path


def _spec_audit_journal(root_dir: Path, ui: UI) -> EvolutionJournal | None:
    """The evolution journal for this root, or None when it is unusable.

    One loader for both directions of the audit trail: the read that
    builds the convergence report and the write that records this run.
    ``EvolutionConfig.load`` already anchors a relative journal path to
    ``root_dir``, so the path it hands back is absolute.

    A config that will not parse degrades to "no journal" - the
    convergence report goes quiet and the journal entry is skipped -
    rather than raising, because a typo in an optional journal knob
    must not cost the operator the findings of a paid architect run.
    Which exceptions that covers is ``load_or_none``'s to know, not
    this call site's.
    """
    config = EvolutionConfig.load_or_none(root_dir, warn=ui.warn)
    if config is None or not config.enabled:
        return None
    return EvolutionJournal(config)


def _record_spec_issues_event(
    issues: list[SpecIssue],
    journal: EvolutionJournal | None,
    project_name: str,
    spec_file: str,
    halted: bool,
    ui: UI,
) -> None:
    """Append a spec_issues event to the evolution journal (R1.7).

    Non-fatal on I/O errors, matching ``EvolutionJournal.record_run``,
    but the failure is surfaced as a warning rather than swallowed:
    the journal is an audit trail, so a silent skip would defeat it.
    No ``run_id`` field: decompose runs before a factory run id exists,
    which is why ``EvolutionJournal.get_spec_audits`` reads these
    entries rather than the run-windowed reader.
    """
    if journal is None:
        return
    entry: dict[str, Any] = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "project": project_name,
        "event_type": SPEC_ISSUES_EVENT,
        "spec_file": spec_file,
        "halted": halted,
        "counts": _issue_counts(issues),
        "issues": _issue_dicts(issues),
        "artifact": SPEC_ISSUES_REL_PATH.as_posix(),
    }
    try:
        journal.append_entries([entry])
    except OSError as exc:
        ui.warn(f"Failed to record spec_issues journal event: {exc}")
