"""The trend of the spec audits across runs, read from the journal and shown to the operator."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from kstrl.evolution import EvolutionJournal
from kstrl.journal_rows import entry_str
from kstrl.spec_issues import _SEVERITY_ORDER, _VALID_SEVERITIES, SpecIssue, _issue_counts

if TYPE_CHECKING:
    from kstrl.ui.base import UI


# ---------------------------------------------------------------------------
# Convergence report (#260)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpecConvergence:
    """This spec audit measured against earlier audits of the same project.

    ``repeated`` counts the PREVIOUS run's issues whose text reappears
    in this one, which is the side the report renders it as. Counting
    the current side instead lets two current issues match one previous
    issue and makes "did not come back" negative, because
    ``_issue_identity`` drops severity and normalizes text while
    ``_parse_spec_issues`` de-duplicates nothing. Counting the previous
    list bounds the number by ``previous_total`` by construction.

    It is a floor on what carried over and never a claim about which
    issues the architect considers resolved.
    """

    current_counts: dict[str, int]
    previous_counts: dict[str, int]
    current_spec_file: str
    previous_spec_file: str
    repeated: int
    blocker_trend: tuple[int, ...]

    @property
    def previous_total(self) -> int:
        return sum(self.previous_counts.values())


def _issue_identity(issue: SpecIssue) -> tuple[str, str]:
    """Match key for one issue: kind plus normalized summary text.

    Whitespace is collapsed and case folded, which is all the
    normalization the stored text supports. Severity is deliberately
    not part of the key, so the same finding re-raised at a different
    severity still matches.
    """
    return (issue.kind, " ".join(issue.summary.split()).casefold())


def _stored_issues(entry: dict[str, Any]) -> list[SpecIssue] | None:
    """Rehydrate one journal entry's issue list into ``SpecIssue``.

    Returns None for the two shapes that cannot be compared, so the
    caller treats them alike and the accounting line reports them:

    - no issue list at all, which is the legacy journal shape;
    - an issue whose severity is not one of ``_SEVERITY_ORDER``.

    The second is round 2 of review, and refusing it is the point.
    ``_issue_counts`` buckets by severity, so an unrecognised one is
    counted as nothing: seven issues stored with ``"severity": null``
    rendered "Previous run raised 0 issue(s)" and put a 0 in the
    blocker trend for a run that raised seven. Scoring part of an audit
    is how a false number reaches the trend; declining to score it is
    how the operator hears about it instead.

    Everything else is still normalized rather than raised on, so a
    journal written by an older version reads. ``location`` and
    ``suggestion`` are not read back: the report compares counts and
    issue text only.
    """
    raw = entry.get("issues")
    if not isinstance(raw, list):
        return None
    stored = [
        SpecIssue(
            severity=entry_str(i, "severity"),
            kind=entry_str(i, "kind"),
            summary=entry_str(i, "summary"),
        )
        for i in raw
        if isinstance(i, dict)
    ]
    if any(i.severity not in _VALID_SEVERITIES for i in stored):
        return None
    return stored


def _build_convergence(
    issues: list[SpecIssue],
    spec_file: str,
    history: list[dict[str, Any]],
) -> SpecConvergence | None:
    """Compare this run's issues with prior audits (``history``, oldest first).

    Returns None when there is no comparable prior audit, so the first
    run on a spec prints nothing.
    """
    audits: list[tuple[str, list[SpecIssue]]] = []
    trend: list[int] = []
    for entry in history:
        stored = _stored_issues(entry)
        if stored is not None:
            audits.append((entry_str(entry, "spec_file"), stored))
            trend.append(sum(1 for i in stored if i.severity == "blocker"))
    if not audits:
        return None

    previous_spec_file, previous_issues = audits[-1]
    current_ids = {_issue_identity(i) for i in issues}
    current_counts = _issue_counts(issues)
    return SpecConvergence(
        current_counts=current_counts,
        previous_counts=_issue_counts(previous_issues),
        current_spec_file=spec_file,
        previous_spec_file=previous_spec_file,
        repeated=sum(1 for i in previous_issues if _issue_identity(i) in current_ids),
        blocker_trend=(*trend, current_counts["blocker"]),
    )


@dataclass(frozen=True)
class ExcludedProject:
    """Spec audits this journal holds under one OTHER project name (#280).

    Not a claim that they are the same work. The report cannot know
    that, and #280 is explicit that it should not try: the operator
    renamed a project and its spec file in the same moment, so neither
    half of the key survives to link the two histories. What this
    carries is the evidence the operator needs to judge it themselves -
    the other project's name, how many audits it holds, which spec
    files those audits read, and when the last of them was written.

    ``audits`` is not derivable from ``spec_files``: the files are
    deduplicated and the count is per audit, so one project auditing
    one file ten times is (10, one file).

    ``read_this_spec`` is #280's first arm: this project audited the
    same file the current run did. It is carried rather than recomputed
    so the ordering rule and the display rule cannot drift apart.

    ``last_recorded`` is the timestamp string on that project's most
    recent entry, taken in file order because the journal is
    append-only. It is whatever was written there, including "" for an
    entry that recorded none, and never a value this code derives.
    """

    project: str
    audits: int
    spec_files: tuple[str, ...]
    read_this_spec: bool
    last_recorded: str


@dataclass(frozen=True)
class ExcludedHistory:
    """Every spec audit in this journal the report does not count (#280).

    Three buckets, because there are three ways for the report to see
    less than the journal holds, and #280 is that any of them going
    unsaid is the failure the report cannot afford. Each round of
    review found another one being missed, so they are enumerated here.
    Every ``spec_issues`` entry falls into exactly one, and
    :func:`_audit_bucket` is the only place that decides which: #338 is
    what happened when two of the three were computed by separate
    predicates that agreed at every project name except "".

    - ``own_recorded`` counts THIS project's audits on disk. The trend
      may count fewer, because ``lookback_runs`` windows it and because
      an entry it cannot score is refused. Round 1 of review found the
      first version counting only the cross-project axis while its
      wording claimed the whole journal.
    - ``projects`` covers every audit under some other project name.
    - ``unattributed`` covers audits whose ``project`` field is absent,
      null, empty or not a string, FOR A NON-EMPTY project name; at "" those
      audits are this project's own, which is what :func:`_audit_bucket`
      decides. Round 2 of review found these counted by neither of the
      other two: three audits on disk reported as one.

    ``lookback`` is carried so the render can separate the two reasons
    the trend counts fewer than ``own_recorded``. An audit outside the
    window is the configured steady state and is a footnote on the
    trend; an audit inside it that could not be scored is an anomaly
    and gets a sentence.

    The three counts are unwindowed by construction: a count of what
    the trend does not cover that was itself windowed would omit
    history silently, which is the bug this exists to fix.
    """

    own_recorded: int
    projects: tuple[ExcludedProject, ...]
    unattributed: int = 0
    lookback: int = 0

    @property
    def other_audits(self) -> int:
        return sum(p.audits for p in self.projects)

    @property
    def is_empty(self) -> bool:
        return self.own_recorded == 0 and not self.projects and self.unattributed == 0

    def unreadable(self, counted: int) -> int:
        """This project's audits the trend was offered but could not score.

        The window offers at most ``lookback`` of them, so anything
        beyond that was never offered and is not an anomaly. A negative
        result is impossible by construction but clamped anyway, since
        a wrong number here would be the defect this class exists to
        prevent.
        """
        offered = min(self.own_recorded, self.lookback) if self.lookback > 0 else 0
        return max(0, offered - counted)

    def windowed_out(self, counted: int) -> int:
        """This project's audits the trend never saw, window included."""
        return max(0, self.own_recorded - counted - self.unreadable(counted))


def _audit_bucket(
    entry: dict[str, Any],
    project_name: str,
) -> Literal["own", "other", "unattributed"]:
    """Which of ``ExcludedHistory``'s three buckets one spec audit is in.

    The ONLY place the rule lives, because #338 is what two copies of
    it cost. ``own_recorded`` was ``entry_str(e, "project") ==
    project_name`` and ``unattributed`` was ``not entry_str(e,
    "project")``. Those are two spellings of the same question at
    ``project_name == ""``, so an audit with an absent, null, empty or
    non-string project satisfied both and was counted twice: five
    audits on disk, eight bucket placements. One call per audit places
    it once, so the three counts sum to the audits by construction
    rather than by the two predicates happening to disagree.

    The ORDER is the decision, and it is own before unattributed.
    :meth:`EvolutionJournal.get_spec_issue_runs` windows the trend by
    the identical ``entry_str(entry, "project") == project`` expression,
    so at "" the trend counts those audits. Calling them unattributed
    here would make ``own_recorded`` 0 for a trend showing 3, and would
    make the rendered note claiming neither the trend nor the
    cross-project line counts them false about audits the trend just
    counted. Agreeing with the window keeps one answer for one journal.

    For every non-empty ``project_name`` the order is unobservable:
    ``project == project_name`` and ``not project`` cannot both hold.
    """
    project = entry_str(entry, "project")
    if project == project_name:
        return "own"
    if not project:
        return "unattributed"
    return "other"


def _excluded_projects(
    audits: list[dict[str, Any]],
    project_name: str,
    spec_file: str,
) -> tuple[ExcludedProject, ...]:
    """Recorded spec audits under some project OTHER than this one.

    Takes audits, not raw journal entries: which rows are spec audits
    is ``EvolutionJournal.get_spec_audits``'s to decide (#314), and a
    second copy of that rule here is how the accounting and the trend
    could come to disagree about the same journal.

    Ordered so the display cap drops only the weakest evidence: a
    project that audited the file this run audited sorts first, then
    the rest by how much history they hold. The cap itself never drops
    a spec-file match; see ``_excluded_line``.

    Audits with no project name are skipped rather than grouped under
    "": an unnamed project is not somewhere the operator can go and
    look, so pointing at it is not evidence.
    """
    by_project: dict[str, list[str]] = {}
    last_seen: dict[str, str] = {}
    for entry in audits:
        if _audit_bucket(entry, project_name) != "other":
            continue
        project = entry_str(entry, "project")
        by_project.setdefault(project, []).append(entry_str(entry, "spec_file"))
        # Only a timestamp that exists replaces one that exists. Round 2
        # of review: assigning unconditionally let one trailing entry
        # with no timestamp erase a good date every earlier entry for
        # that project carried, losing evidence to a single bad row.
        if timestamp := entry_str(entry, "timestamp"):
            last_seen[project] = timestamp
    excluded = [
        ExcludedProject(
            project=project,
            audits=len(files),
            spec_files=tuple(sorted({f for f in files if f})),
            read_this_spec=spec_file in files,
            last_recorded=last_seen.get(project, ""),
        )
        for project, files in by_project.items()
    ]
    return tuple(sorted(excluded, key=lambda e: (not e.read_this_spec, -e.audits, e.project)))


def _excluded_history(
    audits: list[dict[str, Any]],
    project_name: str,
    spec_file: str,
    lookback: int,
) -> ExcludedHistory:
    """Everything the journal records that the report will not count (#280).

    Empty for a first audit in a fresh repo, so the lines it feeds
    never fire on the common case.

    Takes the audits rather than reading them, so the caller reads once
    and the trend and the accounting are computed over the same
    snapshot. That also removes any chance of the two disagreeing
    because the file changed between two reads. Selecting the audits
    out of the journal's entries is the journal's own job (#314), not a
    rule restated here.

    Deliberately NOT windowed by ``lookback_runs``; ``lookback`` is
    carried only so the render can tell a windowed-out audit from one
    the trend could not score. A count of the history the trend
    excludes that was itself windowed would omit history silently,
    which is the bug this exists to fix.
    """
    buckets = Counter(_audit_bucket(e, project_name) for e in audits)
    return ExcludedHistory(
        own_recorded=buckets["own"],
        projects=_excluded_projects(audits, project_name, spec_file),
        unattributed=buckets["unattributed"],
        lookback=lookback,
    )


@dataclass(frozen=True)
class AuditSnapshot:
    """One read of the recorded spec-audit history, in three views.

    A container rather than a tuple because ``audits`` and ``window``
    are both ``list[dict[str, Any]]``: swapping them typechecks, and
    they are exactly the pair #280 exists to keep straight - the trend
    reads the window, the accounting under it reads everything the
    window left out.

    ``frozen=True`` for the rebinding guarantee, not to make this a
    value that can key a dict. Because ``eq`` is on too, the decorator
    would otherwise generate a ``__hash__`` that hashes the two lists
    and raises ``TypeError: unhashable type: 'list'`` from inside a
    method nobody wrote, so the class advertises a capability it does
    not have. Tuples would not fix it: the elements are ``dict``, and
    hashing a tuple hashes its elements, measured. ``__hash__ = None``
    is the honest answer: set in the body, it is an explicit hash the
    decorator leaves alone. It says unhashable, names THIS class when
    somebody tries, and leaves ``==`` and the frozen fields untouched.

    Not generalised, deliberately. Measured: nine other frozen
    dataclasses in ``kstrl/`` declare a top-level ``list``/``dict``/``set``
    field and still advertise a hash that raises. They are left alone
    here because this PR introduced ``AuditSnapshot`` and that is the one
    it owns; a sweep of the other nine wants its own change with a guard
    behind it, rather than nine edits nothing keeps true.
    """

    #: Every spec audit the journal holds, across every project.
    audits: list[dict[str, Any]]
    #: This project's last ``lookback`` audits: what the trend scores.
    window: list[dict[str, Any]]
    #: How far back the window reaches, carried so the render can tell
    #: a windowed-out audit from one the trend could not score.
    lookback: int

    __hash__ = None  # type: ignore[assignment]


def _journal_snapshot(journal: EvolutionJournal | None, project_name: str) -> AuditSnapshot:
    """The recorded audit history behind one convergence report.

    All three views from ONE read, because a journal that is absent has
    none of the three: that keeps the fact here instead of three
    conditionals at the call site.

    Read through ``EvolutionJournal`` rather than through its storage
    path, and windowed by ``get_spec_issue_runs`` rather than by a
    second copy of its rule here (#314). Why each matters is on those
    two methods; the short version is that a caller holding the path
    survives no change of storage, and a second copy of the window rule
    can drift from the one the accounting uses.

    MUST be read BEFORE this run's own audit is appended to the
    journal, or the "previous run" the trend compares against is this
    one.

    One read per decompose, and the whole file: measured at 6.9 KB per
    factory run in this repo, so 1 MB is about 150 runs and 10 MB about
    1500. It costs 4.4 ms at 1.9 MB, 53 ms at 19 MB and 229 ms at
    78 MB, against an architect call measured at 119 to 210 seconds.
    """
    if journal is None:
        return AuditSnapshot(audits=[], window=[], lookback=0)
    audits = journal.get_spec_audits()
    lookback = journal.config.lookback_runs
    return AuditSnapshot(
        audits=audits,
        window=journal.get_spec_issue_runs(project_name, lookback, audits=audits),
        lookback=lookback,
    )


def _surface_convergence(
    report: SpecConvergence | None,
    excluded: ExcludedHistory,
    project_name: str,
    ui: UI,
) -> None:
    """Render the convergence report, or nothing on the first run.

    No report and an empty ``excluded`` (no journal, or a first audit
    in a repo whose journal holds nothing else) renders silently, so
    the common first-run case prints no noise.

    Counts, deltas and the trend, with no "this spec is converging"
    verdict attached: no measured threshold separates converging from
    not. The recorded blocker counts for one spec went 7, 11, 1, 3, 4,
    and the rise from 1 to 3 happened while the operator was resolving
    real issues. The numbers are the evidence; the judgement about
    whether to pay for another round is the operator's.

    No report next to a non-empty ``excluded`` is #280's own shape: the
    audit that renamed the project starts a fresh trend, and the moment
    the history is lost is the moment worth saying so.

    ``No earlier audit of this project is recorded`` is printed ONLY
    when the journal records none. Round 1 of review reproduced it
    firing over three audits of this very project that the report had
    merely failed to read, under ``lookback_runs=0`` and again on a
    legacy journal whose entries carry no issue list. A confident
    statement over less data than the journal holds is the defect #280
    is about, so what prints in that case is the accounting line below.
    """
    if report is None and excluded.is_empty:
        return
    counted = _counted_audits(report)
    ui.section("Spec Convergence")
    if report is not None:
        _surface_trend(report, ui, excluded.windowed_out(counted))
    elif excluded.own_recorded == 0:
        ui.info("No earlier audit of this project is recorded.")
    else:
        ui.info(
            f"No earlier audit of '{project_name}' could be compared, though this "
            f"journal records {excluded.own_recorded}."
        )
    for line in _excluded_lines(excluded, project_name, counted):
        ui.info(line)


def _counted_audits(report: SpecConvergence | None) -> int:
    """How many EARLIER audits the rendered trend actually counted.

    The trend carries one entry per readable prior audit plus this run,
    so the earlier ones are its length minus one. Derived from the
    rendered value rather than recomputed, so the accounting line can
    never disagree with the trend printed directly above it.
    """
    return len(report.blocker_trend) - 1 if report is not None else 0


# How many names one line spells out before summarising the rest. A
# display cap on line length, not a threshold on meaning: nothing is
# dropped from the counts, only from the list of names.
_EXCLUDED_NAMES_SHOWN = 3


def _join_capped(items: Sequence[str], noun: str) -> str:
    """Join ``items``, naming at most ``_EXCLUDED_NAMES_SHOWN`` of them."""
    shown = items[:_EXCLUDED_NAMES_SHOWN]
    rest = len(items) - len(shown)
    joined = ", ".join(shown)
    return f"{joined} and {rest} more {noun}" if rest else joined


def _project_phrase(entry: ExcludedProject) -> str:
    """One project named, with how much history it holds and when.

    The audit count is rendered because it is the evidence the operator
    judges a suspected rename on. Round 2 of review: it was carried on
    the dataclass and dropped at the last step, so a project holding
    100 audits printed identically to one holding 1, and the line
    naming where the history lives could not say how much was there.
    """
    parts = [f"{entry.audits} audit(s)"]
    if entry.spec_files:
        parts.append(_join_capped(entry.spec_files, "file(s)"))
    if entry.last_recorded:
        parts.append(f"last {entry.last_recorded.split('T')[0]}")
    return f"'{entry.project}' ({', '.join(parts)})"


def _excluded_lines(
    excluded: ExcludedHistory,
    project_name: str,
    counted: int,
) -> list[str]:
    """The lines naming audit history this report does not count (#280).

    One per bucket that has something in it, and between the three of
    them plus the trend footnote they account for every ``spec_issues``
    entry in the journal. Each states a count read off disk against a
    count read off the rendered trend, so none can claim more coverage
    than it has.

    An audit the window never offered the trend is NOT one of these
    lines. That is the configured steady state, permanent from the
    eleventh audit at the default lookback, and round 2 of review
    measured the previous version printing a growing "40 recorded, 10
    counted" note on every decompose forever. A warning that always
    fires is noise, so that case is a footnote on the trend line it
    qualifies (see ``_surface_trend``), and only an audit the trend was
    OFFERED and could not score is an anomaly worth a sentence.

    Projects that read this run's spec file are named ahead of the
    rest, and both groups are capped: round 1 of review found the cap
    dropping spec-file matches, and round 2 found the fix removing the
    bound with it, rendering 971 characters for 25 such projects.
    """
    lines: list[str] = []
    unreadable = excluded.unreadable(counted)
    if unreadable:
        lines.append(
            f"Note: {unreadable} earlier audit(s) of '{project_name}' fall inside the "
            f"lookback window but could not be scored, so the trend does not count "
            f"them. An audit is skipped when it records no issue list, or an issue "
            f"whose severity is not blocker, major or minor."
        )
    if excluded.projects:
        matched = _join_capped(
            [_project_phrase(p) for p in excluded.projects if p.read_this_spec],
            "project(s) that read this spec file",
        )
        rest = _join_capped(
            [_project_phrase(p) for p in excluded.projects if not p.read_this_spec],
            "project(s)",
        )
        lines.append(
            "Note: audits are matched by project name, and this report covers "
            f"'{project_name}'. This journal also records {excluded.other_audits} "
            f"spec audit(s) under {', '.join(p for p in (matched, rest) if p)}."
        )
    if excluded.unattributed:
        lines.append(
            f"Note: {excluded.unattributed} spec audit(s) in this journal record no "
            f"project name, so neither the trend nor the line above counts them."
        )
    return lines


def _surface_trend(report: SpecConvergence, ui: UI, windowed_out: int = 0) -> None:
    """The comparison itself: counts, deltas, trend and overlap.

    ``windowed_out`` is how many earlier audits of this project the
    lookback window kept out of the trend. It is a footnote on the
    trend line rather than a Note of its own: once a project has more
    audits than ``lookback_runs`` the condition holds on every run
    forever, so a separate warning would fire permanently and
    round 2 of review measured exactly that. Qualifying the number in
    place says the same thing where it is read and costs no line.
    """
    for severity in _SEVERITY_ORDER:
        current = report.current_counts[severity]
        previous = report.previous_counts[severity]
        delta = current - previous
        ui.kv(
            severity.capitalize(),
            f"{current} (previous run: {previous}, {f'{delta:+d}' if delta else 'no change'})",
        )
    scope = f"; {windowed_out} older audit(s) outside the lookback window" if windowed_out else ""
    ui.kv(
        "Trend",
        ", ".join(str(n) for n in report.blocker_trend) + f" (blockers, oldest run first{scope})",
    )
    ui.info(
        f"Previous run raised {report.previous_total} issue(s): {report.repeated} "
        f"reappear verbatim, {report.previous_total - report.repeated} do not."
    )
    ui.info(
        "Matched on issue text, so a reworded issue counts as new: a floor on "
        "what carried over, not a count of what the architect resolved."
    )
    if report.previous_spec_file and report.previous_spec_file != report.current_spec_file:
        ui.info(
            f"Runs are matched by project name: the previous audit read "
            f"{report.previous_spec_file}, this one read {report.current_spec_file}."
        )
