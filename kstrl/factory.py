"""Factory orchestrator - parallel component execution with 3-phase verification."""

from __future__ import annotations

import functools
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, TextIO

from kstrl.agents.base import UsageTotals, collect_usage
from kstrl.agents.proc import kill_active_process_groups
from kstrl.breaker import BreakerConfig
from kstrl.commandrun import start_heartbeat as _start_heartbeat
from kstrl.config import KstrlConfig
from kstrl.context import IterationContext
from kstrl.contract import (
    ContractCleanupError,
    ContractConfig,
    ContractMode,
    ContractResult,
    run_contract_testing,
)
from kstrl.events import (
    AdversarialAgentSelected,
    ComponentFailed,
    ComponentStarted,
    EventBus,
    EventSink,
    JsonlSink,
    PhaseStarted,
    RunCompleted,
    RunPaths,
    RunPlan,
    RunStarted,
    V1CompatSink,
)
from kstrl.events import (
    ContractResult as ContractResultEvent,
)
from kstrl.feedforward import FeedforwardConfig, build_feedforward_context
from kstrl.fixtures import FixturesConfig
from kstrl.git import fetch_base_branch, resolve_base_ref
from kstrl.interaction import InteractionChannel
from kstrl.knowledge import (
    KnowledgeConfig,
    build_knowledge_context,
    current_run_id,
    distill_facts,
    measure_fact_utilization,
)
from kstrl.linear import LinearConfig, build_linear_sink
from kstrl.manifest import Component, ComponentStatus, Manifest
from kstrl.observability import (
    NotifyConfig,
    NotifyHooks,
    NullProgressLog,
    ProgressLog,
)
from kstrl.pipeline import ComponentPipeline, PipelineHooks, _iso_now
from kstrl.pr import create_prs_in_order, create_single_pr
from kstrl.review import (
    ReviewMode,
    run_chunked_review,
    run_review,
)
from kstrl.sandbox import SandboxConfig
from kstrl.security import (
    SecurityConfig,
    SecurityMode,
    run_chunked_security_review,
    run_security_review,
)
from kstrl.shutdown import StopController
from kstrl.timeout import TimeoutConfig
from kstrl.ui.bridge import EventBridgeUI
from kstrl.verify import VerifyConfig, run_mechanical_verification

if TYPE_CHECKING:
    from kstrl.ui.base import UI


@dataclass
class FactoryConfig:
    """Configuration for factory orchestration."""

    max_parallel: int = 4
    max_retries: int = 3
    retry_delay: float = 5.0
    use_worktrees: bool = True
    single_pr: bool = False
    create_prs: bool = True
    verify_command: str | None = None
    # Phase 1: mechanical verification
    verify_config: VerifyConfig | None = None
    # R2.3 (CRIT-8): explicit skip sentinel for Phase 1. verify_config=None
    # keeps its historical meaning of "use the default checks"; only this
    # flag (set by --no-verify) genuinely skips mechanical verification.
    # The skip is stated in the run output and recorded as a phase_skipped
    # finding so "ran clean" and "never ran" stay distinguishable.
    skip_verification: bool = False
    # Phase 2: reviewer agent
    review_mode: str = ReviewMode.HARD.value
    review_agent_cmd: str | None = None
    review_agent_type: str | None = None
    review_model: str | None = None
    # Phase 2.5: security review (separate LLM call after Phase 2 review)
    security_config: SecurityConfig | None = None
    # Phase 3: contract testing
    contract_config: ContractConfig | None = None
    # Phase 0: feedforward
    feedforward_config: FeedforwardConfig | None = None
    # Observability. R3.2: the progress log defaults ON so a walk-away
    # run always leaves a consumable event trail; progress_log_enabled
    # = false (toml/env) turns it off. progress_log_path=None means the
    # default <root>/.kstrl/progress.jsonl.
    progress_log_path: Path | None = None
    progress_log_enabled: bool = True
    # R3.2: [notify] hooks (on_complete / on_first_failure shell
    # commands). None means run_factory loads NotifyConfig.load(root_dir).
    notify_config: NotifyConfig | None = None
    # R7.4: [linear] integration. None means run_factory loads
    # LinearConfig.load(root_dir). Observability only: the sink attaches
    # to the progress log and its failures never affect the run.
    linear_config: LinearConfig | None = None
    # E4: per-run hard cap on adversarial LLM calls (review + security
    # + knowledge distill). 0 means unbounded. Once exceeded the
    # remaining components skip those phases with an informational log
    # line; mechanical verify + the implementing agent continue. This
    # protects against runaway-cost factory runs.
    max_adversarial_calls: int = 0
    # E6: when True, pause and prompt the user before each component's
    # PR creation step. Off by default; opt-in for sensitive projects.
    pause_before_pr_merge: bool = False
    # R3.1: run-level token budget. 0 means unbounded. Compared against
    # the run's aggregated total_tokens (a lower bound when some calls
    # report no usage); on breach the factory halts LOUDLY - the current
    # component fails with a synthetic budget finding and pending
    # components fail at scheduling instead of burning more spend.
    # Enforcement granularity is the phase boundary: an in-flight
    # engineer loop or review call can overshoot before the parent sees
    # its usage.
    max_total_tokens: int = 0
    # R0.1: timeout limits (agent iteration, component wall clock,
    # scheduler backstop margin). None means run_factory loads
    # TimeoutConfig.load(root_dir) - toml [timeout] section + env.
    timeout_config: TimeoutConfig | None = None
    # R0.2: how long push_create_and_merge_pr waits for merge
    # confirmation before the component is parked as MERGE_PENDING.
    merge_timeout: float = 300.0
    # R0.5: proceed even when another invocation holds the run-level
    # .kstrl/factory.lock. Deliberately CLI-only (no toml/env source):
    # forcing past the lock can corrupt a live run's worktrees and
    # manifest, so it must be an explicit per-invocation decision.
    force_lock: bool = False
    # R3.3: keep a FAILED component's worktree at end-of-run cleanup so
    # the operator can post-mortem it (the failure summary points at
    # it). Kept worktrees are recorded as evidence pointers in the
    # manifest and survive the next run's stale-worktree prune for as
    # long as the component stays FAILED.
    keep_worktrees_on_failure: bool = False
    # R7.2: approved-fixtures oracle for Phase 1. None means run_factory
    # loads FixturesConfig.load(root_dir) - toml [fixtures] section +
    # env - so `ks factory` honors the config with no CLI wiring.
    # Default-off ([fixtures].enabled = false, roadmap user decision 4):
    # fixtures execute PRD-defined commands, so the operator opts in.
    fixtures_config: FixturesConfig | None = None

    @classmethod
    def from_env(cls) -> FactoryConfig:
        """Load factory config from environment variables."""
        from kstrl.config import _parse_bool

        return cls(
            max_parallel=int(os.environ.get("FACTORY_MAX_PARALLEL", "4")),
            max_retries=int(os.environ.get("FACTORY_MAX_RETRIES", "3")),
            retry_delay=float(os.environ.get("FACTORY_RETRY_DELAY", "5.0")),
            merge_timeout=float(os.environ.get("FACTORY_MERGE_TIMEOUT", "300.0")),
            max_adversarial_calls=int(
                os.environ.get("KSTRL_FACTORY_MAX_ADVERSARIAL_CALLS", "0")
            ),
            max_total_tokens=int(
                os.environ.get("KSTRL_FACTORY_MAX_TOTAL_TOKENS", "0")
            ),
            pause_before_pr_merge=_parse_bool(
                os.environ.get("KSTRL_FACTORY_PAUSE_BEFORE_PR_MERGE")
            ),
            progress_log_enabled=_parse_bool(
                os.environ.get("KSTRL_FACTORY_PROGRESS_LOG_ENABLED", "1")
            ),
            keep_worktrees_on_failure=_parse_bool(
                os.environ.get("KSTRL_FACTORY_KEEP_WORKTREES_ON_FAILURE")
            ),
        )

    @classmethod
    def load(cls, root_dir: Path | None = None) -> FactoryConfig:
        """Load factory config with precedence: env > toml > defaults.

        Reads the ``[factory]`` section from ``<root_dir>/kstrl.toml`` if
        present, then overlays any matching env vars on top.
        """
        from kstrl.config import _parse_bool, load_toml_section, resolve_config_file
        if root_dir is None:
            root_dir = Path.cwd()
        config = cls()
        section = load_toml_section(resolve_config_file(root_dir), "factory")
        if "max_parallel" in section:
            config.max_parallel = int(section["max_parallel"])
        if "max_retries" in section:
            config.max_retries = int(section["max_retries"])
        if "retry_delay" in section:
            config.retry_delay = float(section["retry_delay"])
        if "use_worktrees" in section:
            config.use_worktrees = bool(section["use_worktrees"])
        if "single_pr" in section:
            config.single_pr = bool(section["single_pr"])
        if "create_prs" in section:
            config.create_prs = bool(section["create_prs"])
        if "review_mode" in section:
            config.review_mode = str(section["review_mode"])
        if "merge_timeout" in section:
            config.merge_timeout = float(section["merge_timeout"])
        # R2.2: the two safety knobs are reachable via toml (here), env
        # (below) and CLI flags (cli.py factory command).
        if "max_adversarial_calls" in section:
            config.max_adversarial_calls = int(section["max_adversarial_calls"])
        if "max_total_tokens" in section:
            config.max_total_tokens = int(section["max_total_tokens"])
        if "pause_before_pr_merge" in section:
            config.pause_before_pr_merge = bool(section["pause_before_pr_merge"])
        if "progress_log_enabled" in section:
            config.progress_log_enabled = bool(section["progress_log_enabled"])
        if "keep_worktrees_on_failure" in section:
            config.keep_worktrees_on_failure = bool(
                section["keep_worktrees_on_failure"]
            )
        # Env overrides (consistent with from_env)
        if "FACTORY_MAX_PARALLEL" in os.environ:
            config.max_parallel = int(os.environ["FACTORY_MAX_PARALLEL"])
        if "FACTORY_MAX_RETRIES" in os.environ:
            config.max_retries = int(os.environ["FACTORY_MAX_RETRIES"])
        if "FACTORY_RETRY_DELAY" in os.environ:
            config.retry_delay = float(os.environ["FACTORY_RETRY_DELAY"])
        if "FACTORY_MERGE_TIMEOUT" in os.environ:
            config.merge_timeout = float(os.environ["FACTORY_MERGE_TIMEOUT"])
        if "KSTRL_FACTORY_MAX_ADVERSARIAL_CALLS" in os.environ:
            config.max_adversarial_calls = int(
                os.environ["KSTRL_FACTORY_MAX_ADVERSARIAL_CALLS"]
            )
        if "KSTRL_FACTORY_MAX_TOTAL_TOKENS" in os.environ:
            config.max_total_tokens = int(
                os.environ["KSTRL_FACTORY_MAX_TOTAL_TOKENS"]
            )
        if "KSTRL_FACTORY_PAUSE_BEFORE_PR_MERGE" in os.environ:
            config.pause_before_pr_merge = _parse_bool(
                os.environ["KSTRL_FACTORY_PAUSE_BEFORE_PR_MERGE"]
            )
        if "KSTRL_FACTORY_PROGRESS_LOG_ENABLED" in os.environ:
            config.progress_log_enabled = _parse_bool(
                os.environ["KSTRL_FACTORY_PROGRESS_LOG_ENABLED"]
            )
        if "KSTRL_FACTORY_KEEP_WORKTREES_ON_FAILURE" in os.environ:
            config.keep_worktrees_on_failure = _parse_bool(
                os.environ["KSTRL_FACTORY_KEEP_WORKTREES_ON_FAILURE"]
            )
        return config


# R7.1: cross-model review rotation. Self-preference bias means a
# same-family reviewer systematically misses the bug classes its own
# family produces, so when no explicit reviewer config is given the
# review and security phases default to the OPPOSITE model family from
# the engineer (user decision 2: the OpenAI family via the codex CLI
# reviews Claude-engineered code; a codex engineer flips the default to
# claude-code). The engineer always keeps the primary family.
_CROSS_FAMILY_TYPE: dict[str, str] = {
    "claude-code": "codex",
    "codex": "claude-code",
}


def _cli_family(
    agent_cmd: str | None,
    agent_type: str | None,
    claude_available: bool,
) -> str | None:
    """Which MODEL family a (cmd, type) config resolves to, mirroring
    ``agents.get_agent`` dispatch exactly: a custom command is an
    unknown family (None); "claude-sdk" is the Claude family through
    the SDK transport (R7.6 - without this branch it would fall through
    to codex and INVERT the R7.1 rotation for SDK engineers);
    "auto"/None auto-detects claude-code first; any unrecognized type
    string falls through to codex."""
    if agent_cmd:
        return None
    if agent_type in ("claude-code", "claude-sdk"):
        return "claude-code"
    if agent_type in (None, "auto"):
        return "claude-code" if claude_available else "codex"
    return "codex"


def _agent_identity(
    agent_cmd: str | None,
    agent_type: str | None,
    model: str | None,
    claude_available: bool,
) -> str:
    """Reviewing-model identity for a configuration, matching the agent
    adapters' ``name`` property ("codex (gpt-5)", "claude-code",
    "claude-sdk (haiku)", "custom (<cmd>)") so findings attributed
    before an agent exists match what a live run stamps on its
    results. "claude-sdk" keeps its own identity label (the adapter
    name is the transport, distinct from its claude-code FAMILY used
    for rotation)."""
    if agent_cmd:
        return f"custom ({agent_cmd})"
    if agent_type == "claude-sdk":
        label = "claude-sdk"
    else:
        label = _cli_family(agent_cmd, agent_type, claude_available) or "unknown"
    if model:
        return f"{label} ({model})"
    return label


@dataclass(frozen=True)
class AdversarialAgentSelection:
    """Resolved agent configuration for one adversarial phase (R7.1).

    ``source`` records how the resolution went: "explicit" (operator
    config always wins), "cross-family-default" (the R7.1 rotation
    default), or "same-family-fallback" (heterogeneity unavailable;
    ``warning`` then carries the homogeneity risk statement to print).
    """

    phase: str
    agent_cmd: str | None
    agent_type: str | None
    model: str | None
    reasoning: str | None
    source: str
    identity: str
    warning: str | None = None


def resolve_adversarial_selection(
    phase: str,
    *,
    explicit_cmd: str | None,
    explicit_type: str | None,
    explicit_model: str | None,
    fallback_cmd: str | None,
    fallback_type: str | None,
    fallback_model: str | None,
    fallback_reasoning: str | None,
    engineer_cmd: str | None,
    engineer_type: str | None,
    claude_available: bool | None = None,
    codex_available: bool | None = None,
) -> AdversarialAgentSelection:
    """Resolve which agent reviews this run's diffs (R7.1).

    Precedence:
    1. Any explicit field (cmd/type/model) makes the whole selection
       explicit: each unset field falls back to the phase's historical
       fallback, exactly as before R7.1. No warning - an operator who
       pins a same-family reviewer has decided so deliberately.
    2. Otherwise, when the engineer's family is known and the opposite
       family's CLI is available, the reviewer defaults to that family
       (adapter-default model; reasoning deliberately not inherited -
       effort strings do not transfer across families).
    3. Otherwise the reviewer falls back to the same configuration as
       today (same family as the engineer) and ``warning`` names the
       self-preference risk.

    ``claude_available``/``codex_available`` default to probing the
    real CLIs; tests inject both.
    """
    from kstrl.agents import ClaudeCodeAgent, CodexAgent

    if claude_available is None:
        claude_available = ClaudeCodeAgent.is_available()
    if codex_available is None:
        codex_available = CodexAgent.is_available()

    explicit = any(
        v is not None for v in (explicit_cmd, explicit_type, explicit_model)
    )
    if explicit:
        cmd = explicit_cmd if explicit_cmd is not None else fallback_cmd
        agent_type = (
            explicit_type if explicit_type is not None else fallback_type
        )
        model = explicit_model if explicit_model is not None else fallback_model
        return AdversarialAgentSelection(
            phase=phase,
            agent_cmd=cmd,
            agent_type=agent_type,
            model=model,
            reasoning=fallback_reasoning,
            source="explicit",
            identity=_agent_identity(cmd, agent_type, model, claude_available),
        )

    engineer_family = _cli_family(engineer_cmd, engineer_type, claude_available)
    cross_type = (
        _CROSS_FAMILY_TYPE.get(engineer_family) if engineer_family else None
    )
    cross_available = (
        codex_available if cross_type == "codex" else claude_available
    )
    if cross_type is not None and cross_available:
        return AdversarialAgentSelection(
            phase=phase,
            agent_cmd=None,
            agent_type=cross_type,
            model=None,
            reasoning=None,
            source="cross-family-default",
            identity=_agent_identity(None, cross_type, None, claude_available),
        )

    if engineer_family is None:
        warning = (
            f"Homogeneity risk (R7.1): the engineer runs a custom agent "
            f"command, so its model family is unknown and the "
            f"cross-family default cannot be applied; the {phase} "
            f"reviewer falls back to the same configuration. "
            "Self-preference bias means a same-family reviewer "
            "systematically misses the bug classes its own family "
            f"produces. Set an explicit {phase} agent config on a "
            "different model family to restore cross-family review."
        )
    else:
        warning = (
            f"Homogeneity risk (R7.1): the {cross_type} CLI is not "
            f"available, so the {phase} reviewer runs on the same model "
            f"family as the engineer ({engineer_family}). "
            "Self-preference bias means a same-family reviewer "
            "systematically misses the bug classes its own family "
            f"produces. Install the {cross_type} CLI for cross-family "
            f"review, or set an explicit {phase} agent config to accept "
            "the risk silently."
        )
    return AdversarialAgentSelection(
        phase=phase,
        agent_cmd=fallback_cmd,
        agent_type=fallback_type,
        model=fallback_model,
        reasoning=fallback_reasoning,
        source="same-family-fallback",
        identity=_agent_identity(
            fallback_cmd, fallback_type, fallback_model, claude_available,
        ),
        warning=warning,
    )


@dataclass
class ComponentResult:
    """Result from running a single component."""

    component_id: str
    success: bool
    iterations: int = 0
    error: str | None = None
    duration_seconds: float = 0.0
    context_json: str | None = None
    # R3.1: engineer-loop usage aggregated by the worker; pickled back
    # across the ProcessPoolExecutor boundary. None means the worker
    # predates the meter or crashed before the loop started.
    usage: UsageTotals | None = None
    # R7.5: the no-progress circuit breaker halted the loop. Routed by
    # the pipeline to a direct FAILED transition (retrying the same
    # prompt against the same state is the exact spend the breaker
    # exists to stop) with a distinct journal event.
    no_progress: bool = False


@dataclass
class FactoryResult:
    """Overall result from the factory run."""

    completed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    # R0.2: components whose PR merge was initiated but not confirmed.
    # Not failed - a factory re-run re-polls them - but their dependents
    # were not scheduled, so the run is incomplete (nonzero exit code).
    merge_pending: list[str] = field(default_factory=list)
    pr_urls: list[str] = field(default_factory=list)
    # R0.3: unresolved contract failures (one human-readable line per
    # failed check). Non-empty forces a nonzero exit code even when no
    # single component could be blamed.
    contract_failures: list[str] = field(default_factory=list)
    exit_code: int = 0


class FactoryLockHeldError(RuntimeError):
    """Another factory invocation holds the run-level lock on this root."""


@dataclass
class _RunLock:
    """Handle for the run-level factory lock.

    ``held=True`` means we hold an exclusive flock for the whole run and
    may safely prune state left by previous runs. ``held=False`` means we
    are running WITHOUT exclusion (Windows/no-fcntl degrade, or
    ``--force-lock``): stale-state cleanup must be skipped because another
    live invocation may own it.
    """

    fp: IO[str] | None
    held: bool

    def release(self) -> None:
        if self.fp is None:
            return
        try:
            import fcntl
            fcntl.flock(self.fp.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        self.fp.close()
        self.fp = None


def _acquire_run_lock(root_dir: Path, ui: UI, force: bool) -> _RunLock:
    """Take the run-level flock on ``.kstrl/factory.lock`` (R0.5, H-7).

    Held for the entire run so a second ``ks factory`` / ``ks run``
    on the same root refuses to start instead of destroying the first
    invocation's in-flight worktrees and clobbering its manifest. flock
    releases automatically if the holder dies, so a crashed run never
    wedges the root.

    POSIX only, like the A4 per-component lock: without fcntl we degrade
    to no exclusion with a warning. ``force=True`` proceeds past a held
    lock with a warning instead of raising FactoryLockHeldError.
    """
    from kstrl.statedir import state_dir

    lock_path = state_dir(root_dir) / "factory.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:
        ui.warn(
            "Run-level factory lock unavailable on this platform (no "
            "fcntl); concurrent invocations on this root are not excluded"
        )
        return _RunLock(fp=None, held=False)

    # "a+" so a refused attempt can read the holder's pid without
    # truncating it.
    fp = open(lock_path, "a+")
    try:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        holder = ""
        try:
            fp.seek(0)
            holder = fp.read(64).strip()
        except OSError:
            pass
        fp.close()
        holder_note = f" (pid {holder})" if holder else ""
        if force:
            ui.warn(
                f"--force-lock: proceeding while {lock_path} is held"
                f"{holder_note}; concurrent runs can corrupt each "
                f"other's worktrees and manifest"
            )
            return _RunLock(fp=None, held=False)
        raise FactoryLockHeldError(
            f"Another kstrl invocation{holder_note} holds {lock_path}; "
            f"refusing to start a second factory run on this root. "
            f"Wait for it to finish, or re-run with --force-lock to "
            f"override."
        ) from None

    # Holder pid is diagnostic only (shown in the refusal message of a
    # contending invocation); the flock itself is the exclusion.
    try:
        fp.seek(0)
        fp.truncate()
        fp.write(f"{os.getpid()}\n")
        fp.flush()
    except OSError:
        pass
    return _RunLock(fp=fp, held=True)


def _remove_stale_index_lock(root_dir: Path, component_id: str) -> None:
    """Remove a stale index.lock left behind by a killed git operation.

    A timed-out agent is SIGKILLed and can die mid-git-op inside its
    worktree; git then refuses every subsequent operation there. The lock
    for a worktree lives under the MAIN repo's .git/worktrees/<name>/.
    Only the component's own lock is touched - the main repo's
    .git/index.lock may belong to a live operator process and is left
    alone.
    """
    lock = root_dir / ".git" / "worktrees" / component_id / "index.lock"
    try:
        lock.unlink(missing_ok=True)
    except OSError:
        pass


def _setup_worktree(
    component_id: str,
    branch_name: str,
    base_branch: str,
    root_dir: Path,
    run_id: str,
    fresh_from_base: bool = False,
) -> Path:
    """Create a git worktree for a component.

    Worktrees are keyed ``.kstrl/worktrees/<run_id>/<component_id>``
    (R0.5, H-7): two invocations never share a worktree path, so setup
    can only ever remove a leftover from an earlier attempt of THIS run
    (a retry), never another invocation's in-flight worktree. Run-level
    exclusion itself is the ``.kstrl/factory.lock`` flock in run_factory.

    A per-host fcntl flock on ``.kstrl/worktrees/<component_id>.lock``
    (run-agnostic on purpose) still serializes the git commands here for
    the degraded modes that run without the run-level lock (Windows,
    ``--force-lock``), where two invocations could otherwise race on the
    shared branch and .git metadata.

    ``fresh_from_base=True`` (used for retries after a timeout kill, and
    for merge-conflict re-runs under the R7.5 re-run doctrine)
    additionally deletes the component branch so the worktree is recreated
    from ``base_branch`` instead of silently reusing possibly-dirty state
    from the killed attempt (R0.1).

    POSIX only. On Windows the fcntl import fails; we degrade to the
    pre-lock behavior and document the limitation in the runbook.
    """
    worktree_base = root_dir / ".kstrl" / "worktrees" / run_id
    worktree_base.mkdir(parents=True, exist_ok=True)
    worktree_path = worktree_base / component_id
    lock_path = root_dir / ".kstrl" / "worktrees" / f"{component_id}.lock"

    lock_fp = None
    try:
        try:
            import fcntl
            lock_fp = open(lock_path, "w")
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            # Windows / unusual filesystems where flock isn't available.
            # We've already opened lock_fp on the Windows path? No -
            # ImportError on fcntl skips the open above. Just continue
            # without the lock; documented as a Windows non-support
            # caveat.
            lock_fp = None

        # A killed prior attempt may have left git mid-operation.
        _remove_stale_index_lock(root_dir, component_id)

        # Unconditional: a crashed attempt's directory may be gone (tmp
        # cleaner, operator rm -rf) while its .git/worktrees/<comp>/
        # registration survives, and `git worktree add` refuses over a
        # registered-but-missing entry. remove --force clears the
        # registration in that state too (measured on git 2.47); when
        # nothing is registered it fails harmlessly, like `branch -D`.
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=root_dir, capture_output=True, timeout=30,
        )

        if fresh_from_base:
            # Delete the branch from the killed attempt so the add below
            # recreates it from base rather than reusing its commits.
            subprocess.run(
                ["git", "branch", "-D", branch_name],
                cwd=root_dir, capture_output=True, timeout=30,
            )

        # R0.2: cut from origin/<base> when a remote exists so this
        # component builds on the squash-merged history of its
        # dependencies, not a stale local base ref. The fetch is
        # freshness-only and non-fatal: offline runs fall back to the
        # current tracking ref, local-only repos to the local base.
        fetch_base_branch(base_branch, root_dir, timeout=60.0)
        base_ref = resolve_base_ref(base_branch, root_dir)

        result = subprocess.run(
            ["git", "worktree", "add", str(worktree_path), "-b", branch_name, base_ref],
            cwd=root_dir, capture_output=True, text=True, timeout=30,
        )

        if result.returncode != 0:
            # Branch already exists: reuse it WITH its commits. After the
            # run-start preflight (_preflight_component_branches) this can
            # only be a branch created during THIS run - a non-timeout
            # retry resuming its own progress, or single_pr components
            # stacking on the shared branch. Stale branches from previous
            # runs were deleted (fully merged) or refused at preflight,
            # never silently reused here (R0.5).
            result = subprocess.run(
                ["git", "worktree", "add", str(worktree_path), branch_name],
                cwd=root_dir, capture_output=True, text=True, timeout=30,
            )

        if result.returncode != 0:
            error = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(
                f"Failed to create worktree for '{component_id}': {error}"
            )

        return worktree_path
    finally:
        if lock_fp is not None:
            try:
                import fcntl
                fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
            lock_fp.close()


def _cleanup_worktree(component_id: str, root_dir: Path, run_id: str) -> None:
    """Remove a git worktree for a component of the current run."""
    worktree_path = root_dir / ".kstrl" / "worktrees" / run_id / component_id
    if not worktree_path.exists():
        return
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(worktree_path)],
        cwd=root_dir, capture_output=True, timeout=30,
    )


def _evidence_worktrees_to_keep(manifest: Manifest) -> set[str]:
    """Worktree paths the stale-prune pass must preserve (R3.3).

    A worktree kept by ``keep_worktrees_on_failure`` stays referenced as
    the FAILED component's evidence pointer; once the component leaves
    FAILED (retried, or reset) the reference is cleared and the next
    prune removes it. Both the recorded and resolved spellings are
    included so path normalization differences cannot defeat the match.
    """
    keep: set[str] = set()
    for comp in manifest.components:
        if (
            comp.status == ComponentStatus.FAILED.value
            and comp.evidence_worktree
        ):
            keep.add(comp.evidence_worktree)
            try:
                keep.add(str(Path(comp.evidence_worktree).resolve()))
            except OSError:
                pass
    return keep


def _prune_stale_worktrees(
    root_dir: Path, run_id: str, ui: UI, keep: set[str] | None = None,
) -> None:
    """Remove worktrees left behind by previous (crashed/aborted) runs.

    Only called when the run-level flock is genuinely held: any prior
    holder has exited (flock dies with its process), so everything under
    ``.kstrl/worktrees/`` that is not ours - other runs' ``<run_id>/``
    dirs, and pre-R0.5 flat-layout ``<component_id>/`` worktrees - is
    orphaned and safe to remove. Includes worktrees kept for leaked
    workers (R0.1): their owning run is gone, so by the next invocation
    they are stale state, matching the pre-R0.5 force-remove behavior.

    ``keep`` (R3.3) lists evidence worktrees of still-FAILED components
    (kept via keep_worktrees_on_failure); those are preserved so a
    resume does not destroy the post-mortem state it exists to protect.
    """
    keep = keep or set()

    def _kept(path: Path) -> bool:
        if str(path) in keep:
            return True
        try:
            return str(path.resolve()) in keep
        except OSError:
            return False

    worktree_root = root_dir / ".kstrl" / "worktrees"
    if not worktree_root.exists():
        return
    removed = 0
    kept = 0
    for entry in sorted(worktree_root.iterdir()):
        if entry.name == run_id or not entry.is_dir():
            continue  # our own run dir, or a per-component .lock file
        if (entry / ".git").exists():
            # Pre-R0.5 flat layout: the entry itself is a worktree.
            if _kept(entry):
                kept += 1
                continue
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(entry)],
                cwd=root_dir, capture_output=True, timeout=30,
            )
            removed += 1
        else:
            # <run_id>/ dir from a previous run: remove each component
            # worktree inside it.
            entry_kept = 0
            for wt in sorted(entry.iterdir()):
                if wt.is_dir():
                    if _kept(wt):
                        entry_kept += 1
                        continue
                    subprocess.run(
                        ["git", "worktree", "remove", "--force", str(wt)],
                        cwd=root_dir, capture_output=True, timeout=30,
                    )
                    # Whatever git could not remove goes with the dir.
                    shutil.rmtree(wt, ignore_errors=True)
                    removed += 1
            kept += entry_kept
            if entry_kept:
                # Evidence lives inside: keep the run dir itself.
                continue
        # Whatever git could not remove (or non-worktree debris) goes
        # with the dir; `git worktree prune` below drops any metadata
        # orphaned by this.
        shutil.rmtree(entry, ignore_errors=True)
    if removed:
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=root_dir, capture_output=True, timeout=30,
        )
        ui.info(f"  Pruned {removed} stale worktree(s) from previous runs")
    if kept:
        ui.info(
            f"  Preserved {kept} evidence worktree(s) of failed "
            f"components (keep_worktrees_on_failure)"
        )


def _preflight_component_branches(
    manifest: Manifest, root_dir: Path, ui: UI,
) -> list[str]:
    """Refuse to silently reuse component branches from previous runs.

    For every branch a PENDING component would be provisioned on: if it
    already exists and is fully merged into the base branch, delete it
    (setup recreates it from base); if it exists with unmerged commits,
    return an error naming it - the caller refuses the run and the
    operator decides (merge or ``git branch -D``). Previously such
    branches were silently reused with their old commits via the
    worktree-add fallback (R0.5, H-7).

    Note: a squash-merged branch is NOT an ancestor of base (the squash
    rewrites history), so leftovers from squash-merge flows are refused
    rather than auto-deleted. Loud beats lossy.
    """
    errors: list[str] = []
    fetch_base_branch(manifest.base_branch, root_dir, timeout=60.0)
    base_ref = resolve_base_ref(manifest.base_branch, root_dir)
    seen: set[str] = set()
    for comp in manifest.components:
        if comp.status != ComponentStatus.PENDING.value:
            continue
        branch = comp.branch_name
        if branch in seen:
            continue  # single_pr: all components share one branch
        seen.add(branch)
        exists = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=root_dir, capture_output=True, timeout=30,
        )
        if exists.returncode != 0:
            continue
        merged = subprocess.run(
            ["git", "merge-base", "--is-ancestor", branch, base_ref],
            cwd=root_dir, capture_output=True, timeout=30,
        )
        if merged.returncode == 0:
            deleted = subprocess.run(
                ["git", "branch", "-D", branch],
                cwd=root_dir, capture_output=True, text=True, timeout=30,
            )
            if deleted.returncode == 0:
                ui.info(
                    f"  Deleted stale branch '{branch}' from a previous "
                    f"run (fully merged into {manifest.base_branch})"
                )
            else:
                errors.append(
                    f"stale branch '{branch}' (component '{comp.id}') is "
                    f"fully merged but could not be deleted: "
                    f"{deleted.stderr.strip()}"
                )
        else:
            errors.append(
                f"branch '{branch}' (component '{comp.id}') already exists "
                f"with commits not merged into '{manifest.base_branch}'; "
                f"refusing to silently reuse it. Merge it or delete it "
                f"(git branch -D {branch}) and re-run."
            )
    return errors


def _run_component(
    component_id: str,
    prd_path_str: str,
    worktree_path_str: str,
    root_dir_str: str,
    prompt_file_str: str,
    agent_cmd: str | None,
    model: str | None,
    reasoning: str | None,
    agent_type: str | None,
    sleep_seconds: float,
    previous_context_json: str | None = None,
    feedforward_config_dict: dict[str, Any] | None = None,
    scaffold_cmd: str | None = None,
    component_deps: list[str] | None = None,
    knowledge_prefix: str = "",
    progress_file_str: str = "scripts/kstrl/progress.txt",
    codebase_map_file_str: str = "scripts/kstrl/codebase_map.md",
    agent_iteration_timeout: float = 1800.0,
    component_timeout: float = 7200.0,
    max_iterations: int = 10,
    interactive: bool = False,
    allowed_paths: list[str] | None = None,
    breaker_iterations: int = 3,
    breaker_test_command: str | None = None,
    breaker_test_timeout: float = 300.0,
    sandbox_enabled: bool = False,
    sandbox_allow_network: bool = False,
    agent_budget_usd: float | None = None,
    events_dir_str: str | None = None,
    run_id: str = "",
    redirect_output: bool = True,
    live_line: Callable[[str], None] | None = None,
    stop_check: Callable[[], bool] | None = None,
) -> ComponentResult:
    """Run a single component's implementation loop.

    Top-level function (picklable for ProcessPoolExecutor).
    Creates all objects internally - no shared state.

    Chunk 6 (TUI rewrite): with ``events_dir_str`` set, the worker
    writes typed events to <events_dir>/components/<id>/engineer.jsonl,
    the raw agent transcript to engineer.log, and (pool mode) dup2's
    its inherited stdout/stderr onto that same log so parallel workers
    stop interleaving raw lines on the parent terminal. ``live_line``
    (inline mode only - never pickled) mirrors each transcript line to
    the parent's UI so sequential runs keep live engineer output.
    Without ``events_dir_str`` the legacy PlainUI-on-stderr behavior is
    preserved for direct callers.
    """
    from kstrl.agents import get_agent
    from kstrl.loop import run_loop
    from kstrl.ui.bridge import EventBridgeUI, NullPrompter
    from kstrl.ui.plain import PlainUI

    start = time.monotonic()
    worktree_path = Path(worktree_path_str)
    # R0.4: every copy source below resolves against root_dir, never the
    # worker's inherited CWD. prompt.md and the PRD live under gitignored
    # scripts/kstrl/, so a fresh worktree NEVER contains them via git; if
    # a CWD-relative lookup missed them (e.g. --root from another
    # directory) the copies silently no-op'd and the engineer fell back
    # to the harness DEFAULT_PROMPT (phase-f e2e validation, line 38).
    root_dir = Path(root_dir_str)

    ui: UI
    worker_bus: EventBus | None = None
    transcript_fh: TextIO | None = None
    stop_heartbeat: Callable[[], None] | None = None
    run_paths: RunPaths | None = None
    if events_dir_str is None:
        ui = PlainUI(no_color=True)
    else:
        run_paths = RunPaths(root=Path(events_dir_str))
        comp_dir = run_paths.component_dir(component_id)
        try:
            comp_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        if redirect_output:
            # dup2 BEFORE any threads start (chunk 6 invariant); stray
            # library writes land in the transcript, never the terminal.
            _redirect_worker_output(run_paths.engineer_log(component_id))
            # PR B: a shutdown SIGTERM from the parent must group-kill
            # this worker's agent subprocess (its own session leader),
            # not orphan it. Pool mode only - inline mode runs in the
            # parent, whose handlers belong to the cli/TUI.
            _install_worker_signal_forwarding()

    agent = get_agent(
        agent_cmd, model, reasoning, agent_type,
        sandbox=SandboxConfig(
            enabled=sandbox_enabled, allow_network=sandbox_allow_network,
        ),
        max_budget_usd=agent_budget_usd,
    )

    # Copy PRD into worktree if needed
    worktree_prd = worktree_path / prd_path_str
    prd_source = root_dir / prd_path_str
    if not worktree_prd.exists() and prd_source.exists():
        worktree_prd.parent.mkdir(parents=True, exist_ok=True)
        worktree_prd.write_text(prd_source.read_text())

    # The prompt template's $prd_path placeholder (shipped in
    # DEFAULT_PROMPT >= 1.1.0 and the scaffolded prompt.md) is substituted
    # at runtime by loop.py with config.prd_file, so the agent reads the
    # SAME per-component PRD that check_prd_stories re-reads (R2.3, H-11)
    # without overwriting scripts/kstrl/prd.json.

    # Copy prompt into worktree if needed
    worktree_prompt = worktree_path / prompt_file_str
    prompt_source = root_dir / prompt_file_str
    if not worktree_prompt.exists() and prompt_source.exists():
        worktree_prompt.parent.mkdir(parents=True, exist_ok=True)
        worktree_prompt.write_text(prompt_source.read_text())

    # Copy CLAUDE.md / AGENTS.md into the worktree from root_dir. When
    # use_worktrees=False, worktree_path IS the repo root so the files are
    # already in place - the .exists() guards handle this correctly.
    claude_dest = worktree_path / "CLAUDE.md"
    claude_src = root_dir / "CLAUDE.md"
    if not claude_dest.exists() and claude_src.exists():
        claude_dest.write_text(claude_src.read_text())
    agents_dest = worktree_path / "AGENTS.md"
    agents_src = root_dir / "AGENTS.md"
    if not agents_dest.exists():
        if agents_src.is_symlink() and claude_dest.exists():
            # Preserve the AGENTS.md -> CLAUDE.md symlink convention.
            agents_dest.symlink_to("CLAUDE.md")
        elif agents_src.exists():
            agents_dest.write_text(agents_src.read_text())
        elif claude_dest.exists():
            agents_dest.symlink_to("CLAUDE.md")

    # Run scaffold script if configured
    if scaffold_cmd:
        try:
            subprocess.run(
                scaffold_cmd, shell=True, cwd=worktree_path,
                capture_output=True, timeout=120,
            )
        except Exception:
            pass  # scaffold failure is non-fatal

    # Build feedforward context (Phase 0)
    feedforward_prefix: str = ""
    if feedforward_config_dict:
        try:
            ff_config = FeedforwardConfig(**feedforward_config_dict)
            feedforward_prefix = build_feedforward_context(
                worktree_path, ff_config,
                component_id=component_id,
                component_deps=component_deps,
            )
        except Exception:
            pass  # feedforward failure is non-fatal

    # Build context prefix from previous retries
    context_prefix: str | None = None
    parts: list[str] = []
    if knowledge_prefix:
        parts.append(knowledge_prefix)
    if feedforward_prefix:
        parts.append(feedforward_prefix)
    if previous_context_json:
        ctx = IterationContext.from_json(previous_context_json)
        formatted = ctx.format_for_prompt()
        if formatted.strip():
            parts.append(formatted)
    if parts:
        context_prefix = "\n\n".join(parts)

    # R2.3 (CRIT-8): max_iterations, interactive, and allowed_paths come
    # from the invoking config via _submit_args. They were previously
    # hardcoded here (30 / False / unset), which made `ks run N`, -i,
    # and --allowed-paths silent no-ops under the factory pipeline.
    config = KstrlConfig(
        max_iterations=max_iterations,
        prompt_file=worktree_prompt,
        prd_file=worktree_prd,
        progress_file=worktree_path / progress_file_str,
        codebase_map_file=worktree_path / codebase_map_file_str,
        sleep_seconds=sleep_seconds,
        interactive=interactive,
        allowed_paths=list(allowed_paths) if allowed_paths else [],
        kstrl_branch="",
        kstrl_branch_explicit=True,
        agent_cmd=agent_cmd,
        model=model,
        model_reasoning_effort=reasoning,
        agent_type=agent_type,
        ui_mode="plain",
        no_color=True,
    )

    timeouts = TimeoutConfig(
        agent_iteration=agent_iteration_timeout,
        component_total=component_timeout,
    )
    breaker_config = BreakerConfig(
        no_progress_iterations=breaker_iterations,
        test_command=breaker_test_command,
        test_timeout=breaker_test_timeout,
    )

    # Start event-owned resources only after setup succeeds. A get_agent,
    # file-copy, or config failure therefore cannot leak a heartbeat thread
    # or open JSONL/transcript handles in a reusable pool worker.
    if run_paths is not None:
        try:
            transcript_fh = open(
                run_paths.engineer_log(component_id),
                "a", buffering=1, encoding="utf-8",
            )
        except OSError:
            transcript_fh = None

        def _transcript(line: str) -> None:
            if transcript_fh is not None:
                transcript_fh.write(line + "\n")
            if live_line is not None:
                live_line(line)

        worker_bus = EventBus(
            JsonlSink(run_paths.engineer_events(component_id)),
            run_id=run_id, source="worker", component=component_id,
        )
        ui = EventBridgeUI(
            worker_bus, prompter=NullPrompter(), transcript=_transcript,
        )
        stop_heartbeat = _start_heartbeat(worker_bus)

    try:
        result = run_loop(
            config, ui, agent, worktree_path,
            context_prefix=context_prefix, timeouts=timeouts,
            breaker_config=breaker_config,
            bus=worker_bus,
            stop_check=stop_check,
        )
        # Report which limit fired so the retry/fail path can act on it
        # (timeout errors trigger the recreate-from-base retry hygiene).
        if result.completed:
            error = None
        elif result.no_progress:
            error = (
                "no-progress circuit breaker tripped: "
                f"{breaker_iterations} consecutive iteration(s) produced an "
                "unchanged diff hash and test signature"
            )
        elif result.timeout_limit == "component":
            error = (
                f"component timeout: exceeded {component_timeout}s wall clock "
                f"after {result.iterations} iteration(s)"
            )
        elif result.timed_out_iterations:
            error = (
                f"Did not complete ({result.timed_out_iterations} iteration(s) "
                f"hit the {agent_iteration_timeout}s agent iteration timeout)"
            )
        else:
            error = "Did not complete"
        return ComponentResult(
            component_id=component_id,
            success=result.completed,
            iterations=result.iterations,
            error=error,
            duration_seconds=time.monotonic() - start,
            context_json=previous_context_json,
            usage=result.usage,
            no_progress=result.no_progress,
        )
    except Exception as exc:
        return ComponentResult(
            component_id=component_id,
            success=False,
            iterations=0,
            error=str(exc),
            duration_seconds=time.monotonic() - start,
            context_json=previous_context_json,
            # The loop crashed, but any iterations that did run still
            # cost tokens; collect what the agent recorded (R3.1).
            usage=collect_usage(agent),
        )
    finally:
        if stop_heartbeat is not None:
            stop_heartbeat()
        if worker_bus is not None:
            worker_bus.close()
        if transcript_fh is not None:
            try:
                transcript_fh.close()
            except OSError:
                pass


def _install_worker_signal_forwarding() -> None:
    """Pool-worker SIGTERM handler: kill the agent's process group,
    then exit 130. Installed only on a worker's main thread."""
    if threading.current_thread() is not threading.main_thread():
        return

    def _on_term(signum: int, frame: object) -> None:
        del signum, frame
        try:
            kill_active_process_groups()
        finally:
            os._exit(130)

    try:
        signal.signal(signal.SIGTERM, _on_term)
    except (ValueError, OSError):
        pass


def _redirect_worker_output(log_path: Path) -> None:
    """Point the worker's fds 1/2 (and sys.stdout/stderr) at its
    transcript so nothing reaches the parent terminal (chunk 6). Best
    effort: a worker that cannot redirect keeps inherited fds rather
    than dying."""
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        f = open(log_path, "a", buffering=1, encoding="utf-8", errors="replace")
        os.dup2(f.fileno(), 1)
        os.dup2(f.fileno(), 2)
        sys.stdout = f
        sys.stderr = f
    except OSError:
        pass


class _InlineExecutor:
    """Synchronous stand-in for ProcessPoolExecutor when max_parallel
    is 1 (R7.3): the worker runs in-process (no pickling, no process
    spawn - the historical sequential path), wrapped in an already-
    resolved Future so ONE scheduling loop serves both modes. A worker
    exception lands on the future and surfaces at ``future.result()``,
    exactly where a pool worker's would.
    """

    def submit(
        self, fn: Callable[..., ComponentResult], /, *args: Any,
    ) -> Future[ComponentResult]:
        future: Future[ComponentResult] = Future()
        try:
            result = fn(*args)
        except Exception as exc:
            future.set_exception(exc)
        else:
            future.set_result(result)
        return future

    def shutdown(
        self, wait: bool = True, cancel_futures: bool = False,
    ) -> None:
        """Nothing to shut down: every submit already ran to completion."""


def _wait_interruptible(
    futures: set[Future[ComponentResult]],
    timeout: float | None,
    stop: StopController | None,
    slice_seconds: float = 0.5,
) -> tuple[set[Future[ComponentResult]], bool]:
    """concurrent.futures.wait in stop-checkable slices (PR B).

    Returns (done, stopped). Worst-case stop latency is one slice;
    the backstop deadline math is preserved by honoring ``timeout``.
    """
    if stop is None:
        done, _ = wait(futures, timeout=timeout, return_when=FIRST_COMPLETED)
        return done, False
    deadline = (
        time.monotonic() + timeout if timeout is not None else None
    )
    while True:
        if stop.is_set():
            return set(), True
        remaining = (
            None if deadline is None
            else max(0.0, deadline - time.monotonic())
        )
        slice_t = (
            slice_seconds if remaining is None
            else min(slice_seconds, remaining)
        )
        done, _ = wait(futures, timeout=slice_t, return_when=FIRST_COMPLETED)
        if done:
            return done, False
        if remaining is not None and remaining <= slice_seconds:
            return set(), False


def _abort_inflight(
    executor: ProcessPoolExecutor | _InlineExecutor,
    running_futures: dict[Future[ComponentResult], str],
    pipeline: ComponentPipeline,
    ui: UI,
    stop: StopController,
    term_grace: float = 5.0,
) -> None:
    """Group-terminate in-flight workers and record their components as
    aborted (PR B). Pool mode SIGTERMs each worker pid - the worker's
    forwarding handler group-kills its agent subprocess and exits 130;
    stragglers get SIGKILL after the grace period. A second stop request
    skips the rest of that grace period. `_processes` is private executor
    API, so process objects are inspected defensively and only workers
    still reported alive are killed.
    """
    procs = getattr(executor, "_processes", None)
    workers: list[Any] = list(procs.values()) if procs else []
    if workers:
        alive = []
        for worker in workers:
            try:
                pid = worker.pid
                if (
                    isinstance(pid, int)
                    and pid > 1
                    and pid != os.getpid()
                    and worker.is_alive()
                ):
                    worker.terminate()
                    alive.append(worker)
            except (AssertionError, AttributeError, OSError, ValueError):
                pass
        deadline = time.monotonic() + term_grace
        while alive and not stop.force and time.monotonic() < deadline:
            still_alive = []
            for worker in alive:
                try:
                    if worker.is_alive():
                        still_alive.append(worker)
                except (AssertionError, AttributeError, OSError, ValueError):
                    pass
            alive = still_alive
            if alive and not stop.force:
                time.sleep(0.1)
        for worker in alive:
            try:
                if worker.is_alive():
                    worker.kill()
            except (AssertionError, AttributeError, OSError, ValueError):
                pass
    elif isinstance(executor, _InlineExecutor):
        # Inline executor: the agents live in THIS process, so group-kill
        # them directly.
        killed = kill_active_process_groups()
        if killed:
            ui.warn(f"  Terminated {killed} in-flight agent process group(s)")
    for future, comp_id in list(running_futures.items()):
        pipeline.fail_aborted(comp_id, stop.reason)
        running_futures.pop(future, None)
    ui.warn(f"  Aborted in-flight work: {stop.reason}")
    executor.shutdown(wait=False, cancel_futures=True)


def _next_backstop_wait(
    running: Mapping[Future[ComponentResult], str],
    deadlines: Mapping[Future[ComponentResult], float],
    now: float,
) -> float | None:
    """Seconds until the nearest scheduler-backstop deadline among running
    futures. None means no deadline is armed (wait indefinitely for the
    next completion, the pre-R0.1 behavior)."""
    pending = [deadlines[f] for f in running if f in deadlines]
    if not pending:
        return None
    return max(0.0, min(pending) - now)


def _expired_futures(
    running: Mapping[Future[ComponentResult], str],
    deadlines: Mapping[Future[ComponentResult], float],
    now: float,
) -> list[Future[ComponentResult]]:
    """Running futures whose scheduler-backstop deadline has passed."""
    return [
        f for f in running
        if not f.done() and f in deadlines and now >= deadlines[f]
    ]


# Rollup row order for the R3.1 usage table; phases outside this list
# (future additions) sort after, alphabetically.
_USAGE_PHASE_ORDER = ("engineer", "review", "security", "distill")


def _format_usage_rollup(
    usage_meter: Mapping[str, Mapping[str, UsageTotals]],
    run_usage: UsageTotals,
) -> list[str]:
    """Render the per-component, per-phase usage table (R3.1).

    Token and cost columns are sums of CLI self-reports: codex reports
    only a total (in/out columns stay 0), CustomAgent reports nothing.
    Whenever some calls reported no usage the footer says so explicitly -
    the totals are then lower bounds, not measurements (H4).
    """
    header = (
        f"{'component':<24} {'phase':<10} {'calls':>5} "
        f"{'tokens_in':>11} {'tokens_out':>11} {'tokens_total':>13} "
        f"{'cost_usd':>9} {'time_s':>8}"
    )
    lines = [header]

    def _phase_sort_key(phase: str) -> tuple[int, str]:
        try:
            return (_USAGE_PHASE_ORDER.index(phase), phase)
        except ValueError:
            return (len(_USAGE_PHASE_ORDER), phase)

    def _row(label: str, phase: str, totals: UsageTotals) -> str:
        if totals.known_calls > 0:
            tokens_in = f"{totals.input_tokens:,}"
            tokens_out = f"{totals.output_tokens:,}"
            tokens_total = f"{totals.total_tokens:,}"
            cost = f"{totals.cost_usd:.4f}" if totals.cost_usd > 0 else "-"
        else:
            tokens_in = tokens_out = tokens_total = cost = "-"
        return (
            f"{label:<24} {phase:<10} {totals.calls:>5} "
            f"{tokens_in:>11} {tokens_out:>11} {tokens_total:>13} "
            f"{cost:>9} {totals.duration_seconds:>8.0f}"
        )

    for comp_id in sorted(usage_meter):
        phases = usage_meter[comp_id]
        for phase in sorted(phases, key=_phase_sort_key):
            lines.append(_row(comp_id, phase, phases[phase]))
    lines.append(_row("TOTAL", "", run_usage))
    if run_usage.unreported_calls > 0:
        lines.append(
            f"note: {run_usage.unreported_calls} of {run_usage.calls} "
            "call(s) reported no token/cost data; token and cost totals "
            "are lower bounds"
        )
    return lines


def run_factory(
    manifest: Manifest,
    factory_config: FactoryConfig,
    base_config: KstrlConfig,
    ui: UI,
    root_dir: Path,
    manifest_path: Path | None = None,
    *,
    interaction: InteractionChannel | None = None,
    stop: StopController | None = None,
    run_id: str | None = None,
    notify_capture_output: bool = False,
) -> FactoryResult:
    """Run the factory orchestrator with 3-phase verification.

    Phase 1: Mechanical verification (tests, typecheck, lint, PRD, diff scope)
    Phase 2: Second-opinion review (separate agent reviews diff against spec)
    Phase 3: Contract testing (merge tier branches, run integration tests)

    ``manifest_path`` is where run state is SAVED as well as where it was
    loaded from (R0.5, H-15): ``--manifest /custom.json`` must persist to
    /custom.json and ``ks run`` to its own run-manifest.json, never to
    another invocation's resumable ``scripts/kstrl/manifest.json``. None
    keeps the historical default of ``<root>/scripts/kstrl/manifest.json``.

    Holds the run-level ``.kstrl/factory.lock`` flock for the whole run
    (R0.5, H-7); a contending invocation is refused with exit code 2
    unless it passes ``--force-lock``.
    """
    try:
        run_lock = _acquire_run_lock(
            root_dir, ui, force=factory_config.force_lock,
        )
    except FactoryLockHeldError as exc:
        ui.err(str(exc))
        refused = FactoryResult()
        refused.exit_code = 2
        return refused
    try:
        return _run_factory_locked(
            manifest, factory_config, base_config, ui, root_dir,
            manifest_path=manifest_path, lock_held=run_lock.held,
            interaction=interaction, stop=stop, run_id_override=run_id,
            notify_capture_output=notify_capture_output,
        )
    finally:
        run_lock.release()


def _run_factory_locked(
    manifest: Manifest,
    factory_config: FactoryConfig,
    base_config: KstrlConfig,
    ui: UI,
    root_dir: Path,
    manifest_path: Path | None,
    lock_held: bool,
    interaction: InteractionChannel | None = None,
    stop: StopController | None = None,
    run_id_override: str | None = None,
    notify_capture_output: bool = False,
) -> FactoryResult:
    """run_factory body; runs with the run-level lock resolved (held, or
    explicitly degraded via --force-lock / no-fcntl platforms)."""
    factory_start = time.monotonic()
    factory_result = FactoryResult()
    # Stable run id shared by evolution journal and knowledge layer.
    # current_run_id() carries microseconds plus a random nonce, so two
    # factory invocations launched within the same UTC second neither
    # collide on .kstrl/knowledge/<comp>/<run_id>/ directories nor
    # order ambiguously (R1.6: same-second knowledge run dirs must sort
    # by creation time, not by nonce).
    # PR F needs the run dir known before the TUI starts: the caller
    # may mint the id (format unchanged - knowledge.current_run_id).
    run_id = run_id_override if run_id_override else current_run_id()

    # R6.1: structured "<check>:<code>" failure signatures per component
    # (e.g. "linter:E501", "review:scope_creep"), recorded at each
    # failure site from the parser/finding stream and handed to the
    # evolution journal at record_run. In-memory only: the manifest
    # already persists failed_phase/failed_check; the full signature
    # list is a journal concern.
    component_failure_signatures: dict[str, list[str]] = {}

    # Set up progress log. R3.2: defaults ON under .kstrl/ so a
    # walk-away run always leaves an event trail `ks status` can
    # join; [factory] progress_log_enabled = false (or env) opts out.
    # Every event carries run_id so runs sharing the default file stay
    # distinguishable.
    # Dual-write (TUI rewrite chunk 3): typed schema-v2 events go to
    # .kstrl/runs/<run_id>/events.jsonl via the EventBus; V1CompatSink
    # delegates the v1-named subset to a real ProgressLog so the
    # progress.jsonl byte format AND its attached ProgressSink
    # observers (Linear, R7.4) stay untouched. progress_log_enabled =
    # false suppresses BOTH files (symmetric opt-out).
    progress_log: ProgressLog
    # Chunk 7: when the caller's UI is the event bridge (cli commands
    # via build_console), reuse ITS bus so the run's file sinks also
    # capture every imperative Log narration - the imperative call
    # sites become replayable. Tests passing a bare PlainUI get a
    # private bus (their UI already prints directly).
    if isinstance(ui, EventBridgeUI):
        bus = ui.bus
        bus.run_id = run_id
    else:
        bus = EventBus(run_id=run_id)
    journal_path: Path | None = None
    run_paths: RunPaths | None = None
    run_file_sinks: list[EventSink] = []
    if not factory_config.progress_log_enabled:
        progress_log = NullProgressLog()
    else:
        log_path = (
            factory_config.progress_log_path
            or root_dir / ".kstrl" / "progress.jsonl"
        )
        progress_log = ProgressLog(log_path, run_id=run_id, warn=ui.warn)
        journal_path = log_path
        run_paths = RunPaths.for_run(root_dir, run_id)
        run_file_sinks = [
            JsonlSink(run_paths.events_file),
            V1CompatSink(progress_log),
        ]
        for _sink in run_file_sinks:
            bus.add_sink(_sink)

    # R7.4: Linear sink - mirrors failure/budget events onto the issues
    # the decompose hook mapped in the manifest. Observability only;
    # build_linear_sink returns None (with a warning) rather than ever
    # failing the run, and emit() isolates sink exceptions.
    linear_sink = build_linear_sink(
        manifest,
        factory_config.linear_config or LinearConfig.load(root_dir),
        run_id=run_id,
        warn=ui.warn,
    )
    if linear_sink is not None:
        progress_log.attach_sink(linear_sink)

    # R3.2: notification hooks - each condition fires at most once per
    # run, and hook failures only ever warn.
    notify = NotifyHooks(
        factory_config.notify_config or NotifyConfig.load(root_dir),
        run_id=run_id,
        project=manifest.project_name,
        warn=ui.warn,
        capture_output=notify_capture_output,
    )

    bus.emit(RunStarted(
        project=manifest.project_name, components=len(manifest.components),
    ))
    # Chunk 4: the component DAG + budget caps as one event, so a
    # dashboard can draw the board without reading the manifest.
    bus.emit(RunPlan(
        components=tuple(
            {"id": c.id, "title": c.title, "deps": list(c.dependencies)}
            for c in manifest.components
        ),
        max_total_tokens=factory_config.max_total_tokens,
        max_adversarial_calls=factory_config.max_adversarial_calls,
    ))

    # R7.1: resolve which model family reviews this run's diffs ONCE so
    # the choice is stable across components and the homogeneity warning
    # prints once per run, not per component. Explicit config always
    # wins; otherwise review/security default to the opposite family
    # from the engineer when that CLI is available. The selection is an
    # audit-trail event: same-family and cross-family runs must stay
    # distinguishable in the progress log.
    review_selection = resolve_adversarial_selection(
        "review",
        explicit_cmd=factory_config.review_agent_cmd,
        explicit_type=factory_config.review_agent_type,
        explicit_model=factory_config.review_model,
        fallback_cmd=None,
        fallback_type=base_config.agent_type,
        fallback_model=None,
        fallback_reasoning=None,
        engineer_cmd=base_config.agent_cmd,
        engineer_type=base_config.agent_type,
    )
    security_selection: AdversarialAgentSelection | None = None
    if factory_config.security_config is not None:
        sec_cfg = factory_config.security_config
        security_selection = resolve_adversarial_selection(
            "security",
            explicit_cmd=sec_cfg.agent_cmd,
            explicit_type=sec_cfg.agent_type,
            explicit_model=sec_cfg.model,
            fallback_cmd=base_config.agent_cmd,
            fallback_type=base_config.agent_type,
            fallback_model=base_config.model,
            fallback_reasoning=base_config.model_reasoning_effort,
            engineer_cmd=base_config.agent_cmd,
            engineer_type=base_config.agent_type,
        )
    _review_phase_enabled = (
        factory_config.review_mode != ReviewMode.SKIP.value
    )
    _security_phase_enabled = (
        factory_config.security_config is not None
        and factory_config.security_config.mode != SecurityMode.SKIP.value
    )
    for _sel, _enabled in (
        (review_selection, _review_phase_enabled),
        (security_selection, _security_phase_enabled),
    ):
        if _sel is None or not _enabled:
            continue
        if _sel.warning:
            ui.warn(f"  {_sel.warning}")
        bus.emit(AdversarialAgentSelected(
            phase=_sel.phase,
            agent_source=_sel.source,
            identity=_sel.identity,
            agent_type=_sel.agent_type,
            model=_sel.model,
            homogeneous=_sel.warning is not None,
        ))

    # Validate DAG
    ui.section("Factory: Validating DAG")
    dag_errors = manifest.validate_dag()
    if dag_errors:
        for err in dag_errors:
            ui.err(f"  {err}")
        factory_result.exit_code = 1
        return factory_result

    topo_order = manifest.topological_order()
    ui.ok(f"DAG valid: {len(topo_order)} components in dependency order")

    # Crash recovery: reset intermediate states
    for comp in manifest.components:
        if comp.status in (
            ComponentStatus.RUNNING.value,
            ComponentStatus.VERIFYING.value,
        ):
            ui.info(f"  Resetting '{comp.id}' from {comp.status} to PENDING")
            comp.status = ComponentStatus.PENDING.value

    if manifest_path is None:
        manifest_path = root_dir / "scripts" / "kstrl" / "manifest.json"

    # R3.3: persist which run owns this manifest state. completed_at is
    # blanked while the run is in flight and stamped in the summary
    # epilogue, so "did the last run finish?" is answerable from the
    # manifest alone (and later, from Linear).
    manifest.run_id = run_id
    manifest.completed_at = ""
    manifest.save(manifest_path)

    # Load knowledge config once for the entire factory run, BEFORE the
    # pipeline is constructed. Binding it at construction removes the
    # late-binding accident where the old _handle_result closure read a
    # name that was only assigned further down the function (R7.3).
    knowledge_config = KnowledgeConfig.load(root_dir)

    # Scheduling state shared between the scheduler and the pipeline.
    worktree_paths: dict[str, Path] = {}
    component_contexts: dict[str, str] = {}  # comp_id -> context JSON
    # Components whose last failure was a timeout kill: their retry must
    # not trust the surviving worktree/branch state (R0.1 requirement 5).
    fresh_base_retry_ids: set[str] = set()
    # Components abandoned by the scheduler backstop; their workers may
    # still be alive, so their worktrees are never cleaned up here.
    leaked_component_ids: set[str] = set()

    # R7.3: the per-component phase chain and every component state
    # transition live in ComponentPipeline. Hooks are resolved from this
    # module's globals HERE, at run start, so tests patching
    # kstrl.factory.run_review (and friends) keep intercepting the
    # phase functions.
    pipeline = ComponentPipeline(
        manifest=manifest,
        manifest_path=manifest_path,
        factory_config=factory_config,
        base_config=base_config,
        ui=ui,
        root_dir=root_dir,
        run_id=run_id,
        bus=bus,
        journal_path=journal_path,
        run_paths=run_paths,
        interaction=interaction,
        notify=notify,
        review_selection=review_selection,
        security_selection=security_selection,
        knowledge_config=knowledge_config,
        factory_result=factory_result,
        hooks=PipelineHooks(
            run_mechanical_verification=run_mechanical_verification,
            run_review=run_review,
            run_chunked_review=run_chunked_review,
            run_security_review=run_security_review,
            run_chunked_security_review=run_chunked_security_review,
            distill_facts=distill_facts,
            build_knowledge_context=build_knowledge_context,
            measure_fact_utilization=measure_fact_utilization,
            cleanup_worktree=_cleanup_worktree,
        ),
        worktree_paths=worktree_paths,
        component_contexts=component_contexts,
        fresh_base_retry_ids=fresh_base_retry_ids,
        component_failure_signatures=component_failure_signatures,
    )

    # R0.2 crash recovery: MERGE_PENDING is re-pollable, not failed.
    # Re-poll before scheduling so confirmed merges unblock dependents.
    pipeline.repoll_merge_pending()

    # Determine effective parallelism
    max_parallel = factory_config.max_parallel
    if not factory_config.use_worktrees:
        max_parallel = 1
        ui.info("Worktrees disabled: running sequentially")
    if factory_config.single_pr and max_parallel > 1:
        # R0.5 (H-8): single_pr components all live on ONE branch, and a
        # branch can only be checked out in one worktree at a time -
        # parallel same-tier components would hard-fail on "already
        # checked out". Sequential is the only layout that works.
        max_parallel = 1
        ui.info(
            "single_pr mode: components share one branch; "
            "forcing max_parallel=1"
        )

    # R0.1: TimeoutConfig is the single source for the agent-iteration and
    # component wall-clock limits. Enforcement layers: the adapters kill
    # their subprocess group, run_loop aborts on the component wall clock,
    # and the scheduler backstop below catches a worker that hangs outside
    # both (e.g. a stuck scaffold or feedforward step).
    timeout_cfg = factory_config.timeout_config or TimeoutConfig.load(root_dir)
    backstop_seconds = (
        timeout_cfg.component_total + timeout_cfg.scheduler_backstop_margin
        if timeout_cfg.component_total > 0 else 0.0
    )

    # R7.5: no-progress circuit breaker limits, forwarded into every
    # engineer loop. When [breaker].test_command is unset, the stall
    # probe falls back to the explicitly configured Phase 1 test command
    # (never the smart default: the probe runs inside the engineer loop
    # and must only execute commands the operator chose).
    breaker_cfg = BreakerConfig.load(root_dir)
    if (
        breaker_cfg.test_command is None
        and factory_config.verify_config is not None
        and factory_config.verify_config.test_command
    ):
        breaker_cfg = replace(
            breaker_cfg,
            test_command=factory_config.verify_config.test_command,
        )

    # R7.5: OS-level sandbox intent for engineer agent subprocesses.
    # A custom agent command has no generic sandbox surface, so intent
    # that cannot be honored is refused loudly instead of silently
    # dropped (an operator who opted in must not believe the boundary
    # exists when it does not).
    sandbox_cfg = SandboxConfig.load(root_dir)
    if sandbox_cfg.enabled and base_config.agent_cmd:
        ui.warn(
            "  [sandbox] enabled but the agent is a custom command; "
            "sandbox settings CANNOT be applied to it and are ignored "
            "(worktree isolation remains the only boundary)"
        )

    # R0.5 worktree crash recovery + stale-branch policy. Both only
    # apply to worktree mode: without worktrees the factory neither
    # creates branches nor worktree dirs.
    if factory_config.use_worktrees:
        if lock_held:
            _prune_stale_worktrees(
                root_dir, run_id, ui,
                keep=_evidence_worktrees_to_keep(manifest),
            )
        else:
            ui.warn(
                "  Run lock not held; skipping stale-worktree cleanup "
                "(another live invocation may own them)"
            )
        branch_errors = _preflight_component_branches(manifest, root_dir, ui)
        if branch_errors:
            ui.err("Refusing to run: stale component branches found")
            for line in branch_errors:
                ui.err(f"  {line}")
            factory_result.exit_code = 2
            return factory_result

    ui.section("Factory: Execution")
    ui.kv("Max parallel", str(max_parallel))
    ui.kv("Max retries", str(factory_config.max_retries))
    ui.kv("Review mode", factory_config.review_mode)
    contract_mode = (
        factory_config.contract_config.mode
        if factory_config.contract_config else "skip"
    )
    ui.kv("Contract check", contract_mode)
    ui.kv(
        "Agent timeout",
        f"{timeout_cfg.agent_iteration}s"
        if timeout_cfg.agent_iteration > 0 else "<disabled>",
    )
    ui.kv(
        "Component timeout",
        f"{timeout_cfg.component_total}s"
        if timeout_cfg.component_total > 0 else "<disabled>",
    )

    def _path_relative_to_root(path: Path) -> str:
        """Render `path` relative to root_dir for use inside per-component
        worktrees. Falls back to the absolute path string when relativization
        fails (e.g. a path on a different mount)."""
        if not path.is_absolute():
            return str(path)
        try:
            return path.relative_to(root_dir).as_posix()
        except ValueError:
            try:
                return path.resolve().relative_to(root_dir.resolve()).as_posix()
            except ValueError:
                return str(path)

    prompt_file_rel = _path_relative_to_root(base_config.prompt_file)
    progress_file_rel = _path_relative_to_root(base_config.progress_file)
    codebase_map_file_rel = _path_relative_to_root(base_config.codebase_map_file)

    def _launch_component(comp: Component) -> Path | None:
        """Set up worktree for a component. Returns worktree path or None."""
        try:
            if factory_config.use_worktrees:
                fresh_from_base = comp.id in fresh_base_retry_ids
                fresh_base_retry_ids.discard(comp.id)
                wt_path = _setup_worktree(
                    comp.id, comp.branch_name, manifest.base_branch, root_dir,
                    run_id, fresh_from_base=fresh_from_base,
                )
            else:
                wt_path = root_dir
            worktree_paths[comp.id] = wt_path
            return wt_path
        except RuntimeError as exc:
            ui.err(f"  Worktree setup failed for '{comp.id}': {exc}")
            # pipeline.fail covers the R3.2 notify + progress-log
            # calls and stamps the R3.3 failure/evidence fields.
            pipeline.fail(
                comp, str(exc), phase="provisioning", check="worktree_setup",
            )
            return None

    # Build feedforward config dict for serialization to worker processes
    ff_config_dict: dict[str, Any] | None = None
    if factory_config.feedforward_config and factory_config.feedforward_config.enabled:
        fc = factory_config.feedforward_config
        ff_config_dict = {
            "enabled": fc.enabled,
            "module_map": fc.module_map,
            "public_interfaces": fc.public_interfaces,
            "dependency_graph": fc.dependency_graph,
            "conventions": fc.conventions,
            "max_context_tokens": fc.max_context_tokens,
        }

    def _submit_args(comp: Component, wt_path: Path) -> tuple[Any, ...]:
        ctx_json = component_contexts.get(comp.id)
        knowledge_prefix = ""
        if knowledge_config.enabled:
            try:
                knowledge_prefix = build_knowledge_context(
                    manifest, comp, knowledge_config.knowledge_root, knowledge_config,
                )
            except Exception:
                pass  # knowledge retrieval is non-fatal
        return (
            comp.id, comp.prd_path, str(wt_path), str(root_dir),
            prompt_file_rel, base_config.agent_cmd, base_config.model,
            base_config.model_reasoning_effort, base_config.agent_type,
            base_config.sleep_seconds, ctx_json,
            ff_config_dict,
            comp.scaffold or None,
            comp.dependencies or None,
            knowledge_prefix,
            progress_file_rel,
            codebase_map_file_rel,
            timeout_cfg.agent_iteration,
            timeout_cfg.component_total,
            # R2.3 (CRIT-8): forward the invoking config's loop settings;
            # they were previously dropped here and _run_component ran a
            # hardcoded 30 non-interactive iterations with no path guard.
            base_config.max_iterations,
            base_config.interactive,
            base_config.allowed_paths or None,
            # R7.5: no-progress circuit breaker limits.
            breaker_cfg.no_progress_iterations,
            breaker_cfg.test_command,
            breaker_cfg.test_timeout,
            # R7.5: OS-level sandbox intent for the engineer's agent CLI.
            sandbox_cfg.enabled,
            sandbox_cfg.allow_network,
            # R7.6: in-loop USD budget for the claude-sdk engineer.
            base_config.agent_budget_usd,
            # Chunk 6: worker event channel (None when progress logging
            # is disabled - transcripts and events off together).
            str(run_paths.root) if run_paths is not None else None,
            run_id,
        )

    def _run_scheduling_pass() -> None:
        """Run ready components until nothing is PENDING-and-ready.

        Called once per contract pass: a contract breaker reset to
        PENDING after a failed contract phase re-enters scheduling via
        the outer loop in run_factory (R0.3) - previously the reset
        happened after the only scheduling loop had exited, so the
        promised retry never ran.

        ONE loop serves sequential and parallel scheduling (R7.3): the
        launch protocol (budget gate -> begin_attempt -> provisioning ->
        submit) appears exactly once. max_parallel <= 1 swaps the
        process pool for _InlineExecutor, which runs the worker
        synchronously in-process - the historical sequential behavior -
        behind the identical submit/wait/result flow.

        Manual executor lifecycle: on a backstop breach we must NOT wait
        for the (possibly hung) worker at shutdown, which the
        `with ProcessPoolExecutor(...)` form would do.
        """
        executor: ProcessPoolExecutor | _InlineExecutor
        if max_parallel <= 1:
            executor = _InlineExecutor()
        else:
            executor = ProcessPoolExecutor(max_workers=max_parallel)
        slots_cap = max(1, max_parallel)
        running_futures: dict[Future[ComponentResult], str] = {}
        future_deadlines: dict[Future[ComponentResult], float] = {}
        try:
            while True:
                if stop is not None and stop.is_set():
                    _abort_inflight(
                        executor, running_futures, pipeline, ui, stop,
                    )
                    return
                ready = manifest.get_ready_components()
                slots = slots_cap - len(running_futures)

                # Components transitioned WITHOUT a launch this pass
                # (budget gate, provisioning failure). When that happens
                # and nothing is running, the loop must re-derive the
                # ready set rather than stop - R3.1's scheduling gate
                # promises to fail every remaining pending component
                # loudly, and a provisioning failure must not strand
                # still-schedulable siblings.
                transitioned_without_launch = 0
                for comp in ready[:slots]:
                    # R3.1 scheduling gate: a blown token budget fails
                    # pending components loudly instead of launching an
                    # engineer loop that would only add spend.
                    if pipeline.token_budget_exceeded():
                        pipeline.fail_for_budget(comp, "scheduling")
                        transitioned_without_launch += 1
                        continue
                    pipeline.begin_attempt(comp)
                    manifest.save(manifest_path)
                    bus.emit(ComponentStarted(component=comp.id))
                    ui.info(f"  Starting: {comp.id}")

                    wt_path = _launch_component(comp)
                    if wt_path is None:
                        transitioned_without_launch += 1
                        continue

                    # Provisioning succeeded: the engineer phase starts
                    # immediately before submission. process_result closes
                    # normal exits; fail_scheduler_backstop closes timeouts.
                    bus.emit(PhaseStarted(
                        component=comp.id, phase="engineer",
                        attempt=comp.retries + 1,
                    ))
                    args = _submit_args(comp, wt_path)
                    if isinstance(executor, _InlineExecutor):
                        # In-process worker: no fd redirection (it would
                        # hijack the parent terminal), and each transcript
                        # line mirrors to the parent UI so sequential runs
                        # keep live engineer output. functools.partial
                        # binds kstrl.factory._run_component AT SUBMIT
                        # TIME, so tests patching it still intercept.
                        # mypy cannot prove the unknown-length *args
                        # tuple stops before the kwargs; _submit_args
                        # ends at run_id by construction.
                        task = functools.partial(
                            _run_component, *args,
                            redirect_output=False,  # type: ignore[misc]
                            live_line=functools.partial(
                                ui.stream_line, "AI",
                            ),
                            stop_check=(
                                stop.is_set if stop is not None else None
                            ),
                        )
                        future = executor.submit(task)
                    else:
                        future = executor.submit(_run_component, *args)
                    running_futures[future] = comp.id
                    if backstop_seconds > 0:
                        future_deadlines[future] = (
                            time.monotonic() + backstop_seconds
                        )

                if not running_futures:
                    if transitioned_without_launch:
                        continue
                    break

                # Wait for the next completion, bounded by the nearest
                # backstop deadline. The worker enforces its own timeouts
                # (adapter kill + loop wall clock); this scheduler-side
                # deadline is the last line of defense when a worker hangs
                # outside those layers.
                wait_timeout = _next_backstop_wait(
                    running_futures, future_deadlines, time.monotonic(),
                )
                done, stopped = _wait_interruptible(
                    set(running_futures), wait_timeout, stop,
                )
                if stopped:
                    assert stop is not None
                    _abort_inflight(
                        executor, running_futures, pipeline, ui, stop,
                    )
                    return

                if done:
                    # Preserve pre-R0.1 semantics: process one completion
                    # per pass so freed slots are refilled promptly.
                    future = next(iter(done))
                    comp_id = running_futures.pop(future)
                    future_deadlines.pop(future, None)
                    try:
                        comp_result = future.result()
                    except Exception as exc:
                        comp_result = ComponentResult(
                            component_id=comp_id, success=False, error=str(exc),
                        )
                    pipeline.process_result(comp_id, comp_result)
                    continue

                # Nothing completed inside the window: fail every
                # component past its backstop deadline and keep going.
                now = time.monotonic()
                for future in _expired_futures(
                    running_futures, future_deadlines, now,
                ):
                    comp_id = running_futures.pop(future)
                    future_deadlines.pop(future, None)
                    leaked_component_ids.add(comp_id)
                    pipeline.fail_scheduler_backstop(comp_id, backstop_seconds)
        finally:
            if leaked_component_ids:
                ui.warn(
                    "Shutting down worker pool without waiting: "
                    f"{len(leaked_component_ids)} worker(s) may still be running"
                )
                executor.shutdown(wait=False, cancel_futures=True)
            else:
                executor.shutdown(wait=True)

    def _cleanup_pass_worktrees() -> None:
        """Remove component worktrees left behind by a scheduling pass.

        With ``keep_worktrees_on_failure`` (R3.3), a FAILED component's
        worktree is kept and recorded as its evidence pointer instead of
        removed, so the failure summary can point the operator at it.
        """
        if not factory_config.use_worktrees:
            return
        ui.section("Factory: Cleanup")
        kept_evidence = False
        for comp_id in worktree_paths:
            if comp_id in leaked_component_ids:
                # A possibly-live worker still owns this worktree; removing
                # it under the worker risks corrupting the main repo's
                # worktree metadata.
                ui.warn(
                    f"  Keeping worktree for '{comp_id}' "
                    f"(leaked worker may still be running)"
                )
                continue
            comp = manifest.get_component(comp_id)
            if (
                factory_config.keep_worktrees_on_failure
                and comp is not None
                and comp.status == ComponentStatus.FAILED.value
            ):
                comp.evidence_worktree = str(worktree_paths[comp_id])
                kept_evidence = True
                ui.info(
                    f"  Keeping failed worktree for post-mortem: "
                    f"{worktree_paths[comp_id]}"
                )
                continue
            _cleanup_worktree(comp_id, root_dir, run_id)
        if kept_evidence:
            manifest.save(manifest_path)
        # Drop the run's now-empty worktree dir; leaked workers' and
        # failed components' kept worktrees leave it non-empty and it
        # stays for the next run's prune pass (which preserves recorded
        # evidence worktrees of still-FAILED components).
        try:
            os.rmdir(root_dir / ".kstrl" / "worktrees" / run_id)
        except OSError:
            pass
        ui.ok("Worktrees cleaned up")

    def _record_contract_event(cr: ContractResult) -> None:
        """Append a contract_result event to the evolution journal.

        Written for pass AND fail (R0.3): the journal is the audit trail
        for every contract phase outcome, including intermediate failures
        that a breaker retry later resolves. Non-fatal on I/O errors,
        matching EvolutionJournal.record_run.
        """
        from kstrl.evolution import JOURNAL_SCHEMA_VERSION, EvolutionConfig

        # R2.1: honor [evolution] in kstrl.toml + env, resolved against
        # the factory root rather than whatever the process CWD is.
        evo_config = EvolutionConfig.load(root_dir)
        if not evo_config.enabled:
            return
        entry = {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "run_id": run_id,
            "project": manifest.project_name,
            "component_id": cr.breaker or "",
            "event_type": "contract_result",
            "tier": cr.tier,
            "passed": cr.passed,
            "breaker": cr.breaker,
            "components_tested": cr.components_tested,
            "test_output": cr.test_output[:2000],
            "duration_seconds": round(cr.duration_seconds, 2),
        }
        try:
            evo_config.journal_path.parent.mkdir(parents=True, exist_ok=True)
            with open(evo_config.journal_path, "a") as f:
                f.write(json.dumps(entry, separators=(",", ":")) + "\n")
        except OSError as exc:
            # Evolution recording is non-fatal, but never silent (R6.1).
            ui.warn(
                f"  Evolution journal write failed (non-fatal): {exc}"
            )

    # Per-component PRs are squash-merged into base as each component
    # completes, so at contract time tier re-merges would be content
    # no-ops and blame attribution would be meaningless: the contract
    # phase instead tests the integrated base branch (R0.3). single_pr
    # defers its one PR until after the contract phase, so it stays in
    # deferred-merge (tier merge + bisection) mode.
    components_merged = factory_config.create_prs and not factory_config.single_pr

    # R0.3: scheduling + contract testing form one outer loop so a
    # contract breaker reset to PENDING actually re-enters scheduling.
    # Termination: every reset consumes one of the breaker's bounded
    # retries, and any pass without a reset breaks out.
    while True:
        _run_scheduling_pass()
        _cleanup_pass_worktrees()

        if stop is not None and stop.is_set():
            ui.warn(f"  Run stopped: {stop.reason}")
            break

        # PHASE 3: Contract testing
        contract_config = factory_config.contract_config
        if (
            contract_config is None
            or contract_config.mode == ContractMode.SKIP.value
        ):
            break

        try:
            contract_results = run_contract_testing(
                manifest, root_dir, contract_config, ui,
                components_merged=components_merged,
            )
        except ContractCleanupError as exc:
            # A contract temp worktree survived removal. The user's
            # checkout is untouched, but .kstrl/contract holds stale
            # state - fail the run loudly instead of continuing.
            ui.err(f"  Contract cleanup FAILED: {exc}")
            factory_result.contract_failures.append(
                f"contract cleanup failed: {exc}"
            )
            break

        for cr in contract_results:
            bus.emit(ContractResultEvent(
                tier=cr.tier, passed=cr.passed, breaker=cr.breaker,
                duration_seconds=round(cr.duration_seconds, 2),
            ))
            _record_contract_event(cr)

        failures = [cr for cr in contract_results if not cr.passed]
        if not failures:
            break

        # Reset retryable breakers to PENDING; the outer loop then
        # re-enters scheduling so the promised retry actually runs.
        any_breaker_reset = False
        for cr in failures:
            if not cr.breaker:
                continue
            breaker = manifest.get_component(cr.breaker)
            if breaker and breaker.retries < factory_config.max_retries:
                # R3.3: the completed attempt's findings are superseded
                # by the contract-triggered re-run; journal them before
                # the retry increments the attempt counter.
                pipeline.journal_superseded_findings(breaker)
                breaker.retries += 1
                breaker.status = ComponentStatus.PENDING.value
                breaker.error = f"Contract test failed at tier {cr.tier}"
                component_failure_signatures[cr.breaker] = [
                    f"contract:tier_{cr.tier}",
                ]
                # Remove from completed list
                if cr.breaker in factory_result.completed:
                    factory_result.completed.remove(cr.breaker)
                ctx = IterationContext.from_json(
                    component_contexts.get(cr.breaker, "{}")
                )
                ctx.add_contract_failure(cr.test_output[:500])
                component_contexts[cr.breaker] = ctx.to_json()
                manifest.save(manifest_path)
                any_breaker_reset = True
                ui.warn(
                    f"  Contract breaker '{cr.breaker}' sent back for retry"
                )

        if any_breaker_reset:
            continue

        # Terminal contract failure: nothing left to retry. Record it in
        # the run result so the summary shows it and the exit code is
        # nonzero (previously this fell through silently and the run
        # exited 0 with broken integrated code).
        for cr in failures:
            detail = (cr.test_output or "").strip()
            summary_line = detail.splitlines()[-1][:200] if detail else ""
            if cr.breaker:
                breaker = manifest.get_component(cr.breaker)
                if breaker is not None:
                    breaker.status = ComponentStatus.FAILED.value
                    breaker.error = (
                        f"Contract test failed at tier {cr.tier} "
                        f"(retries exhausted)"
                    )
                    breaker.completed_at = _iso_now()
                    breaker.failed_phase = "contract"
                    breaker.failed_check = f"tier_{cr.tier}"
                    component_failure_signatures[cr.breaker] = [
                        f"contract:tier_{cr.tier}",
                    ]
                if cr.breaker in factory_result.completed:
                    factory_result.completed.remove(cr.breaker)
                if cr.breaker not in factory_result.failed:
                    factory_result.failed.append(cr.breaker)
                bus.emit(ComponentFailed(
                    component=cr.breaker,
                    error=(
                        f"Contract test failed at tier {cr.tier} "
                        f"(retries exhausted)"
                    ),
                ))
                notify.fire_first_failure(
                    cr.breaker,
                    f"Contract test failed at tier {cr.tier} "
                    f"(retries exhausted)",
                )
                factory_result.contract_failures.append(
                    f"tier {cr.tier}: breaker '{cr.breaker}' "
                    f"(retries exhausted): {summary_line}"
                )
            else:
                factory_result.contract_failures.append(
                    f"tier {cr.tier}: contract tests failed, no blame "
                    f"attributed (components: "
                    f"{', '.join(cr.components_tested)}): {summary_line}"
                )
            ui.err(
                f"  Contract failure recorded for tier {cr.tier}; "
                f"run will exit nonzero"
            )
        manifest.save(manifest_path)
        break

    # Create PRs for any remaining components that weren't handled per-component
    # (e.g. single-pr mode, or stragglers from parallel execution)
    if factory_config.create_prs:
        if factory_config.single_pr:
            result = create_single_pr(manifest, root_dir, ui)
            if result:
                factory_result.pr_urls.append(result[1])
            manifest.save(manifest_path)
        else:
            # Per-component PRs are created in _handle_result; only handle stragglers
            remaining = [
                c for c in manifest.components
                if c.status == "completed" and not c.pr_url
            ]
            if remaining:
                pr_results = create_prs_in_order(manifest, root_dir, ui)
                factory_result.pr_urls.extend(url for _, url in pr_results)
                manifest.save(manifest_path)

    # Summary
    factory_duration = time.monotonic() - factory_start
    bus.emit(RunCompleted(
        completed=len(factory_result.completed),
        failed=len(factory_result.failed),
        skipped=len(factory_result.skipped),
        duration_seconds=round(factory_duration, 2),
    ))
    # Detach (not close) the console bus: post-run cli narration must
    # not reopen the run's files. The file sinks themselves close.
    for _sink in run_file_sinks:
        bus.remove_sink(_sink)
        _sink.close()

    # R0.2: collect components parked awaiting merge confirmation. Built
    # from the manifest (not accumulated during the run) so it reflects
    # the final state after any crash-recovery re-poll.
    factory_result.merge_pending = [
        c.id for c in manifest.components
        if c.status == ComponentStatus.MERGE_PENDING.value
    ]

    ui.section("Factory: Summary")
    ui.kv("Completed", str(len(factory_result.completed)))
    ui.kv("Failed", str(len(factory_result.failed)))
    ui.kv("Skipped", str(len(factory_result.skipped)))
    # R3.3 failure summary: per failed component, which gate fired and
    # where the last attempt's evidence lives, so the run is diagnosable
    # without reading raw JSON.
    if factory_result.failed:
        ui.subsection("Failure summary")
        for failed_id in factory_result.failed:
            failed_comp = manifest.get_component(failed_id)
            if failed_comp is None:
                continue
            ui.err(
                f"  {failed_id}: "
                f"phase={failed_comp.failed_phase or 'unknown'} "
                f"check={failed_comp.failed_check or 'unknown'} "
                f"(attempt {failed_comp.retries + 1})"
            )
            if failed_comp.error:
                ui.info(f"    error: {failed_comp.error[:160]}")
            if failed_comp.evidence_worktree:
                ui.info(
                    f"    worktree: {failed_comp.evidence_worktree}"
                )
            elif factory_config.use_worktrees:
                ui.info(
                    "    worktree: removed (re-run with "
                    "--keep-worktrees-on-failure to keep it)"
                )
            if failed_comp.evidence_debug_dir:
                ui.info(
                    f"    raw outputs: {failed_comp.evidence_debug_dir}"
                )
            if (
                failed_comp.journal_offset_start >= 0
                and journal_path is not None
            ):
                end = (
                    str(failed_comp.journal_offset_end)
                    if failed_comp.journal_offset_end >= 0 else "end"
                )
                ui.info(
                    f"    journal: {journal_path} bytes "
                    f"[{failed_comp.journal_offset_start}:{end}]"
                )
            ui.info(f"    retry with: ks retry {failed_id}")
    if factory_result.contract_failures:
        ui.kv("Contract failures", str(len(factory_result.contract_failures)))
        for line in factory_result.contract_failures:
            ui.err(f"  {line}")
    if factory_result.merge_pending:
        ui.kv("Merge pending", str(len(factory_result.merge_pending)))
    ui.kv("Duration", f"{factory_duration:.0f}s")
    # R3.1 usage rollup: per component, per phase, plus the run total.
    if pipeline.run_usage.calls > 0:
        ui.subsection("Usage rollup")
        for line in _format_usage_rollup(pipeline.usage_meter, pipeline.run_usage):
            ui.info(f"  {line}")
    if pipeline.token_budget_exceeded():
        ui.err(
            f"TOKEN BUDGET EXCEEDED: {pipeline.run_usage.total_tokens} total tokens "
            f"recorded >= max_total_tokens ({factory_config.max_total_tokens})"
        )
    if factory_result.pr_urls:
        ui.kv("PRs created", str(len(factory_result.pr_urls)))
        for url in factory_result.pr_urls:
            ui.info(f"  {url}")
    if factory_result.merge_pending:
        ui.warn(
            "Some PR merges are unconfirmed; re-run the factory to "
            "re-poll them: " + ", ".join(factory_result.merge_pending)
        )

    if stop is not None and stop.is_set():
        factory_result.exit_code = 130
    elif factory_result.failed or factory_result.contract_failures:
        factory_result.exit_code = 1
    elif factory_result.merge_pending:
        # Incomplete, not failed: unconfirmed merges blocked their
        # dependents. Nonzero so automation notices; a re-run re-polls.
        factory_result.exit_code = 1
    elif factory_result.skipped and not factory_result.completed:
        factory_result.exit_code = 1

    # R3.3: the run reached its terminal state; stamp the manifest so a
    # resume (and Linear, later) can tell a finished run from a crash.
    # Stamped BEFORE the completion notification so a hook that reads
    # the manifest sees the terminal state.
    manifest.completed_at = _iso_now()
    manifest.save(manifest_path)

    # R3.2: run-end notification. Fires on every run that reached the
    # summary, whatever the outcome; early refusals (invalid DAG, held
    # lock, stale branches) never notify because no work was started.
    notify.fire_complete(
        f"completed={len(factory_result.completed)} "
        f"failed={len(factory_result.failed)} "
        f"skipped={len(factory_result.skipped)} "
        f"merge_pending={len(factory_result.merge_pending)} "
        f"exit_code={factory_result.exit_code}"
    )

    # Record run to evolution journal
    try:
        from kstrl.evolution import EvolutionConfig, EvolutionJournal

        evo_config = EvolutionConfig.load(root_dir)
        if evo_config.enabled:
            journal = EvolutionJournal(evo_config)
            journal.record_run(
                run_id, manifest, factory_result,
                usage_by_component={
                    comp_id: {
                        phase: totals.to_dict()
                        for phase, totals in phases.items()
                    }
                    for comp_id, phases in pipeline.usage_meter.items()
                },
                run_usage=pipeline.run_usage.to_dict(),
                failure_signatures=component_failure_signatures,
            )
    except Exception as exc:
        # Evolution recording is non-fatal, but never silent (R6.1).
        ui.warn(f"Evolution journal recording failed (non-fatal): {exc}")

    return factory_result
