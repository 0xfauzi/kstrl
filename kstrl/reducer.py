"""Pure reducer: fold schema-v2 events into a renderable RunState.

Chunk 2 of the TUI rewrite. Every surface (plain lines, ``ralph
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
    # pending | running | verifying | completed | merge_pending | failed
    # ("skipped" only ever comes from the manifest join - no event marks it)
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
    # Run-level rollup of every component_usage event (R3.1 semantics:
    # lower bounds whenever unreported_calls > 0).
    usage_calls: int = 0
    unreported_calls: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    # Budget caps from run_plan (0 = unbounded/unknown).
    max_total_tokens: int = 0
    max_adversarial_calls: int = 0
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

    if isinstance(event, ev.SpecIssueRecorded):
        severity = (
            event.severity
            if event.severity in SPEC_ISSUE_SEVERITIES
            else "unknown"
        )
        state.spec_issue_counts[severity] = (
            state.spec_issue_counts.get(severity, 0) + 1
        )
        state.spec_issues.append({
            "severity": severity,
            "kind": event.kind,
            "summary": event.summary,
            "location": event.location,
            "suggestion": event.suggestion,
        })
        if len(state.spec_issues) > MAX_SPEC_ISSUES:
            del state.spec_issues[0]
        return
    if isinstance(event, ev.ArtifactWritten):
        # Run-scoped even when a component is stamped (per-component
        # PRDs): artifacts are a run-level record.
        state.artifacts.append({
            "label": event.label,
            "path": event.path,
            "component": event.component,
        })
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
        comp.phase_history.append({
            "phase": event.phase,
            "passed": event.passed,
            "detail": event.detail,
            "duration_seconds": event.duration_seconds,
            "attempt": comp.attempt or 1,
        })
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
    elif isinstance(event, ev.FindingRecorded):
        comp.findings_count += 1
        comp.recent_findings.append({
            "phase": event.phase,
            "category": event.category,
            "severity": event.severity,
            "location": event.location,
            "explanation": event.explanation,
            "attempt": event.attempt,
            "model": event.model,
        })
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
        comp.error = (
            f"token budget exceeded: {event.total_tokens} >= "
            f"{event.max_total_tokens}"
        )


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
    return ev.event_from_dict({
        "event": name,
        "ts": ts,
        "run_id": obj.get("run_id") or "",
        "component": obj.get("component") or "",
        "source": "orchestrator",
        "seq": 0,
        "data": data_dict,
    })


# ---------------------------------------------------------------------------
# Disk loading (v2 run dirs, v1 fallback)
# ---------------------------------------------------------------------------


def _sort_key(event: ev.Event) -> tuple[float, str, int]:
    return (event.ts, event.source, event.seq)


def _v2_run_dirs(root_dir: Path) -> list[Path]:
    from kstrl.statedir import state_dir

    runs_root = state_dir(root_dir) / "runs"
    try:
        candidates = [
            d for d in runs_root.iterdir()
            if (d / "events.jsonl").exists()
        ]
    except OSError:
        return []
    # Sort by the stamp after the kind prefix (kstrl.runid) so
    # decompose-*/factory-* interleave chronologically; newest last.
    from kstrl.runid import run_sort_key

    return sorted(candidates, key=lambda d: run_sort_key(d.name))


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


def load_run_state(
    root_dir: Path, run_id: str = "",
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
