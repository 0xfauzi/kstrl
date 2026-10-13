"""FactoryConfig: the [factory] settings, their loading and their gates."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kstrl.acceptance import BaseReading, PinnedPlan
from kstrl.acceptance_carried import Carried
from kstrl.autonomy_config import (
    strict_bool,
)
from kstrl.base_gates import (
    BaseGates,
)
from kstrl.config_numbers import check_number, check_numbers
from kstrl.contract import (
    ContractConfig,
)
from kstrl.feedforward import CodebaseScanConfig
from kstrl.launch_record import FlagValue
from kstrl.linear import LinearConfig
from kstrl.manifest import (
    Component,
)
from kstrl.observability import (
    NotifyConfig,
)
from kstrl.policy import PolicyConfig
from kstrl.requirements import SpecRequirement
from kstrl.review import (
    ReviewMode,
)
from kstrl.rung import Rung
from kstrl.security import (
    SecurityConfig,
    SecurityMode,
)
from kstrl.stack import (
    Stack,
    stack_in_force,
)
from kstrl.timeout import TimeoutConfig
from kstrl.verify_model import VerifyConfig
from kstrl.worktree_setup import WorktreeSetup

if TYPE_CHECKING:
    from kstrl.ui.base import UI

#: R10.3: the two settings [factory] claim_agreement accepts.
VALID_CLAIM_AGREEMENT = ("advisory", "block")

#: #562: the settings [factory] review_mode accepts, taken from the
#: ReviewMode that Phase 2 parses the field with
#: (``pipeline._phase_review``), so load and use cannot disagree.
VALID_REVIEW_MODES = tuple(mode.value for mode in ReviewMode)


def _validate_choice(value: str, source: str, accepted: tuple[str, ...]) -> str:
    """Reject a value outside ``accepted`` where it is read.

    Used for ``claim_agreement`` and ``review_mode``. Before #562
    ``review_mode`` was checked only when Phase 2 built a ``ReviewMode``
    from it, so a typo in kstrl.toml ran and paid for the engineer and
    then crashed with a traceback. Raising here turns the same typo into
    a ``config_preflight`` refusal before any agent call. Silently
    treating an unrecognised value as a default would be worse than
    either: the operator would believe a gate was set when it was not.
    """
    if value not in accepted:
        raise ValueError(
            f"invalid {source} {value!r}; expected " + " or ".join(repr(v) for v in accepted)
        )
    return value


@dataclass
class FactoryConfig:
    """Configuration for factory orchestration."""

    max_parallel: int = 4
    max_retries: int = 3
    retry_delay: float = 5.0
    use_worktrees: bool = True
    single_pr: bool = False
    create_prs: bool = True
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
    # R10.3 claim agreement: what to do when the engineer marked a
    # story passes=true and the reviewer did not independently confirm
    # it. "advisory" records a finding and lets the component proceed;
    # "block" also reverts the flag in the PRD and retries the story.
    # Ships advisory so the gate's first output is a measurement rather
    # than a wall. The autonomy ladder can force blocking on from L1
    # upward (see review.claim_blocks); it can never turn it off.
    claim_agreement: str = "advisory"
    review_agent_cmd: str | None = None
    review_agent_type: str | None = None
    review_model: str | None = None
    # #603: the code and integration reviewer's call limit, and the
    # architect's (`ks decompose`, `ks factory --spec`). Both run in the
    # parent process, so with no limit a hung call holds the whole run.
    # 0 means no limit (#467); a call killed at its limit is an
    # infrastructure error, never a verdict.
    review_timeout_seconds: float = 0.0
    architect_timeout_seconds: float = 0.0
    # Phase 2.5: security review (separate LLM call after Phase 2 review).
    # A config that names none gets SecurityConfig's own default (advisory,
    # #696 slice 9), never a silent skip.
    security_config: SecurityConfig = field(default_factory=SecurityConfig)
    # Phase 3: contract testing
    contract_config: ContractConfig | None = None
    # Phase 0: codebase scan
    codebase_scan_config: CodebaseScanConfig | None = None
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
    #
    # Since #195 this is the one flag that OUTRANKS the autonomy ladder,
    # and only when the operator set it: see explicit_fields below.
    pause_before_pr_merge: bool = False
    # #195: which config keys the OPERATOR set, as opposed to keys that
    # kept their built-in default. PROVENANCE, not a setting: it has no
    # toml key, no env var and no CLI flag of its own, and it is marked
    # metadata["provenance"] so the surfaces that sweep dataclass fields
    # (cli._collect_toml_notes) can skip it by asking rather than by
    # matching a name.
    #
    # Populated by FactoryConfig.load (key presence in [factory] / env
    # var presence), FactoryConfig.from_env (env var presence) and the
    # `ks factory` CLI flag. A bare FactoryConfig() has none, which is
    # the safe direction: not-explicit means the ladder decides.
    #
    # "The operator set it" is true of every source EXCEPT one, and the
    # exception is named here rather than left for a later reader to
    # discover: serve.subprocess_factory_runner always passes one of
    # --pause-before-pr-merge / --no-pause-before-pr-merge to its child,
    # synthesised from the queue item's merge disposition and the ladder.
    # A serve child's provenance is therefore SERVE's decision, made on
    # the operator's behalf, not a key the operator wrote. The two agree
    # wherever the item's disposition came from a person; they do not
    # where serve defaulted it.
    #
    # Key presence, never a value comparison. An env var set to a value
    # equal to the default IS an explicit request, and comparing values
    # would read it as absent (measured: config_report gets exactly this
    # wrong for KSTRL_FACTORY_PAUSE_BEFORE_PR_MERGE=0).
    explicit_fields: frozenset[str] = field(default=frozenset(), metadata={"provenance": True})
    # R3.1: run-level token budget. 0 means unbounded. Compared against
    # the run's aggregated total_tokens (a lower bound when some calls
    # report no usage); on breach the factory halts LOUDLY - the current
    # component fails with a synthetic budget finding and pending
    # components fail at scheduling instead of burning more spend.
    # Enforcement granularity is the phase boundary plus, since R8, the
    # gap between engineer iterations: an in-flight engineer loop or
    # review call can still overshoot before the parent sees its usage.
    #
    # CAVEAT, measured: total_tokens counts CACHE READS at par with
    # input tokens. A real run reached 1,864,081 total tokens of which
    # 1,781,669 (95.6%) were cache reads, for $1.22 of actual spend, so
    # a 500k "budget" halted at $1.22. This ceiling measures something
    # real but nearly uncorrelated with money; max_cost_usd below is the
    # one an operator usually means.
    max_total_tokens: int = 0
    # R8: run-level COST budget in USD. 0.0 means unbounded, matching the
    # max_total_tokens convention. Both ceilings may be set; whichever is
    # reached first halts. Compared against the run's aggregated
    # cost_usd, which - exactly like the token total - is a CLI
    # self-report and a LOWER BOUND whenever some calls report no cost.
    #
    # NOT a hard cap, and it must never be described as one: it has the
    # same between-iterations enforcement granularity as the token
    # ceiling. Measured: one engineer call of 376s overshot the entire
    # 500k token cap by 3.7x, and nothing in this design would have
    # interrupted it. Distinct from [agent] budget_usd, which is
    # adapter-internal (claude-sdk only) and bounds a single turn.
    max_cost_usd: float = 0.0
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
    # #482: run the record-only integration review after Phase 3. Its
    # outcome is recorded and printed and gates nothing in this slice.
    integration_review: bool = True
    # #483: when true, a stop without a clean integration verdict fails the
    # run. No fix is built: every open code finding is handed off (#696). Off
    # until the owner turns it on after the `integration` calibration (#480
    # decision 2).
    integration_blocking: bool = False
    # #233: fail a component whose gate failure count has not fallen for
    # this many consecutive attempts that failed in the same phase (a
    # phase change starts the count again). 0 (the default) is off: the
    # operator sets it after reading the per-attempt `failure_count` the
    # journal records on every superseded attempt.
    convergence_attempts: int = 0
    # #624: the time limit of the command that gives a kstrl worktree its
    # own dependencies (see kstrl/worktree_setup.py), in seconds, 0 = no
    # limit (#467). The command is the [stack]'s ``setup`` (#696).
    worktree_setup_timeout: float = 0.0
    # #696: the project's [stack], read by ``load``. Its ``setup`` is the
    # worktree setup and its ``env`` is what setup sees. Read here as well
    # as in VerifyConfig because --no-verify drops the VerifyConfig and a
    # worktree still needs its setup. Provenance: no [factory] key.
    project_stack: Stack | None = field(default=None, metadata={"provenance": True})
    # #700 slice 2: the two rungs a run under a [stack] proved before its
    # base gates (``_preflight_rungs``), cleared and their scratch removed
    # when the run ends (``run_factory``). The setup runs in the first and
    # every check in the second. Never read from kstrl.toml, env or a flag.
    setup_rung: Rung | None = field(default=None, metadata={"provenance": True})
    test_rung: Rung | None = field(default=None, metadata={"provenance": True})
    # R8.1: declarative merge-policy envelope for Phase 1. None means
    # run_factory resolves it from the toml [policy] section + env, via
    # RunEnvelope.resolve's ``policy_override`` seam (#192). Opt-in
    # ([policy].enabled = false): existing runs unchanged.
    #
    # INPUT ONLY: read once by RunEnvelope.resolve, never written back.
    # Round 2 of #192 deleted the write-back that used to be described
    # here, because it was unconditional: a caller reusing one
    # FactoryConfig for a second run had run 1's clamped envelope
    # already sitting in this field, so run 2 resolved nothing from
    # kstrl.toml and recorded run 1's hash as its own. The run's
    # resolved policy lives on the pipeline's RunEnvelope; read it
    # there. TestTheEnvelopeDoesNotOutliveItsRun fails if the write
    # comes back.
    policy_config: PolicyConfig | None = None
    # #436: the options `ks factory` was launched with on its command
    # line, minus launch_record.NOT_REPLAYED. Written to the run's
    # launch record before anything is spent, so `ks retry` can re-enter
    # the factory with them. Empty for every entry point that is not the
    # `ks factory` command, and never read from kstrl.toml or env.
    launch_flags: tuple[tuple[str, FlagValue], ...] = field(
        default=(), metadata={"provenance": True}
    )
    # #654 slice 4: `ks factory --accept-red-base <sha12>`. A run on a base
    # whose gates fail proceeds when this is at least 12 characters of the
    # sha the run measures (kstrl/base_gates.py::apply_acceptance). Per run:
    # no toml key and no env var, and `ks retry` replays it from the launch
    # record, so a retry on a base that moved refuses again. "" is none.
    accept_red_base: str = ""
    # #700 slice 4: `ks factory --acceptance <dir>`, the operator's plan of
    # acceptance checks, outside the repository. Per run: no toml key and no
    # env var, and `ks retry` replays it. "" is none. The plan pinned from it
    # (``acceptance.pin_plan``) and what the base replay read
    # (``acceptance.replay_base``) are set by the preflights.
    acceptance_dir: str = ""
    # #700 slice 7: `ks factory --design-acceptance`, the verification
    # designer writes the plan instead (``acceptance_design``). Per run, like
    # ``acceptance_dir``, and `ks retry` replays it.
    design_acceptance: bool = False
    # #700, owner decision of 2026-10-09: `ks factory --bug-report`, the run
    # fixes a reported bug, so a check the base keeps must fail on the base
    # (``acceptance_design.unreproduced``). Per run, and `ks retry` replays it.
    bug_report: bool = False
    acceptance_plan: PinnedPlan | None = field(default=None, metadata={"provenance": True})
    acceptance_base: BaseReading | None = field(default=None, metadata={"provenance": True})
    # #466: the checks of earlier features that passed on this run's base
    # (``acceptance_carried.carried_on_base``); Phase 3 replays them.
    acceptance_carried: tuple[Carried, ...] = field(default=(), metadata={"provenance": True})
    # #696 slice 7: the base reading ``base_refused_before_architect`` took
    # before `ks factory --spec` paid the architect. The run's own base gates
    # reuse it while the base names the same commit, so the base is measured
    # once. Never read from kstrl.toml, env or a flag.
    base_reading: BaseGates | None = field(default=None, metadata={"provenance": True})
    # #639 slice 5: the requirements of the decision register this run bound
    # (``_run_preflights``), which the engineers, the verification designer
    # and the PR bodies read. Never read from kstrl.toml, env or a flag.
    requirements: tuple[SpecRequirement, ...] = field(default=(), metadata={"provenance": True})
    # #700 (owner decision 2026-10-09): the harness whose sandbox confines
    # the engineer, or "" when none does. The acceptance record and the PR
    # bodies read it. Never read from kstrl.toml, env or a flag.
    engineer_sandbox: str = field(default="", metadata={"provenance": True})

    def resolved_verify_config(self) -> VerifyConfig:
        """The VerifyConfig Phase 1 runs with (#261).

        ``verify_config=None`` (``skip_verification`` is the separate,
        explicit skip sentinel) is the run's own ``[stack]`` with every
        other ``[verify]`` setting at its default, NOT a reload from disk.
        Before the #696 flag day the fallback was a bare ``VerifyConfig()``,
        which ran kstrl's built-in commands in any tree; with no stack it now
        fails closed (``verify.NO_STACK_CHECK``). ``pipeline._phase_verify``
        calls this, and so does ``engineer_verify_config`` below, which is
        what stops the gate and the engineer prompt answering the question
        two ways.
        """
        config = (
            self.verify_config
            if self.verify_config is not None
            else VerifyConfig(project_stack=self.project_stack)
        )
        return config if self.test_rung is None else replace(config, rung=self.test_rung)

    def engineer_verify_config(self) -> VerifyConfig | None:
        """What the engineer may be told Phase 1 will run, or None.

        None when ``--no-verify`` disabled Phase 1: no gate runs, so
        naming commands would claim a check that never happens.

        A method rather than a free function taking the two fields: the
        coupling between them is the point, and unpacking them at each
        call site made ``(cfg.verify_config, False)`` a legal miscall.
        """
        return None if self.skip_verification else self.resolved_verify_config()

    def worktree_setup(self, scaffold: str = "") -> WorktreeSetup:
        """The setup a worktree gets (#624): ``scaffold`` when a component
        names one, else the ``[stack]``'s ``setup``, with the stack's ``env``
        (#696). With no stack only a scaffold runs."""
        if self.project_stack is not None:
            unconfirmed = self.project_stack.unconfirmed
            return WorktreeSetup(
                scaffold or self.project_stack.setup,
                self.worktree_setup_timeout,
                self.project_stack.env,
                rung=self.setup_rung,
                refusal=f"the [stack] in kstrl.toml {unconfirmed}" if unconfirmed else "",
            )
        return WorktreeSetup(scaffold, self.worktree_setup_timeout)

    def worktree_setup_for_component(self, comp: Component) -> WorktreeSetup | None:
        """The setup for a component's own worktree: ``None`` under
        ``use_worktrees=False``, where the engineer runs in the operator's
        own checkout and kstrl installs nothing (#624)."""
        return self.worktree_setup(comp.scaffold) if self.use_worktrees else None

    def worktree_setup_summary(self) -> str:
        """The run-header value for the configured setup command (#624)."""
        return self.worktree_setup().command or "none"

    def _apply_worktree_setup_overlay(self, section: dict[str, Any]) -> None:
        """Overlay ``[factory] worktree_setup_timeout`` from the toml
        ``section``, then its env var, precedence env > toml > default (#624)."""
        if "worktree_setup_timeout" in section:
            self.worktree_setup_timeout = float(section["worktree_setup_timeout"])
        if "KSTRL_FACTORY_WORKTREE_SETUP_TIMEOUT" in os.environ:
            self.worktree_setup_timeout = float(os.environ["KSTRL_FACTORY_WORKTREE_SETUP_TIMEOUT"])

    def __post_init__(self) -> None:
        # R10.3: catch a bad claim mode wherever the config is
        # built, not only in load(). A FactoryConfig assembled from CLI
        # flags or in a test goes through here too.
        _validate_choice(
            self.claim_agreement,
            "[factory] claim_agreement",
            VALID_CLAIM_AGREEMENT,
        )
        _validate_choice(self.review_mode, "[factory] review_mode", VALID_REVIEW_MODES)

    @classmethod
    def from_env(cls) -> FactoryConfig:
        """Load factory config from environment variables."""
        from kstrl.config import parse_bool

        return cls(
            max_parallel=int(os.environ.get("FACTORY_MAX_PARALLEL", "4")),
            max_retries=int(os.environ.get("FACTORY_MAX_RETRIES", "3")),
            retry_delay=float(os.environ.get("FACTORY_RETRY_DELAY", "5.0")),
            merge_timeout=float(os.environ.get("FACTORY_MERGE_TIMEOUT", "300.0")),
            max_adversarial_calls=int(os.environ.get("KSTRL_FACTORY_MAX_ADVERSARIAL_CALLS", "0")),
            review_timeout_seconds=check_number(
                float(os.environ.get("KSTRL_FACTORY_REVIEW_TIMEOUT_SECONDS", "0")),
                "KSTRL_FACTORY_REVIEW_TIMEOUT_SECONDS",
            ),
            architect_timeout_seconds=check_number(
                float(os.environ.get("KSTRL_FACTORY_ARCHITECT_TIMEOUT_SECONDS", "0")),
                "KSTRL_FACTORY_ARCHITECT_TIMEOUT_SECONDS",
            ),
            max_total_tokens=check_number(
                int(os.environ.get("KSTRL_FACTORY_MAX_TOTAL_TOKENS", "0")),
                "KSTRL_FACTORY_MAX_TOTAL_TOKENS",
            ),
            max_cost_usd=check_number(
                float(os.environ.get("KSTRL_FACTORY_MAX_COST_USD", "0")),
                "KSTRL_FACTORY_MAX_COST_USD",
            ),
            pause_before_pr_merge=parse_bool(os.environ.get("KSTRL_FACTORY_PAUSE_BEFORE_PR_MERGE")),
            explicit_fields=(
                frozenset({"pause_before_pr_merge"})
                if "KSTRL_FACTORY_PAUSE_BEFORE_PR_MERGE" in os.environ
                else frozenset()
            ),
            progress_log_enabled=parse_bool(
                os.environ.get("KSTRL_FACTORY_PROGRESS_LOG_ENABLED", "1")
            ),
            keep_worktrees_on_failure=parse_bool(
                os.environ.get("KSTRL_FACTORY_KEEP_WORKTREES_ON_FAILURE")
            ),
            integration_review=parse_bool(os.environ.get("KSTRL_FACTORY_INTEGRATION_REVIEW", "1")),
            integration_blocking=parse_bool(os.environ.get("KSTRL_FACTORY_INTEGRATION_BLOCKING")),
            convergence_attempts=_validate_convergence_attempts(
                _env_int(os.environ.get("KSTRL_FACTORY_CONVERGENCE_ATTEMPTS", "0")),
                "KSTRL_FACTORY_CONVERGENCE_ATTEMPTS",
            ),
            worktree_setup_timeout=check_number(
                float(os.environ.get("KSTRL_FACTORY_WORKTREE_SETUP_TIMEOUT", "0")),
                "KSTRL_FACTORY_WORKTREE_SETUP_TIMEOUT",
            ),
            # R10.3: unlike review_mode next door, this key HAS an env
            # var, so from_env must read it. `ks factory` uses from_env
            # as the environment-only baseline that _collect_toml_notes
            # diffs against, and a field missing here is reported to the
            # operator as "from kstrl.toml" when it came from the
            # environment - a false provenance claim in the one place
            # they look to find out where a setting came from.
            claim_agreement=_validate_choice(
                os.environ.get("KSTRL_FACTORY_CLAIM_AGREEMENT", "advisory"),
                "KSTRL_FACTORY_CLAIM_AGREEMENT",
                VALID_CLAIM_AGREEMENT,
            ),
        )

    @classmethod
    def load(cls, root_dir: Path | None = None) -> FactoryConfig:
        """Load factory config with precedence: env > toml > defaults.

        Reads the ``[factory]`` section from ``<root_dir>/kstrl.toml`` if
        present, then overlays any matching env vars on top.
        """
        from kstrl.config import load_toml_section, parse_bool, resolve_config_file

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
            config.review_mode = _validate_choice(
                str(section["review_mode"]),
                "[factory] review_mode",
                VALID_REVIEW_MODES,
            )
        if "claim_agreement" in section:
            config.claim_agreement = _validate_choice(
                str(section["claim_agreement"]),
                "[factory] claim_agreement",
                VALID_CLAIM_AGREEMENT,
            )
        if "merge_timeout" in section:
            config.merge_timeout = float(section["merge_timeout"])
        # #603: read without a branch, as convergence_attempts is below,
        # so this loader's cyclomatic complexity does not grow.
        config.review_timeout_seconds = float(
            section.get("review_timeout_seconds", config.review_timeout_seconds)
        )
        config.architect_timeout_seconds = float(
            section.get("architect_timeout_seconds", config.architect_timeout_seconds)
        )
        # R2.2: the two safety knobs are reachable via toml (here), env
        # (below) and CLI flags (cli.py factory command).
        if "max_adversarial_calls" in section:
            config.max_adversarial_calls = int(section["max_adversarial_calls"])
        if "max_total_tokens" in section:
            config.max_total_tokens = check_number(
                int(section["max_total_tokens"]),
                "[factory] max_total_tokens",
            )
        if "max_cost_usd" in section:
            config.max_cost_usd = check_number(
                float(section["max_cost_usd"]),
                "[factory] max_cost_usd",
            )
        if "pause_before_pr_merge" in section:
            # strict_bool, not bool(): since #195 an explicit true here
            # outranks the autonomy ladder, so coercing `= "false"` to
            # True would manufacture an explicit request the operator
            # never wrote and then keep the gate up at every level. This
            # makes a quoted value a config_preflight refusal, the same
            # way [autonomy] enabled is.
            config.pause_before_pr_merge = strict_bool(
                section, "pause_before_pr_merge", config.pause_before_pr_merge
            )
            config.explicit_fields |= {"pause_before_pr_merge"}
        if "progress_log_enabled" in section:
            config.progress_log_enabled = bool(section["progress_log_enabled"])
        if "keep_worktrees_on_failure" in section:
            config.keep_worktrees_on_failure = bool(section["keep_worktrees_on_failure"])
        config._apply_worktree_setup_overlay(section)
        config.integration_review = strict_bool(
            section, "integration_review", config.integration_review
        )
        config.integration_blocking = strict_bool(
            section, "integration_blocking", config.integration_blocking
        )
        config.convergence_attempts = _validate_convergence_attempts(
            section.get("convergence_attempts", config.convergence_attempts),
            "[factory] convergence_attempts",
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
            config.max_adversarial_calls = int(os.environ["KSTRL_FACTORY_MAX_ADVERSARIAL_CALLS"])
        config.review_timeout_seconds = float(
            os.environ.get("KSTRL_FACTORY_REVIEW_TIMEOUT_SECONDS", config.review_timeout_seconds)
        )
        config.architect_timeout_seconds = float(
            os.environ.get(
                "KSTRL_FACTORY_ARCHITECT_TIMEOUT_SECONDS", config.architect_timeout_seconds
            )
        )
        if "KSTRL_FACTORY_MAX_TOTAL_TOKENS" in os.environ:
            config.max_total_tokens = check_number(
                int(os.environ["KSTRL_FACTORY_MAX_TOTAL_TOKENS"]), "KSTRL_FACTORY_MAX_TOTAL_TOKENS"
            )
        if "KSTRL_FACTORY_MAX_COST_USD" in os.environ:
            config.max_cost_usd = check_number(
                float(os.environ["KSTRL_FACTORY_MAX_COST_USD"]), "KSTRL_FACTORY_MAX_COST_USD"
            )
        if "KSTRL_FACTORY_PAUSE_BEFORE_PR_MERGE" in os.environ:
            config.pause_before_pr_merge = parse_bool(
                os.environ["KSTRL_FACTORY_PAUSE_BEFORE_PR_MERGE"]
            )
            config.explicit_fields |= {"pause_before_pr_merge"}
        if "KSTRL_FACTORY_PROGRESS_LOG_ENABLED" in os.environ:
            config.progress_log_enabled = parse_bool(
                os.environ["KSTRL_FACTORY_PROGRESS_LOG_ENABLED"]
            )
        if "KSTRL_FACTORY_KEEP_WORKTREES_ON_FAILURE" in os.environ:
            config.keep_worktrees_on_failure = parse_bool(
                os.environ["KSTRL_FACTORY_KEEP_WORKTREES_ON_FAILURE"]
            )
        config.integration_review = parse_bool(
            os.environ.get("KSTRL_FACTORY_INTEGRATION_REVIEW", str(config.integration_review))
        )
        config.integration_blocking = parse_bool(
            os.environ.get("KSTRL_FACTORY_INTEGRATION_BLOCKING", str(config.integration_blocking))
        )
        config.convergence_attempts = _validate_convergence_attempts(
            _env_int(
                os.environ.get(
                    "KSTRL_FACTORY_CONVERGENCE_ATTEMPTS", str(config.convergence_attempts)
                )
            ),
            "KSTRL_FACTORY_CONVERGENCE_ATTEMPTS",
        )
        if "KSTRL_FACTORY_CLAIM_AGREEMENT" in os.environ:
            config.claim_agreement = _validate_choice(
                os.environ["KSTRL_FACTORY_CLAIM_AGREEMENT"],
                "KSTRL_FACTORY_CLAIM_AGREEMENT",
                VALID_CLAIM_AGREEMENT,
            )
        # Last, so every [factory] key above has been read when a bad
        # [stack] raises (the entry check's unread-name report).
        config.project_stack = stack_in_force(root_dir)
        return check_numbers(config)


def _validate_convergence_attempts(value: object, source: str) -> int:
    """``convergence_attempts`` as a whole number of at least 0 (#233).

    0 is off. A bool and a quoted number are refused, as strict_bool refuses
    a quoted bool.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{source} must be a whole number of at least 0, got {value!r}")
    return value


def _env_int(raw: str) -> object:
    """An environment value as an int when it is one, else the text, so the
    validator names the bad value in its own words."""
    try:
        return int(raw)
    except ValueError:
        return raw


def merge_gate_unreachable_warning(config: FactoryConfig) -> str | None:
    """Warning when the merge gate is on but can never run, else None.

    The pipeline reaches ``_phase_checkpoint`` only when ``create_prs`` is
    on AND ``single_pr`` is off, so a config whose FINAL resolved values
    have the gate on while the checkpoint is unreachable is a governance
    control failing open (#207). The flag can arrive in that state three
    ways: set explicitly (kstrl.toml / env) under `ks run`, which forces
    ``create_prs = False``; flipped on by the L1/L2 autonomy bundle AFTER
    any command-level notice already ran; or combined with ``single_pr``
    mode, whose aggregate PR is created without a checkpoint. Called from
    ``run_factory`` after autonomy resolution - the one point that sees
    the final values on every path - so all three cases warn.
    """
    if not config.pause_before_pr_merge:
        return None
    if not config.create_prs:
        return (
            "pause_before_pr_merge is on but create_prs is off: no PR is "
            "created, so the merge gate can never run. The gate is "
            "honoured only by PR-creating invocations (`ks factory` / "
            "`ks serve`), not `ks run`."
        )
    if config.single_pr:
        return (
            "pause_before_pr_merge is on but single_pr mode creates one "
            "aggregate PR without a per-component checkpoint, so the "
            "merge gate never runs. Disable single_pr to honour the "
            "merge gate."
        )
    return None


def review_enabled(config: FactoryConfig) -> bool:
    """Will Phase 2 run a reviewer at all?

    One predicate rather than a repeated ``!= ReviewMode.SKIP.value``,
    because both readers carry the same caveat about WHEN they may ask
    (the autonomy ladder can force review back on) and two copies of a
    caveat drift.
    """
    return config.review_mode != ReviewMode.SKIP.value


def security_enabled(config: FactoryConfig) -> bool:
    """Will Phase 2.5 run a security reviewer at all?"""
    return config.security_config.mode != SecurityMode.SKIP.value


def claim_gate_unreachable_warning(config: FactoryConfig) -> str | None:
    """Warning when the claim gate is on but can never run, else None.

    ``claim_agreement = "block"`` asks the harness to fail a component
    whose story the reviewer did not confirm, and `review_mode = "skip"`
    means no reviewer runs, so there is never a verdict to confirm with.
    `_phase_review` returns before the claim check on that path, so
    the gate is not merely lenient, it is absent. Same shape and same
    reason as ``merge_gate_unreachable_warning``: a governance control
    silently failing open is worse than one that was never configured,
    because the operator believes it is on.

    Called from ``run_factory`` after autonomy resolution, because the
    L1/L2 bundle forces ``review_mode`` to hard and can therefore make a
    config that looked unreachable reachable.
    """
    if config.claim_agreement != "block":
        return None
    if not review_enabled(config):
        return (
            "claim_agreement is 'block' but review_mode is 'skip': no "
            "reviewer runs, so no story can be confirmed and the "
            "claim gate never fires. Set review_mode to 'advisory' "
            "or 'hard' to honour it."
        )
    return None


def warn_unreachable_gates(factory_config: FactoryConfig, ui: UI) -> None:
    """Warn about a merge gate or a claim gate that the run cannot reach."""
    # Issue #207 (review P1): checked AFTER autonomy resolution, because
    # the L1/L2 bundle can flip pause_before_pr_merge on when no config
    # flag ever set it - any earlier check misses that path and the gate
    # would be reported ON while _phase_checkpoint stays unreachable.
    gate_warning = merge_gate_unreachable_warning(factory_config)
    if gate_warning is not None:
        ui.warn(gate_warning)
    # R10.3: same timing, same reason - the L1/L2 bundle forces
    # review_mode to hard above, so a claim gate that looked
    # unreachable before autonomy resolved may be reachable now.
    claim_warning = claim_gate_unreachable_warning(factory_config)
    if claim_warning is not None:
        ui.warn(claim_warning)
