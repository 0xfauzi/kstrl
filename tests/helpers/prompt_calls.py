"""The run identity a test hands a flow it calls directly (#567).

Every scope that records an agent's prompt carries an identity, so a test
that calls ``decompose_spec`` or ``run_feature`` itself, outside any
command, passes one. Both put a record, if the agent writes one, under the
test's own project, where a command run would put it.
"""

from __future__ import annotations

import io
from pathlib import Path

from kstrl.agents.base import ARCHITECT_COMPONENT, ARCHITECT_ROLE
from kstrl.agents.prompt_record import AgentCall
from kstrl.commandrun import CommandRun, open_command_run
from kstrl.events import RunPaths
from kstrl.ui.plain import PlainUI

TEST_RUN_ID = "test-run"


def architect_call(root_dir: Path) -> AgentCall:
    """The architect's identity for a direct ``decompose_spec`` call."""
    return AgentCall(
        run_root=RunPaths.for_run(root_dir, TEST_RUN_ID).root,
        run_id=TEST_RUN_ID,
        component=ARCHITECT_COMPONENT,
        role=ARCHITECT_ROLE,
        attempt=1,
    )


def offline_run(root_dir: Path, kind: str, component: str = "") -> CommandRun:
    """A command run with recording off and no heartbeat, for a direct call.

    Its bus is private and has no sink, and ``close`` has nothing to stop.
    """
    return open_command_run(
        PlainUI(no_color=True, file=io.StringIO()),
        root_dir,
        kind,
        component=component,
        enabled=False,
        heartbeat=False,
    )
