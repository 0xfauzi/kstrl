"""Home as an operator queue: what needs you, what is active, what shipped (#433).

Built on the home worker thread from what is already on disk, so nothing
here runs on the Textual event loop.

**Needs you** is the set of places where an operator action is what
blocks progress, and nothing else:

- an OPEN inbox item (a decision), one row each;
- a component the manifest records as FAILED for which a retry is
  available: ``retry_plan.preview_retry`` accepts it. That is the
  recovery the retry screen offers.

A failed RUN is not a needs-you row by itself. It is history, and one of:

- **superseded**: a later run of the same kind ran (not merely carried)
  one of the components it failed. The row names that successor.
- **current**: a component it failed is still FAILED in the manifest, so
  the failure already appears under needs you, as a component row.

**Active** is every run whose writer is alive (``RunRef.live``) and every
``ks serve`` item in flight or queued (``serve_view``). A running
component's row carries its agent health (``agent_health``).

**Delivery** is the newest finished factory run's integration review,
merges and main's CI state (``delivery``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from kstrl.tui.agent_health import agent_health
from kstrl.tui.delivery import Delivery, merges_of, read_delivery
from kstrl.tui.integration_view import review_files
from kstrl.tui.run_status import FAILED, failed_cause, run_reason
from kstrl.tui.serve_view import ServeState, read_serve_state
from kstrl.tui.theme import short_run_id

if TYPE_CHECKING:
    from kstrl.inbox import InboxItem
    from kstrl.manifest import Manifest
    from kstrl.reducer import ComponentState, RunState
    from kstrl.tui.home_data import RunSummary
    from kstrl.tui.runs import RunRef

DECISION = "decision"
FAILURE = "failure"


@dataclass(frozen=True)
class NeedsYouRow:
    #: DECISION | FAILURE.
    kind: str
    #: The inbox item id, or the component id.
    key: str
    what: str
    #: What resolves it, with the key that opens that screen.
    action: str
    run_id: str = ""


@dataclass(frozen=True)
class ActiveRow:
    #: The run's kind, or "ks serve".
    source: str
    label: str
    #: running | queued #n | leased
    state: str
    detail: str
    run_id: str = ""
    #: The detail for a narrow terminal; "" when ``detail`` is already short.
    short_detail: str = ""


@dataclass(frozen=True)
class OperatorQueue:
    #: The rows read. A source that could not be read (or is disabled) is
    #: named in ``unreadable``, and its count is not a zero.
    needs_you: tuple[NeedsYouRow, ...] = ()
    unreadable: tuple[str, ...] = ()
    active: tuple[ActiveRow, ...] = ()
    serve: ServeState | None = None
    delivery: Delivery | None = None
    #: failed run id -> the run that superseded it.
    superseded: Mapping[str, str] = field(default_factory=dict)

    @property
    def decisions(self) -> int | None:
        if INBOX_UNREADABLE in self.unreadable or INBOX_DISABLED in self.unreadable:
            return None
        return sum(1 for row in self.needs_you if row.kind == DECISION)

    @property
    def failures(self) -> int | None:
        if MANIFEST_UNREADABLE in self.unreadable:
            return None
        return sum(1 for row in self.needs_you if row.kind == FAILURE)


def _ran(state: RunState, component_id: str) -> bool:
    comp = state.components.get(component_id)
    return comp is not None and not comp.carried and comp.status != "pending"


def _failed_components(state: RunState) -> list[str]:
    return [
        cid
        for cid, comp in state.components.items()
        if comp.status == "failed" and not comp.carried
    ]


def _successor(
    ref: RunRef,
    failed: list[str],
    later_refs: Sequence[RunRef],
    states: Mapping[str, RunState],
) -> str:
    """The first of ``later_refs`` (oldest first) of ``ref``'s kind that ran
    one of ``failed``, or ""."""
    for later in later_refs:
        later_state = states.get(later.run_id)
        if later.kind == ref.kind and later_state is not None:
            if any(_ran(later_state, cid) for cid in failed):
                return later.run_id
    return ""


def supersessions(
    refs: Sequence[RunRef],
    summaries: Mapping[str, RunSummary],
    states: Mapping[str, RunState],
) -> dict[str, str]:
    """failed run id -> the earliest LATER run of its kind that ran a
    component it failed. ``refs`` is newest first, as discovery returns it."""
    oldest_first = list(reversed(refs))
    found: dict[str, str] = {}
    for index, ref in enumerate(oldest_first):
        summary = summaries.get(ref.run_id)
        state = states.get(ref.run_id)
        if summary is None or state is None or summary.state != FAILED:
            continue
        successor = _successor(ref, _failed_components(state), oldest_first[index + 1 :], states)
        if successor:
            found[ref.run_id] = successor
    return found


def _decision_rows(items: Sequence[InboxItem]) -> list[NeedsYouRow]:
    rows = []
    for item in items:
        what = item.title
        if item.component and item.component not in what:
            what = f"{what} ({item.component})"
        rows.append(
            NeedsYouRow(
                kind=DECISION,
                key=item.id,
                what=what,
                action="decide (6)",
                run_id=item.run_id,
            )
        )
    return rows


@dataclass(frozen=True)
class FailureEntry:
    """One failed component in one run, and what can be done about it."""

    run_id: str
    component_id: str
    #: ``<phase>: <cause>`` as the board shows it.
    cause: str
    attempts: int
    failed_ts: float
    #: "retry available", "superseded by <run>", or why neither.
    recovery: str
    retryable: bool = False
    successor: str = ""
    #: The newest failed phase's stored gate output (#462), if any.
    gate_logs: tuple[str, ...] = ()


def _retry_problem(manifest: Manifest, component_id: str) -> str:
    """ "" when ``preview_retry`` accepts the component, else its refusal."""
    from kstrl.retry_plan import preview_retry

    try:
        preview_retry(manifest, component_id)
    except ValueError as exc:
        return str(exc)
    return ""


def _later_run_that_ran(
    component_id: str,
    kind: str,
    later_newest_first: Sequence[RunRef],
    states: Mapping[str, RunState],
) -> str:
    for later in reversed(later_newest_first):
        later_state = states.get(later.run_id)
        if later.kind == kind and later_state is not None and _ran(later_state, component_id):
            return later.run_id
    return ""


def _manifest_only_entry(manifest: Manifest, component_id: str) -> FailureEntry:
    comp = manifest.get_component(component_id)
    assert comp is not None
    where = "/".join(part for part in (comp.failed_phase, comp.failed_check) if part)
    cause = f"{where}: {comp.error}" if where and comp.error else where or comp.error
    problem = _retry_problem(manifest, component_id)
    return FailureEntry(
        run_id="",
        component_id=component_id,
        cause=cause,
        attempts=comp.retries + 1,
        failed_ts=0.0,
        recovery=f"no retry: {problem}" if problem else "retry available",
        retryable=not problem,
    )


def _newest_gate_logs(comp: ComponentState) -> tuple[str, ...]:
    for entry in reversed(comp.phase_history):
        if not entry.get("passed"):
            return tuple(str(path) for path in entry.get("gate_logs") or ())
    return ()


def _run_entry(
    ref: RunRef,
    comp: ComponentState,
    current: bool,
    manifest: Manifest | None,
    successor: str,
) -> FailureEntry:
    if current and manifest is not None:
        problem = _retry_problem(manifest, comp.component_id)
        recovery = f"no retry: {problem}" if problem else "retry available"
    else:
        problem = "not current"
        recovery = f"superseded by {successor}" if successor else "no longer failed in the manifest"
    return FailureEntry(
        run_id=ref.run_id,
        component_id=comp.component_id,
        cause=failed_cause(comp),
        attempts=max(comp.attempt, 1),
        failed_ts=comp.last_event_ts,
        recovery=recovery,
        retryable=not problem,
        successor=successor,
        gate_logs=_newest_gate_logs(comp),
    )


def failure_queue(
    manifest: Manifest | None,
    refs: Sequence[RunRef],
    states: Mapping[str, RunState],
) -> list[FailureEntry]:
    """Every failed component of every listed run, current ones first.

    A component's failure is CURRENT in the newest run that failed it,
    while the manifest still records it FAILED; ``preview_retry`` then
    says whether a retry is available. Any other failure is superseded by
    the first later run of the same kind that ran the component, or, when
    no listed run did, is no longer current because the manifest moved.
    """
    still_failed = _manifest_failed(manifest)
    current: list[FailureEntry] = []
    past: list[FailureEntry] = []
    for index, ref in enumerate(refs):
        state = states.get(ref.run_id)
        if state is None:
            continue
        for cid in _failed_components(state):
            is_current = cid in still_failed and cid not in {e.component_id for e in current}
            successor = (
                "" if is_current else _later_run_that_ran(cid, ref.kind, refs[:index], states)
            )
            entry = _run_entry(ref, state.components[cid], is_current, manifest, successor)
            (current if is_current else past).append(entry)
    listed = {entry.component_id for entry in current}
    return current + _unlisted_failures(manifest, still_failed - listed) + past


def _unlisted_failures(manifest: Manifest | None, ids: set[str]) -> list[FailureEntry]:
    """Failures no listed run shows, in manifest order (ks retry's order)."""
    if manifest is None:
        return []
    return [
        _manifest_only_entry(manifest, comp.id) for comp in manifest.components if comp.id in ids
    ]


def _manifest_failed(manifest: Manifest | None) -> set[str]:
    if manifest is None:
        return set()
    return {comp.id for comp in manifest.components if comp.status == "failed"}


def failure_rows(
    manifest: Manifest,
    refs: Sequence[RunRef],
    states: Mapping[str, RunState],
) -> list[NeedsYouRow]:
    """The needs-you rows: current failures a retry can act on."""
    return [
        NeedsYouRow(
            kind=FAILURE,
            key=entry.component_id,
            what=f"{entry.component_id} failed" + (f" · {entry.cause}" if entry.cause else ""),
            action="retry (3)",
            run_id=entry.run_id,
        )
        for entry in failure_queue(manifest, refs, states)
        if entry.retryable
    ]


def _active_run_rows(
    refs: Sequence[RunRef],
    states: Mapping[str, RunState],
    now: float,
) -> list[ActiveRow]:
    from kstrl.tui.run_status import RUNNING

    rows = []
    for ref in refs:
        if not ref.live:
            continue
        state = states.get(ref.run_id)
        if state is None:
            rows.append(
                ActiveRow(ref.kind, ref.run_id, RUNNING, "folding run state...", ref.run_id)
            )
            continue
        reason = run_reason(RUNNING, state, now)
        detail, short = reason, ""
        moving = [
            comp for comp in state.components.values() if comp.status in ("running", "verifying")
        ]
        if moving:
            health = agent_health(ref.run_dir, moving[0], now)
            detail = f"{reason} · {health.text()}"
            # Health first: a narrow cell cuts the end, and the reason
            # is the part the board repeats.
            short = f"{health.text(short=True)} · {reason}"
        rows.append(ActiveRow(ref.kind, ref.run_id, RUNNING, detail, ref.run_id, short))
    return rows


def _serve_rows(serve: ServeState | None) -> list[ActiveRow]:
    if serve is None:
        return []
    rows = []
    for item in serve.items:
        if item.state == "queued":
            state = f"queued #{item.position}"
            detail = item.title
        else:
            state = "running" if item.state == "running" else "starting"
            run = f"run {short_run_id(item.run_id)}" if item.run_id else "run not recorded"
            detail = f"{item.title} · {run}"
        rows.append(ActiveRow("ks serve", item.item_id, state, detail, item.run_id))
    return rows


def newest_finished_factory(
    refs: Sequence[RunRef], states: Mapping[str, RunState]
) -> RunRef | None:
    """The run home's delivery section describes: the newest finished
    factory run that delivered something (a merge, a release ref, or an
    integration review), else the newest finished one. A run that failed
    before merging anything is not the latest delivery."""
    finished = [
        ref
        for ref in refs
        if ref.kind == "factory"
        and (state := states.get(ref.run_id)) is not None
        and state.finished
    ]
    for ref in finished:
        state = states[ref.run_id]
        if state.release_ref or merges_of(state) or review_files(ref.run_dir):
            return ref
    return finished[0] if finished else None


def build_queue(
    root_dir: Path,
    refs: Sequence[RunRef],
    summaries: Mapping[str, RunSummary],
    states: Mapping[str, RunState],
    now: float,
) -> OperatorQueue:
    """Every home section from disk. Each source that fails is named in
    ``unreadable`` and the others still render."""
    unreadable: list[str] = []
    rows: list[NeedsYouRow] = []
    items, inbox_problem = _open_inbox_items(root_dir)
    if inbox_problem:
        unreadable.append(inbox_problem)
    rows.extend(_decision_rows(items))
    manifest, problem = load_manifest(root_dir)
    if problem:
        unreadable.append(MANIFEST_UNREADABLE)
    elif manifest is not None:
        rows.extend(failure_rows(manifest, refs, states))
    from kstrl.tui.runs import factory_lock_held

    newest_factory = next((ref.run_id for ref in refs if ref.kind == "factory"), "")
    serve = read_serve_state(root_dir, newest_factory, factory_lock_held(root_dir))
    finished = newest_finished_factory(refs, states)
    delivery = (
        read_delivery(root_dir, finished.run_dir, states[finished.run_id])
        if finished is not None
        else None
    )
    return OperatorQueue(
        needs_you=tuple(rows),
        unreadable=tuple(unreadable),
        active=tuple(_active_run_rows(refs, states, now) + _serve_rows(serve)),
        serve=serve,
        delivery=delivery,
        superseded=supersessions(refs, summaries, states),
    )


#: ``OperatorQueue.unreadable`` entries.
INBOX_UNREADABLE = "inbox"
INBOX_DISABLED = "inbox disabled"
MANIFEST_UNREADABLE = "manifest"


def _open_inbox_items(root_dir: Path) -> tuple[list[InboxItem], str]:
    """Open items read the way ``ks inbox`` reads them, or why none were.

    A disabled inbox and an unreadable one are both "not counted": a
    zero from either would claim that nothing waits.
    """
    from kstrl.inbox import Inbox, InboxConfig

    try:
        config = InboxConfig.load(root_dir)
        if not config.enabled:
            return [], INBOX_DISABLED
        box = Inbox(root_dir, config)
        if box.scan().unreadable:
            return [], INBOX_UNREADABLE
        return box.open_items(), ""
    except Exception:  # noqa: BLE001 - home must render whatever the inbox holds
        return [], INBOX_UNREADABLE


def load_manifest(root_dir: Path) -> tuple[Manifest | None, str]:
    """The manifest, or None and why; (None, "") when there is none."""
    from kstrl.manifest import Manifest

    path = root_dir / "scripts" / "kstrl" / "manifest.json"
    if not path.exists():
        return None, ""
    try:
        return Manifest.load(path), ""
    except (OSError, ValueError) as exc:
        return None, str(exc) or type(exc).__name__
