"""Embedded mode: `ralph factory` with the dashboard (stage 3 PR F).

Sequence (each step maps to a spike finding or plan decision):
1.  Mint the run id BEFORE anything starts (PR B's override) so the
    run dir is known to the TUI from frame one.
2.  Orchestrator narration renders to <run_dir>/orchestrator.log
    through the SAME console architecture as the terminal (bridge ->
    bus -> UIBackedRenderer -> PlainUI-on-a-file); run_factory then
    attaches the run's file sinks to that bus as usual. A root logging
    FileHandler catches module loggers (evolution, agents) that would
    otherwise scribble on the alt screen; notify hooks run
    output-captured (spike: measured 5-line alt-screen corruption).
3.  Signal handlers install BEFORE app.run() (spike finding 2: Textual
    leaves the terminal raw on SIGTERM); a signal requests the same
    graceful stop as the TUI's quit flow.
4.  The TUI tails the SAME files as `ralph dash` - one data path, so a
    TUI crash cannot lose orchestrator state: the fallback loop keeps
    streaming events as plain lines until the run finishes.
5.  finally: detach the channel (pending prompts degrade to their
    non-interactive defaults - a dead TUI never hangs the run), join
    the orchestrator, restore the terminal.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from kstrl.events import CallbackSink, EventBus, RunPaths
from kstrl.interaction import QueueInteractionChannel
from kstrl.knowledge import current_run_id
from kstrl.render import UIBackedRenderer
from kstrl.shutdown import StopController, install_signal_handlers
from kstrl.tui.app import KstrlTuiApp, Mode
from kstrl.tui.bridge import OrchestratorHandle, start_orchestrator
from kstrl.tui.tail import RunTailer
from kstrl.ui.bridge import EventBridgeUI, NullPrompter
from kstrl.ui.plain import PlainUI

if TYPE_CHECKING:
    from kstrl.config import KstrlConfig
    from kstrl.factory import FactoryConfig
    from kstrl.manifest import Manifest

ANSI_RESTORE = "\x1b[?1049l\x1b[?25h\x1b[0m"


def _install_exclusive_root_handler(
    root_logger: logging.Logger, handler: logging.Handler,
) -> list[logging.Handler]:
    """Route root-logger output only to ``handler`` until restored."""
    previous = list(root_logger.handlers)
    for existing in previous:
        root_logger.removeHandler(existing)
    root_logger.addHandler(handler)
    return previous


def _restore_root_handlers(
    root_logger: logging.Logger,
    handler: logging.Handler,
    previous: list[logging.Handler],
) -> None:
    root_logger.removeHandler(handler)
    for existing in previous:
        root_logger.addHandler(existing)


def run_factory_embedded(
    manifest: Manifest,
    factory_config: FactoryConfig,
    base_config: KstrlConfig,
    root_dir: Path,
    manifest_path: Path | None,
    *,
    poll_interval: float = 0.2,
) -> int:
    run_id = current_run_id()
    run_paths = RunPaths.for_run(root_dir, run_id)
    run_paths.root.mkdir(parents=True, exist_ok=True)

    # Orchestrator narration -> orchestrator.log via the standard
    # console stack; prompts go through the queue channel, never a TTY.
    log_fh = open(
        run_paths.root / "orchestrator.log", "a",
        buffering=1, encoding="utf-8",
    )
    renderer = UIBackedRenderer(PlainUI(no_color=True, file=log_fh))
    bus = EventBus(CallbackSink(renderer.handle))
    orchestrator_ui = EventBridgeUI(bus, prompter=NullPrompter())

    channel = QueueInteractionChannel()
    stop = StopController()

    # Module loggers (evolution, agents/*) must not hit the alt screen.
    root_logger = logging.getLogger()
    log_handler = logging.FileHandler(
        run_paths.root / "orchestrator.log", encoding="utf-8",
    )
    previous_log_handlers = _install_exclusive_root_handler(
        root_logger, log_handler,
    )

    uninstall = install_signal_handlers(stop)
    handle: OrchestratorHandle | None = None
    try:
        handle = start_orchestrator(
            manifest, factory_config, base_config, orchestrator_ui,
            root_dir, manifest_path,
            run_id=run_id, stop=stop, channel=channel,
        )
        app = KstrlTuiApp(
            run_dir=run_paths.root, root_dir=root_dir,
            mode=Mode.EMBEDDED, poll_interval=poll_interval,
            channel=channel, orchestrator=handle,
        )
        # The app attaches the channel itself in on_mount - attaching
        # before app.run() would race call_from_thread on a
        # not-yet-running app (found by test).
        try:
            code = app.run()
        except Exception as exc:  # noqa: BLE001 - TUI crash != run crash
            sys.stdout.write(ANSI_RESTORE)
            sys.stdout.flush()
            print(
                f"TUI failed ({exc}); the run continues - streaming "
                f"plain output until it finishes.",
                file=sys.stderr,
            )
            channel.detach()
            code = _plain_fallback(handle, run_paths.root)
        return code if code is not None else handle.exit_code
    finally:
        channel.detach()
        if handle is not None:
            handle.join()
        uninstall()
        _restore_root_handlers(
            root_logger, log_handler, previous_log_handlers,
        )
        log_handler.close()
        try:
            log_fh.close()
        except OSError:
            pass
        sys.stdout.write(ANSI_RESTORE)
        sys.stdout.flush()


def _plain_fallback(handle: OrchestratorHandle, run_dir: Path) -> int:
    """TUI died: stream the run's events as plain lines until done."""
    renderer = UIBackedRenderer(PlainUI(no_color=True))
    tailer = RunTailer(run_dir)
    while True:
        for event in tailer.poll_events().events:
            renderer.handle(event)
        if handle.done():
            for event in tailer.poll_events().events:  # final drain
                renderer.handle(event)
            return handle.exit_code
        time.sleep(0.5)
