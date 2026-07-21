"""Run-scoped data flow for the app (TUI surface D1).

Everything needed to observe (and, for launched sessions, drive) ONE
run, extracted from KstrlTuiApp so the home shell can open, close,
and swap runs without the app being born bound to a single run dir.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from kstrl.tui.state import StateStore
from kstrl.tui.tail import RunTailer, TextTailer

if TYPE_CHECKING:
    from kstrl.interaction import QueueInteractionChannel
    from kstrl.tui.bridge import CommandHandle


@dataclass
class RunContext:
    """One run's tailer + reducer store + transcript tailers.

    ``owns_app_exit`` is True for CLI-scoped runs (dash / embedded):
    the run finishing exits the app. Home-launched sessions set it
    False - finishing keeps the board up for post-mortem reading and
    escape pops back home. ``channel``/``handle`` arrive with the
    launch seam (D6); observe-only contexts leave them None.
    """

    run_dir: Path
    tailer: RunTailer
    store: StateStore
    transcript_tailers: dict[str, TextTailer] = field(default_factory=dict)
    channel: QueueInteractionChannel | None = None
    handle: CommandHandle | None = None
    owns_app_exit: bool = True

    @classmethod
    def observe(
        cls, run_dir: Path, root_dir: Path, *, owns_app_exit: bool = True,
    ) -> RunContext:
        return cls(
            run_dir=run_dir,
            tailer=RunTailer(run_dir),
            store=StateStore(root_dir, run_id=run_dir.name),
            owns_app_exit=owns_app_exit,
        )

    def transcript_tailer(self, component_id: str) -> TextTailer:
        tailer = self.transcript_tailers.get(component_id)
        if tailer is None:
            tailer = TextTailer(
                self.run_dir / "components" / component_id / "engineer.log",
            )
            self.transcript_tailers[component_id] = tailer
        return tailer
