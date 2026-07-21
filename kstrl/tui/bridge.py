"""Worker-thread handle for embedded mode.

A command core runs on a NON-daemon background thread; Textual owns
the main thread. The only crossings are:
- QueueInteractionChannel (PR A): the core blocks, the TUI resolves
  via App.call_from_thread - the mechanism the spike's G4 soak
  validated (zero deadlocks across 33k events).
- StopController (PR B): both sides can request a stop.
- This handle: the TUI polls done() and reads the exit code.

Generalized from the factory-only orchestrator (TUI surface A3): any
``Callable[[], int]`` command core runs the same way. Cores must
RETURN exit codes, not ``sys.exit()`` - a SystemExit raised on the
thread is boxed as a result (its code, not a crash), but that path is
a bug's safety net, not the contract.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from kstrl.factory import run_factory

if TYPE_CHECKING:
    from kstrl.config import KstrlConfig
    from kstrl.factory import FactoryConfig
    from kstrl.interaction import QueueInteractionChannel
    from kstrl.manifest import Manifest
    from kstrl.shutdown import StopController
    from kstrl.ui.base import UI


@dataclass
class CommandHandle:
    thread: threading.Thread
    stop: StopController
    result_box: list[int] = field(default_factory=list)
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
            return self.result_box[0]
        return 1  # died without a result


# Compat alias: the app and tests grew up on the factory-only name.
OrchestratorHandle = CommandHandle


def start_command_thread(
    target: Callable[[], int],
    *,
    stop: StopController,
    name: str = "kstrl-worker",
) -> CommandHandle:
    result_box: list[int] = []
    error_box: list[BaseException] = []

    def _run() -> None:
        try:
            result_box.append(int(target()))
        except SystemExit as exc:
            # A missed sys.exit inside a core: honor its code rather
            # than collapsing every exit to a crash.
            code = exc.code
            if isinstance(code, int):
                result_box.append(code)
            elif code is None:
                result_box.append(0)
            else:
                result_box.append(1)
        except BaseException as exc:  # noqa: BLE001 - surfaced via the box
            error_box.append(exc)

    thread = threading.Thread(target=_run, name=name, daemon=False)
    handle = CommandHandle(
        thread=thread, stop=stop,
        result_box=result_box, error_box=error_box,
    )
    thread.start()
    return handle


def start_orchestrator(
    manifest: Manifest,
    factory_config: FactoryConfig,
    base_config: KstrlConfig,
    ui: UI,
    root_dir: Path,
    manifest_path: Path | None,
    *,
    run_id: str,
    stop: StopController,
    channel: QueueInteractionChannel,
) -> CommandHandle:
    def _target() -> int:
        return run_factory(
            manifest, factory_config, base_config, ui, root_dir,
            manifest_path=manifest_path,
            interaction=channel,
            stop=stop,
            run_id=run_id,
            notify_capture_output=True,
        ).exit_code

    return start_command_thread(
        _target, stop=stop, name="kstrl-orchestrator",
    )
