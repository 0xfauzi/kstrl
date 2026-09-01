"""R8.2 threshold replay: what WOULD the ladder have done, historically?

The R8 cycle carries one standing rule: **no assumed thresholds**. Every
number in :mod:`kstrl.autonomy` is a placeholder taken from the roadmap
table, and none may gate or demote anything until it has been replayed
against real run data and the would-have-fired count recorded.

This module is that replay. It reads the recorded history
(``.kstrl/experiments.tsv``, written per run) and answers two questions:

1. Is there even enough data to calibrate? A threshold tuned on a handful
   of runs is a guess wearing a number's clothes, so the report leads with
   the sample size and says plainly when it is too small.
2. Replaying the ladder over that history, which transitions would have
   fired, and when?

It deliberately reports rather than decides: nothing here mutates ladder
state. Reading a replay is a human act.

Decisive-run definition (matters, and is the main judgement call here): a
run counts as decisive when it produced a verdict about the factory's
JUDGEMENT. A run that died on a git push or a PR-creation failure is an
infrastructure casualty - it says nothing about whether the factory's
reviews are trustworthy - so it is excluded. Counting those would let a
string of broken runs unlock a promotion, which is exactly backwards.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from kstrl.autonomy import (
    MIN_DECISIVE_RUNS,
    THRESHOLDS,
    AutonomyLevel,
    AutonomyState,
    DemotionTrigger,
)
from kstrl.evolution import INFRASTRUCTURE_CHECKS
from kstrl.verify import SCOPE_UNREADABLE_CHECK

DEFAULT_EXPERIMENTS_PATH = Path(".kstrl/experiments.tsv")

#: `common_failure` prefixes that mark an infrastructure casualty rather
#: than a judgement. Kept as a prefix tuple so a new plumbing failure
#: family is one entry, not a new code path.
#:
#: #315: the first four come from `evolution.INFRASTRUCTURE_CHECKS`
#: rather than being retyped here. The two consumers disagreed about
#: `pr:merge-conflict` - plumbing to this module, an engineer-loop
#: failure to the journal - and a taxonomy that answers a question twice
#: will eventually answer it two ways. Deriving means enrolling a check
#: as infrastructure in `_CATEGORY_BY_CHECK` reaches both in one edit,
#: and the enrolment guard forces every emitted name through that table.
#:
#: _REPLAY_ONLY_PREFIXES is the deliberate remainder: this module asks a
#: WIDER question than the journal's category does. The journal asks
#: which part of the factory produced a failure; this asks whether the
#: run yielded a verdict about the factory's JUDGEMENT at all, and a
#: failure can be a gate's honest verdict and still answer that with no.
#: `scope_unreadable:` (#294) is the harness failing to establish its own
#: input: the component's scope could not be read from the pre-run tree,
#: so nothing was ever measured about the change. The same reasoning that
#: attaches `Finding.infrastructure_error` to that check applies to the
#: signature stream, and it was missed there first: a run dominated by it
#: was counted DECISIVE and fed autonomy promotion and demotion as
#: evidence about the factory's judgement. #294 makes that more likely
#: rather than less, because refusing the component before its engineer
#: runs makes this the modal signature of the run instead of one of
#: three retries. The journal keeps it under `verification` all the same:
#: it IS a Phase 1 gate result, and #294 gave it its own gate, its own
#: table row and its own proposal arm on that basis. That divergence is
#: the point of splitting this constant in two, and
#: `tests/test_check_name_enrolment.py` pins both halves of it.
#:
#: `git:`, `infra:` and `timeout:` name families no version of kstrl has
#: ever emitted: measured with `git log -S`, all three strings entered
#: the tree in 606cbb1, the commit that created this tuple, and no call
#: site produces them. Kept because an experiments.tsv row outlives the
#: code that wrote it and the cost of a prefix that matches nothing is
#: zero. They are NOT enrolled in `_CATEGORY_BY_CHECK`: that table
#: describes names kstrl emits, and the enrolment guard would make any
#: real `git:` gate enrol itself on the day it lands.
_REPLAY_ONLY_PREFIXES: tuple[str, ...] = (
    f"{SCOPE_UNREADABLE_CHECK}:",
    "git:",
    "infra:",
    "timeout:",
)

INFRA_FAILURE_PREFIXES: tuple[str, ...] = tuple(
    sorted({f"{check}:" for check in INFRASTRUCTURE_CHECKS} | set(_REPLAY_ONLY_PREFIXES))
)


@dataclass
class RunRecord:
    """One row of experiments.tsv, typed and normalized."""

    run_id: str
    timestamp: str
    project: str
    components_total: int
    completed: int
    failed: int
    skipped: int
    retry_rate: float
    common_failure: str

    @property
    def infra_aborted(self) -> bool:
        """True when the run's dominant failure was plumbing, not judgement."""
        return self.common_failure.startswith(INFRA_FAILURE_PREFIXES)

    @property
    def decisive(self) -> bool:
        """A run that yielded a verdict about the factory's judgement.

        Requires at least one component to have reached a terminal state
        AND the run not to have been an infrastructure casualty.
        """
        if self.infra_aborted:
            return False
        return (self.completed + self.failed) > 0


def _as_int(value: str) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _as_float(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def load_runs(path: Path) -> list[RunRecord]:
    """Parse experiments.tsv. Missing file -> no runs (not an error)."""
    if not path.exists():
        return []
    runs: list[RunRecord] = []
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            if not row.get("run_id"):
                continue
            runs.append(
                RunRecord(
                    run_id=row.get("run_id", ""),
                    timestamp=row.get("timestamp", ""),
                    project=row.get("project", ""),
                    components_total=_as_int(row.get("components_total", "0")),
                    completed=_as_int(row.get("completed", "0")),
                    failed=_as_int(row.get("failed", "0")),
                    skipped=_as_int(row.get("skipped", "0")),
                    retry_rate=_as_float(row.get("retry_rate", "0")),
                    common_failure=(row.get("common_failure") or "").strip(),
                )
            )
    return runs


@dataclass
class ReplayReport:
    """Outcome of replaying the ladder over recorded history."""

    total_runs: int
    decisive_runs: int
    infra_aborted_runs: int
    projects: list[str]
    components_merged: int
    would_promote: list[str] = field(default_factory=list)
    would_demote: list[str] = field(default_factory=list)
    final_level: int = int(AutonomyLevel.L1_SUPERVISED)
    thresholds: dict[str, int] = field(default_factory=lambda: dict(THRESHOLDS))

    @property
    def sufficient_data(self) -> bool:
        """Whether the sample can calibrate anything at all."""
        return self.decisive_runs >= MIN_DECISIVE_RUNS

    def render(self) -> str:
        lines = [
            "Autonomy threshold replay (R8.2)",
            "=" * 34,
            "",
            f"Runs recorded:        {self.total_runs}",
            f"  decisive:           {self.decisive_runs}",
            f"  infra-aborted:      {self.infra_aborted_runs} (excluded)",
            f"Components merged:    {self.components_merged}",
            f"Projects:             {', '.join(self.projects) or '(none)'}",
            "",
            "Thresholds replayed (ALL UNMEASURED PLACEHOLDERS):",
        ]
        lines.extend(f"  {name:<34} {value}" for name, value in sorted(self.thresholds.items()))
        lines.extend(
            [
                "",
                f"Would-have-promoted:  {len(self.would_promote)}",
                *(f"  + {entry}" for entry in self.would_promote),
                f"Would-have-demoted:   {len(self.would_demote)}",
                *(f"  - {entry}" for entry in self.would_demote),
                "",
                f"Final level after replay: L{self.final_level}",
                "",
            ]
        )
        if not self.sufficient_data:
            lines.extend(
                [
                    "VERDICT: INSUFFICIENT DATA.",
                    f"  {self.decisive_runs} decisive run(s) against a "
                    f"MIN_DECISIVE_RUNS floor of {MIN_DECISIVE_RUNS}.",
                    "  No threshold above can be calibrated from this sample, and",
                    "  none should gate a real transition yet. The ladder is safe",
                    "  to run - L1 grants nothing - but promotion beyond L1 rests",
                    "  on evidence that does not exist yet.",
                    "  Required: more real factory runs (see docs/dark-factory-roadmap.md,",
                    "  'User-run measurements required').",
                ]
            )
        else:
            lines.append(
                "VERDICT: sample meets the minimum; thresholds may be tuned "
                "against it and the result recorded in the roadmap."
            )
        return "\n".join(lines)


def replay(runs: list[RunRecord]) -> ReplayReport:
    """Replay the ladder over recorded runs, reporting hypothetical moves.

    Promotion is replayed as "criteria would have been MET" - it never
    fires unattended in reality, since a human ack is required, so a
    would-promote entry means "the factory would have been eligible to
    ask", not "it would have promoted itself".
    """
    state = AutonomyState()
    report = ReplayReport(
        total_runs=len(runs),
        decisive_runs=sum(1 for r in runs if r.decisive),
        infra_aborted_runs=sum(1 for r in runs if r.infra_aborted),
        projects=sorted({r.project for r in runs if r.project}),
        components_merged=sum(r.completed for r in runs),
    )
    for run in runs:
        if not run.decisive:
            continue
        state.record_decisive_run()
        for _ in range(run.completed):
            state.record_merged_component()
        # A run whose components failed review is the closest proxy the
        # recorded history has for a judgement-quality regression. Real
        # demotion triggers (policy violation, calibration regression,
        # health breach) are not in experiments.tsv, so this replay can
        # only bound the demote side from below - stated, not hidden.
        if run.failed and not run.infra_aborted:
            record = state.demote(
                DemotionTrigger.CALIBRATION_REGRESSION,
                f"replay: {run.failed} component(s) failed in {run.run_id}",
            )
            if record is not None:
                report.would_demote.append(
                    f"{run.timestamp} {run.run_id}: "
                    f"L{record.from_level} -> L{record.to_level} "
                    f"({record.trigger})"
                )
        if state.autonomy_level is not AutonomyLevel.L4_DEPLOY and not state.promotion_blockers():
            # Advance the SIMULATED level, not just the report. Recording
            # eligibility without moving would pin the replay at L1
            # forever: it would re-report the same L2 opportunity on every
            # subsequent run and never exercise the L3/L4 thresholds, the
            # cool-down, or any post-promotion demotion. The ack is a
            # real-world requirement, not a replay one - here we ask "what
            # would the criteria have allowed?", so the simulation grants
            # it and says so.
            record = state.promote(
                actor="replay",
                ack=f"simulated: criteria met at {run.run_id}",
            )
            report.would_promote.append(
                f"{run.timestamp} {run.run_id}: L{record.from_level} -> "
                f"L{record.to_level} (eligible; human ack still required "
                "in reality)"
            )
    report.final_level = state.level
    return report


def replay_file(path: Path | None = None, root_dir: Path | None = None) -> ReplayReport:
    """Replay from an experiments.tsv path (default ``.kstrl/`` under root)."""
    if path is None:
        base = root_dir or Path.cwd()
        path = base / DEFAULT_EXPERIMENTS_PATH
    return replay(load_runs(path))
