"""ks factory: build a manifest, or a spec through the architect, component by
component, under the run lock that a re-entering command can hand to it.
"""

from __future__ import annotations

import functools
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import click

from kstrl.agents import get_agent
from kstrl.architect import OwnerAnswerError, SpecBlockerError, decompose_spec
from kstrl.cli_factory_options import factory_options
from kstrl.cli_preflight import (
    _agent_preflight,
    _agent_settings,
    _check_agent_preflight,
    _check_prompt_preflight,
    _collect_evolution_notes,
    _collect_toml_notes,
    _measure_base_before_architect,
    _resolve_factory_run_lock,
    _stack_checkpoint,
)
from kstrl.cli_report import _print_execution_order
from kstrl.cli_seam import _console_ui, _use_cli_value, cli
from kstrl.commandrun import open_command_run
from kstrl.config import KstrlConfig, reconcile_progress_config
from kstrl.config_report import normalize_ui_mode as _normalize_ui_mode
from kstrl.events import (
    ARCHITECT_COMPONENT,
    ARCHITECT_ROLE,
    UsageTotals,
    collect_usage,
    usage_cursor,
)
from kstrl.factory import run_factory
from kstrl.factory_config import FactoryConfig
from kstrl.git import resolve_base_branch
from kstrl.launch_record import replayable_flags
from kstrl.owner import PromptKind, PromptRequest, UiInteractionChannel
from kstrl.plan import Manifest
from kstrl.proc import (
    RunLock,
    StopController,
    TimeoutConfig,
    install_signal_handlers,
    limit_seconds,
)
from kstrl.serve_spend import ARCHITECT_RUN_KIND

if TYPE_CHECKING:
    # Annotation only, and the cheapest available: with
    # `from __future__ import annotations` above, _check_document's
    # signature is a string at run time and this block never executes.
    # Not a deferral - line 104 already imports kstrl.verify.verify at module
    # scope, so it is on `ks --help`'s import path either way (measured
    # here at 5.2ms of cli.py's 84.7ms cumulative).
    from kstrl.verify import VerifyConfig


@cli.command()
@factory_options
def factory(
    spec: Path | None,
    manifest_path: Path | None,
    root: Path | None,
    project_name: str | None,
    base_branch: str | None,
    single_pr: bool,
    max_parallel: int | None,
    max_retries: int | None,
    create_prs: bool | None,
    no_verify: bool,
    accept_red_base: str,
    acceptance: str,
    design_acceptance: bool,
    bug_report: bool,
    review_mode: str | None,
    review_agent_cmd: str | None,
    review_model: str | None,
    security_mode: str | None,
    security_agent_cmd: str | None,
    security_model: str | None,
    security_fail_threshold: str | None,
    contract_check: str | None,
    agent_timeout: float | None,
    component_timeout: float | None,
    max_adversarial_calls: int | None,
    max_total_tokens: int | None,
    max_cost_usd: float | None,
    pause_before_pr_merge: bool | None,
    progress_log: Path | None,
    no_worktrees: bool,
    keep_worktrees_on_failure: bool,
    force_lock: bool,
    yes: bool,
    agent_cmd: str | None,
    model: str | None,
    reasoning: str | None,
    agent_type: str,
    sleep: float,
    ui: str,
    no_color: bool,
) -> None:
    """Run the software factory - decompose and execute a spec.

    Provide either --spec (to decompose first) or --manifest (to resume).
    """
    ctx = click.get_current_context()

    if not spec and not manifest_path:
        ui_impl = _console_ui("auto", no_color)
        ui_impl.err("Either --spec or --manifest is required")
        sys.exit(2)

    root_dir = root.resolve() if root else Path.cwd()

    force_rich = os.environ.get("KSTRL_UI_FORCE_RICH") == "1"
    ui_impl = _console_ui(_normalize_ui_mode(ui), no_color, force_rich=force_rich)

    effective_cmd, effective_model, effective_reasoning, effective_type = _agent_settings(
        ctx, root_dir, agent_cmd, model, reasoning, agent_type
    )

    # R2.4 mirror (measured 2026-07-20): canonicalize aliases like
    # "claude" before get_agent, whose unrecognized-type fallthrough is
    # codex; the preflight also covers the no-agent-available check.
    canonical_type, type_error, type_hint = _agent_preflight(
        effective_cmd,
        effective_type,
    )
    if type_error:
        ui_impl.err(type_error)
        if type_hint:
            ui_impl.info(type_hint)
        sys.exit(2)
    effective_type = canonical_type or effective_type

    agent = get_agent(
        effective_cmd, effective_model, effective_reasoning, effective_type, root_dir=root_dir
    )

    # R8 review (#180): the budget ceilings are checked before the
    # decompose call below, so the architect never spends under a ceiling
    # that bounds nothing. The flags are checked by their click type
    # (config_numbers.LimitNumber, #571) before this body runs, and the
    # file and env values inside load. The flags are NOT applied here:
    # applying them early would change what _collect_toml_notes reports
    # as overridden further down.
    # #696 slice 3: before the configs that hold the stack are loaded, so an
    # answer given here is the one they read, and before any spend.
    _stack_checkpoint(root_dir, ui_impl, no_verify=no_verify)
    factory_config = FactoryConfig.load(root_dir)

    # Get or create manifest.
    #
    # #257: a --spec run also pays for the architect here, before the
    # factory run's id or directory exists, so what it cost has to be
    # carried into the run by hand for `--max-cost-usd` to bound five
    # roles instead of four. A --manifest resume ran no architect and
    # leaves this empty.
    #
    # Only the paths that reach run_factory carry it: a blocker halt exits
    # below, and its spend is recorded only in the decompose run the
    # architect ran as (#567). `ks serve` charges that run when no factory
    # run of the launch names it (#587). The factory run names it with
    # ``architect_run_id``, so the spend is counted once either way.
    # #597: `ks factory` takes the run lock before its first change, above
    # the manifest load and before the architect spends anything on
    # --spec, and holds it across the confirmation below (cli.py:3132,
    # lane #594's own lines). See ``_resolve_factory_run_lock`` for the
    # handed-vs-acquired resolution.
    run_lock: RunLock = _resolve_factory_run_lock(ctx, root_dir, ui_impl, force_lock)

    try:
        architect_usage = UsageTotals()
        architect_run_id = ""
        if manifest_path:
            try:
                manifest = Manifest.load(manifest_path)
            except Exception as exc:
                ui_impl.err(f"Failed to load manifest: {exc}")
                sys.exit(2)
        else:
            assert spec is not None
            if not project_name:
                ui_impl.err("--project-name is required with --spec")
                sys.exit(2)

            # Read BEFORE the work, and sliced from there afterwards, the
            # same way `decompose_spec` reports it - so the two derivations
            # of "what the architect spent" cannot disagree, and neither
            # rests on an invariant about who else touched this agent.
            usage_before = usage_cursor(agent)
            # #567: the architect runs as a decompose run of its own, the way
            # `ks decompose` runs it, so
            # its prompt records, events and transcript have a run to live
            # in. Not the factory's run: that directory would exist for the
            # whole architect call with no event stream, and a factory-kind
            # run with no stream is what safe mode reports as "could not
            # read run" (measured on a failed decompose).
            architect_run = open_command_run(
                ui_impl,
                root_dir,
                ARCHITECT_RUN_KIND,
                component=ARCHITECT_COMPONENT,
            )
            architect_run_id = architect_run.run_id
            spec_base = resolve_base_branch(base_branch, root_dir)
            try:
                manifest = decompose_spec(
                    spec_path=spec,
                    project_name=project_name,
                    # The --manifest path above is untouched: a manifest
                    # already carries its own base_branch. Only the --spec
                    # path has a flag to resolve, and detection is the
                    # default rather than the literal "main" (#259).
                    base_branch=spec_base,
                    single_pr=single_pr,
                    agent=agent,
                    ui=ui_impl,
                    root_dir=root_dir,
                    bus=architect_run.bus,
                    transcript=architect_run.transcript_writer(ARCHITECT_COMPONENT),
                    prompt_call=architect_run.agent_call(ARCHITECT_COMPONENT, ARCHITECT_ROLE),
                    force_lock=force_lock,
                    run_lock=run_lock,
                    timeout=limit_seconds(factory_config.architect_timeout_seconds),
                    before_spend=functools.partial(
                        _measure_base_before_architect,
                        root_dir,
                        spec_base,
                        factory_config,
                        accept_red_base,
                        no_verify,
                        architect_run_id,
                        ui_impl,
                    ),
                )
            except (SpecBlockerError, OwnerAnswerError) as exc:
                # Architect halted: it escalated a question only the owner
                # can answer (#260), or the owner's inbox answers cannot be
                # read (#639). Surface it and exit cleanly. The user
                # answers, edits the spec and re-runs, iterating against the
                # persisted artifacts (R1.7).
                ui_impl.err(str(exc))
                for line in exc.artifact_lines():
                    ui_impl.info(line)
                sys.exit(2)
            except ValueError as exc:
                ui_impl.err(str(exc))
                sys.exit(1)
            finally:
                architect_run.close()
            architect_usage = collect_usage(agent, since=usage_before)

        # Build configs (R2.1). Resolution order for every phase config:
        # explicit CLI flag > env > kstrl.toml > dataclass default. The
        # loaders handle env-over-toml-over-default; flags use None
        # sentinels so "not passed" is distinguishable from "passed the
        # default value", and an explicitly-passed flag is applied on top.
        from kstrl.acceptance import ContractConfig
        from kstrl.project import CodebaseScanConfig
        from kstrl.review import SecurityConfig
        from kstrl.verify import VerifyConfig

        toml_notes: list[str] = []

        # factory_config was loaded in the budget preflight above.
        _collect_toml_notes(
            toml_notes,
            "factory",
            factory_config,
            FactoryConfig.from_env(),
            flag_overridden={
                name
                for name, passed in (
                    ("max_parallel", max_parallel is not None),
                    ("max_retries", max_retries is not None),
                    ("create_prs", create_prs is not None),
                    ("review_mode", review_mode is not None),
                    ("max_adversarial_calls", max_adversarial_calls is not None),
                    ("max_total_tokens", max_total_tokens is not None),
                    ("max_cost_usd", max_cost_usd is not None),
                    ("pause_before_pr_merge", pause_before_pr_merge is not None),
                    ("use_worktrees", no_worktrees),
                    ("keep_worktrees_on_failure", keep_worktrees_on_failure),
                    # The manifest is authoritative for single_pr, so a toml
                    # value never becomes effective in this command.
                    ("single_pr", True),
                )
                if passed
            },
        )
        if max_parallel is not None:
            factory_config.max_parallel = max_parallel
        if max_retries is not None:
            factory_config.max_retries = max_retries
        if create_prs is not None:
            factory_config.create_prs = create_prs
        if review_mode is not None:
            factory_config.review_mode = review_mode
        if max_adversarial_calls is not None:
            factory_config.max_adversarial_calls = max_adversarial_calls
        if max_total_tokens is not None:
            factory_config.max_total_tokens = max_total_tokens
        if max_cost_usd is not None:
            factory_config.max_cost_usd = max_cost_usd
        if pause_before_pr_merge is not None:
            factory_config.pause_before_pr_merge = pause_before_pr_merge
            # #195: passing the flag either way is an explicit operator
            # request, so the ladder may not lower it. This is also what
            # makes `ks serve` honour a stop_at_pr item at L3: its child
            # always gets one of --pause-before-pr-merge /
            # --no-pause-before-pr-merge (serve.subprocess_factory_runner).
            factory_config.explicit_fields |= {"pause_before_pr_merge"}
        if no_worktrees:
            factory_config.use_worktrees = False
        if keep_worktrees_on_failure:
            factory_config.keep_worktrees_on_failure = True
        factory_config.single_pr = manifest.single_pr
        factory_config.review_agent_cmd = review_agent_cmd
        factory_config.review_model = review_model
        factory_config.progress_log_path = progress_log
        if progress_log is not None:
            # An explicit --progress-log path is an explicit opt-in; it wins
            # over a toml/env progress_log_enabled = false.
            factory_config.progress_log_enabled = True
        factory_config.force_lock = force_lock
        factory_config.accept_red_base = accept_red_base
        factory_config.acceptance_dir = acceptance
        factory_config.design_acceptance = design_acceptance
        factory_config.bug_report = bug_report
        # #436: what `ks retry` replays; see kstrl/launch_record.py.
        factory_config.launch_flags = replayable_flags(ctx)
        # R2.3: --no-verify is an explicit skip sentinel that run_factory
        # honors; verify_config=None alone would substitute default checks.
        factory_config.skip_verification = no_verify

        v_config: VerifyConfig | None = None
        if not no_verify:
            v_config = VerifyConfig.load(root_dir)
            _collect_toml_notes(
                toml_notes,
                "verify",
                v_config,
                VerifyConfig.from_env(),
                flag_overridden=set(),
            )

        s_config = SecurityConfig.load(root_dir)
        _collect_toml_notes(
            toml_notes,
            "security",
            s_config,
            SecurityConfig.from_env(),
            flag_overridden={
                name
                for name, passed in (
                    ("mode", security_mode is not None),
                    ("agent_cmd", security_agent_cmd is not None),
                    ("model", security_model is not None),
                    ("fail_threshold", security_fail_threshold is not None),
                )
                if passed
            },
        )
        if security_mode is not None:
            s_config.mode = security_mode
        if security_agent_cmd is not None:
            s_config.agent_cmd = security_agent_cmd
        if security_model is not None:
            s_config.model = security_model
        if security_fail_threshold is not None:
            s_config.fail_threshold = security_fail_threshold

        contract_resolved = ContractConfig.load(root_dir)
        _collect_toml_notes(
            toml_notes,
            "contract",
            contract_resolved,
            ContractConfig.from_env(),
            flag_overridden={
                name for name, passed in (("mode", contract_check is not None),) if passed
            },
        )
        if contract_check is not None:
            contract_resolved.mode = contract_check
        # mode == "skip" keeps the historical contract of passing no config.
        c_config: ContractConfig | None = (
            contract_resolved if contract_resolved.mode != "skip" else None
        )

        ff_config = CodebaseScanConfig.load(root_dir)
        _collect_toml_notes(
            toml_notes,
            "codebase_scan",
            ff_config,
            CodebaseScanConfig.from_env(),
            flag_overridden=set(),
        )

        # Evolution config is consumed inside run_factory via
        # EvolutionJournal.open; swept here only for the NOTE lines, and by
        # a helper because a failed load must not cost the run. The helper
        # says why this section and not its five raising siblings.
        _collect_evolution_notes(toml_notes, root_dir, ui_impl)

        # R0.1: TimeoutConfig is the single source for timeout values.
        timeout_config = TimeoutConfig.load(root_dir)
        _collect_toml_notes(
            toml_notes,
            "timeout",
            timeout_config,
            TimeoutConfig.from_env(),
            flag_overridden={
                name
                for name, passed in (
                    ("agent_iteration", agent_timeout is not None),
                    ("component_total", component_timeout is not None),
                )
                if passed
            },
        )
        if agent_timeout is not None:
            timeout_config.agent_iteration = agent_timeout
        if component_timeout is not None:
            timeout_config.component_total = component_timeout

        factory_config.verify_config = v_config
        factory_config.security_config = s_config
        factory_config.contract_config = c_config
        factory_config.codebase_scan_config = ff_config
        factory_config.timeout_config = timeout_config

        # Display summary and confirm (resolved values, not raw flags)
        ui_impl.section("Factory Plan")
        ui_impl.kv("Project", manifest.project_name)
        ui_impl.kv("Components", str(len(manifest.components)))
        ui_impl.kv("Base branch", manifest.base_branch)
        ui_impl.kv("Single PR", "yes" if manifest.single_pr else "no")
        ui_impl.kv("Max parallel", str(factory_config.max_parallel))
        ui_impl.kv("Create PRs", "yes" if factory_config.create_prs else "no")

        # R2.1 behavior change: kstrl.toml sections that used to be silently
        # ignored now take effect. Surface every value a toml section moved
        # away from the CLI default so existing setups see the change.
        for note in toml_notes:
            ui_impl.info(note)

        _print_execution_order(manifest, ui_impl)

        _factory_channel = UiInteractionChannel(ui_impl)
        if not yes and _factory_channel.can_prompt():
            response = _factory_channel.request(
                PromptRequest(
                    kind=PromptKind.CONFIRM,
                    header="Proceed with factory execution?",
                    options=("Start", "Quit"),
                    default=0,
                )
            )
            # #594: only an answered Start starts a paid run. An unanswered
            # confirm carries no choice (#647), so it quits exactly as Quit does.
            if response.choice != 0:
                ui_impl.info("Factory not started.")
                sys.exit(0)

        kstrl_dir = root_dir / "scripts" / "kstrl"
        base_config = KstrlConfig.load(root_dir)
        if _use_cli_value(ctx, "agent_cmd"):
            base_config.agent_cmd = agent_cmd
        if _use_cli_value(ctx, "model"):
            base_config.model = model
        if _use_cli_value(ctx, "reasoning"):
            base_config.model_reasoning_effort = reasoning
        if _use_cli_value(ctx, "agent_type"):
            base_config.agent_type = agent_type
        if _use_cli_value(ctx, "sleep"):
            base_config.sleep_seconds = sleep
        base_config.ui_mode = "plain"
        base_config.no_color = True

        # R2.4 mirror for the factory path (measured 2026-07-20 on the first
        # real factory run): without this, a toml alias like type = "claude"
        # reaches get_agent RAW in every engineer worker and silently falls
        # through to the codex default - and cli_family misreads the
        # engineer family, inverting the R7.1 reviewer rotation.
        _check_agent_preflight(base_config, ui_impl)

        # --no-verify leaves no reader, so there is nothing to reconcile.
        if v_config is not None:
            # R8 review: the progress log's writer ([paths] progress) and its
            # reader ([verify] progress_file_path) default to the same
            # derivation, so they agree until exactly ONE is set - and then the
            # self-critique check inspects a file the engineer never wrote and
            # fails for a reason the operator cannot see from either setting.
            # The reconciliation lives in config.py (and is applied in ONE path
            # domain, review finding 3) so a test can exercise the same wiring
            # this command runs.
            mismatch = reconcile_progress_config(base_config, v_config, root_dir)
            if mismatch is not None:
                ui_impl.warn(mismatch)

        # Ensure prompt file exists
        if not base_config.prompt_file.exists():
            default_prompt = kstrl_dir / "prompt.md"
            if default_prompt.exists():
                base_config.prompt_file = default_prompt

        # #286: after the fallback above, so it speaks about the file every
        # worker will actually read (#569: from the root checkout, never a
        # copy in its worktree).
        _check_prompt_preflight(base_config.prompt_file, ui_impl)

        # R0.5 (H-15): state saves back to the file it was loaded from.
        # --manifest /custom.json persists to /custom.json; --spec runs keep
        # the default scripts/kstrl/manifest.json that decompose wrote.
        stop = StopController()
        uninstall = install_signal_handlers(stop)
        try:
            result = run_factory(
                manifest,
                factory_config,
                base_config,
                ui_impl,
                root_dir,
                manifest_path=manifest_path,
                stop=stop,
                architect_usage=architect_usage,
                architect_run_id=architect_run_id,
                run_lock=run_lock,
            )
        finally:
            uninstall()
        sys.exit(result.exit_code)
    finally:
        run_lock.release()
