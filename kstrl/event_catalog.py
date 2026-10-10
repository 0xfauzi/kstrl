"""The schema-v2 event vocabulary of kstrl runs.

This module owns the :class:`Event` base class, its type registry, the
envelope fields, and the registered event dataclasses. The sinks, the
tolerant reader and the run-directory layout are in :mod:`kstrl.events`.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Final

from kstrl.appendio import JOURNAL_REPAIR_EVENT

SCHEMA_VERSION: Final = 2

_ENVELOPE_FIELDS: Final = frozenset({"ts", "run_id", "component", "source", "seq", "kstrl_version"})

_REGISTRY: dict[str, type[Event]] = {}


@dataclass(frozen=True, kw_only=True)
class Event:
    """Base class for schema-v2 events.

    Envelope fields (``ts``/``run_id``/``component``/``source``/``seq``)
    are stamped by :meth:`EventBus.emit`; payload fields are everything a
    subclass adds. Every payload field MUST have a default so decoding
    is total (forward compatibility contract).
    """

    type: ClassVar[str] = ""  # registry key; "" = abstract base

    ts: float = 0.0
    run_id: str = ""
    component: str = ""
    source: str = "orchestrator"
    seq: int = 0
    #: The kstrl that wrote the line (#451). Stamped by EventBus.emit;
    #: "" on a line written before stamping.
    kstrl_version: str = ""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.type:
            _REGISTRY[cls.type] = cls

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        for f in dataclasses.fields(self):
            if f.name in _ENVELOPE_FIELDS:
                continue
            value = getattr(self, f.name)
            data[f.name] = list(value) if isinstance(value, tuple) else value
        return {
            "schema": SCHEMA_VERSION,
            "event": type(self).type,
            "ts": self.ts,
            "run_id": self.run_id,
            "component": self.component,
            "source": self.source,
            "seq": self.seq,
            "kstrl_version": self.kstrl_version,
            "data": data,
        }

    def to_json_line(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"), default=str)


@dataclass(frozen=True, kw_only=True)
class UnknownEvent(Event):
    """An event whose type or shape this build does not understand.

    Preserves the raw envelope so copies/tees are lossless and reducers
    can count what they skipped instead of crashing on it.
    """

    type: ClassVar[str] = "unknown"
    type_name: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        if self.raw:
            return dict(self.raw)
        return super().to_dict()


# ---------------------------------------------------------------------------
# v1-named events (1:1 with ProgressLog's catalogue; V1CompatSink maps these)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class RunStarted(Event):
    type: ClassVar[str] = "factory_started"
    project: str = ""
    components: int = 0
    #: The process that ran this run. `ks serve` charges a decompose run to
    #: the launch whose child process this is (#587). 0 in a stream
    #: written before #587.
    pid: int = 0
    #: The run that holds this factory run's architect records. `ks factory
    #: --spec` runs its architect as a decompose run of its own (#567) and
    #: names it here (#587). "" when this run decomposed nothing.
    architect_run_id: str = ""


@dataclass(frozen=True, kw_only=True)
class ComponentStarted(Event):
    type: ClassVar[str] = "component_started"


@dataclass(frozen=True, kw_only=True)
class ComponentCompleted(Event):
    type: ClassVar[str] = "component_completed"
    duration_seconds: float = 0.0
    iterations: int = 0


@dataclass(frozen=True, kw_only=True)
class ComponentFailed(Event):
    type: ClassVar[str] = "component_failed"
    error: str = ""


@dataclass(frozen=True, kw_only=True)
class ComponentSkipped(Event):
    """A planned component that ended intentionally without completing."""

    type: ClassVar[str] = "component_skipped"
    reason: str = ""


@dataclass(frozen=True, kw_only=True)
class CircuitBreakerTripped(Event):
    """R7.5: engineer loop halted on the no-progress breaker."""

    type: ClassVar[str] = "circuit_breaker_tripped"
    iterations: int = 0
    error: str = ""


@dataclass(frozen=True, kw_only=True)
class ComponentRetrying(Event):
    type: ClassVar[str] = "component_retrying"
    attempt: int = 0
    reason: str = ""


@dataclass(frozen=True, kw_only=True)
class VerificationResultEvent(Event):
    """One run of ``verify.run_mechanical_verification``.

    ``phase`` and ``advisory`` are #288. The factory's Phase 1 leaves
    both at their defaults: it is the gate, and it emits once per
    component attempt inside its own ``phase="verify"`` bracket.
    `ks feature` emits several per run - one after the implement loop and
    one after each repair attempt - and gates on none of them, so it
    names the loop it measured and marks the verdict advisory.

    Without those two fields a consumer reading `events.jsonl` cannot
    tell which loop a verdict is about (filtering by type loses the
    ordering that would otherwise say), and reads ``passed=False``
    followed by ``phase_completed(passed=True)`` as a contradiction
    rather than as a report next to a gate. Both default, so old
    payloads decode unchanged; adding them later would not reach runs
    already on disk.
    """

    type: ClassVar[str] = "verification_result"
    passed: bool = False
    checks: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()
    duration_seconds: float = 0.0
    #: The loop whose output was measured ("implement", "repair-2").
    #: Empty for the factory, whose PhaseStarted/PhaseCompleted bracket
    #: already names it.
    phase: str = ""
    #: True when nothing gated on this verdict.
    advisory: bool = False
    #: ``"check:reason"`` per check that was asked for and measured
    #: nothing (#306), e.g. ``"test_adequacy:retired"``.
    #: Deliberately NOT folded into ``checks``, which names what ran:
    #: a consumer counting green checks must not count these. Defaults
    #: empty, so payloads already on disk decode unchanged.
    not_measured: tuple[str, ...] = ()
    #: #700: the isolation the commands ran under, as the emitter states
    #: it: a rung's label, or ``kstrl.rung.HOST_LABEL``. Empty when the emitter
    #: ran no command, and on every payload written before #700.
    isolation: str = ""
    #: #462: the absolute path of each file holding a failed gate's
    #: output, one per failed test / typecheck / lint gate whose write
    #: succeeded. The output itself stays out of the event.
    gate_logs: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class ReviewResultEvent(Event):
    """Phase 2 review AND phase 2.5 security (mode startswith "security")."""

    type: ClassVar[str] = "review_result"
    passed: bool = False
    mode: str = ""
    fail_count: int = 0
    advisory_count: int = 0
    duration_seconds: float = 0.0


@dataclass(frozen=True, kw_only=True)
class ComponentUsage(Event):
    """R3.1 cost meter capture: mirror of ``UsageTotals.to_dict()`` plus
    the phase. Token/cost figures are CLI self-reports - lower bounds
    whenever ``unreported_calls`` > 0.

    R8: ``token_calls`` is the narrower coverage figure - calls that
    reported an actual token count, as opposed to cost alone -
    and ``cost_calls`` is its mirror for the cost axis. Payloads written
    before those fields landed omit them and decode to 0; the decoder
    reads only keys it knows, so old and new readers interoperate both
    ways."""

    type: ClassVar[str] = "component_usage"
    phase: str = ""
    calls: int = 0
    known_calls: int = 0
    token_calls: int = 0
    cost_calls: int = 0
    unreported_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    duration_seconds: float = 0.0


@dataclass(frozen=True, kw_only=True)
class BudgetExceeded(Event):
    """A run-level ceiling stopped a component.

    R8: ``ceiling`` names WHICH one (``"max_total_tokens"`` /
    ``"max_cost_usd"``), because a consumer that assumed "token" would
    tell the operator to raise the wrong knob. Empty on payloads written
    before the cost ceiling landed.

    R8 review (#180): ``condition`` and ``ceilings`` carry the same halt
    structurally, because ``ceiling`` alone conflated two orthogonal
    facts. ``condition`` is ``"breached"`` (a total reached a ceiling) or
    ``"unenforceable"`` (a configured ceiling provably cannot fire) -
    only the former licenses a "N >= cap" sentence, and rendering one for
    the latter reported ``token budget exceeded: 0 >= 500`` on runs whose
    totals never moved. ``ceilings`` holds one or many identities; the
    unenforceable halt legitimately names both, which no single-value
    field could express. ``ceiling`` stays as the joined legacy string so
    payloads written before this change still decode.

    R8 (measured): ``coverage`` records, per named ceiling, how many of
    the run's metered calls that ceiling actually counted and which roles
    it did not - one entry per ceiling, mirroring
    ``CeilingCoverage.to_dict()``. Without it the halt record states a
    total without saying what the total covers, which is how a run whose
    reviewer reported tokens and no cost recorded a dollar ceiling that
    bounded the engineer alone. Empty on payloads written before this
    landed, and on halts whose ceilings covered every call."""

    type: ClassVar[str] = "budget_exceeded"
    total_tokens: int = 0
    max_total_tokens: int = 0
    cost_usd: float = 0.0
    max_cost_usd: float = 0.0
    ceiling: str = ""
    condition: str = ""
    ceilings: tuple[str, ...] = ()
    coverage: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True, kw_only=True)
class BudgetCoverage(Event):
    """A configured ceiling stopped covering every metered call.

    Emitted ONCE per ceiling per run, at the first phase whose usage
    leaves that ceiling short - the earliest point the evidence exists,
    and long before the halt message the operator would otherwise be
    reading only after the money is spent.

    Run-scoped (no component): coverage is a property of the run's
    adapters, not of whichever component happened to expose it.

    ``uncovered_tokens`` is deliberately a TOKEN count. The uncovered
    calls reported no price and this codebase holds no price table;
    converting them to dollars would put an invented number in the audit
    trail, which is worse than a missing one."""

    type: ClassVar[str] = "budget_coverage"
    ceiling: str = ""
    axis: str = ""
    calls: int = 0
    covered_calls: int = 0
    uncovered_calls: int = 0
    uncovered_tokens: int = 0
    uncovered_roles: tuple[str, ...] = ()
    detail: str = ""


def budget_halt_kind(
    condition: str,
    ceilings: Sequence[str],
    ceiling: str = "",
) -> str:
    """Classify a :class:`BudgetExceeded` payload: the ONE vocabulary.

    Returns ``"unenforceable"``, ``"cost"`` or ``"token"``. Every surface
    that renders a budget halt classifies through here rather than
    re-testing ``ceiling == "max_cost_usd"`` locally: those local tests
    were what silently reclassified the multi-ceiling unenforceable halt
    as a token breach in both the reducer and the Linear sink, and a
    third copy of the rule is how a mislabel becomes a divergence.

    Legacy payloads carry only the joined ``ceiling`` string and no
    condition; for them the single-value test is still the only reading
    available, and it was correct for everything written back then.
    """
    if condition == "unenforceable":
        return "unenforceable"
    if ceilings:
        return "cost" if tuple(ceilings) == ("max_cost_usd",) else "token"
    return "cost" if ceiling == "max_cost_usd" else "token"


@dataclass(frozen=True, kw_only=True)
class ContractResult(Event):
    type: ClassVar[str] = "contract_result"
    tier: int = 0
    passed: bool = False
    breaker: str | None = None
    duration_seconds: float = 0.0


@dataclass(frozen=True, kw_only=True)
class RunCompleted(Event):
    type: ClassVar[str] = "factory_completed"
    completed: int = 0
    failed: int = 0
    skipped: int = 0
    duration_seconds: float = 0.0
    #: R8.7 slice 1. The commit this run's merges produced, the rule
    #: that chose it, and the reason no release followed. Recorded on
    #: EVERY run: ``release_withheld`` is never "" for a run the
    #: factory drove, so a reader can always tell why.
    release_ref: str = ""
    release_ref_rule: str = ""
    release_withheld: str = ""


@dataclass(frozen=True, kw_only=True)
class MergePendingV1(Event):
    """v1-parity twin of :class:`PrMergePending` (kept so the compat
    file's ``merge_pending`` line survives unchanged; the reducer
    prefers the v2 event)."""

    type: ClassVar[str] = "merge_pending"
    pr_url: str = ""
    error: str = ""


@dataclass(frozen=True, kw_only=True)
class PhaseSkipped(Event):
    type: ClassVar[str] = "phase_skipped"
    phase: str = ""
    reason: str = ""


@dataclass(frozen=True, kw_only=True)
class DiffFetchFailed(Event):
    type: ClassVar[str] = "diff_fetch_failed"
    error: str = ""


@dataclass(frozen=True, kw_only=True)
class ReviewDivergence(Event):
    """#265: the retry loop was growing the change without the review
    retiring a single one of its blocking findings. Parallel series, one
    entry per attempt in the window that tripped it."""

    type: ClassVar[str] = "review_divergence"
    attempts: tuple[int, ...] = ()
    #: Lines ADDED PLUS REMOVED against the base, per attempt.
    lines_changed: tuple[int, ...] = ()
    files_changed: tuple[int, ...] = ()
    #: ``ReviewResult.fail_count`` per attempt, so this joins against
    #: ``ReviewResultEvent.fail_count`` rather than disagreeing with it.
    blocking_findings: tuple[int, ...] = ()
    #: Whether this trip actually failed the component. False in
    #: advisory mode, where it was recorded and the component retried.
    #: Named apart from ``blocking_findings`` above, which counts the
    #: reviewer's findings and is a different sense of the word.
    blocked: bool = False


@dataclass(frozen=True, kw_only=True)
class AdversarialAgentSelected(Event):
    """agent_type/model stay optional (None, not ""): the v1 line wrote
    JSON null for unset values and byte parity is this chunk's contract."""

    type: ClassVar[str] = "adversarial_agent_selected"
    phase: str = ""
    agent_source: str = ""
    identity: str = ""
    agent_type: str | None = None
    model: str | None = None
    homogeneous: bool = False


# ---------------------------------------------------------------------------
# v2-only events (dropped by V1CompatSink; the TUI/reducer's real signal)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class RunPlan(Event):
    """The component DAG plus budget caps, emitted right after
    ``factory_started`` so consumers need no manifest read to draw the
    board. ``components`` entries: {"id", "title", "deps": [...]}."""

    type: ClassVar[str] = "run_plan"
    components: tuple[Mapping[str, Any], ...] = ()
    max_total_tokens: int = 0
    max_adversarial_calls: int = 0
    max_cost_usd: float = 0.0


@dataclass(frozen=True, kw_only=True)
class ComponentScopeResolved(Event):
    """One component's write scope, as resolved before any engineer ran.

    #269: both guards read a single plan-time snapshot
    (``scope.ComponentScope``) instead of each re-reading a PRD, so the
    scope decision is made once and has to be recorded once - otherwise
    the only way to answer "why was this component allowed to write
    that?" after the run is to re-derive a value from files the run has
    since changed. ``scope_source`` says which authority supplied the
    list (the component PRD, the run-wide flag, or nothing) and
    ``origin`` names the file or flag it came from.

    ``scope_source``, not ``source``: that name is an ENVELOPE field on
    every event, so a payload field sharing it is overwritten by the bus
    at emit and dropped from the serialised record.
    """

    type: ClassVar[str] = "component_scope_resolved"
    scope_source: str = ""
    origin: str = ""
    allowed_paths: tuple[str, ...] = ()
    harness_paths: tuple[str, ...] = ()
    error: str = ""
    #: The component's manifest ``status`` when the run resolved scopes,
    #: before crash recovery and before anything was scheduled (#448).
    #: The run writes no other event for a component it does not
    #: schedule, so this is how a surface that folds only this run's
    #: events tells "completed by an earlier run" from "not started".
    #: Empty in a log written before #448.
    manifest_status: str = ""


@dataclass(frozen=True, kw_only=True)
class PhaseStarted(Event):
    type: ClassVar[str] = "phase_started"
    phase: str = ""
    attempt: int = 0


@dataclass(frozen=True, kw_only=True)
class PhaseCompleted(Event):
    type: ClassVar[str] = "phase_completed"
    phase: str = ""
    passed: bool = False
    detail: str = ""
    duration_seconds: float = 0.0


@dataclass(frozen=True, kw_only=True)
class IterationStarted(Event):
    type: ClassVar[str] = "iteration_started"
    iteration: int = 0
    max_iterations: int = 0


@dataclass(frozen=True, kw_only=True)
class IterationCompleted(Event):
    type: ClassVar[str] = "iteration_completed"
    iteration: int = 0
    duration_seconds: float = 0.0
    completed: bool = False
    timed_out: bool = False
    #: #233: the gates ``[verify] fast_iteration_checks`` ran after this
    #: iteration that failed. Empty when they all passed, when none are
    #: configured, and when the iteration completed or was killed, in
    #: which case they did not run.
    fast_checks_failed: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class WorkerHeartbeat(Event):
    type: ClassVar[str] = "worker_heartbeat"
    pid: int = 0
    elapsed_seconds: float = 0.0


@dataclass(frozen=True, kw_only=True)
class CheckpointRequested(Event):
    type: ClassVar[str] = "checkpoint_requested"
    kind: str = ""
    question: str = ""


@dataclass(frozen=True, kw_only=True)
class CheckpointResolved(Event):
    """``decided_by``: "auto" (non-interactive default), "operator", or
    "inbox" (parked - an unanswered or out-of-range gate, #594)."""

    type: ClassVar[str] = "checkpoint_resolved"
    kind: str = ""
    decision: str = ""
    decided_by: str = ""


@dataclass(frozen=True, kw_only=True)
class PrCreated(Event):
    type: ClassVar[str] = "pr_created"
    pr_number: int = 0
    pr_url: str = ""


@dataclass(frozen=True, kw_only=True)
class PrMerged(Event):
    type: ClassVar[str] = "pr_merged"
    pr_number: int = 0
    pr_url: str = ""
    merge_sha: str = ""


@dataclass(frozen=True, kw_only=True)
class PrMergePending(Event):
    type: ClassVar[str] = "pr_merge_pending"
    pr_url: str = ""
    error: str = ""


@dataclass(frozen=True, kw_only=True)
class DistillResult(Event):
    """Pre-PR knowledge distillation outcome: the knowledge layer's
    WRITE side, for one component.

    The read side - did the engineer use the facts it was given - is
    :class:`FactUtilizationMeasured`, deliberately a separate event.
    Carrying utilization here too would duplicate one measurement
    across two events and invite a consumer to double count, and this
    event is only emitted when distillation actually ran.
    """

    type: ClassVar[str] = "distill_result"
    facts_written: int = 0
    duration_seconds: float = 0.0
    #: #495: True when the distiller's reply did not parse. A reply that
    #: parsed to an empty facts list is False: the two used to share one
    #: status. Read by ``kstrl.distill_readiness`` for ``ks evolve``.
    parse_failed: bool = False


@dataclass(frozen=True, kw_only=True)
class FactUtilizationMeasured(Event):
    """Did the engineer reference the facts injected into its prompt?

    Emitted once per component per attempt, on EVERY path the pipeline
    can take once a diff is obtainable - including components that go
    on to fail review or security, and including the paths where
    distillation is skipped or raises. That is the point of it being
    its own event: `evolution.jsonl` and `events.jsonl` must carry the
    same population, and the earlier design emitted only from a
    successful distill.

    ``measured`` must be read FIRST. False means we could not measure -
    it does NOT mean the engineer referenced nothing, and ``reason``
    says which it was. A measured ``referenced=0`` IS evidence, that
    injected facts went unused.

    The counts are matched against ADDED diff lines and the progress
    log only, so deleting the code that expressed a fact does not score
    as referencing it. Per-tier counts can sum to less than the totals;
    read ``core_*`` for the ratio that is about the component actually
    being built, since the sibling tier inflates the denominator.
    """

    type: ClassVar[str] = "fact_utilization_measured"
    measured: bool = False
    injected: int = 0
    referenced: int = 0
    reason: str = ""
    core_injected: int = 0
    core_referenced: int = 0
    dependency_injected: int = 0
    dependency_referenced: int = 0
    sibling_injected: int = 0
    sibling_referenced: int = 0


@dataclass(frozen=True, kw_only=True)
class FindingRecorded(Event):
    """One typed adversarial finding, streamed as it is recorded."""

    type: ClassVar[str] = "finding_recorded"
    phase: str = ""
    category: str = ""
    severity: str = ""
    location: str = ""
    explanation: str = ""
    attempt: int = 0
    # R7.1 attribution: the reviewing model identity ("codex (gpt-5)"),
    # extracted from the finding's model: tag; "" when no reviewer ran.
    model: str = ""


@dataclass(frozen=True, kw_only=True)
class SpecIssueRecorded(Event):
    """One architect spec issue, streamed as decompose parses it.

    Mirrors decompose.SpecIssue; ``kind`` is the architect's issue
    taxonomy (ambiguity, contradiction, ...), not a run kind.
    """

    type: ClassVar[str] = "spec_issue_recorded"
    severity: str = ""  # blocker | major | minor
    kind: str = ""
    summary: str = ""
    location: str = ""
    suggestion: str = ""


@dataclass(frozen=True, kw_only=True)
class ArtifactWritten(Event):
    """A durable output landed on disk (prd, manifest, spec-issues,
    codebase map...). ``path`` is root-relative when possible."""

    type: ClassVar[str] = "artifact_written"
    label: str = ""
    path: str = ""


@dataclass(frozen=True, kw_only=True)
class AutonomyTransition(Event):
    """R8.2: the autonomy level changed (promotion or demotion).

    ``direction``: promote | demote. ``actor`` is the human who
    acknowledged a promotion, or "system" for an automatic demotion;
    ``trigger`` carries the DemotionTrigger label on demotions only.
    Emitted for every transition so the level in force at any past moment
    is reconstructable from the event stream alone.
    """

    type: ClassVar[str] = "autonomy_transition"
    direction: str = ""
    from_level: int = 0
    to_level: int = 0
    actor: str = ""
    trigger: str = ""
    reason: str = ""


@dataclass(frozen=True, kw_only=True)
class AutonomyLevelApplied(Event):
    """R8.2: the flag bundle a run started under.

    Recorded at run start so a run's permissions are auditable after the
    fact even if the stored level later changes.

    ``flags`` describes the bundle the run USES, not the one the level
    awarded. The two differ in one case (#195): an explicit
    ``pause_before_pr_merge = true`` keeps the merge gate at L3 and L4,
    and a ``flags`` taken from the level alone would record "merge gate:
    off" for a run that pauses at every component.

    ``overrides`` names any config flag that contradicted the bundle, and
    says which won: "bundle wins" for the ones it overruled, "gate
    retained by explicit request" for the one it may not.
    """

    type: ClassVar[str] = "autonomy_level_applied"
    level: int = 0
    label: str = ""
    flags: tuple[str, ...] = ()
    overrides: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class JournalRepaired(Event):
    """#331: this file's tail was not newline-terminated when a sink
    opened it, so a crash interrupted a write, and a newline was written
    before this event to stop the unterminated tail swallowing it.

    A REGISTERED event rather than a raw appended line, which is the
    whole reason this class exists. A raw line decodes to
    ``UnknownEvent`` and is counted in ``RunState.unknown_events``, and
    "this build does not understand it" is false for a row this build
    wrote deliberately. As a registered type it is understood and
    inert: ``reducer.apply`` falls through every isinstance branch for
    it and advances only the clock, so no component, phase or count
    moves.

    The envelope is empty (``run_id`` "", ``source`` at its default).
    The sink sits BELOW the bus that stamps those fields, and it repairs
    at open time, before the run has told it anything.
    """

    type: ClassVar[str] = JOURNAL_REPAIR_EVENT
    detail: str = ""


@dataclass(frozen=True, kw_only=True)
class Log(Event):
    """The escape hatch for imperative narration (the old UI protocol).

    ``kind``: line | kv | section | subsection | title | hr | channel |
    stream | startup_art. ``key`` carries the kv key / channel name /
    stream tag; ``severity``: info | ok | warn | error.
    """

    type: ClassVar[str] = "log"
    severity: str = "info"
    kind: str = "line"
    key: str = ""
    text: str = ""
