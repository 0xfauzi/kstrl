"""StateStore: the dashboard's single state source (stage 3 PR D).

The ONLY module in kstrl/tui/ that imports the reducer - any
reducer-contract drift has a one-module blast radius (plan decision).
The manifest join is lazy and mtime-cached: authoritative snapshot
data (DAG, PR URLs, evidence pointers) without a read per frame.
"""

from __future__ import annotations

from pathlib import Path

from kstrl.events import Event
from kstrl.manifest import Manifest
from kstrl.reducer import RunState, apply


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
