"""Schema-v2 typed event model for kstrl runs (TUI rewrite, stage 1).

The filesystem is the event bus: the orchestrator and its workers append
one JSON object per line to files under ``.kstrl/runs/<run_id>/`` and
every surface (plain line output, the Textual TUI, ``ks status``) is
a projection of that stream. The vocabulary (the :class:`Event`
dataclasses) is in :mod:`kstrl.event_catalog`. This module owns the sinks
that write them, the tolerant reader that parses them back, and the
run-directory layout.

Envelope (one JSON object per line)::

    {"schema": 2, "event": "<type>", "ts": <float epoch seconds>,
     "run_id": "...", "component": "...", "source": "orchestrator|worker",
     "seq": <int>, "kstrl_version": "...", "data": {<payload fields>}}

Envelope fields are stamped by :meth:`EventBus.emit`, never by call
sites. Decoding is TOTAL: every payload field has a default, unknown
event names become :class:`UnknownEvent` (losslessly re-serializable),
mistyped payload values degrade to the field default, and a torn tail
line parses to ``None``. Sinks are observability, never control flow:
:class:`EventBus` isolates sink exceptions and counts drops.

Naming note: v1 compatibility (``.kstrl/progress.jsonl``) is provided by
:class:`V1CompatSink`, which delegates to a real
:class:`~kstrl.observability.ProgressLog` so its file format AND its
attached ``ProgressSink`` observers (e.g. the Linear sink, R7.4) keep
working unchanged.
"""

from __future__ import annotations

import dataclasses
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Protocol

from kstrl.appendio import (
    REPAIR_DETAIL,
    append_terminated,
    open_for_append,
)
from kstrl.event_catalog import (
    _ENVELOPE_FIELDS,
    _REGISTRY,
    AdversarialAgentSelected,
    BudgetCoverage,
    BudgetExceeded,
    CircuitBreakerTripped,
    ComponentCompleted,
    ComponentFailed,
    ComponentRetrying,
    ComponentStarted,
    ComponentUsage,
    ContractResult,
    DiffFetchFailed,
    Event,
    JournalRepaired,
    MergePendingV1,
    PhaseSkipped,
    ReviewResultEvent,
    RunCompleted,
    RunStarted,
    UnknownEvent,
    VerificationResultEvent,
)
from kstrl.jsonread import read_json
from kstrl.observability import ProgressLog
from kstrl.version import kstrl_version

_FIELD_DEFAULTS: dict[str, dict[str, Any]] = {}


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
        "kstrl_version": (
            obj.get("kstrl_version") if isinstance(obj.get("kstrl_version"), str) else ""
        ),
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
        obj = read_json(stripped)
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
    a lock so heartbeat threads can share it with the main thread.

    #331: the handle is opened ``"a+b"`` and the tail is probed ONCE,
    at the first emit. Without that probe a crash mid-write cost the
    next event as well as the torn one, measured through ``read_events``
    and ``reducer.fold``: ``['factory_started']`` where two events were
    written, and no components at all in the folded state.

    Once per sink, not once per event, is the reason
    ``handle_ends_without_newline`` takes a HANDLE. This sink holds one
    open for a whole run; re-probing would pay a seek and a read on
    every event and would write a second repair row for a tear it had
    already repaired.

    No lock on the file. One process owns each of these files: the
    orchestrator owns ``events.jsonl`` and each worker owns its own
    ``engineer.jsonl``. The threading lock below is about threads
    sharing this object, which is a different question and predates
    this change.

    THE ``"a+b"`` WIDENING, and it is the QUIETEST of the three sites
    that took it, so it is stated here rather than left to the module
    that opens the handle. A file this process can write but not read
    can no longer be appended to: the open raises. This sink is reached
    through ``EventBus.emit``, which catches every exception per sink
    and increments ``dropped``, and ``dropped`` has no production
    reader. So on a mode-0200 ``events.jsonl`` the whole stream is lost
    with no message on any surface, where 568bca4 wrote it. The same is
    true of ``progress.jsonl``, which the factory reaches through a
    ``V1CompatSink`` on the same bus, so both file streams of a run go
    quiet together.

    That was decided rather than overlooked. Reaching it needs a
    deliberate ``chmod 0200`` or an ACL on a file kstrl created itself
    at the umask default; the alternative to the widening is the
    fail-OPEN shape #327 round 1 found, where an unreadable file was
    reported as "not torn" and appended to blind. A warning on the
    first drop was considered and left out: ``events`` has no logger,
    the bus has no UI, and the surface it would reach is
    ``orchestrator.log``, which is the surface #333 exists because
    nobody reads. Giving ``dropped`` a real reader is the fix, and it
    is a change to the run summary rather than to this sink.
    """

    def __init__(self, path: Path, *, mkdir: bool = True) -> None:
        self.path = path
        if mkdir:
            path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._fh: IO[bytes] | None = None

    def emit(self, event: Event) -> None:
        line = event.to_json_line() + "\n"
        with self._lock:
            if self._fh is None:
                self._fh = self._probe_and_write(line)
            else:
                # Every later emit writes straight through. The probe is
                # not repeated: this handle has been at the end of the
                # file since the first emit, so nothing can have torn it
                # without this process being dead.
                self._fh.write(line.encode("utf-8"))
                self._fh.flush()

    def _probe_and_write(self, line: str) -> IO[bytes]:
        """Open, probe and write the first line, returning the bound handle.

        The handle is returned rather than assigned, and that is the
        whole point of the method: :meth:`emit` binds ``self._fh`` only
        after this returns, so a first write that RAISES leaves the sink
        unbound and the next emit probes again.

        THE FLUSH IS IN HERE, and that is not tidiness (#352 round 2,
        F2). ``open_for_append`` returns a ``BufferedRandom`` with an
        8 KiB buffer and an event line is smaller than that: measured, a
        121-byte write leaves 0 bytes on disk until the flush. So an
        out-of-space error surfaces at the FLUSH and not at the write,
        and while the flush sat one line below the binding in
        :meth:`emit`, the ``ENOSPC`` this docstring names as its own
        example was the one case that still left the sink bound, in the
        no-probe branch, on an unterminated tail. Everything the first
        emit has to get right is under the one ``try`` now.

        THIS flush is the mechanism; the ``else`` scoping of the other
        one is not, and that is measured rather than assumed. Moving
        ``emit``'s flush back out of the ``else`` leaves the sink
        unbound and the probe repeated, because this one already ran:
        that mutation is green and benign. Deleting THIS line binds the
        sink and drops the second probe. The ``else`` earns its place by
        not flushing an already-flushed handle, which is a smaller
        claim.

        Assigning first was a real hole. ``EventBus.emit`` catches every
        exception per sink and increments ``dropped``, which nothing in
        the product reads, so a first emit that hit ``ENOSPC`` on the
        write or ``EIO`` on the probe's read was silent; the sink then
        took the no-probe branch for the rest of the run and wrote
        straight onto the unterminated tail. Measured on a torn
        ``events.jsonl`` with the first write made to raise:
        ``read_events`` returned NOTHING, because the concatenated line
        does not parse at all.

        The handle is closed on the way out. Leaking it would hold a
        descriptor for the life of the run for a file this sink is
        about to reopen on the next emit.
        """
        handle = open_for_append(self.path)
        try:
            append_terminated(
                handle,
                line,
                repair=JournalRepaired(detail=REPAIR_DETAIL).to_json_line() + "\n",
            )
            handle.flush()
        except BaseException:
            handle.close()
            raise
        return handle

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

    KNOWN LIMITATION, v1 has no room for it (#288 review round 2).
    ``VerificationResultEvent`` gained ``phase`` and ``advisory``; the
    v1 ``ProgressLog.verification_result`` signature has neither, and
    widening it would change the progress.jsonl line format this sink
    exists to hold still. So a v1 reader sees an ADVISORY report and a
    GATE verdict as the same row: ``summarize_events`` cannot tell them
    apart, and ``_phase_for_event`` reports the component in phase
    "verify", a phase `ks feature` does not have.

    ``not_measured`` (#306) is dropped here for the same reason, and a
    v1 reader is not told which enabled check measured nothing.
    ``gate_logs`` (#462) is dropped for the same reason: a v1 reader is not
    told where a failed gate's output was written, though the file is.

    Nothing is wrong today, because `ks feature` is the only command
    that emits advisory reports and it attaches no ``V1CompatSink``. The
    moment a command does both, forward both fields, which means a v2
    progress-log format rather than an edit here.
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
                comp,
                event.passed,
                list(event.checks),
                list(event.failures),
                event.duration_seconds,
            )
        elif isinstance(event, ReviewResultEvent):
            log.review_result(
                comp,
                event.passed,
                event.mode,
                event.fail_count,
                event.advisory_count,
                event.duration_seconds,
            )
        elif isinstance(event, ComponentUsage):
            log.component_usage(
                comp,
                event.phase,
                {
                    "calls": event.calls,
                    "known_calls": event.known_calls,
                    "token_calls": event.token_calls,
                    "cost_calls": event.cost_calls,
                    "unreported_calls": event.unreported_calls,
                    "input_tokens": event.input_tokens,
                    "output_tokens": event.output_tokens,
                    "cache_read_tokens": event.cache_read_tokens,
                    "cache_creation_tokens": event.cache_creation_tokens,
                    "total_tokens": event.total_tokens,
                    "cost_usd": event.cost_usd,
                    "duration_seconds": event.duration_seconds,
                },
            )
        elif isinstance(event, BudgetExceeded):
            log.budget_exceeded(
                comp,
                event.total_tokens,
                event.max_total_tokens,
                cost_usd=event.cost_usd,
                max_cost_usd=event.max_cost_usd,
                ceiling=event.ceiling,
                condition=event.condition,
                ceilings=event.ceilings,
                coverage=event.coverage,
            )
        elif isinstance(event, BudgetCoverage):
            # Mirrored rather than left v2-only: a ceiling that counts
            # only part of the run is a BUDGET fact, and every other
            # budget fact reaches progress.jsonl (and through it the
            # v1 `ks status` arm and the Linear ProgressSink).
            log.budget_coverage(
                ceiling=event.ceiling,
                axis=event.axis,
                calls=event.calls,
                covered_calls=event.covered_calls,
                uncovered_calls=event.uncovered_calls,
                uncovered_tokens=event.uncovered_tokens,
                uncovered_roles=event.uncovered_roles,
                detail=event.detail,
            )
        elif isinstance(event, ContractResult):
            log.contract_result(
                event.tier,
                event.passed,
                event.breaker,
                event.duration_seconds,
            )
        elif isinstance(event, RunCompleted):
            log.factory_completed(
                event.completed,
                event.failed,
                event.skipped,
                event.duration_seconds,
            )
        elif isinstance(event, MergePendingV1):
            log.emit(
                "merge_pending",
                comp,
                {
                    "pr_url": event.pr_url,
                    "error": event.error,
                },
            )
        elif isinstance(event, PhaseSkipped):
            log.emit(
                "phase_skipped",
                comp,
                {
                    "phase": event.phase,
                    "reason": event.reason,
                },
            )
        elif isinstance(event, DiffFetchFailed):
            log.emit("diff_fetch_failed", comp, {"error": event.error})
        elif isinstance(event, AdversarialAgentSelected):
            log.emit(
                "adversarial_agent_selected",
                data={
                    "phase": event.phase,
                    "source": event.agent_source,
                    "identity": event.identity,
                    "agent_type": event.agent_type,
                    "model": event.model,
                    "homogeneous": event.homogeneous,
                },
            )
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
            kstrl_version=kstrl_version(),
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

    root: Path  # <project>/.kstrl/runs/<run_id>

    @classmethod
    def for_run(cls, project_root: Path, run_id: str) -> RunPaths:
        return cls(root=project_root / ".kstrl" / "runs" / run_id)

    @property
    def events_file(self) -> Path:
        return self.root / "events.jsonl"

    def component_dir(self, component_id: str) -> Path:
        return self.root / "components" / component_id

    def engineer_events(self, component_id: str) -> Path:
        return self.component_dir(component_id) / "engineer.jsonl"

    def engineer_log(self, component_id: str) -> Path:
        return self.component_dir(component_id) / "engineer.log"

    def engineer_usage(self, component_id: str) -> Path:
        """Latest engineer-loop usage snapshot (R8).

        Deliberately NOT an event: the worker rewrites this file at every
        iteration boundary, and the reducer sums ``component_usage``
        events, so streaming cumulative snapshots would double count in
        every rollup. It exists so a worker killed by a shutdown does
        not take its spend with it - the parent reads it only for
        futures that never delivered a result.
        """
        return self.component_dir(component_id) / "engineer_usage.json"

    def phase_log(self, component_id: str, phase: str) -> Path:
        return self.component_dir(component_id) / f"{phase}.log"
