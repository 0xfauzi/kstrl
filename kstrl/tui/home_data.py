"""Home shell data: run summaries + quick stats (TUI surface D2).

Summarizing a run is a full reducer fold (file IO + fold), so the
home screen computes these on a worker thread, renders honest "·"
cells until they land, and caches by every folded stream's identity,
mtime, and size so only movers recompute. Numbers keep R3.1 semantics:
whenever a run has unreported calls, its totals are LOWER BOUNDS and
carry the "+".
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from kstrl.reducer import RunState, fold, read_run_dir
from kstrl.tui.operator_queue import OperatorQueue, build_queue
from kstrl.tui.run_status import state_word
from kstrl.tui.runs import RunRef


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    outcome: str  # live | done | failed | stale
    components_done: int
    components_failed: int
    components_total: int
    total_tokens: int
    tokens_lower_bound: bool
    cost_usd: float
    #: running | completed | failed | unknown (tui.run_status, #433 F4).
    state: str = ""


@dataclass(frozen=True)
class HomeStats:
    last: RunSummary | None
    #: Open inbox items, or None when the inbox could not be read (#433 E3).
    inbox_open: int | None = None
    #: Manifest components in the failed state, the ones retry can act on;
    #: None when there is no readable manifest.
    failed_components: int | None = None
    #: Every home section (#433 increment 2); None until the worker lands.
    queue: OperatorQueue | None = None


def fold_run(ref: RunRef) -> RunState:
    return fold(read_run_dir(ref.run_dir))


def summarize_state(ref: RunRef, state: RunState) -> RunSummary:
    # A run's row counts what the run did. A component it carried from
    # an earlier run keeps that run's status on the board, but it was
    # not completed or failed HERE (#448).
    ran = [comp for comp in state.components.values() if not comp.carried]
    done = sum(1 for comp in ran if comp.status == "completed")
    failed = sum(1 for comp in ran if comp.status == "failed")
    total = len(state.plan_order) or len(state.components)
    if ref.live:
        outcome = "live"
    elif state.finished:
        outcome = "failed" if failed else "done"
    else:
        outcome = "stale"
    return RunSummary(
        run_id=ref.run_id,
        outcome=outcome,
        components_done=done,
        components_failed=failed,
        components_total=total,
        total_tokens=state.total_tokens,
        # The TOKEN axis specifically (R8 review finding 1): a call that
        # reported a cost and no token count is not "unreported", yet it
        # still leaves total_tokens short.
        tokens_lower_bound=state.tokens_are_lower_bound,
        cost_usd=state.cost_usd,
        state=state_word(outcome),
    )


def summarize_run(ref: RunRef) -> RunSummary:
    return summarize_state(ref, fold_run(ref))


class SummaryCache:
    """Run-stream signature -> summary + folded state; recompute only
    movers. The signature covers events.jsonl AND every worker stream
    (a worker append must invalidate even when the top-level mtime
    holds still). The retained states feed the home preview board -
    bounded by the run-browser cap, so at most ~15 RunStates live."""

    def __init__(self) -> None:
        self._cache: dict[
            str,
            tuple[tuple[tuple[str, int, int, int, int], ...], RunSummary],
        ] = {}
        self._states: dict[str, RunState] = {}

    def refresh(self, refs: list[RunRef]) -> dict[str, RunSummary]:
        out: dict[str, RunSummary] = {}
        active = {ref.run_id for ref in refs}
        for stale in self._cache.keys() - active:
            del self._cache[stale]
            self._states.pop(stale, None)
        for ref in refs:
            signature = _run_stream_signature(ref)
            hit = self._cache.get(ref.run_id)
            if hit is not None and hit[0] == signature:
                # Liveness is not in the signature: a run that dies writes
                # nothing, so a summary cached while it was live would say
                # "running" for ever. Re-derive the word from the cached
                # state (no re-fold) on every refresh (#433 F4).
                cached_state = self._states.get(ref.run_id)
                summary = hit[1] if cached_state is None else summarize_state(ref, cached_state)
                self._cache[ref.run_id] = (signature, summary)
                out[ref.run_id] = summary
                continue
            state = fold_run(ref)
            summary = summarize_state(ref, state)
            self._cache[ref.run_id] = (signature, summary)
            self._states[ref.run_id] = state
            out[ref.run_id] = summary
        return out

    def state_for(self, run_id: str) -> RunState | None:
        return self._states.get(run_id)


def _run_stream_signature(
    ref: RunRef,
) -> tuple[tuple[str, int, int, int, int], ...]:
    """Identity for every stream consumed by ``read_run_dir``."""
    paths = [ref.events_path]
    components_dir = ref.run_dir / "components"
    try:
        component_dirs = sorted(components_dir.iterdir())
    except OSError:
        component_dirs = []
    paths.extend(
        component_dir / "engineer.jsonl"
        for component_dir in component_dirs
        if component_dir.is_dir()
    )
    signature: list[tuple[str, int, int, int, int]] = []
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        signature.append(
            (
                str(path.relative_to(ref.run_dir)),
                stat.st_dev,
                stat.st_ino,
                stat.st_mtime_ns,
                stat.st_size,
            )
        )
    return tuple(signature)


def gather_stats(
    summaries: dict[str, RunSummary],
    newest_run_id: str,
    root_dir: Path | None = None,
) -> HomeStats:
    if root_dir is None:
        return HomeStats(last=summaries.get(newest_run_id))
    return HomeStats(
        last=summaries.get(newest_run_id),
        inbox_open=open_inbox_count(root_dir),
        failed_components=failed_component_count(root_dir),
    )


def gather_home(
    summaries: dict[str, RunSummary],
    refs: list[RunRef],
    cache: SummaryCache,
    root_dir: Path,
    now: float,
) -> HomeStats:
    """Stats and the operator queue from ONE read of each source.

    The counts on the needs-you title come from the same rows the section
    lists, so the title and the rows cannot disagree.
    """
    states = {ref.run_id: state for ref in refs if (state := cache.state_for(ref.run_id))}
    queue = build_queue(root_dir, refs, summaries, states, now)
    return HomeStats(
        last=summaries.get(refs[0].run_id) if refs else None,
        inbox_open=queue.decisions,
        failed_components=queue.failures,
        queue=queue,
    )


def open_inbox_count(root_dir: Path) -> int | None:
    """Open inbox items, read the way ``ks inbox`` reads them.

    None when the inbox cannot be counted: a config that does not load,
    a control directory that cannot be read. None renders as nothing; a
    count of 0 renders as "nothing is waiting on you", which is a claim
    this function only makes when it read the log.
    """
    from kstrl.inbox import Inbox, InboxConfig

    try:
        config = InboxConfig.load(root_dir)
        if not config.enabled:
            return None
        scan = Inbox(root_dir, config).scan()
    except Exception:  # noqa: BLE001 - home must render whatever the inbox holds
        return None
    return None if scan.unreadable else scan.open_count()


def failed_component_count(root_dir: Path) -> int | None:
    """Failed components in the manifest the retry screen reads.

    0 when there is no manifest: nothing can be retried, which is a
    count, not a failure to read. None only when a manifest exists and
    cannot be read, so the home line makes no claim about it.
    """
    from kstrl.manifest import Manifest

    manifest_file = root_dir / "scripts" / "kstrl" / "manifest.json"
    if not manifest_file.exists():
        return 0
    try:
        manifest = Manifest.load(manifest_file)
    except (OSError, ValueError):
        return None
    return sum(1 for comp in manifest.components if comp.status == "failed")
