"""Every pre-spend refusal of a run: scope, decision register, base gates, rungs, stack and pins."""

from __future__ import annotations

import functools
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from kstrl.acceptance import SETUP_ZONE, TEST_ZONE, pin_plan, prove_zones
from kstrl.acceptance import write_record as write_isolation_record
from kstrl.architect import (
    DecisionRegister,
    DecisionRegisterError,
    bind_register,
    read_decisions,
)
from kstrl.events import (
    CheckpointResolved,
    ComponentScopeResolved,
    EventBus,
)
from kstrl.factory.acceptance_design import (
    acceptance_source,
)
from kstrl.factory.factory_worktrees import (
    _evidence_worktrees_to_keep,
    _preflight_component_branches,
    _prune_stale_worktrees,
)
from kstrl.factory.plan_gate import spec_pin_errors, stack_pin_errors
from kstrl.pipeline import (
    FactoryConfig,
    run_limits,
    write_launch_record,
)
from kstrl.plan import (
    ComponentStatus,
    Manifest,
    RunScope,
)
from kstrl.proc import TimeoutConfig, refusal_of, release, sandbox_hint
from kstrl.project import (
    NO_STACK,
    STACK_KIND,
    Stack,
    StackRefused,
    confirmed_stack,
    stack_paths,
    unconfirmed_lines,
)
from kstrl.verify import (
    BaseGates,
    ScopeHazard,
    VerifyConfig,
    apply_acceptance,
    measure_base_gates,
    refusal_lines,
    scope_entry_hazard,
    warning_lines,
)
from kstrl.verify import write_record as write_base_gates_record

if TYPE_CHECKING:
    from kstrl.ui import UI


_SCOPE_HAZARD_REASONS: dict[ScopeHazard, str] = {
    "absolute": "it is an absolute path, and diff names are repository-relative",
    "traversal": "it traverses outside the repository with '..'",
    "root": "it reduces to the repository root, which matches no path prefix",
    "whitespace": "it has leading or trailing whitespace and scope matching is exact",
}


def _authored_scope_errors(
    comp_id: str,
    allowed: list[str],
    harness: list[str],
) -> list[str]:
    """allowedPaths entries that authorise nothing.

    The backstop for HAND-WRITTEN manifests, which never went through
    ``decompose._validate_allowed_path_entry``. An entry that cannot
    match is worse than a missing one: it reads as authorisation and
    grants none, so every file the operator meant to authorise is
    reported outside scope - the same unwinnable retry loop #264
    describes, from the other end, and just as expensive because it is
    discovered only after a full engineer attempt.

    """
    errors: list[str] = []
    for entry in allowed:
        hazard = scope_entry_hazard(entry)
        if hazard is None:
            continue
        errors.append(
            f"component '{comp_id}': allowedPaths entry '{entry}' can never "
            f"match a changed file because {_SCOPE_HAZARD_REASONS[hazard]}, so "
            "every file it was meant to authorise will be reported outside "
            "scope. Use a repository-relative prefix ('src/') or an exact "
            "repository-relative file path. Allowed paths (complete list): "
            f"{', '.join(allowed)}; plus harness artifacts: {', '.join(harness)}."
        )
    return errors


def _preflight_component_scope(
    manifest: Manifest,
    run_scope: RunScope,
) -> list[str]:
    """Refuse a component whose scope cannot work, before any spend.

    Pure path comparison against the plan-time snapshot: no git, no
    agent, no LLM, and (#269) no second reading of a PRD - the list
    judged here is the identical object both guards will enforce, so a
    scope this accepts cannot be a different scope by the time it is
    used. The measured failure this backstops (#264) cost $14.49 and 41
    minutes across three engineer attempts that each produced the
    identical ``diff_scope`` rejection. One line of output in the first
    second is the right price for that.

    Two checks the issue asked for are deliberately NOT here, both
    because they would assert something that cannot happen:

    - "does ``allowedPaths`` cover the component's prdPath and progress
      log?" is answered structurally.
      ``KstrlConfig.component_harness_files`` carves those files out at
      both guards, so the required set and the carve-out are one list
      and the assertion is a tautology.
    - "is any HARNESS path unmatchable?" (an absolute or escaping
      ``[paths] progress`` / ``codebase_map`` / ``prdPath``) was refused
      here until the #268 review pointed out that
      ``config.reconcile_progress_config`` documents exactly that
      configuration as SUPPORTED and self-consistent: joining an
      absolute path onto a worktree is a no-op, so writer and reader
      both land on that one file in the main checkout. Such a file is
      outside every worktree, so it never appears in a component's
      ``git diff`` and can never BE a scope violation. There was nothing
      to guard, and refusing the run contradicted a documented feature.

    Two things are refused, and both are unwinnable rather than merely
    wrong:

    - An ``unresolved`` snapshot: the pre-run PRD would not read and no
      run-wide flag stood in for it (#293 review). This is the one
      verdict the engineer cannot change. ``check_scope_unreadable``
      fails closed on ``scope.error``, and the snapshot is FIXED for the life
      of the run, so every retry re-runs an identical attempt into an
      identical failure - the measured cost above, in the one branch
      the plan-time snapshot would otherwise have kept. It is reported
      per component, not deduplicated, because each names its own file.
    - An authored ``allowedPaths`` entry that cannot match a changed
      file at all. Each distinct SCOPE is examined once, not each
      component: the snapshot's fallback is the run-wide
      ``--allowed-paths`` flag, so one bad entry there is shared by
      every component and N copies of the same paragraph help nobody.
      Two components with genuinely different authored lists are both
      reported, each naming its own complete list.
    """
    errors: list[str] = []
    seen: set[tuple[str, ...]] = set()
    for comp in manifest.components:
        if comp.status != ComponentStatus.PENDING.value:
            continue
        scope = run_scope.for_component(comp.id)
        if not scope.is_trustworthy:
            errors.append(
                f"component '{comp.id}': {scope.error}. Phase 1 fails "
                "closed on a scope it could not establish, and the "
                "snapshot is fixed for the run, so every attempt would "
                "fail identically. Restore that file in the main "
                "checkout. A run-wide --allowed-paths cannot stand in "
                "for it: a scope that could not be READ is not a scope "
                "that does not exist."
            )
            continue
        if scope.allowed_paths is None or tuple(scope.allowed_paths) in seen:
            continue
        seen.add(tuple(scope.allowed_paths))
        errors.extend(_authored_scope_errors(comp.id, scope.allowed_paths, scope.harness_paths))
    return errors


def _record_run_scope(
    run_scope: RunScope,
    bus: EventBus,
    ui: UI,
    *,
    manifest: Manifest,
) -> None:
    """Write down what each component may change, and why (#269).

    A scope resolved once has to be RECORDED once, or the only way to
    answer "why was this component allowed to write that?" after the run
    is to re-derive it from files the run has since changed - which is
    the thing the snapshot exists to stop anyone doing. One
    ``component_scope_resolved`` event per component carries the
    authored list, kstrl's carve-out, and which authority supplied them.

    The terminal gets a summary rather than N paragraphs, because the
    per-component detail is already in the journal and an operator
    watching a 12-component run does not need 12 identical lines.

    A component whose scope could not be established is deliberately
    NOT warned about here (#293 review). This function is handed every
    component in the manifest, including ones this run will never
    schedule, so a warning saying "Phase 1 will fail it closed" was
    false for a completed or failed component whose PRD has since been
    archived - alarming an operator about work that is not going to
    happen. The PENDING ones are refused outright by
    ``_preflight_component_scope`` moments later, which is louder than
    a warning and stops the run; the event above keeps the record for
    every component either way.
    """
    resolved = run_scope.by_component
    if not resolved:
        return
    # #448: the run's only record of a component it will not schedule.
    # Indexed, not .get(): the snapshot is resolved from this manifest,
    # so a missing id is a broken invariant and must raise.
    statuses = {comp.id: comp.status for comp in manifest.components}
    for comp_id, scope in resolved.items():
        bus.emit(
            ComponentScopeResolved(
                component=comp_id,
                scope_source=scope.source,
                origin=scope.origin,
                allowed_paths=tuple(scope.allowed_paths or ()),
                harness_paths=tuple(scope.harness_paths),
                error=scope.error or "",
                manifest_status=statuses[comp_id],
            )
        )
    counts = Counter(scope.source for scope in resolved.values())
    ui.info(
        f"  Scope resolved for {len(resolved)} component(s): "
        + ", ".join(f"{count} from {source}" for source, count in sorted(counts.items()))
    )


def report_preflight(ui: UI, headline: str, errors: list[str]) -> bool:
    """Print a refusal and say whether one happened."""
    if not errors:
        return False
    ui.err(f"Refusing to run: {headline}")
    for line in errors:
        ui.err(f"  {line}")
    return True


def _preflight_decision_register(
    manifest: Manifest,
    root_dir: Path,
) -> tuple[list[str], DecisionRegister]:
    """The architect's register bound to this run, or the reason it cannot bind (#260).

    Read once per run because it is a run-wide artifact the decompose
    wrote, and BOUND to this manifest before anything is scheduled.

    Round 2: the path is fixed, so without the bind a register left by
    another project sits exactly where this run looks. Reproduced: a
    factory run on project-b/b.md with project A's register beside it
    handed the engineer project A's binding instruction, because both
    happened to have a component called comp-a. ``bind_register``
    refuses that, a halted register, and an unreadable one. A MISSING
    register is legal and quiet: it is the normal state for every
    project that predates this, and a write that FAILED now fails the
    decompose outright rather than leaving one behind. A manifest with
    no ``spec_file`` is legal and quiet too: it did not come from a
    decompose, so nothing binds it.

    Round 3: this is a pre-spend refusal like the scope and branch
    checks, so it reports and exits like one. Letting the error out of
    ``run_factory`` gave the operator a traceback and exit 1, where
    every sibling refusal gives a sentence and exit 2.
    """
    # #639: the requirements bind exactly when the decisions do, because
    # bind_register returns the whole register or an empty one. A manifest
    # with no spec_file binds nothing, so it gets no requirements either.
    try:
        return [], bind_register(
            read_decisions(root_dir),
            manifest.project_name,
            manifest.spec_file,
            manifest.spec_digest,
        )
    except DecisionRegisterError as exc:
        return [str(exc)], DecisionRegister()


#: Why ``base-gates.json`` holds no reading under ``--no-verify`` (#654).
BASE_GATES_SKIPPED_NO_VERIFY = "--no-verify: Phase 1 runs no gate"


def _preflight_base_gates(
    base_branch: str,
    root_dir: Path,
    factory_config: FactoryConfig,
    run_id: str,
    ui: UI,
) -> list[str]:
    """Why the base branch must not be built on, or [] (#654).

    Measures Phase 1's gates on the base commit and records the reading in
    the run directory. Refuses when a gate measurably fails there or the
    record cannot be written, and warns about what it could not measure.
    Under ``--no-verify`` Phase 1 runs no gate, so nothing is measured and
    the record says why.
    """
    accept = factory_config.accept_red_base
    verify_config = factory_config.engineer_verify_config()
    if verify_config is None:
        skipped = BaseGates(base_branch)
        reasons, _ = apply_acceptance(skipped, [], accept)
        return (
            write_base_gates_record(
                root_dir,
                run_id,
                skipped,
                reasons,
                skipped_reason=BASE_GATES_SKIPPED_NO_VERIFY,
                accept=accept,
            )
            + reasons
        )
    ui.info(f"  Measuring the gates on the base branch {base_branch}...")
    reading = measure_base_gates(
        root_dir,
        base_branch,
        verify_config,
        factory_config.worktree_setup(),
        ui,
        measured=factory_config.base_reading,
    )
    factory_config.base_reading = reading
    for line in warning_lines(reading):
        ui.warn(f"  {line}")
    reasons, accepted = apply_acceptance(reading, refusal_lines(reading), accept)
    for line in accepted:
        ui.warn(f"  Accepted by --accept-red-base {accept}: {line}")
    return (
        write_base_gates_record(
            root_dir, run_id, reading, reasons, accept=accept, accepted=tuple(accepted)
        )
        + reasons
        + _sandbox_lines(reading, factory_config, reasons)
        + _accept_lines(reading, reasons)
    )


def _accept_lines(reading: BaseGates, reasons: list[str]) -> list[str]:
    """How to build on a refused base that ``--accept-red-base`` would let run,
    or []. A new repository's checks fail on its base because no code exists
    yet. Printed only, like :func:`_sandbox_lines`."""
    if not reasons or apply_acceptance(reading, reasons, reading.base_sha)[0]:
        return []
    return [
        "A repository whose code does not exist yet fails its checks on the base: "
        f"ks factory --accept-red-base {reading.base_sha[:12]} builds on it for this run."
    ]


def _sandbox_lines(
    reading: BaseGates, factory_config: FactoryConfig, reasons: list[str]
) -> list[str]:
    """The sandbox hint for a refused base whose setup or a check failed
    inside a proven rung, or [] (#700, the #625 trial). Printed only: the
    record and the reasons above it do not carry it."""
    if not reasons:
        return []
    if reading.setup_error:
        hint = sandbox_hint(factory_config.setup_rung)
    elif reading.result is not None and not all(check.passed for check in reading.result.checks):
        hint = sandbox_hint(factory_config.test_rung)
    else:
        hint = ""
    return [hint] if hint else []


def _preflight_rungs(
    root_dir: Path, factory_config: FactoryConfig, run_id: str, ui: UI
) -> list[str]:
    """Why this run's commands have no proven rung to run in, or [] (#700).

    Under a ``[stack]`` both zones are proven once per run, before the base
    gates, in the policy every command then runs in: the trees kstrl makes
    worktrees in (the checkout itself without worktrees), plus the stack's
    ``writable`` and ``readable``. Below a proven rung the run refuses;
    there is no opt-in to run a stack's commands on the host (decision
    5(a)). On a platform with no prover both "rungs" are the host
    fallback, which never refuses and whose label says nothing was
    isolated (owner decision 2026-10-05). The record is written either
    way. Without a stack nothing is proven and every command runs on the
    host, as before.
    """
    stack = factory_config.project_stack
    if stack is None:
        return []
    trees = (
        [root_dir / ".kstrl" / "contract", root_dir / ".kstrl" / "worktrees"]
        if factory_config.use_worktrees
        else [root_dir]
    )
    rungs = prove_zones(
        root_dir,
        [*trees, *stack_paths(root_dir, stack.writable)],
        stack_paths(root_dir, stack.readable),
        stack.browser,
    )
    errors = write_isolation_record(root_dir, run_id, stack.digest, rungs)
    errors += [
        f"the {zone} zone is {refusal_of(rung)}" for zone, rung in rungs.items() if refusal_of(rung)
    ]
    if errors:
        release(rungs.values())
        return errors
    factory_config.setup_rung, factory_config.test_rung = rungs[SETUP_ZONE], rungs[TEST_ZONE]
    for label in dict.fromkeys(rung.label for rung in rungs.values()):
        ui.info(f"  Isolation: {label}")
    return []


def _refused_rung_or_base(
    base_branch: str, root_dir: Path, factory_config: FactoryConfig, run_id: str, ui: UI
) -> bool:
    """Prove the rung, then measure the base gates inside it (#700 slice 2,
    #654); True when either refused. The base gates never run below a
    proven rung under a [stack]."""
    return report_preflight(
        ui,
        "the isolation rung is not proven",
        _preflight_rungs(root_dir, factory_config, run_id, ui),
    ) or report_preflight(
        ui,
        "the base branch fails a gate Phase 1 runs, or its reading cannot be recorded",
        _preflight_base_gates(base_branch, root_dir, factory_config, run_id, ui),
    )


def base_refused_before_architect(
    base_branch: str,
    root_dir: Path,
    factory_config: FactoryConfig,
    verify_config: VerifyConfig,
    accept_red_base: str,
    run_id: str,
    ui: UI,
) -> bool:
    """Measure the base before `ks factory --spec` pays the architect (#696 slice 7).

    The reading the run takes (``_refused_rung_or_base``): the rung is proven,
    the base gates run inside it, and the record goes to ``run_id``'s
    directory, the architect's run, where `ks serve` reads a red base. True
    when it refused. The rungs are proven on a copy of the config and
    released here, so the run proves its own; the reading is kept on
    ``factory_config.base_reading`` for the run to reuse.
    """
    probe = replace(factory_config, verify_config=verify_config, accept_red_base=accept_red_base)
    try:
        refused = _refused_rung_or_base(base_branch, root_dir, probe, run_id, ui)
    finally:
        release((probe.setup_rung, probe.test_rung))
    factory_config.base_reading = probe.base_reading
    return refused


class BaseRefusedError(Exception):
    """The base measured before the architect was paid refused the run (#696 slice 7)."""

    def artifact_lines(self) -> list[str]:
        """Nothing more to point at: the refusal was reported as it was measured."""
        return []


def base_check_before_architect(
    root_dir: Path,
    base_branch: str,
    factory_config: FactoryConfig,
    verify_config: VerifyConfig,
    run_id: str,
    ui: UI,
) -> Callable[[], None]:
    """The ``before_spend`` for a command that pays the architect without a run (#696 slice 7).

    Under a confirmed ``[stack]`` the returned callable measures the base as
    `ks factory --spec` does and raises :class:`BaseRefusedError` when it
    refuses. With no ``[stack]``, or one no person confirmed, there is no
    check kstrl may run, so it says so and measures nothing.
    """
    try:
        confirmed = confirmed_stack(root_dir)
    except StackRefused:
        confirmed = None
    if confirmed is None:
        return functools.partial(
            ui.info,
            "kstrl.toml has no confirmed [stack], so there is no base check to run "
            "before the architect",
        )

    def measure() -> None:
        if base_refused_before_architect(
            base_branch, root_dir, factory_config, verify_config, "", run_id, ui
        ):
            raise BaseRefusedError("the base branch was refused before the architect was paid")

    return measure


def _preflight_stack(
    manifest: Manifest, root_dir: Path, factory_config: FactoryConfig, run_id: str
) -> list[str]:
    """Why this run must not use the ``[stack]`` in force, or [] (#696 slice 3).

    No stack refuses unless ``--no-verify`` runs no check (#696 slice 4). A
    stack no person confirmed refuses, and its one inbox item is filed. A
    confirmed stack still refuses a plan made under another one.
    """
    stack = factory_config.project_stack
    if stack is None and not factory_config.skip_verification:
        return [NO_STACK]
    if stack is not None and stack.unconfirmed:
        return unconfirmed_lines(root_dir, stack, run_id=run_id)
    return stack_pin_errors(manifest, stack)


def _preflight_pins(
    manifest: Manifest, root_dir: Path, factory_config: FactoryConfig, run_id: str
) -> tuple[str, list[str]]:
    """The spec pin (#639), then the stack (#696), then the acceptance plan
    (#700 slice 4): the headline and reasons of the first that refuses, or
    an empty list. One call, so ``_run_preflights`` gains no branch (its
    cyclomatic ratchet is at 10)."""
    spec_errors = spec_pin_errors(manifest, root_dir)
    if spec_errors:
        return "the plan does not match the spec it was made from", spec_errors
    stack_errors = _preflight_stack(manifest, root_dir, factory_config, run_id)
    if stack_errors == [NO_STACK]:
        return "kstrl.toml has no [stack]", stack_errors
    if stack_errors:
        return (
            "the [stack] in kstrl.toml is not confirmed, or is not the one this plan was made "
            "under",
            stack_errors,
        )
    source, acceptance_errors = acceptance_source(root_dir, manifest, factory_config)
    if acceptance_errors:
        return "the acceptance plan cannot be used", acceptance_errors
    factory_config.acceptance_plan, acceptance_errors = pin_plan(
        root_dir, manifest, source, factory_config.project_stack
    )
    return "the acceptance plan cannot be used", acceptance_errors


def _emit_stack_confirmation(bus: EventBus, stack: Stack | None) -> None:
    """Record how this run's ``[stack]`` was confirmed (#696 slice 3).

    ``decided_by`` is "inbox" for an APPROVED item and "operator" for an
    answer at the prompt with ``[inbox]`` disabled, which nothing else
    records: it held for this run only.
    """
    if stack is not None and not stack.unconfirmed:
        bus.emit(
            CheckpointResolved(kind=STACK_KIND, decision="approved", decided_by=stack.confirmed_by)
        )


def _run_preflights(
    manifest: Manifest,
    run_scope: RunScope,
    root_dir: Path,
    factory_config: FactoryConfig,
    run_id: str,
    ui: UI,
    *,
    lock_held: bool,
    manifest_path: Path,
    interrupted_branches: Mapping[str, str],
    timeout_cfg: TimeoutConfig,
) -> DecisionRegister | None:
    """Every pre-spend refusal, cheapest first, and what survives them.

    Returns the decision register this run bound, or ``None`` to
    refuse. It returns a value rather than a bool because the register
    check is one of the refusals and its result is the thing the
    engineers and the integration review need; an empty register is a
    normal, proceeding answer, so the caller checks ``is None``.

    The register goes first (#260 round 3): it is one small file read,
    no git and no agent, so it is cheaper than the scope comparison and
    far cheaper than the branch walk.

    Scope goes next (#264): it is a pure path comparison with no git
    and no agent behind it, and a component that cannot pass diff_scope
    must not reach the engineer - the measured cost of finding out later
    was $14.49 across three identical attempts. It also runs without
    worktrees, unlike the R0.5 branch policy below, which only applies
    to worktree mode: without worktrees the factory neither creates
    branches nor worktree dirs.

    The base gates go after scope (#654): they run the project's own
    suite, the dearest check here, in a throwaway worktree of the base
    commit, so they also run without worktrees, where Phase 1 still runs.
    """
    # #436 first: `ks retry` reads this record to replay the run's flags,
    # so a run that cannot leave one must not start (CLAUDE.md, artifact
    # identity: a failed write of an artifact a later phase reads is the
    # writing phase's failure).
    if report_preflight(
        ui,
        "the run's launch record cannot be written",
        write_launch_record(
            root_dir,
            run_id,
            manifest_path,
            factory_config.launch_flags,
            run_limits(factory_config, timeout_cfg),
        ),
    ):
        return None
    register_errors, run_register = _preflight_decision_register(manifest, root_dir)
    if report_preflight(ui, "the architect decision register cannot bind", register_errors):
        return None
    factory_config.requirements = run_register.requirements
    headline, pin_errors = _preflight_pins(manifest, root_dir, factory_config, run_id)
    if report_preflight(ui, headline, pin_errors):
        return None
    if report_preflight(
        ui,
        "components cannot pass the scope check",
        _preflight_component_scope(manifest, run_scope),
    ):
        return None
    if _refused_rung_or_base(manifest.base_branch, root_dir, factory_config, run_id, ui):
        return None
    if not factory_config.use_worktrees:
        return run_register
    if lock_held:
        flat_worktrees = _prune_stale_worktrees(
            root_dir,
            run_id,
            ui,
            keep=_evidence_worktrees_to_keep(manifest),
        )
        if report_preflight(ui, "flat worktrees from before R0.5 found", flat_worktrees):
            return None
    else:
        ui.warn(
            "  Run lock not held; skipping stale-worktree cleanup "
            "(another live invocation may own them)"
        )
    if report_preflight(
        ui,
        "stale component branches found",
        _preflight_component_branches(manifest, root_dir, ui, interrupted_branches),
    ):
        return None
    return run_register
