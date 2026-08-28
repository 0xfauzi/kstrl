"""Pure reducer: fold schema-v2 events into a renderable RunState.

Chunk 2 of the TUI rewrite. Every surface (plain lines, ``kstrl
status``, the Textual dashboard) renders from :class:`RunState`, which
is produced ONLY by folding events - never by ad-hoc file peeking. The
manifest remains the authoritative snapshot for DAG/PR/evidence joins;
this module owns the temporal view.

Two entry points:

- :func:`fold` - pure: events in, fresh ``RunState`` out.
- :func:`apply` - one incremental step, for tail-follow consumers.
  ``fold(events)`` is definitionally ``apply`` over each event in order
  (a property the tests enforce by splitting streams at random offsets).

v1 compatibility: :func:`upconvert_v1` lifts a ``progress.jsonl``
envelope dict into a typed event so the same reducer serves both
layouts. Phase is authoritative when ``phase_started`` events exist and
falls back to the v1 inference heuristic (ported from
``observability._phase_for_event``) otherwise.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kstrl import events as ev
from kstrl.agents.base import CEILING_AXES
from kstrl.observability import latest_run_id, parse_event_ts, read_progress_events

# Bounded so a security-heavy run cannot bloat the state.
MAX_RECENT_FINDINGS = 50
MAX_SPEC_ISSUES = 100
MAX_ARTIFACTS = 100
SPEC_ISSUE_SEVERITIES = frozenset({"blocker", "major", "minor"})


@dataclass
class ComponentState:
    """Per-component temporal state, folded from events."""

    component_id: str
    title: str = ""
    deps: tuple[str, ...] = ()
    # pending | running | verifying | completed | merge_pending | failed | skipped
    status: str = "pending"
    phase: str = ""
    phase_explicit: bool = False  # a phase_started was seen; inference stops
    attempt: int = 0
    iteration: int = 0
    max_iterations: int = 0
    last_event: str = ""
    last_event_ts: float = 0.0
    last_heartbeat_ts: float = 0.0
    usage_calls: int = 0
    unreported_calls: int = 0
    # Per-axis coverage (R8 review finding 1). Kept beside the totals
    # they qualify: a component's cost_usd means something different
    # when cost_calls < usage_calls.
    token_calls: int = 0
    cost_calls: int = 0
    coverage_unknown_calls: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    findings_count: int = 0
    recent_findings: list[dict[str, Any]] = field(default_factory=list)
    # Completed-phase history for the detail screen's timeline:
    # {"phase", "passed", "detail", "duration_seconds", "attempt"}.
    phase_history: list[dict[str, Any]] = field(default_factory=list)
    pr_url: str = ""
    pr_number: int = 0
    pr_state: str = ""  # "" | created | merge_pending | merged
    checkpoint_open: str = ""  # kind of the unresolved checkpoint_requested
    error: str = ""

    @property
    def tokens_are_lower_bound(self) -> bool:
        """This component's ``total_tokens`` omits a call's spend."""
        return (self.usage_calls - self.coverage_unknown_calls - self.token_calls) > 0

    @property
    def cost_is_lower_bound(self) -> bool:
        """This component's ``cost_usd`` omits a call's spend.

        Per axis, like the run-level twin: a component whose engineer
        reported cost and whose reviewer reported only tokens has an
        exact token total and a partial cost one.
        """
        return (self.usage_calls - self.coverage_unknown_calls - self.cost_calls) > 0


@dataclass(frozen=True)
class CoverageGap:
    """One ceiling that stopped covering every metered call.

    Folded from :class:`events.BudgetCoverage`, which is run-scoped: the
    gap belongs to the run's adapters, not to whichever component
    happened to expose it. ``uncovered_tokens`` stays a TOKEN count on
    this surface too - the dashboard has no price table either, and a
    dollar figure invented for display is still an invented figure.
    """

    ceiling: str = ""
    axis: str = ""
    calls: int = 0
    covered_calls: int = 0
    uncovered_calls: int = 0
    uncovered_tokens: int = 0
    uncovered_roles: tuple[str, ...] = ()
    detail: str = ""


@dataclass
class RunState:
    """Run-level temporal state; the TUI's single source of truth."""

    run_id: str = ""
    project: str = ""
    started_ts: float = 0.0
    last_event_ts: float = 0.0
    finished: bool = False
    plan_order: list[str] = field(default_factory=list)
    components: dict[str, ComponentState] = field(default_factory=dict)
    # Run-level rollup of every component_usage event. R3.1 semantics,
    # narrowed per axis by R8: each total is a lower bound whenever some
    # call did not report THAT figure (see tokens_are_lower_bound /
    # cost_is_lower_bound), which unreported_calls alone cannot say.
    usage_calls: int = 0
    unreported_calls: int = 0
    # Per-axis coverage (R8 review finding 1). ``unreported_calls`` fires
    # only when a call reported NOTHING, so it stayed at 0 on the
    # measured run whose cross-family reviewer reported tokens and no
    # cost - the run the whole PARTIAL-coverage concept exists for. The
    # dashboard cannot mark a total as a lower bound with a signal that
    # is blind to the case.
    token_calls: int = 0
    cost_calls: int = 0
    #: Calls from a usage payload written before the per-axis fields
    #: existed. ``known_calls > 0`` with both axis counts at 0 is
    #: impossible in a post-R8 payload (a known call reported tokens or
    #: cost), so it identifies a legacy one. Those calls are coverage-
    #: UNKNOWN, not coverage-MISSING: claiming a gap that was never
    #: measured is the same class of false statement as hiding one.
    coverage_unknown_calls: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    # Budget caps from run_plan (0 = unbounded/unknown).
    max_total_tokens: int = 0
    max_adversarial_calls: int = 0
    max_cost_usd: float = 0.0
    # Coverage gaps announced by the pipeline, keyed by axis (one
    # ceiling per axis). Corroborates - and names the roles behind - what
    # the per-axis call counts already imply.
    coverage_gaps: dict[str, CoverageGap] = field(default_factory=dict)
    unknown_events: int = 0
    # Decompose vocabulary (run-scoped). Counts are complete; the lists
    # are FIFO-bounded so a pathological stream cannot grow state.
    spec_issue_counts: dict[str, int] = field(default_factory=dict)
    spec_issues: list[dict[str, str]] = field(default_factory=list)
    # {"label", "path", "component"} per artifact_written.
    artifacts: list[dict[str, str]] = field(default_factory=list)

    @property
    def kind(self) -> str:
        """Command kind from the run-id prefix; kind-agnostic folds
        never set it, and pre-kinds ids default to factory."""
        from kstrl.runid import run_kind

        return run_kind(self.run_id) or "factory"

    def _axis_is_lower_bound(self, axis: str, covered_calls: int) -> bool:
        """Does this axis's total omit at least one call's spend?

        Two independent sources, either of which is sufficient: the
        per-call arithmetic over the usage stream, and the pipeline's own
        ``budget_coverage`` announcement. The announcement is computed
        from the orchestrator's meter, so it can be ahead of what a
        tailing dashboard has folded; the arithmetic works on runs with
        no configured ceiling, where no announcement is ever emitted.

        Coverage-unknown calls (legacy payloads) are excluded from both
        sides of the arithmetic, so an old run dir renders exactly as it
        did before rather than growing a lower-bound marker nobody
        measured.
        """
        gap = self.coverage_gaps.get(axis)
        if gap is not None and gap.uncovered_calls > 0:
            return True
        uncovered = self.usage_calls - self.coverage_unknown_calls - covered_calls
        return uncovered > 0

    @property
    def tokens_are_lower_bound(self) -> bool:
        """``total_tokens`` omits at least one call's spend."""
        return self._axis_is_lower_bound("token", self.token_calls)

    @property
    def cost_is_lower_bound(self) -> bool:
        """``cost_usd`` omits at least one call's spend.

        Independent of :attr:`tokens_are_lower_bound` in both directions:
        on the measured run tokens were fully covered while cost was not,
        so a single shared marker would have been wrong on one axis
        whichever way it fired.
        """
        return self._axis_is_lower_bound("cost", self.cost_calls)


def _infer_phase(event: ev.Event) -> str | None:
    """v1 fallback, ported from observability._phase_for_event."""
    if isinstance(event, ev.ComponentStarted):
        return "engineer"
    if isinstance(event, ev.ComponentUsage):
        return event.phase or None
    if isinstance(event, ev.VerificationResultEvent):
        return "verify"
    if isinstance(event, ev.ReviewResultEvent):
        return "security" if event.mode.startswith("security") else "review"
    if isinstance(event, ev.ComponentRetrying):
        return "retrying"
    if isinstance(event, ev.ComponentFailed):
        return "failed"
    if isinstance(event, ev.ComponentCompleted):
        return "done"
    if isinstance(event, ev.BudgetExceeded):
        return "budget-halt"
    return None


def _component(state: RunState, component_id: str) -> ComponentState:
    comp = state.components.get(component_id)
    if comp is None:
        comp = ComponentState(component_id=component_id)
        state.components[component_id] = comp
    return comp


def apply(state: RunState, event: ev.Event) -> None:  # noqa: C901 - flat dispatch
    """Fold one event into ``state`` (mutates in place)."""
    if isinstance(event, ev.UnknownEvent):
        state.unknown_events += 1
        # Unknown events still move the run clock - a future emitter's
        # activity must not read as staleness.
        if event.ts:
            state.last_event_ts = max(state.last_event_ts, event.ts)
        return

    if event.ts:
        if not state.started_ts:
            state.started_ts = event.ts
        state.last_event_ts = max(state.last_event_ts, event.ts)
    if not state.run_id and event.run_id:
        state.run_id = event.run_id

    if isinstance(event, ev.RunStarted):
        state.project = event.project or state.project
        return
    if isinstance(event, ev.RunCompleted):
        state.finished = True
        return
    if isinstance(event, ev.RunPlan):
        state.max_total_tokens = event.max_total_tokens
        state.max_adversarial_calls = event.max_adversarial_calls
        state.max_cost_usd = event.max_cost_usd
        state.plan_order = []
        for entry in event.components:
            if not isinstance(entry, Mapping):
                continue
            cid = entry.get("id")
            if not isinstance(cid, str) or not cid:
                continue
            state.plan_order.append(cid)
            comp = _component(state, cid)
            title = entry.get("title")
            deps = entry.get("deps")
            if isinstance(title, str):
                comp.title = title
            if isinstance(deps, (list, tuple)):
                comp.deps = tuple(str(d) for d in deps)
        return
    if isinstance(event, ev.ContractResult):
        # Run-scoped in v1 (breaker only inside data); attribute it so
        # the board shows contract activity on the blamed component.
        if event.breaker:
            comp = _component(state, event.breaker)
            comp.last_event = type(event).type
            comp.last_event_ts = event.ts or comp.last_event_ts
            if not event.passed:
                comp.error = f"contract failed at tier {event.tier}"
        return

    if isinstance(event, ev.BudgetCoverage):
        # Run-scoped, so it MUST be handled above the
        # `if not event.component: return` guard below - that early
        # return is what dropped it before (R8 review finding 1).
        # Keyed by axis: one ceiling counts one axis, and the axis is
        # what every surface renders against. A ceiling-only legacy
        # payload still resolves through CEILING_AXES.
        axis = event.axis or CEILING_AXES.get(event.ceiling, "")
        state.coverage_gaps[axis] = CoverageGap(
            ceiling=event.ceiling,
            axis=axis,
            calls=event.calls,
            covered_calls=event.covered_calls,
            uncovered_calls=event.uncovered_calls,
            uncovered_tokens=event.uncovered_tokens,
            uncovered_roles=tuple(event.uncovered_roles),
            detail=event.detail,
        )
        return
    if isinstance(event, ev.SpecIssueRecorded):
        severity = event.severity if event.severity in SPEC_ISSUE_SEVERITIES else "unknown"
        state.spec_issue_counts[severity] = state.spec_issue_counts.get(severity, 0) + 1
        state.spec_issues.append(
            {
                "severity": severity,
                "kind": event.kind,
                "summary": event.summary,
                "location": event.location,
                "suggestion": event.suggestion,
            }
        )
        if len(state.spec_issues) > MAX_SPEC_ISSUES:
            del state.spec_issues[0]
        return
    if isinstance(event, ev.ArtifactWritten):
        # Run-scoped even when a component is stamped (per-component
        # PRDs): artifacts are a run-level record.
        state.artifacts.append(
            {
                "label": event.label,
                "path": event.path,
                "component": event.component,
            }
        )
        if len(state.artifacts) > MAX_ARTIFACTS:
            del state.artifacts[0]
        return

    if not event.component:
        return
    comp = _component(state, event.component)
    comp.last_event = type(event).type
    if event.ts:
        comp.last_event_ts = max(comp.last_event_ts, event.ts)

    if not comp.phase_explicit:
        inferred = _infer_phase(event)
        if inferred:
            comp.phase = inferred

    if isinstance(event, ev.ComponentStarted):
        comp.status = "running"
        comp.error = ""
    elif isinstance(event, ev.PhaseStarted):
        comp.phase_explicit = True
        comp.phase = event.phase
        comp.attempt = max(comp.attempt, event.attempt)
        if event.phase and event.phase != "engineer":
            if comp.status == "running":
                comp.status = "verifying"
    elif isinstance(event, ev.PhaseCompleted):
        comp.phase_explicit = True
        comp.phase_history.append(
            {
                "phase": event.phase,
                "passed": event.passed,
                "detail": event.detail,
                "duration_seconds": event.duration_seconds,
                "attempt": comp.attempt or 1,
            }
        )
        if not event.passed and event.detail:
            comp.error = event.detail
    elif isinstance(event, ev.ComponentCompleted):
        comp.status = "completed"
        comp.iteration = event.iterations or comp.iteration
        if comp.phase_explicit:
            comp.phase = "done"
    elif isinstance(event, ev.ComponentFailed):
        comp.status = "failed"
        comp.error = event.error
        if comp.phase_explicit:
            comp.phase = "failed"
    elif isinstance(event, ev.ComponentSkipped):
        comp.status = "skipped"
        comp.error = event.reason
        if comp.phase_explicit:
            comp.phase = "skipped"
    elif isinstance(event, ev.CircuitBreakerTripped):
        comp.error = event.error
    elif isinstance(event, ev.ComponentRetrying):
        comp.status = "running"
        comp.attempt = max(comp.attempt, event.attempt)
    elif isinstance(event, ev.IterationStarted):
        comp.iteration = event.iteration
        comp.max_iterations = event.max_iterations
    elif isinstance(event, ev.WorkerHeartbeat):
        comp.last_heartbeat_ts = max(comp.last_heartbeat_ts, event.ts)
    elif isinstance(event, ev.ComponentUsage):
        comp.usage_calls += event.calls
        comp.unreported_calls += event.unreported_calls
        comp.total_tokens += event.total_tokens
        comp.cost_usd += event.cost_usd
        state.usage_calls += event.calls
        state.unreported_calls += event.unreported_calls
        state.total_tokens += event.total_tokens
        state.cost_usd += event.cost_usd
        # Per-axis coverage (R8 review finding 1): dropping these left
        # the dashboard unable to tell a measured total from one that
        # counts a subset of the run's roles.
        comp.token_calls += event.token_calls
        comp.cost_calls += event.cost_calls
        state.token_calls += event.token_calls
        state.cost_calls += event.cost_calls
        legacy = event.known_calls > 0 and event.token_calls == 0 and event.cost_calls == 0
        if legacy:
            # See RunState.coverage_unknown_calls: a known call always
            # reported tokens or cost, so this shape can only be a
            # payload written before the axis fields existed.
            comp.coverage_unknown_calls += event.known_calls
            state.coverage_unknown_calls += event.known_calls
    elif isinstance(event, ev.FindingRecorded):
        comp.findings_count += 1
        comp.recent_findings.append(
            {
                "phase": event.phase,
                "category": event.category,
                "severity": event.severity,
                "location": event.location,
                "explanation": event.explanation,
                "attempt": event.attempt,
                "model": event.model,
            }
        )
        if len(comp.recent_findings) > MAX_RECENT_FINDINGS:
            del comp.recent_findings[0]
    elif isinstance(event, ev.PrCreated):
        comp.pr_url = event.pr_url or comp.pr_url
        comp.pr_number = event.pr_number or comp.pr_number
        comp.pr_state = "created"
    elif isinstance(event, ev.PrMerged):
        comp.pr_url = event.pr_url or comp.pr_url
        comp.pr_number = event.pr_number or comp.pr_number
        comp.pr_state = "merged"
    elif isinstance(event, ev.PrMergePending):
        comp.pr_url = event.pr_url or comp.pr_url
        comp.pr_state = "merge_pending"
        comp.status = "merge_pending"
        comp.error = event.error or comp.error
    elif isinstance(event, ev.MergePendingV1):
        # v1-parity twin: only act when the richer v2 event is absent
        # (dual-write emits both; this keeps v1-only logs informative).
        if comp.pr_state != "merge_pending":
            comp.pr_url = event.pr_url or comp.pr_url
            comp.pr_state = "merge_pending"
            comp.status = "merge_pending"
            comp.error = event.error or comp.error
    elif isinstance(event, ev.CheckpointRequested):
        comp.checkpoint_open = event.kind or "checkpoint"
    elif isinstance(event, ev.CheckpointResolved):
        comp.checkpoint_open = ""
    elif isinstance(event, ev.BudgetExceeded):
        # Names the ceiling that tripped, via the shared classifier so
        # this surface cannot drift from the Linear sink's reading of
        # the same payload. Payloads written before the cost ceiling
        # landed carry no ``ceiling`` and decode to "", in which case
        # the token wording is the only honest reading.
        kind = ev.budget_halt_kind(event.condition, event.ceilings, event.ceiling)
        if kind == "unenforceable":
            # No threshold was crossed, so there is no ">=" to state.
            # The old wording claimed one and printed the untouched
            # totals as evidence for it (review finding on #180).
            named = ", ".join(event.ceilings) or event.ceiling or "budget"
            comp.error = (
                f"budget ceiling unenforceable ({named}): no configured ceiling can still fire"
            )
        elif kind == "cost":
            comp.error = f"cost budget exceeded: ${event.cost_usd:.6f} >= ${event.max_cost_usd}"
        else:
            comp.error = f"token budget exceeded: {event.total_tokens} >= {event.max_total_tokens}"


def fold(events: Iterable[ev.Event], run_id: str = "") -> RunState:
    """Pure fold: fresh RunState from an event iterable.

    ``run_id`` non-empty filters to that run's events (events with an
    empty run_id always pass - pre-R3.2 v1 logs carry none).
    """
    state = RunState(run_id=run_id)
    for event in events:
        if run_id and event.run_id and event.run_id != run_id:
            continue
        apply(state, event)
    return state


# ---------------------------------------------------------------------------
# v1 up-conversion
# ---------------------------------------------------------------------------


def upconvert_v1(obj: Mapping[str, Any]) -> ev.Event:
    """Lift one v1 progress.jsonl envelope dict into a typed event.

    v1 envelope: ``{ts: iso-str, event, run_id?, component?, data?}``.
    Never raises; anything unliftable becomes UnknownEvent.
    """
    ts_raw = obj.get("ts")
    ts = 0.0
    if isinstance(ts_raw, str):
        parsed = parse_event_ts(ts_raw)
        if parsed is not None:
            ts = parsed.timestamp()
    data = obj.get("data")
    data_dict: dict[str, Any] = dict(data) if isinstance(data, Mapping) else {}
    name = obj.get("event")
    # Field renames between v1 data keys and v2 payload fields.
    if name == "adversarial_agent_selected" and "source" in data_dict:
        data_dict["agent_source"] = data_dict.pop("source")
    return ev.event_from_dict(
        {
            "event": name,
            "ts": ts,
            "run_id": obj.get("run_id") or "",
            "component": obj.get("component") or "",
            "source": "orchestrator",
            "seq": 0,
            "data": data_dict,
        }
    )


# ---------------------------------------------------------------------------
# Disk loading (v2 run dirs, v1 fallback)
# ---------------------------------------------------------------------------


def _sort_key(event: ev.Event) -> tuple[float, str, int]:
    return (event.ts, event.source, event.seq)


def _v2_run_dirs(root_dir: Path) -> list[Path]:
    """Run dirs that carry an event stream, oldest first (newest last).

    Tolerant by contract: ``load_run_state`` answers an unreadable
    ``runs/`` with "no runs" and falls back to the v1 log. A caller that
    must distinguish "no runs" from "could not look" wants
    :func:`run_dirs_newest_first` instead.
    """
    from kstrl.runid import run_sort_key

    try:
        return sorted(
            (d for d in _run_dirs_unsorted(root_dir) if (d / "events.jsonl").exists()),
            key=lambda d: run_sort_key(d.name),
        )
    except OSError:
        return []


def read_run_dir(run_dir: Path) -> list[ev.Event]:
    """All events of one v2 run dir (orchestrator + workers), sorted."""
    events = ev.read_events(run_dir / "events.jsonl")
    comp_root = run_dir / "components"
    if comp_root.is_dir():
        try:
            comp_dirs = sorted(comp_root.iterdir())
        except OSError:
            comp_dirs = []
        for comp_dir in comp_dirs:
            events.extend(ev.read_events(comp_dir / "engineer.jsonl"))
    events.sort(key=_sort_key)
    return events


def _run_dirs_unsorted(root_dir: Path) -> list[Path]:
    """Run directories in filesystem order, unsorted.

    Shared so the two orderings below cannot disagree about which
    directories exist. Raises on an unreadable ``runs/``; a missing one
    is not an error, it is "no runs".
    """
    from kstrl.statedir import state_dir

    runs_root = state_dir(root_dir) / "runs"
    if not runs_root.exists():
        return []
    return [d for d in runs_root.iterdir() if d.is_dir()]


def run_dirs_newest_first(root_dir: Path) -> list[Path]:
    """Every run directory under ``.kstrl/runs/``, newest first.

    Differs from :func:`_v2_run_dirs` in the two ways a caller asking
    "did a gate run" needs:

    - it includes a run that left no ``events.jsonl``. A run with
      ``[factory] progress_log_enabled = false`` writes its accounting
      files and no events at all (``factory.py``, the ``usage_paths``
      comment), so filtering on the stream would hide the newest run
      behind an older one.
    - it does not swallow a filesystem error. ``_v2_run_dirs`` answers
      an unreadable ``runs/`` with an empty list, which reads as "no
      runs" and therefore as "nothing was skipped".

    ``safemode`` is that caller. It also cannot use the folded
    ``ComponentState.recent_findings``, which is capped at
    :data:`MAX_RECENT_FINDINGS` and would lose a skip behind a noisy
    component.
    """
    from kstrl.runid import run_sort_key

    return sorted(
        _run_dirs_unsorted(root_dir),
        key=lambda d: run_sort_key(d.name),
        reverse=True,
    )


def load_run_state(
    root_dir: Path,
    run_id: str = "",
) -> tuple[RunState, Path | None]:
    """Reconstruct run state from disk.

    Resolution order:
    1. ``.kstrl/runs/<run_id>/`` (or the newest run dir when ``run_id``
       is empty) - the v2 layout, workers' engineer.jsonl merged in.
    2. ``.kstrl/progress.jsonl`` up-converted - the v1 fallback.

    Returns ``(state, source_path)``; ``source_path`` is None when no
    stream exists (state is then empty).
    """
    run_dirs = _v2_run_dirs(root_dir)
    if run_id:
        run_dirs = [d for d in run_dirs if d.name == run_id]
    if run_dirs:
        run_dir = run_dirs[-1]
        events = read_run_dir(run_dir)
        return fold(events, run_id=run_id), run_dir / "events.jsonl"

    v1_path = root_dir / ".kstrl" / "progress.jsonl"
    raw = read_progress_events(v1_path)
    if not raw:
        return RunState(run_id=run_id), None
    rid = run_id or latest_run_id(raw)
    typed = [upconvert_v1(e) for e in raw]
    return fold(typed, run_id=rid), v1_path
