"""Launched-run sessions for the home shell (TUI surface D6).

The pre-App portion of run_embedded, factored so an ALREADY-RUNNING
app (home) can start a command without owning a new terminal: mint
the run id, route core narration to <run_dir>/orchestrator.log via
the standard console stack, take module loggers off the alt screen,
and put the command core on a worker thread behind the queue channel.

Validation that can fail fast (missing manifest/spec) raises
LaunchError BEFORE any thread starts - the form shows the message and
nothing was mutated.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from kstrl.commandrun import open_command_run
from kstrl.events import CallbackSink, EventBus, RunPaths
from kstrl.interaction import QueueInteractionChannel
from kstrl.launch import (
    DecomposeLaunch,
    FactoryLaunch,
    LaunchSpec,
    assemble_factory_configs,
)
from kstrl.render import UIBackedRenderer
from kstrl.runid import mint_run_id
from kstrl.shutdown import StopController
from kstrl.tui.bridge import CommandHandle, start_command_thread
from kstrl.tui.embed import (
    _install_exclusive_root_handler,
    _restore_root_handlers,
)
from kstrl.ui.bridge import EventBridgeUI, NullPrompter
from kstrl.ui.plain import PlainUI

if TYPE_CHECKING:
    pass


class LaunchError(Exception):
    """A launch failed validation before anything started."""


@dataclass
class RunSession:
    run_dir: Path
    kind: str
    channel: QueueInteractionChannel
    handle: CommandHandle
    _cleanups: list[Callable[[], None]] = field(default_factory=list)

    def close(self) -> None:
        """Detach the channel and restore process-global state. Join
        the handle FIRST (or stop it) - closing under a live thread
        only detaches; the run itself keeps its files."""
        self.channel.detach()
        for cleanup in reversed(self._cleanups):
            try:
                cleanup()
            except OSError:
                pass
        self._cleanups.clear()


def start_run_session(
    spec: LaunchSpec,
    root_dir: Path,
    *,
    stop: StopController | None = None,
) -> RunSession:
    """Start ``spec``'s command core on a worker thread; the caller
    (the app's launch seam) attaches the channel and tails the run
    dir like any other run."""
    stop = stop or StopController()
    # Validate FIRST: a LaunchError must leave zero disk state behind
    # (no run dir, no open log handle).
    if isinstance(spec, FactoryLaunch):
        kind = "factory"
        prepared = _prepare_factory(spec, root_dir)
    elif isinstance(spec, DecomposeLaunch):
        kind = "decompose"
        prepared = _prepare_decompose(spec, root_dir)
    else:
        raise LaunchError(
            f"the launcher does not support {type(spec).__name__} yet; "
            "run the command from the CLI (it has the same embedded TUI)",
        )

    run_id = mint_run_id(kind)
    run_paths = RunPaths.for_run(root_dir, run_id)
    run_paths.root.mkdir(parents=True, exist_ok=True)

    log_fh = open(
        run_paths.root / "orchestrator.log", "a",
        buffering=1, encoding="utf-8",
    )
    renderer = UIBackedRenderer(PlainUI(no_color=True, file=log_fh))
    bus = EventBus(CallbackSink(renderer.handle))
    ui = EventBridgeUI(bus, prompter=NullPrompter())
    channel = QueueInteractionChannel()

    target = prepared(run_id, ui, channel, stop)

    root_logger = logging.getLogger()
    log_handler = logging.FileHandler(
        run_paths.root / "orchestrator.log", encoding="utf-8",
    )
    previous = _install_exclusive_root_handler(root_logger, log_handler)

    cleanups: list[Callable[[], None]] = [
        lambda: log_fh.close(),
        lambda: log_handler.close(),
        lambda: _restore_root_handlers(root_logger, log_handler, previous),
    ]
    try:
        handle = start_command_thread(
            target, stop=stop, name=f"kstrl-{kind}",
        )
    except BaseException:
        for cleanup in reversed(cleanups):
            cleanup()
        raise
    return RunSession(
        run_dir=run_paths.root,
        kind=kind,
        channel=channel,
        handle=handle,
        _cleanups=cleanups,
    )


# A prepared launch: everything validated, waiting for the session's
# run_id / console / channel / stop to close over.
PreparedLaunch = Callable[
    [str, EventBridgeUI, QueueInteractionChannel, StopController],
    Callable[[], int],
]


def _prepare_factory(spec: FactoryLaunch, root_dir: Path) -> PreparedLaunch:
    from kstrl.manifest import Manifest

    manifest_file = (
        spec.manifest_path
        or root_dir / "scripts" / "kstrl" / "manifest.json"
    )
    if not manifest_file.exists():
        raise LaunchError(
            f"no manifest at {manifest_file} - decompose a spec first",
        )
    try:
        manifest = Manifest.load(manifest_file)
    except (OSError, ValueError) as exc:
        raise LaunchError(f"failed to load {manifest_file}: {exc}") from exc

    factory_config, base_config = assemble_factory_configs(
        root_dir, single_pr=manifest.single_pr,
    )
    if spec.max_parallel is not None:
        factory_config.max_parallel = spec.max_parallel
    if spec.review_mode is not None:
        factory_config.review_mode = spec.review_mode

    def build(
        run_id: str,
        ui: EventBridgeUI,
        channel: QueueInteractionChannel,
        stop: StopController,
    ) -> Callable[[], int]:
        def target() -> int:
            from kstrl.factory import run_factory

            return run_factory(
                manifest, factory_config, base_config, ui, root_dir,
                manifest_path=manifest_file,
                interaction=channel,
                stop=stop,
                run_id=run_id,
                notify_capture_output=True,
            ).exit_code

        return target

    return build


def _prepare_decompose(
    spec: DecomposeLaunch, root_dir: Path,
) -> PreparedLaunch:
    from kstrl.agents import get_agent
    from kstrl.config import KstrlConfig

    spec_path = (
        spec.spec_path if spec.spec_path.is_absolute()
        else root_dir / spec.spec_path
    )
    if not spec_path.exists():
        raise LaunchError(f"spec not found: {spec_path}")
    if not spec.project_name.strip():
        raise LaunchError("project name is required")

    config = KstrlConfig.load(root_dir)
    agent = get_agent(
        config.agent_cmd, config.model, config.model_reasoning_effort,
        config.agent_type,
    )

    def build(
        run_id: str,
        ui: EventBridgeUI,
        channel: QueueInteractionChannel,
        stop: StopController,
    ) -> Callable[[], int]:
        del channel, stop  # decompose has no prompts; stop is v2 work

        def target() -> int:
            from kstrl.decompose import SpecBlockerError, decompose_spec

            command_run = open_command_run(
                ui, root_dir, "decompose",
                component="architect", run_id=run_id,
            )
            try:
                try:
                    manifest = decompose_spec(
                        spec_path=spec_path,
                        project_name=spec.project_name,
                        base_branch=spec.base_branch,
                        single_pr=spec.single_pr,
                        agent=agent,
                        ui=ui,
                        root_dir=root_dir,
                        bus=command_run.bus,
                        transcript=command_run.transcript_writer("architect"),
                    )
                    ui.ok(
                        f"Decomposed into {len(manifest.components)} "
                        "components"
                    )
                    return 0
                except SpecBlockerError as exc:
                    ui.err(str(exc))
                    if exc.artifact_path is not None:
                        ui.info(
                            f"Spec issues written to: {exc.artifact_path}"
                        )
                    return 2
                except ValueError as exc:
                    ui.err(str(exc))
                    return 1
            finally:
                command_run.close()

        return target

    return build
