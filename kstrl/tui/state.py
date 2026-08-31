"""StateStore: the dashboard's single state source (stage 3 PR D).

The module that owns the reducer contract for the live-run screens, so
reducer drift has a small blast radius (plan decision). Stated as
"small" rather than "one module": ``home_data.py`` folds run dirs for
the home board and imports the reducer directly, so the original
"ONLY module" claim has not been true since that board landed, and a
decision leaning on it would be leaning on nothing.

The manifest join is lazy and mtime-cached: authoritative snapshot
data (DAG, PR URLs, evidence pointers) without a read per frame.
"""

from __future__ import annotations

from pathlib import Path

from kstrl.agents.base import ARCHITECT_COMPONENT, ARCHITECT_ROLE
from kstrl.events import Event
from kstrl.manifest import Manifest
from kstrl.reducer import RunState, apply


def architect_component_id(state: RunState) -> str:
    """The key THIS run dir uses for the architect's pseudo-component.

    #281 moved the role's key off the bare word so it could not collide
    with an LLM-chosen component id. Run directories are the durable
    record, so every decompose dir written before that still says
    ``architect``, and reading only the new key rendered a COMPLETED
    historical run as failed, left the transcript tailing a path that
    does not exist, and let the stale pseudo-row through as a graph
    node. That is not a conservative degradation, it is a wrong one,
    which is why this seam gets a fallback and ``serve`` does not.

    The fallback cannot resurrect the bug, for two independent reasons:

    - It is reachable only on ``decompose`` runs. A factory run keeps
      the pessimistic read, so a manifest component named `architect`
      is never mistaken for the role.
    - Within a decompose run it is unreachable after #281 anyway.
      ``_decompose_spec_impl`` emits the architect's ``RunPlan`` entry
      and ``ComponentStarted`` unconditionally as its first component
      events, before the spec is ever handed to an LLM, and the reducer
      creates a row from ``RunPlan``. So any post-#281 dir answers on
      the first branch, and the LLM's own components - which arrive at
      the SECOND ``RunPlan``, strictly later - are never consulted.

    ``serve.read_run_spend`` gets no such fallback, and the reason is
    about ITS seam rather than about this one's placement. It reads
    factory runs, where an old role row and a component genuinely named
    `architect` are the same shape and no probe can separate them, so it
    stays pessimistic and reports the day as a floor. See
    ``RunSpend.unmetered_phases``.

    Living in ``kstrl/tui/`` is a LAYERING choice, not a safety
    mechanism, and it is worth being exact about that because the
    tempting claim is wrong: a gate on ``decompose`` would be a no-op for
    ``serve`` wherever this function lived, since ``owned_run_spend`` is
    ``read_run_spend``'s only caller and already narrows to
    ``SPAWNED_RUN_KIND``. What the placement actually buys is that an
    era shim for RENDERING sits with the renderer, so a future
    money-reading caller has to reach into the TUI package on purpose
    rather than find this beside the fold.

    Not normalised inside ``StateStore.apply_events`` either, which
    would otherwise be the tidier shape: ``transcript_component`` has to
    stay the key the dir actually wrote, because it becomes the path
    segment in ``components/<key>/engineer.log``. Rewriting the key on
    fold would re-break the transcript tail it is here to fix.

    On a pre-#281 dir whose architect ALSO named a component
    `architect`, the two were already merged into one row when the dir
    was written and no reader can separate them now. Returning that row
    is the best available reading, and it is what the dashboard showed
    at the time.
    """
    if ARCHITECT_COMPONENT in state.components:
        return ARCHITECT_COMPONENT
    if state.kind == "decompose" and ARCHITECT_ROLE in state.components:
        return ARCHITECT_ROLE
    return ARCHITECT_COMPONENT


def planned_component_ids(state: RunState) -> list[str]:
    """The run's REAL components, in plan order, without the architect.

    One definition because it has now moved twice: the decompose
    summary's count and the DAG table's row set are the same rule, and
    #281 and this change each had to edit both. Spelling it once is what
    stops the third edit reaching only one of them.
    """
    pseudo = architect_component_id(state)
    return [cid for cid in state.plan_order if cid != pseudo]


class StateStore:
    def __init__(self, root_dir: Path, run_id: str = "") -> None:
        self.root_dir = root_dir
        self._state = RunState(run_id=run_id)
        self._manifest: Manifest | None = None
        self._manifest_mtime = 0.0

    @property
    def state(self) -> RunState:
        return self._state

    def apply_events(self, events: list[Event]) -> bool:
        """Fold tailed events; True when anything changed."""
        for event in events:
            apply(self._state, event)
        return bool(events)

    def reset(self) -> None:
        """Discard folded event state before applying a rebuilt snapshot."""
        self._state = RunState(run_id=self._state.run_id)

    def manifest(self) -> Manifest | None:
        """The factory manifest, reloaded only when its mtime moves."""
        path = self.root_dir / "scripts" / "kstrl" / "manifest.json"
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return self._manifest
        if mtime != self._manifest_mtime:
            try:
                self._manifest = Manifest.load(path)
                self._manifest_mtime = mtime
            except (OSError, ValueError):
                pass
        return self._manifest
