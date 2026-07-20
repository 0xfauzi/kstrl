"""Home shell data: run summaries + quick stats (TUI surface D2).

Summarizing a run is a full reducer fold (file IO + fold), so the
home screen computes these on a worker thread, renders honest "·"
cells until they land, and caches by (run_id, events mtime) so only
movers recompute. Numbers keep R3.1 semantics: whenever a run has
unreported calls, its totals are LOWER BOUNDS and carry the "+".
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from kstrl.proposals import list_proposals
from kstrl.reducer import fold, read_run_dir
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


@dataclass(frozen=True)
class HomeStats:
    last: RunSummary | None
    pending_proposals: int


def summarize_run(ref: RunRef) -> RunSummary:
    state = fold(read_run_dir(ref.run_dir))
    done = sum(
        1 for comp in state.components.values()
        if comp.status == "completed"
    )
    failed = sum(
        1 for comp in state.components.values() if comp.status == "failed"
    )
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
        tokens_lower_bound=state.unreported_calls > 0,
        cost_usd=state.cost_usd,
    )


class SummaryCache:
    """(run_id, events mtime) -> RunSummary; recompute only movers."""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, RunSummary]] = {}

    def refresh(self, refs: list[RunRef]) -> dict[str, RunSummary]:
        out: dict[str, RunSummary] = {}
        for ref in refs:
            hit = self._cache.get(ref.run_id)
            if hit is not None and hit[0] == ref.mtime:
                out[ref.run_id] = hit[1]
                continue
            summary = summarize_run(ref)
            self._cache[ref.run_id] = (ref.mtime, summary)
            out[ref.run_id] = summary
        return out


def pending_proposal_count(root_dir: Path) -> int:
    proposals_dir = root_dir / ".kstrl" / "proposals"
    return sum(
        1 for proposal in list_proposals(proposals_dir)
        if not proposal.applied
    )


def gather_stats(
    root_dir: Path, summaries: dict[str, RunSummary],
    newest_run_id: str,
) -> HomeStats:
    return HomeStats(
        last=summaries.get(newest_run_id),
        pending_proposals=pending_proposal_count(root_dir),
    )
