"""The child processes.

The process groups, the deadlines, the scrubbed runs, the isolation rungs,
the run lock and the worktree sweep.
"""

from __future__ import annotations

from kstrl.proc.factory_lock import (
    FactoryLockHeldError,
    LeftoverAgentsError,
    RunLock,
    acquire_run_lock,
    held_or_acquired_run_lock,
)
from kstrl.proc.proc import (
    SESSION_STOPPED_PREFIX,
    TIMEOUT_MESSAGE_PREFIX,
    DeadlineStreamer,
    kill_active_process_groups,
    timeout_message,
)
from kstrl.proc.procdispose import drain_or_abandon, reap_or_abandon
from kstrl.proc.procgroup import (
    pid_is_alive,
    read_group_liveness,
    safe_pgid,
    signal_group,
    signal_probe_alive,
)
from kstrl.proc.rung import (
    ENV_PROGRAM,
    HOST_LABEL,
    PROVER_PLATFORMS,
    HostFallback,
    ProvenRung,
    Rung,
    host_fallback,
    label_of,
    nono_argv,
    refusal_of,
    release,
    sandbox_hint,
    zone_dir,
)
from kstrl.proc.scrubbed_run import (
    SECRET_NAME_FRAGMENTS,
    ChildOutputDecodeError,
    readable_text,
    run_scrubbed,
    start_scrubbed,
)
from kstrl.proc.shutdown import StopController, install_signal_handlers
from kstrl.proc.temp_worktree import (
    ContractCleanupError,
    create_temp_worktree,
    remove_temp_worktree,
)
from kstrl.proc.timeout import (
    NO_LIMIT,
    TIMEOUT_SECTION,
    TimeoutConfig,
    describe_limit_seconds,
    limit_seconds,
)
from kstrl.proc.worktree_setup import NO_SETUP, WorktreeSetup
from kstrl.proc.worktree_sweep import (
    WorktreeSweep,
    leftover_agents,
    leftover_lines,
    sweep_findings,
    sweep_worktree,
    warn_sweep,
)

__all__ = [
    "ChildOutputDecodeError",
    "ContractCleanupError",
    "DeadlineStreamer",
    "ENV_PROGRAM",
    "FactoryLockHeldError",
    "HOST_LABEL",
    "HostFallback",
    "LeftoverAgentsError",
    "NO_LIMIT",
    "NO_SETUP",
    "PROVER_PLATFORMS",
    "ProvenRung",
    "RunLock",
    "Rung",
    "SECRET_NAME_FRAGMENTS",
    "SESSION_STOPPED_PREFIX",
    "StopController",
    "TIMEOUT_MESSAGE_PREFIX",
    "TIMEOUT_SECTION",
    "TimeoutConfig",
    "WorktreeSetup",
    "WorktreeSweep",
    "acquire_run_lock",
    "create_temp_worktree",
    "describe_limit_seconds",
    "drain_or_abandon",
    "held_or_acquired_run_lock",
    "host_fallback",
    "install_signal_handlers",
    "kill_active_process_groups",
    "label_of",
    "leftover_agents",
    "leftover_lines",
    "limit_seconds",
    "nono_argv",
    "pid_is_alive",
    "read_group_liveness",
    "readable_text",
    "reap_or_abandon",
    "refusal_of",
    "release",
    "remove_temp_worktree",
    "run_scrubbed",
    "safe_pgid",
    "sandbox_hint",
    "signal_group",
    "signal_probe_alive",
    "start_scrubbed",
    "sweep_findings",
    "sweep_worktree",
    "timeout_message",
    "warn_sweep",
    "zone_dir",
]
