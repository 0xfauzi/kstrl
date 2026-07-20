"""Orchestrator thread handle for embedded mode (stage 3 PR F).

The orchestrator runs run_factory on a NON-daemon background thread;
Textual owns the main thread. The only crossings are:
- QueueInteractionChannel (PR A): orchestrator blocks, TUI resolves
  via App.call_from_thread - the mechanism the spike's G4 soak
  validated (zero deadlocks across 33k events).
- StopController (PR B): both sides can request a stop.
- This handle: the TUI polls done() and reads the result.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ralph_py.factory import run_factory

if TYPE_CHECKING:
    from ralph_py.config import RalphConfig
    from ralph_py.factory import FactoryConfig, FactoryResult
    from ralph_py.interaction import QueueInteractionChannel
    from ralph_py.manifest import Manifest
    from ralph_py.shutdown import StopController
    from ralph_py.ui.base import UI


@dataclass
class OrchestratorHandle:
    thread: threading.Thread
    stop: StopController
    result_box: list[FactoryResult] = field(default_factory=list)
    error_box: list[BaseException] = field(default_factory=list)

    def done(self) -> bool:
        return not self.thread.is_alive()

    def join(self, timeout: float | None = None) -> None:
        self.thread.join(timeout)

    @property
    def exit_code(self) -> int:
        if self.error_box:
            return 1
        if self.result_box:
            return self.result_box[0].exit_code
        return 1  # died without a result


def start_orchestrator(
    manifest: Manifest,
    factory_config: FactoryConfig,
    base_config: RalphConfig,
    ui: UI,
    root_dir: Path,
    manifest_path: Path | None,
    *,
    run_id: str,
    stop: StopController,
    channel: QueueInteractionChannel,
) -> OrchestratorHandle:
    result_box: list[FactoryResult] = []
    error_box: list[BaseException] = []

    def _target() -> None:
        try:
            result_box.append(run_factory(
                manifest, factory_config, base_config, ui, root_dir,
                manifest_path=manifest_path,
                interaction=channel,
                stop=stop,
                run_id=run_id,
                notify_capture_output=True,
            ))
        except BaseException as exc:  # noqa: BLE001 - surfaced via the box
            error_box.append(exc)

    thread = threading.Thread(
        target=_target, name="ralph-orchestrator", daemon=False,
    )
    handle = OrchestratorHandle(
        thread=thread, stop=stop,
        result_box=result_box, error_box=error_box,
    )
    thread.start()
    return handle
