"""The state that one component pipeline holds, and the hooks that it takes.

``PipelineState`` is the base class of ``ComponentPipeline``. It holds the
constructor, the shared run state (#193) and the derived paths. ``PipelineHooks``
holds the phase functions that the factory injects.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from kstrl import events as ev
from kstrl.agents.base import UsageTotals
from kstrl.divergence import AttemptReading
from kstrl.inbox import Inbox
from kstrl.interaction import InteractionChannel, UiInteractionChannel
from kstrl.manifest import Manifest
from kstrl.observability import NotifyHooks
from kstrl.review import ReviewResult
from kstrl.runenvelope import RunEnvelope
from kstrl.runstate import RunState
from kstrl.scope import RunScope
from kstrl.security import SecurityResult
from kstrl.verify import MechanicalVerification
from kstrl.waivers import ApprovalSnapshot
from kstrl.worktree_sweep import WorktreeSweep

if TYPE_CHECKING:
    from kstrl.config import KstrlConfig
    from kstrl.factory import AdversarialAgentSelection, FactoryConfig, FactoryResult
    from kstrl.knowledge import KnowledgeConfig
    from kstrl.pipeline import FactUtilization
    from kstrl.ui.base import UI


def _iso_now() -> str:
    """Current UTC time as ISO 8601, matching the manifest timestamps."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass(frozen=True)
class PipelineHooks:
    """Injected phase functions (LLM / subprocess seams).

    The factory resolves these from its module globals when the run
    starts, so tests patching ``kstrl.factory.run_review`` (and
    friends) keep intercepting them; pipeline unit tests inject stubs
    directly.
    """

    # Typed by shape, not ``Callable[..., VerificationResult]`` (#316):
    # Phase 1 is the seam that carries a component's SCOPE, and ``...``
    # checks nothing about it. See ``verify.MechanicalVerification``.
    #
    # The three hooks below were NOT cleared, they were not done. Each
    # carries the same hazard, measured: `run_review` and
    # `run_security_review` take adjacent `prd_path` / `worktree_path`
    # Paths and are called positionally below; `distill_facts` takes
    # three Paths among ten positional arguments. Transposing any of
    # those type-checks clean today, exactly as `harness_paths` did.
    # Tightening them is not one character each - their call sites pass
    # positionally, so it is a signature plus a call-site change plus a
    # drift test per hook, in three more modules. Its own issue.
    run_mechanical_verification: MechanicalVerification
    run_review: Callable[..., ReviewResult]
    run_security_review: Callable[..., SecurityResult]
    distill_facts: Callable[..., tuple[int, str, bool]]
    # No build_knowledge_context seam by design (#191). The distill
    # phase once rebuilt the knowledge prefix to measure utilization
    # against; that rebuild read the store AFTER distillation had
    # written this run's facts into it. The prefix is now captured by
    # the factory at submit time and handed over via
    # ComponentPipeline.record_injected_knowledge, so there is no way
    # to reintroduce the rebuild through this struct.
    measure_fact_utilization: Callable[..., dict[str, int]]
    cleanup_worktree: Callable[[str, Path, str], WorktreeSweep]


class PipelineState:
    """The state of one component pipeline: its constructor and the shared run state."""

    def __init__(
        self,
        *,
        manifest: Manifest,
        manifest_path: Path,
        factory_config: FactoryConfig,
        base_config: KstrlConfig,
        ui: UI,
        root_dir: Path,
        run_id: str,
        bus: ev.EventBus,
        journal_path: Path | None,
        run_paths: ev.RunPaths | None = None,
        interaction: InteractionChannel | None = None,
        notify: NotifyHooks,
        review_selection: AdversarialAgentSelection,
        security_selection: AdversarialAgentSelection | None,
        knowledge_config: KnowledgeConfig,
        run_scope: RunScope,
        # #192: required, with no default and no ``or ...load()``
        # fallback - that fallback is the defect the envelope removes.
        run_envelope: RunEnvelope,
        hooks: PipelineHooks,
        # #193: the five mutable structures the factory and this
        # pipeline share, in one object. Handed over, never copied - see
        # kstrl/runstate.py.
        run_state: RunState,
    ) -> None:
        self.manifest = manifest
        self.manifest_path = manifest_path
        self.factory_config = factory_config
        self.base_config = base_config
        self.ui = ui
        self.root_dir = root_dir
        # #192: every run-level config section this pipeline enforces
        # arrives already resolved, and this constructor resolves none of
        # its own. The phases run per component attempt, so a read here
        # made a component's enforcement diverge from what the run
        # recorded.
        #
        # Round 1 of #192 loaded these four in this constructor instead.
        # The review measured the cost: a malformed [inbox] raised out of
        # __init__ with no handler above it and above the line that
        # records the architect's spend, so `serve` charged $0 for a
        # launch that had spent real money (#257). The envelope is
        # resolved before the run directory exists now, where a bad
        # section is a refusal naming the section and the key.
        #
        # Named locally rather than read through ``self.run_envelope``
        # at every use: these four are what the pipeline enforces, and
        # ONE of the four names predates #192. Counted at the branch
        # base 414d662: self.sandbox_config 3 readers, and
        # self.inbox_config and self.divergence_config 0 each, and a
        # fixtures config (removed by #700 slice 8) 0, because those sections
        # were resolved into locals inside the phases. Round 1 of #192
        # created the other three names, so keeping them is a choice
        # this change made rather than a shape it inherited: one
        # spelling per section at the point of use, against reading
        # ``self.run_envelope.<section>`` at nine sites. The envelope
        # also carries what the autonomy ladder can clamp - [policy]
        # and the level - so the clamped values are the ones enforced
        # and recorded.
        #
        # #266 review finding 3 for [sandbox]: the reviewer roles were
        # built with read_only=True and NO sandbox, so `[sandbox]
        # enabled = true` reached the engineer and never the reviewers -
        # the one pair of roles that now runs shell commands inside the
        # tree under review.
        self.sandbox_config = run_envelope.sandbox
        self.inbox_config = run_envelope.inbox
        self.divergence_config = run_envelope.divergence
        self.run_envelope = run_envelope
        self.run_id = run_id
        self.bus = bus
        self.journal_path = journal_path
        self.run_paths = run_paths
        # PR A: the interaction seam. Defaults to today's terminal
        # behavior; embedded mode (PR F) injects a QueueInteractionChannel.
        self.interaction: InteractionChannel = (
            interaction if interaction is not None else UiInteractionChannel(ui)
        )
        self.notify = notify
        # R8.3: lazily built so a disabled inbox costs nothing and a
        # broken one can never fail a run (see _inbox_add).
        self._inbox: Inbox | None = None
        self._inbox_disabled = False
        self._inbox_typed: set[str] = set()
        # #595: the approvals this run's checks may apply, read once by
        # snapshot_waivers. None until then, which applies none.
        self._approvals: ApprovalSnapshot | None = None
        self.review_selection = review_selection
        self.security_selection = security_selection
        self.knowledge_config = knowledge_config
        # #269: the run's plan-time scope snapshot. The pipeline READS
        # it and never resolves one of its own, which is what stops
        # Phase 1 and the in-loop guard drifting apart.
        self.run_scope = run_scope
        self.hooks = hooks
        self.run_state = run_state

        # R3.1 cost meter: per-component, per-phase usage rollup plus a
        # run-level total. Phases: "engineer" (loop iterations, reported
        # by the worker), "review", "security", "distill" (fresh agent
        # instance per phase, so an instance's accumulated usage_records
        # ARE that phase's spend). Retried attempts accumulate: every
        # attempt cost real tokens, so the meter never forgets a failed
        # attempt.
        self.usage_meter: dict[str, dict[str, UsageTotals]] = {}
        # #191: the knowledge prefix each component's engineer ACTUALLY
        # saw, captured by the factory at submit time. Keyed by
        # component id and overwritten every attempt. None means the
        # capture failed or knowledge was off, which is NOT the same as
        # "" ("knowledge on, store empty, nothing to inject").
        self.injected_knowledge: dict[str, str | None] = {}
        # #191: fact-utilization per component, read at run end by the
        # evolution journal. The journal - not the event stream - is the
        # durable L2+ gate evidence.
        self.fact_utilization: dict[str, FactUtilization] = {}
        # #265: one reading per attempt whose REVIEWER ran and failed the
        # component, keyed by component id. Named for the phase rather
        # than for the change: the predicate is phase-agnostic, so a
        # second phase wiring into it later must get its own store or it
        # would compare one phase's finding identities against another's.
        #
        # In-run only, deliberately: `ks retry` already resets `retries`
        # and clears the finding stream, so an operator's explicit retry
        # is meant to start from a clean slate and this history must not
        # outlive it. It also stays out of IterationContext, which is
        # rendered into the engineer's prompt - a cost governor has no
        # business in the agent's context.
        self.review_readings: dict[str, list[AttemptReading]] = {}
        # #233: (phase, gate failure count) of each consecutive failed
        # attempt, keyed by component id, for [factory] convergence_attempts.
        # In-run only for the reasons review_readings gives above, and
        # cleared by an attempt that produced no count or failed in a
        # different phase, and by a contract-breaker reset
        # (record_contract_failure).
        self.failure_counts: dict[str, list[tuple[str, int]]] = {}
        # #247: which skippable phases produced a reading, per component
        # and per attempt, as (attempt, phase) pairs. Merged into the
        # retry context at whichever gate finally fails, because that is
        # the only moment the context is written.
        #
        # In-run only and NOT a constructor parameter, unlike
        # component_contexts: after record_contract_failure moved here,
        # nothing outside the pipeline reads it. The record itself
        # travels between attempts inside the context's JSON, so a fresh
        # process picks up what the previous one observed.
        #
        # Only the two context writers merge these, so an attempt that
        # ends WITHOUT writing a context - a terminal failure, a halt, a
        # pass - drops what it observed. No test fails on that because
        # the direction is safe: a reading that never arrives retires
        # nothing, so a finding is shown again rather than cleared by an
        # observation nobody carried forward. There is also no next
        # attempt on those paths to show it to.
        self._phase_readings: dict[str, set[tuple[int, str]]] = {}
        # Components whose usage snapshot could not be retired
        # before this attempt launched; disk salvage is refused
        # for them (R8 review P2 on 22e99b4).
        self._usage_salvage_unsafe: set[str] = set()
        self.run_usage = UsageTotals()

        # R8 (measured): ceilings whose coverage gap has already been
        # announced this run. One warning per ceiling - the gap only
        # widens, and a line per phase would train the operator to
        # ignore it. The final numbers travel with the halt and the
        # rollup.
        self._coverage_announced: set[str] = set()

        # E4: adversarial-call counter shared across review / security /
        # knowledge phases. When max_adversarial_calls is 0 the budget is
        # unbounded. Otherwise, once it is exhausted: a hard-mode review
        # or security phase REFUSES and the component fails (R10.5,
        # #226), an advisory one is skipped with a warning and a
        # recorded phase_skipped, and the distiller is always skipped
        # because it gates nothing.
        self._adversarial_calls = 0

        # R6.4: monotonic start of each component's current attempt, so
        # the recorded duration covers the whole attempt (engineer loop +
        # verify + review + security + PR flow), not just the engineer
        # loop, and backstop-timeout failures stop recording 0.0.
        self._attempt_started_monotonic: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Shared run state (#193)
    # ------------------------------------------------------------------
    #
    # Read-only on purpose. Nothing in kstrl/ or tests/ rebinds any of
    # these five names; every writer mutates the object in place, and a
    # setter would be the one way to replace a shared structure with a
    # private one. Same reason ``usage_paths`` below is derived rather
    # than stored.

    @property
    def factory_result(self) -> FactoryResult:
        return self.run_state.factory_result

    @property
    def worktree_paths(self) -> dict[str, Path]:
        return self.run_state.worktree_paths

    @property
    def component_contexts(self) -> dict[str, str]:
        return self.run_state.component_contexts

    @property
    def fresh_base_retry_ids(self) -> set[str]:
        return self.run_state.fresh_base_retry_ids

    @property
    def component_failure_signatures(self) -> dict[str, list[str]]:
        return self.run_state.component_failure_signatures

    def component_base(self, comp_id: str) -> str:
        """What ``comp_id``'s change is judged against (#543).

        The commit its worktree started at when that commit holds code the
        base branch lacks (a dependency that reached no base, or the
        earlier components on single_pr's shared branch), otherwise the
        manifest's base branch. Every phase that diffs reads it here, so
        the in-loop guard, Phase 1, the diff, both reviewers and the
        divergence reading judge one change.
        """
        return self.run_state.component_bases.get(comp_id, self.manifest.base_branch)

    @property
    def usage_paths(self) -> ev.RunPaths:
        """Where engineer-loop usage snapshots live.

        R8: deliberately SEPARATE from ``run_paths``, which is None when
        progress logging is off - accounting must survive the
        observability opt-out (review finding P2-d). Derived rather than
        passed since #193: the factory computed the identical
        ``RunPaths.for_run(root_dir, run_id)``, handed it in, and read it
        straight back off the object at ``factory.py``'s
        ``_salvage_aborted_usage``. Nothing inside this class ever read
        the stored attribute. The old parameter was ``RunPaths | None``
        and the None case existed only for callers that did not pass it,
        so it is gone with the parameter.
        """
        return ev.RunPaths.for_run(self.root_dir, self.run_id)
