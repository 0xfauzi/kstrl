"""One scope decision per component, made before the first engineer call.

#264 put kstrl's own files inside every component's effective scope, and
#268 made the component PRD agent-writable by design as a consequence.
Both guards then had to answer "may this component write this file?"
from a file the component could rewrite, and #268 answered that with
detection: compare the worktree PRD against the pre-run copy and refuse
a widened one.

Detection is the weaker of the two available answers, and it carries a
cost of its own. Every false positive is a hard stop, because the
refusal becomes a Phase 1 failure and the component retries into the
identical failure forever. #268's first attempt did exactly that: it
compared ``allowedPaths`` as LISTS, so a benign re-serialisation failed
the component closed and it never recovered. That was caught in review
and narrowed to set-additions, but the shape of the risk is intrinsic
to comparing a value the agent can rewrite. This paragraph is the one
copy of that history; everything else that needs it points here.

This module makes the rewrite unable to matter. Each component's scope
is resolved ONCE, from the pre-run tree, before any engineer runs, and
the resulting :class:`ComponentScope` is what both guards read:

- the in-loop guard, through ``factory._submit_args`` ->
  ``factory._run_component`` -> ``loop.run_loop``;
- the Phase 1 gate, through ``pipeline.ComponentPipeline`` ->
  ``verify.check_diff_scope`` (or ``verify.check_scope_source``, when
  the snapshot is ``unresolved``).

Neither re-reads a PRD to answer the scope question, so there is
nothing for an agent to widen and nothing to compare. The snapshot is
also what makes ``use_worktrees=False`` covered rather than excepted:
that mode has no isolation boundary and therefore no pre-run copy
distinct from the live one, so a comparison there had nothing to
compare, while a value read before the agent started is just as
trustworthy with or without a worktree.

What this does NOT cover, and why ``PRD.tamper_changes`` stays: scope is
only one of the things Phase 1 reads out of the component PRD. The
stories reach ``verify.check_prd_stories``, the fixtures reach
``fixtures.check_fixtures_from_prd``, the criteria reach the reviewer
and the R10.3 set-point sensor. Those readers need the LIVE file,
because the agent setting ``passes`` is the whole job, so they cannot be
served from a snapshot and the comparison is still the only answer
available for them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

from kstrl.prd import PRD

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from kstrl.config import KstrlConfig
    from kstrl.manifest import Component, Manifest

#: Where a component's AUTHORED scope came from. Recorded on every
#: snapshot so a scope decision is auditable after the run, which a
#: value re-derived at two call sites never was.
#:
#: - ``component_prd``: the component's own ``allowedPaths``, written by
#:   the architect and read from the pre-run tree.
#: - ``run_flag``: the run-wide ``--allowed-paths``, used when the PRD
#:   carries no scope of its own (legacy PRDs predate the field). NOT
#:   when the PRD could not be read: that is ``unresolved``, because a
#:   scope nobody could read is not a scope that does not exist.
#: - ``unconstrained``: neither, which ``check_diff_scope`` treats as no
#:   constraint. The historical behaviour for legacy PRDs.
#: - ``unresolved``: no trustworthy scope could be established. Phase 1
#:   fails CLOSED on it (R1.5).
ScopeSource = Literal["component_prd", "run_flag", "unconstrained", "unresolved"]

#: The origin recorded when the run-wide flag supplied the scope. The
#: flag has no file to name, and the two sites that build a
#: ``run_flag`` snapshot must agree on what to call it.
RUN_FLAG_ORIGIN = "--allowed-paths"


@dataclass(frozen=True)
class ComponentScope:
    """One component's scope, as it stood before the engineer ran.

    ``allowed_paths`` is the AUTHORED list and ``harness_paths`` is
    kstrl's own carve-out (#264). They stay separate all the way into
    every failure message: an operator has to be able to tell what THEY
    authorised from what the harness added on their behalf, and a retry
    agent must not read its own PRD or progress log as the thing it has
    to stop writing.

    Frozen, so the snapshot cannot be repointed for the life of the run.
    The lists inside it are handed to consumers as they are rather than
    copied, which is safe because every consumer copies at its own
    boundary: ``check_diff_scope`` builds a fresh ``effective`` list,
    ``KstrlConfig`` copies ``allowed_paths`` into the worker's config,
    and ``run_loop`` folds ``guard_ignored_paths`` into a new list
    before the first iteration.
    """

    allowed_paths: list[str] | None
    harness_paths: list[str] = field(default_factory=list)
    source: ScopeSource = "unresolved"
    #: The file or flag the authored list came from, for the audit
    #: record: a repository-relative PRD path, ``--allowed-paths``, or
    #: empty when nothing supplied one. Empty means empty (#293
    #: review) - an ``unconstrained`` or ``unresolved`` snapshot names
    #: no origin, because naming the PRD would record it as the source
    #: of a list it did not supply. Where the list should have come
    #: from is the ``error``'s business, and it names that path.
    origin: str = ""
    #: Set only for ``unresolved``. Refuses the component at
    #: ``factory._preflight_component_scope`` before any engineer call,
    #: and, if it ever gets past that, reaches Phase 1 as
    #: ``allowed_paths_error``, where ``verify.check_scope_source``
    #: fails closed on it under its own name (#294) instead of under
    #: ``diff_scope``, whose retry context reads as "narrow the diff".
    error: str | None = None

    @property
    def is_trustworthy(self) -> bool:
        """Whether anything may be ENFORCED from this snapshot.

        False only for ``unresolved``. The one predicate, so the two
        consumers holding the object ask the same question rather than
        each re-deriving it from a different field: the preflight
        refuses on it, and ``factory._worker_scope`` declines to hand
        the in-loop guard a list it does not have. An unconstrained
        snapshot IS trustworthy - "the architect authored no scope" is
        knowledge, and the historical no-constraint pass.
        """
        return self.error is None

    @classmethod
    def resolve(
        cls,
        comp: Component,
        root_dir: Path,
        base_config: KstrlConfig,
    ) -> ComponentScope:
        """Read one component's scope out of the pre-run tree.

        ``root_dir``, never a worktree. The whole point is that this
        value is established before any agent could have touched it, so
        the source has to be the tree the operator controls.

        The run-wide ``--allowed-paths`` flag is the fallback when the
        PRD carries no scope of its own. That resolution used to live in
        ``factory._component_scope`` and applied to the in-loop guard
        ALONE, while Phase 1 ignored the flag entirely - so a run could
        enforce two different allowlists at its two guards. Keeping the
        fallback rather than dropping it is the answer that removes no
        authority: the flag is operator-authored, is not agent-writable,
        and is the ONLY scope source ``ks run --allowed-paths`` has
        (R2.3/CRIT-8 fixed it being a silent no-op there). Dropping it
        would have made Phase 1 the arbiter of a list the operator never
        gets to set; adopting it makes Phase 1 enforce what the engineer
        was already being held to in-loop.

        A PRD that will not READ is a different thing from a PRD that
        carries no scope, and the flag does NOT cover the first
        (#293 review). "No allowedPaths" is knowledge: the architect
        wrote none, so the operator's run-wide list is the intended
        authority. "Could not read it" is the absence of knowledge: the
        component may have had a narrow authored scope, and silently
        enforcing the operator's typically much broader list in its
        place is a widening nobody chose. So the error WINS over the
        flag - R1.5's own principle, moved to plan time - and the
        component gets an ``unresolved`` snapshot that
        ``factory._preflight_component_scope`` refuses before any spend
        and ``check_scope_source`` fails closed on if it ever gets past
        that.

        An earlier version of this docstring justified the opposite
        ordering with "the same unreadable file also fails
        ``check_prd_stories``". That is FALSE under worktrees:
        ``check_prd_stories`` reads ``wt_path / prd_path``, and a
        repository that tracks its component PRDs gives the worktree a
        valid copy from the branch while the working-tree copy in the
        main checkout is truncated or unreadable. Nothing else would
        have noticed.

        It still does not abort scheduling by raising (R8 review finding
        5: OSError, not just FileNotFoundError, because a PRD path that
        is a directory raises IsADirectoryError). The failure becomes a
        recorded snapshot and then a refusal, not an exception.

        The four outcomes share one tail deliberately. Returning early
        from each ``except`` arm meant writing the ``run_flag`` fallback
        twice, in two methods, so a reader had to hold both to see that
        the ordering below is the whole policy.
        """
        harness = base_config.component_harness_files(comp.prd_path, root_dir)
        run_flag = list(base_config.allowed_paths) or None
        authored: list[str] | None = None
        error: str | None = None
        try:
            prd = PRD.load(root_dir / comp.prd_path)
        except FileNotFoundError as exc:
            error = f"pre-run PRD not found ({comp.prd_path}): {exc}"
        except OSError as exc:
            error = f"pre-run PRD could not be read ({comp.prd_path}): {exc}"
        except ValueError as exc:
            error = f"pre-run PRD failed to parse ({comp.prd_path}): {exc}"
        else:
            authored = list(prd.allowed_paths or ()) or None

        if authored is not None:
            return cls(authored, harness, "component_prd", comp.prd_path)
        # Before the flag, never after it: an unreadable PRD is not an
        # absent scope, and a run-wide list must not stand in for one
        # nobody could read.
        if error is not None:
            return cls(None, harness, "unresolved", "", error)
        if run_flag is not None:
            return cls(run_flag, harness, "run_flag", RUN_FLAG_ORIGIN)
        return cls(None, harness, "unconstrained")


@dataclass(frozen=True)
class RunScope:
    """Every component's scope for one run, resolved in one pass.

    Built by ``factory._run_factory_locked`` before the pipeline exists
    and before the scheduler can launch anything, then handed to both
    guards. Resolving the whole manifest at once, rather than each
    component as it is scheduled, is what makes the value independent of
    when a component runs: a retry reads the same snapshot the first
    attempt did, which under ``use_worktrees=False`` is the difference
    between a scope the agent could have edited between attempts and one
    it could not.
    """

    by_component: Mapping[str, ComponentScope]

    @classmethod
    def resolve(
        cls,
        manifest: Manifest,
        root_dir: Path,
        base_config: KstrlConfig,
    ) -> RunScope:
        """Snapshot every component in the manifest.

        Every component, not only the PENDING ones: a component that is
        resumed, retried or re-run must be judged against the scope this
        run recorded, and filtering here would leave those looking up a
        key that is not there.
        """
        return cls(
            MappingProxyType(
                {
                    comp.id: ComponentScope.resolve(comp, root_dir, base_config)
                    for comp in manifest.components
                }
            )
        )

    def for_component(self, component_id: str) -> ComponentScope:
        """The snapshot for ``component_id``, or a fail-closed stand-in.

        The single read point for both guards: the in-loop guard through
        ``factory._submit_args`` and Phase 1 through
        ``pipeline._phase_verify``. Neither derives a scope of its own,
        which is what stops them drifting apart the way
        ``factory._component_scope`` and the old
        ``_resolve_verify_scope`` did.

        Total by construction. A component the plan-time pass never saw
        cannot be judged against a scope this run recorded, so it gets
        an ``unresolved`` snapshot: no authored list, an EMPTY carve-out
        (reporting more, never less) and an error that fails Phase 1
        closed. Unreachable while the pipeline and the snapshot are
        built from the same manifest, which is the whole arrangement -
        but "unreachable" is a claim about today's call graph, and the
        fail-closed stand-in is what keeps it from becoming a silent
        pass tomorrow.
        """
        scope = self.by_component.get(component_id)
        if scope is not None:
            return scope
        return ComponentScope(
            None,
            [],
            "unresolved",
            "",
            "no plan-time scope was resolved for this component",
        )
