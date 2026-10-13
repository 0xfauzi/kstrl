"""The spec audit of ``decompose_spec``: findings, decisions, and the halt on an escalation."""

from __future__ import annotations

import functools
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

from kstrl.decisions import (
    SpecDecision,
    escalations,
    open_escalation_item,
    parse_decisions,
    write_decisions,
)
from kstrl.decompose_reply import (
    _file_stack_proposal,
)
from kstrl.event_catalog import (
    ArtifactWritten,
    ComponentFailed,
    Event,
    PhaseCompleted,
    PhaseStarted,
    RunCompleted,
    SpecIssueRecorded,
)
from kstrl.events import EventBus
from kstrl.owner_answers import OwnerAnswers
from kstrl.requirements import parse_requirements
from kstrl.spec_convergence import (
    _build_convergence,
    _excluded_history,
    _journal_snapshot,
    _surface_convergence,
)
from kstrl.spec_issues import (
    _parse_spec_issues,
    _record_spec_issues_event,
    _spec_audit_journal,
    _surface_spec_decisions,
    _surface_spec_issues,
    persist_spec_issues,
)
from kstrl.usage import ARCHITECT_COMPONENT

if TYPE_CHECKING:
    from kstrl.requirements import SpecRequirement
    from kstrl.spec_issues import SpecIssue
    from kstrl.ui import UI


class SpecBlockerError(Exception):
    """Raised when the architect ESCALATED at least one question (#260).

    The decompose pipeline halts until the owner answers, rather than
    letting the architect guess at a product, scope or risk judgement.
    It no longer halts on blocker-severity findings as such: five real
    runs raised 26 blockers and not one of them was a judgement only the
    owner could make, so the architect halted on questions it had
    already answered in its own suggestions.

    The escalated decisions are attached via ``escalations``.
    ``artifact_path`` points at the persisted spec-issues.json (R1.7)
    and ``decisions_path`` at decisions.json, each when its write
    succeeded, so callers can direct the user at a durable record
    instead of scrollback. Both, because after #260 they hold different
    halves of the halt: the finding is in the audit, the question the
    owner has to answer and the reason it was not answered are in the
    register.
    """

    def __init__(
        self,
        escalated: list[SpecDecision],
        artifact_path: Path | None = None,
        decisions_path: Path | None = None,
    ):
        self.escalations = escalated
        self.artifact_path = artifact_path
        self.decisions_path = decisions_path
        summary_lines = [f"- {d.question}\n  owner must decide: {d.resolution}" for d in escalated]
        super().__init__(
            "Architect escalated; the owner must answer before re-running:\n"
            + "\n".join(summary_lines),
        )

    def artifact_lines(self) -> list[str]:
        """Where the operator should go to answer, one line per record.

        R1.7 says point at a file rather than at scrollback, and every
        handler of this exception owes the same pointers, so the wording
        lives here instead of being repeated at each of them.
        """
        lines: list[str] = []
        if self.artifact_path is not None:
            lines.append(f"Spec issues written to: {self.artifact_path}")
        if self.decisions_path is not None:
            lines.append(f"Architect decisions written to: {self.decisions_path}")
        return lines


def _write_decompose_artifact(
    label: str,
    noun: str,
    writer: Callable[[], Path],
    *,
    ui: UI,
    emit: Callable[[Event], None],
    rel_display: Callable[[Path], str],
    required: bool = False,
) -> Path | None:
    """Attempt one durable decompose artifact, and say what happened.

    The write policy the R1.7 audit and the #260 decision register
    share, in one place so the two cannot drift: attempt, announce,
    emit, and on ``OSError`` be loud without masking. Returns the path,
    or ``None`` when the write failed, which is what the caller passes
    to ``SpecBlockerError`` so the halt never points at a file that is
    not there.

    ``required`` says the artifact IS part of the result rather than a
    record of it. The decision register beside a saved manifest is: a
    later ``ks factory`` reads it to bind engineers, and a missing
    register binds nothing and says nothing, so one swallowed write
    error would silently disable the register for every run against
    that manifest. The halting copy is not required, because the halt
    reaches the operator through ``SpecBlockerError`` either way.
    """
    try:
        path = writer()
    except OSError as exc:
        if required:
            raise
        ui.err(f"Failed to persist {noun} to disk: {exc}")
        return None
    ui.ok(f"{noun.capitalize()} written: {path}")
    emit(ArtifactWritten(label=label, path=rel_display(path)))
    return path


def _audit_spec(
    data: dict[str, Any],
    *,
    ui: UI,
    emit: Callable[[Event], None],
    rel_display: Callable[[Path], str],
    root_dir: Path,
    project_name: str,
    spec_path: Path,
    spec_source: str,
    pin: str,
    stack_pin: str,
    answers: OwnerAnswers,
    bus: EventBus | None,
    run_started: float,
) -> tuple[
    list[SpecIssue], list[SpecDecision], tuple[SpecRequirement, ...], Callable[..., Path | None]
]:
    """Audit the architect's reply, persist the findings, and stop on an escalation.

    Raises ``SpecBlockerError`` through ``_halt`` when the architect
    escalated a question. Otherwise returns what the plan write needs.
    """

    # Surface AND persist red-team findings before doing any further
    # work (R1.7): the artifact and journal event are written for halt,
    # success, and clean-audit outcomes alike. If any issue is a
    # blocker, halt before generating PRDs - the architect explicitly
    # judged the spec un-decomposable.
    emit(
        PhaseStarted(
            component=ARCHITECT_COMPONENT,
            phase="audit",
            attempt=1,
        )
    )
    audit_start = time.monotonic()
    spec_issues = _parse_spec_issues(data)
    _surface_spec_issues(spec_issues, ui)
    for issue in spec_issues:
        emit(
            SpecIssueRecorded(
                severity=issue.severity,
                kind=issue.kind,
                summary=issue.summary,
                location=issue.location,
                suggestion=issue.suggestion,
            )
        )
    # #260: the halt keys on the architect's own disposition, not on a
    # severity label. Everything it decided, assumed or spiked rides
    # along; only a question it refused to answer stops the run.
    decisions = parse_decisions(data)
    requirements = parse_requirements(data)
    escalated = escalations(decisions)
    _surface_spec_decisions(decisions, ui)

    # The R1.7 artifacts are written FIRST, before any optional journal
    # work. On the halt path they are the only durable record the
    # operator gets, so nothing that can fail is allowed upstream of
    # them: #260 briefly put a config load in front of this and a typo
    # in an unrelated journal knob destroyed the findings of a paid run.
    #
    # One helper, called twice, so the two writes cannot drift on the
    # policy they share: attempt, announce, emit, and on OSError be
    # loud without masking. They stay two independent attempts because
    # neither may take the other down, and after #260 they hold
    # different halves of a halt.
    write_artifact = functools.partial(
        _write_decompose_artifact,
        ui=ui,
        emit=emit,
        rel_display=rel_display,
    )
    artifact_path = write_artifact(
        "spec_issues",
        "spec audit",
        lambda: persist_spec_issues(
            spec_issues,
            root_dir=root_dir,
            project_name=project_name,
            spec_file=spec_path.name,
            halted=bool(escalated),
        ),
    )
    # #260 round 2: the register is written on the HALT path here, and
    # on the success path only after the manifest commits (below).
    # Round 1 wrote it here unconditionally, so a decompose that halted
    # or failed later left a fresh register beside an OLDER manifest,
    # and the factory read it as that manifest's decisions. The halt
    # copy is stamped ``halted`` and the factory refuses to bind one.
    write_register = functools.partial(
        write_artifact,
        "decisions",
        "architect decisions",
    )
    # #260: what this audit says about the previous one, read BEFORE
    # this run is appended to the journal below - otherwise the
    # "previous run" the report compares against would be this one.
    journal = _spec_audit_journal(root_dir, ui)
    # ONE read feeds both the trend and the accounting under it, so the
    # two are computed over the same snapshot and cannot disagree
    # because the file changed between two reads.
    snapshot = _journal_snapshot(journal, project_name)
    # #280: the trend is keyed on the project name, so a rename starts a
    # fresh one and the runs before it drop out of view. Keying
    # differently would be worse (the spec is edited every round, so a
    # content hash never matches, and #260's own loop renamed the file
    # too), so what is fixed is the silence rather than the key.
    _surface_convergence(
        _build_convergence(spec_issues, spec_path.name, snapshot.window),
        _excluded_history(snapshot.audits, project_name, spec_path.name, snapshot.lookback),
        project_name,
        ui,
    )
    _record_spec_issues_event(
        spec_issues,
        journal=journal,
        project_name=project_name,
        spec_file=spec_path.name,
        halted=bool(escalated),
        ui=ui,
    )
    # #696 slice 7: on the halt path too, since a conflict with the [stack]
    # kstrl.toml holds is one the architect escalates.
    _file_stack_proposal(data, root_dir, stack_pin, spec_source, bus, ui)
    emit(
        PhaseCompleted(
            component=ARCHITECT_COMPONENT,
            phase="audit",
            passed=not escalated,
            detail=f"{len(escalated)} escalation(s)" if escalated else "",
            duration_seconds=round(time.monotonic() - audit_start, 2),
        )
    )
    if escalated:
        _halt(
            escalated,
            decisions=decisions,
            requirements=requirements,
            write_register=write_register,
            artifact_path=artifact_path,
            ui=ui,
            emit=emit,
            rel_display=rel_display,
            root_dir=root_dir,
            project_name=project_name,
            spec_path=spec_path,
            spec_source=spec_source,
            pin=pin,
            answers=answers,
            bus=bus,
            run_started=run_started,
        )
    return spec_issues, decisions, requirements, write_register


def _halt(
    escalated: list[SpecDecision],
    *,
    decisions: list[SpecDecision],
    requirements: tuple[SpecRequirement, ...],
    write_register: Callable[..., Path | None],
    artifact_path: Path | None,
    ui: UI,
    emit: Callable[[Event], None],
    rel_display: Callable[[Path], str],
    root_dir: Path,
    project_name: str,
    spec_path: Path,
    spec_source: str,
    pin: str,
    answers: OwnerAnswers,
    bus: EventBus | None,
    run_started: float,
) -> NoReturn:
    """Write the halting register, open the escalation item and raise ``SpecBlockerError``."""
    decisions_path = None

    decisions_path = write_register(
        lambda: write_decisions(
            decisions,
            root_dir=root_dir,
            project_name=project_name,
            spec_file=spec_path.name,
            halted=True,
            spec_digest=pin,
            answered_items=answers.item_ids,
            answers_digest=answers.digest,
            requirements=requirements,
        ),
    )
    open_escalation_item(
        escalated,
        root_dir,
        project_name,
        spec_source,
        spec_digest=pin,
        register_path=rel_display(decisions_path) if decisions_path is not None else "",
        run_id=bus.run_id if bus is not None else "",
        warn=ui.warn,
        spec_path=spec_path,
    )
    # The run dir must read as FINISHED, not dead: the halt is the
    # architect's judgment, delivered before the error propagates.
    emit(
        ComponentFailed(
            component=ARCHITECT_COMPONENT,
            error=f"spec halted: {len(escalated)} escalated question(s)",
        )
    )
    emit(
        RunCompleted(
            completed=0,
            failed=1,
            duration_seconds=round(time.monotonic() - run_started, 2),
        )
    )
    raise SpecBlockerError(
        escalated,
        artifact_path=artifact_path,
        decisions_path=decisions_path,
    )
