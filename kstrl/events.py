"""Schema-v2 typed event model for Ralph runs (TUI rewrite, stage 1).

The filesystem is the event bus: the orchestrator and its workers append
one JSON object per line to files under ``.ralph/runs/<run_id>/`` and
every surface (plain line output, the Textual TUI, ``ralph status``) is
a projection of that stream. This module owns the vocabulary: the
:class:`Event` dataclasses, the sinks that write them, the tolerant
reader that parses them back, and the run-directory layout.

Envelope (one JSON object per line)::

    {"schema": 2, "event": "<type>", "ts": <float epoch seconds>,
     "run_id": "...", "component": "...", "source": "orchestrator|worker",
     "seq": <int>, "data": {<payload fields>}}

Envelope fields are stamped by :meth:`EventBus.emit`, never by call
sites. Decoding is TOTAL: every payload field has a default, unknown
event names become :class:`UnknownEvent` (losslessly re-serializable),
mistyped payload values degrade to the field default, and a torn tail
line parses to ``None``. Sinks are observability, never control flow:
:class:`EventBus` isolates sink exceptions and counts drops.

Naming note: v1 compatibility (``.ralph/progress.jsonl``) is provided by
:class:`V1CompatSink`, which delegates to a real
:class:`~kstrl.observability.ProgressLog` so its file format AND its
attached ``ProgressSink`` observers (e.g. the Linear sink, R7.4) keep
working unchanged.
"""

from __future__ import annotations

import dataclasses
import json
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Final, Protocol, TextIO

from kstrl.observability import ProgressLog

SCHEMA_VERSION: Final = 2

_ENVELOPE_FIELDS: Final = frozenset({"ts", "run_id", "component", "source", "seq"})

_REGISTRY: dict[str, type[Event]] = {}
_FIELD_DEFAULTS: dict[str, dict[str, Any]] = {}


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
    type: ClassVar[str] = "verification_result"
    passed: bool = False
    checks: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()
    duration_seconds: float = 0.0


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
    whenever ``unreported_calls`` > 0."""

    type: ClassVar[str] = "component_usage"
    phase: str = ""
    calls: int = 0
    known_calls: int = 0
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
    type: ClassVar[str] = "budget_exceeded"
    total_tokens: int = 0
    max_total_tokens: int = 0


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
class DiffUnsplittable(Event):
    type: ClassVar[str] = "diff_unsplittable"
    error: str = ""
    diff_chars: int = 0


@dataclass(frozen=True, kw_only=True)
class DiffChunked(Event):
    type: ClassVar[str] = "diff_chunked"
    chunks: int = 0
    diff_chars: int = 0


@dataclass(frozen=True, kw_only=True)
class ChunkBudgetInsufficient(Event):
    type: ClassVar[str] = "chunk_budget_insufficient"
    phase: str = ""
    chunks: int = 0
    remaining: int = 0


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
    """``decided_by``: "auto" (non-interactive default) or "operator"."""

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


@dataclass(frozen=True, kw_only=True)
class PrMergePending(Event):
    type: ClassVar[str] = "pr_merge_pending"
    pr_url: str = ""
    error: str = ""


@dataclass(frozen=True, kw_only=True)
class DistillResult(Event):
    type: ClassVar[str] = "distill_result"
    facts_written: int = 0
    duration_seconds: float = 0.0


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


# ---------------------------------------------------------------------------
# Decoding (total: never raises)
# ---------------------------------------------------------------------------


def _field_defaults(cls: type[Event]) -> dict[str, Any]:
    cached = _FIELD_DEFAULTS.get(cls.type)
    if cached is not None:
        return cached
    defaults: dict[str, Any] = {}
    for f in dataclasses.fields(cls):
        if f.name in _ENVELOPE_FIELDS:
            continue
        if f.default is not dataclasses.MISSING:
            defaults[f.name] = f.default
        elif f.default_factory is not dataclasses.MISSING:
            defaults[f.name] = f.default_factory()
    _FIELD_DEFAULTS[cls.type] = defaults
    return defaults


def _coerce(default: Any, value: Any) -> Any:
    """Return a value type-compatible with ``default``, or the default.

    bool is checked before int (bool subclasses int); ints are accepted
    for float fields; lists become tuples for tuple fields.
    """
    if default is None:
        return value
    if isinstance(default, bool):
        return value if isinstance(value, bool) else default
    if isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return default
        return float(value)
    if isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, int):
            return default
        return value
    if isinstance(default, str):
        return value if isinstance(value, str) else default
    if isinstance(default, tuple):
        return tuple(value) if isinstance(value, (list, tuple)) else default
    return value


def _envelope_kwargs(obj: Mapping[str, Any]) -> dict[str, Any]:
    ts = obj.get("ts")
    seq = obj.get("seq")
    return {
        "ts": float(ts) if isinstance(ts, (int, float)) and not isinstance(ts, bool) else 0.0,
        "run_id": obj.get("run_id") if isinstance(obj.get("run_id"), str) else "",
        "component": obj.get("component") if isinstance(obj.get("component"), str) else "",
        "source": obj.get("source") if isinstance(obj.get("source"), str) else "orchestrator",
        "seq": seq if isinstance(seq, int) and not isinstance(seq, bool) else 0,
    }


def event_from_dict(obj: Mapping[str, Any]) -> Event:
    """Decode one envelope dict into a typed event. Never raises."""
    name = obj.get("event")
    cls = _REGISTRY.get(name) if isinstance(name, str) else None
    envelope = _envelope_kwargs(obj)
    if cls is None or cls is UnknownEvent:
        return UnknownEvent(
            type_name=name if isinstance(name, str) else "",
            raw=dict(obj),
            **envelope,
        )
    payload: dict[str, Any] = {}
    raw_data = obj.get("data")
    defaults = _field_defaults(cls)
    if isinstance(raw_data, Mapping):
        for fname, default in defaults.items():
            if fname in raw_data:
                payload[fname] = _coerce(default, raw_data[fname])
    try:
        return cls(**payload, **envelope)
    except Exception:  # noqa: BLE001 - decode is total by contract
        return UnknownEvent(
            type_name=name if isinstance(name, str) else "",
            raw=dict(obj),
            **envelope,
        )


def parse_event_line(line: str) -> Event | None:
    """One JSONL line -> Event, or None for torn/blank/non-dict lines."""
    stripped = line.strip()
    if not stripped:
        return None
    try:
        obj = json.loads(stripped)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    return event_from_dict(obj)


def read_events(path: Path) -> list[Event]:
    """Tolerant reader: skips torn/blank lines, returns [] for a missing
    file (mirrors ``observability.read_progress_events``)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return []
    events: list[Event] = []
    for line in lines:
        event = parse_event_line(line)
        if event is not None:
            events.append(event)
    return events


# ---------------------------------------------------------------------------
# Sinks and the bus
# ---------------------------------------------------------------------------


class EventSink(Protocol):
    """Destination for stamped events. Sinks must never raise into the
    run - EventBus isolates them anyway, but a sink should be cheap."""

    def emit(self, event: Event) -> None: ...

    def close(self) -> None: ...


class NullSink:
    def emit(self, event: Event) -> None:  # noqa: ARG002 - protocol
        return

    def close(self) -> None:
        return


class CallbackSink:
    """Wraps a callable; used for same-thread renderers and inline tees."""

    def __init__(self, callback: Callable[[Event], None]) -> None:
        self._callback = callback

    def emit(self, event: Event) -> None:
        self._callback(event)

    def close(self) -> None:
        return


class JsonlSink:
    """Append-only JSONL writer; one line per event, flushed, guarded by
    a lock so heartbeat threads can share it with the main thread."""

    def __init__(self, path: Path, *, mkdir: bool = True) -> None:
        self.path = path
        if mkdir:
            path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._fh: TextIO | None = None

    def emit(self, event: Event) -> None:
        line = event.to_json_line()
        with self._lock:
            if self._fh is None:
                self._fh = open(self.path, "a", encoding="utf-8")
            self._fh.write(line + "\n")
            self._fh.flush()

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.close()
                finally:
                    self._fh = None


class V1CompatSink:
    """Projects v1-named events onto a real :class:`ProgressLog`.

    Delegating (rather than re-serializing) keeps two contracts intact
    by construction: the progress.jsonl line format, and the R7.4
    ``ProgressSink`` observers attached to the log (e.g. Linear).
    v2-only events are dropped silently - that is the point.
    """

    def __init__(self, progress_log: ProgressLog) -> None:
        self._log = progress_log

    @property
    def path(self) -> Path:
        return self._log.path

    def emit(self, event: Event) -> None:  # noqa: C901 - flat dispatch table
        log = self._log
        comp = event.component
        if isinstance(event, RunStarted):
            log.factory_started(event.project, event.components)
        elif isinstance(event, ComponentStarted):
            log.component_started(comp)
        elif isinstance(event, ComponentCompleted):
            log.component_completed(comp, event.duration_seconds, event.iterations)
        elif isinstance(event, CircuitBreakerTripped):
            log.circuit_breaker_tripped(comp, event.iterations, event.error)
        elif isinstance(event, ComponentFailed):
            log.component_failed(comp, event.error)
        elif isinstance(event, ComponentRetrying):
            log.component_retrying(comp, event.attempt, event.reason)
        elif isinstance(event, VerificationResultEvent):
            log.verification_result(
                comp, event.passed, list(event.checks), list(event.failures),
                event.duration_seconds,
            )
        elif isinstance(event, ReviewResultEvent):
            log.review_result(
                comp, event.passed, event.mode, event.fail_count,
                event.advisory_count, event.duration_seconds,
            )
        elif isinstance(event, ComponentUsage):
            log.component_usage(comp, event.phase, {
                "calls": event.calls,
                "known_calls": event.known_calls,
                "unreported_calls": event.unreported_calls,
                "input_tokens": event.input_tokens,
                "output_tokens": event.output_tokens,
                "cache_read_tokens": event.cache_read_tokens,
                "cache_creation_tokens": event.cache_creation_tokens,
                "total_tokens": event.total_tokens,
                "cost_usd": event.cost_usd,
                "duration_seconds": event.duration_seconds,
            })
        elif isinstance(event, BudgetExceeded):
            log.budget_exceeded(comp, event.total_tokens, event.max_total_tokens)
        elif isinstance(event, ContractResult):
            log.contract_result(
                event.tier, event.passed, event.breaker, event.duration_seconds,
            )
        elif isinstance(event, RunCompleted):
            log.factory_completed(
                event.completed, event.failed, event.skipped,
                event.duration_seconds,
            )
        elif isinstance(event, MergePendingV1):
            log.emit("merge_pending", comp, {
                "pr_url": event.pr_url, "error": event.error,
            })
        elif isinstance(event, PhaseSkipped):
            log.emit("phase_skipped", comp, {
                "phase": event.phase, "reason": event.reason,
            })
        elif isinstance(event, DiffFetchFailed):
            log.emit("diff_fetch_failed", comp, {"error": event.error})
        elif isinstance(event, DiffUnsplittable):
            log.emit("diff_unsplittable", comp, {
                "error": event.error, "diff_chars": event.diff_chars,
            })
        elif isinstance(event, DiffChunked):
            log.emit("diff_chunked", comp, {
                "chunks": event.chunks, "diff_chars": event.diff_chars,
            })
        elif isinstance(event, ChunkBudgetInsufficient):
            log.emit("chunk_budget_insufficient", comp, {
                "phase": event.phase, "chunks": event.chunks,
                "remaining": event.remaining,
            })
        elif isinstance(event, AdversarialAgentSelected):
            log.emit("adversarial_agent_selected", data={
                "phase": event.phase,
                "source": event.agent_source,
                "identity": event.identity,
                "agent_type": event.agent_type,
                "model": event.model,
                "homogeneous": event.homogeneous,
            })
        # v2-only events: dropped by design.

    def close(self) -> None:
        return


class EventBus:
    """Stamps the envelope and fans out to sinks, isolating failures.

    ``run_id``/``component``/``source`` defaults fill empty envelope
    fields; ``seq`` is per-bus monotonic; ``ts`` is wall-clock at emit
    (the TUI's last-event-age depends on this being wall time).
    """

    def __init__(
        self,
        *sinks: EventSink,
        run_id: str = "",
        source: str = "orchestrator",
        component: str = "",
    ) -> None:
        self._sinks: list[EventSink] = list(sinks)
        self.run_id = run_id
        self.source = source
        self.component = component
        self.dropped = 0
        self._seq = 0
        self._lock = threading.Lock()

    def add_sink(self, sink: EventSink) -> None:
        self._sinks.append(sink)

    def remove_sink(self, sink: EventSink) -> None:
        """Detach one sink (does not close it). Lets a long-lived
        console bus shed a run's file sinks at run end without
        disturbing its renderer."""
        try:
            self._sinks.remove(sink)
        except ValueError:
            pass

    def emit(self, event: Event) -> Event:
        with self._lock:
            self._seq += 1
            seq = self._seq
        stamped = dataclasses.replace(
            event,
            ts=time.time(),
            run_id=event.run_id or self.run_id,
            component=event.component or self.component,
            source=self.source,
            seq=seq,
        )
        for sink in self._sinks:
            try:
                sink.emit(stamped)
            except Exception:  # noqa: BLE001 - observability never breaks the run
                self.dropped += 1
        return stamped

    def close(self) -> None:
        for sink in self._sinks:
            try:
                sink.close()
            except Exception:  # noqa: BLE001 - close is best-effort
                self.dropped += 1


# ---------------------------------------------------------------------------
# Run directory layout
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunPaths:
    """Canonical layout of one run's on-disk stream."""

    root: Path  # <project>/.ralph/runs/<run_id>

    @classmethod
    def for_run(cls, project_root: Path, run_id: str) -> RunPaths:
        return cls(root=project_root / ".ralph" / "runs" / run_id)

    @property
    def events_file(self) -> Path:
        return self.root / "events.jsonl"

    def component_dir(self, component_id: str) -> Path:
        return self.root / "components" / component_id

    def engineer_events(self, component_id: str) -> Path:
        return self.component_dir(component_id) / "engineer.jsonl"

    def engineer_log(self, component_id: str) -> Path:
        return self.component_dir(component_id) / "engineer.log"

    def phase_log(self, component_id: str, phase: str) -> Path:
        return self.component_dir(component_id) / f"{phase}.log"
