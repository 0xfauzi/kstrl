"""The state that the steps of one factory run share (#776, B3).

``_run_factory_locked`` builds one ``RunContext`` and gives it to the
steps that it divided out of itself. This slice holds the fields that the
finish steps read.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kstrl.autonomy_config import AutonomyConfig
    from kstrl.events import EventBus, EventSink
    from kstrl.factory_autonomy import _LadderOutcome
    from kstrl.factory_config import FactoryConfig
    from kstrl.factory_result import FactoryResult
    from kstrl.integration_phase import IntegrationRecord
    from kstrl.manifest import Manifest
    from kstrl.observability import NotifyHooks
    from kstrl.pipeline import ComponentPipeline
    from kstrl.runenvelope import RunEnvelope
    from kstrl.runstate import RunState
    from kstrl.shutdown import StopController
    from kstrl.ui.base import UI


@dataclass(frozen=True)
class RunContext:
    """References to the objects of one run, never copies of them.

    The class is frozen so that a step cannot rebind a field. A step
    changes the run through the objects that a field refers to
    (``factory_config``, ``manifest``, ``run_state``, ``factory_result``
    and ``pipeline``). A copy of one of them would leave a green suite
    and no effect (#193).
    """

    manifest: Manifest
    manifest_path: Path
    factory_config: FactoryConfig
    ui: UI
    root_dir: Path
    stop: StopController | None
    run_id: str
    run_started_at: str
    factory_start: float
    factory_result: FactoryResult
    run_state: RunState
    run_envelope: RunEnvelope
    autonomy_config: AutonomyConfig
    ladder: _LadderOutcome | None
    bus: EventBus
    run_file_sinks: list[EventSink]
    journal_path: Path | None
    pipeline: ComponentPipeline
    notify: NotifyHooks
    integration_record: IntegrationRecord
