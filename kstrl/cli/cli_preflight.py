"""What a spending command resolves and refuses before its first paid call: the
agent it runs, the prompt and PRD it reads, the configured stack and base, and
the NOTE lines that say which kstrl.toml values took effect.
"""

from __future__ import annotations

import json
import sys
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click
from click.core import ParameterSource

from kstrl.agents import (
    CLAUDE_FAMILY,
    PROBE_ENV_VAR,
    ClaudeCodeAgent,
    ClaudeSdkAgent,
    CodexAgent,
    probe_family,
)
from kstrl.cli.cli_seam import _actor, _resolve_path, _use_cli_value
from kstrl.cli.config_report import normalize_ui_mode as _normalize_ui_mode
from kstrl.config import (
    AGENT_TYPE_ALIASES,
    VALID_AGENT_TYPES,
    KstrlConfig,
    canonical_agent_type,
    parse_paths,
)
from kstrl.factory import base_refused_before_architect, report_preflight
from kstrl.io import read_json_file
from kstrl.owner import PromptKind, PromptRequest, UiInteractionChannel
from kstrl.pipeline import FactoryConfig, cli_family
from kstrl.plan import PRD
from kstrl.proc import RunLock, acquire_run_lock
from kstrl.project import (
    NO_STACK,
    STACK_OPTIONS,
    Stack,
    StackRefused,
    confirmed_stack,
    decide_at_prompt,
    staleness_notice,
    unconfirmed_lines,
)
from kstrl.ui import UI

if TYPE_CHECKING:
    pass


#: The ``click`` meta key under which `ks retry` and `ks inbox approve` hand
#: `ks factory` the run lock they took before changing anything (#597).
#: ``meta`` is one dict shared by every context in the chain.
_HANDED_RUN_LOCK = "kstrl.handed_run_lock"


def _resolve_factory_run_lock(
    ctx: click.Context, root_dir: Path, ui_impl: UI, force_lock: bool
) -> RunLock:
    """The lock `ks factory` runs under: handed in by a caller, or newly acquired (#597).

    `ks retry` and `ks inbox approve` on a parked merge take the lock
    themselves and hand it on through ``ctx.meta``, so this never acquires
    a second time in the same process (flock refuses that even within one
    process); a handed lock whose own ``release()`` has already run is
    #597's own defect one level up, so that is refused rather than
    silently re-acquired. This checks ``released``, not ``fp is None``:
    a ``--force-lock`` lock and the no-fcntl degrade also have
    ``fp=None`` while still legitimate to hand on, so ``fp`` alone cannot
    tell a forced or degraded lock from a released one. Its own function
    so the boolean checks below cost nothing against `factory`'s own
    cognitive-complexity ratchet.
    """
    handed = ctx.meta.get(_HANDED_RUN_LOCK)
    if handed is not None and (not isinstance(handed, RunLock) or handed.released):
        raise RuntimeError(f"a released run lock was handed to `ks factory`: {handed!r}")
    return handed or acquire_run_lock(root_dir, ui_impl, force=force_lock)


def _agent_settings(
    ctx: click.Context,
    root_dir: Path,
    agent_cmd: str | None,
    model: str | None,
    reasoning: str | None,
    agent_type: str | None,
) -> tuple[str | None, str | None, str | None, str]:
    """The engineer agent's command, model, reasoning effort and type for
    ``decompose`` and ``factory``: a flag, else the environment, else
    ``[agent]`` in kstrl.toml.

    ``KstrlConfig.load`` already applies the environment over the file, so
    this reads the same values ``run_factory`` builds the engineer from.
    Reading the environment alone refused a project whose kstrl.toml sets
    ``[agent] command`` on a machine with neither claude nor codex, before
    the run that would have used that command started."""
    config = KstrlConfig.load(root_dir)
    return (
        agent_cmd or config.agent_cmd,
        model if _use_cli_value(ctx, "model") else config.model,
        reasoning if _use_cli_value(ctx, "reasoning") else config.model_reasoning_effort,
        (agent_type if _use_cli_value(ctx, "agent_type") else config.agent_type) or "auto",
    )


def _reject_blank_project_name(
    ctx: click.Context,
    param: click.Parameter,
    value: str | None,
) -> str | None:
    """Refuse an empty or whitespace-only project name at the boundary (#338).

    The project name is an identity: it keys the journal audits, the
    decision register and, under ``--single-pr``, the branch. "" was
    accepted by Click and is the one value at which the convergence
    accounting counted an audit with no project as BOTH this project's
    and unattributed, reporting five audits as eight. Refusing it here
    means no downstream reader has to carry a special case for it, and
    the refusal lands before the architect is invoked, measured at 119
    to 210 seconds against a frontier model on a real spec.

    Rejects or returns the value VERBATIM; it never strips. " x " is a
    strange name but it is the name the operator typed, and quietly
    substituting "x" would write a manifest, a branch and a journal
    audit under something else.

    On all three ``--project-name`` options, ``ks queue add``
    included, whose "" default is the sentinel ``serve`` derives
    ``queue-<id>`` from. Click runs a callback over an option's own
    default, so the refusal is gated on the parameter SOURCE rather
    than left off the option: a "" the operator never typed is
    returned untouched, an explicit "" or "   " is refused. A source
    this cannot resolve is refused too, because a check that cannot
    prove the value came from the default must flag, not clear.

    ``tests/test_project_name_boundary.py`` walks the Click tree and
    fails if a fourth ``--project-name`` option, or a fourth parameter
    bound to ``project_name``, appears without this callback, so the
    boundary is closed by census rather than by a list of the options
    someone remembered.
    """
    source = ctx.get_parameter_source(param.name) if param.name else None
    if value is not None and not value.strip() and source is not ParameterSource.DEFAULT:
        raise click.BadParameter("must not be empty or whitespace-only", ctx=ctx, param=param)
    return value


# Accepted spellings for the agent type across the config surface.
# kstrl.toml documents "claude" | "codex" | "custom"; the --agent-type
# flags and KSTRL_AGENT_TYPE historically use "claude-code" | "codex" |
# "auto". Both families resolve to get_agent's vocabulary here.
# One vocabulary, defined next to the adapters it selects (R8 review):
# the CLI and get_agent previously kept separate tables and disagreed
# about "claude".


class _AgentTypeChoice(click.Choice[str]):
    """``--agent-type``: the values ``[agent] type`` and ``KSTRL_AGENT_TYPE`` accept (#565).

    ``--help`` lists ``VALID_AGENT_TYPES``, ``claude`` and ``claude-code``
    both, because both are accepted and both are in use. The check is
    ``canonical_agent_type``, the one ``validate_agent_type`` and
    ``get_agent`` use, so the flag also takes the spellings they take:
    any case, surrounding space, and "" for auto. The value is passed on
    as typed, as the config doors pass theirs. ``launch_record`` checks a
    replayed value through this type as well.
    """

    def __init__(self) -> None:
        super().__init__(VALID_AGENT_TYPES, case_sensitive=False)

    def convert(self, value: Any, param: click.Parameter | None, ctx: click.Context | None) -> str:
        if isinstance(value, str) and canonical_agent_type(value) is not None:
            return value
        return super().convert(value, param, ctx)


def _agent_preflight(
    agent_cmd: str | None,
    agent_type: str | None,
) -> tuple[str | None, str | None, str | None]:
    """Check that the agent the resolved config selects is reachable.

    Mirrors the factory/decompose preflight (R2.4, H-12): the check must
    look for whichever agent the config actually selects, not
    hardcode codex. Returns ``(canonical_agent_type, error, hint)``:
    ``canonical_agent_type`` is the get_agent-vocabulary spelling to
    construct the agent with (so a toml ``type = "claude"`` selects
    Claude Code rather than falling through to the codex default), and
    ``error``/``hint`` are user-facing lines when the preflight fails.
    """
    if agent_cmd:
        # Custom command takes precedence in get_agent regardless of
        # type; there is nothing to look up in PATH.
        return agent_type, None, None

    normalized = (agent_type or "auto").strip().lower()
    canonical = AGENT_TYPE_ALIASES.get(normalized)

    if canonical is None:
        return (
            agent_type,
            f"Unknown agent type {agent_type!r} "
            "(expected: claude, claude-sdk, codex, custom, or auto)",
            "Fix [agent].type in kstrl.toml, KSTRL_AGENT_TYPE, or --agent-type",
        )
    if canonical == "custom":
        return (
            agent_type,
            'Agent type "custom" is configured but no agent command is set',
            "Set [agent].command in kstrl.toml, AGENT_CMD, or --agent-cmd",
        )
    if canonical == "claude-code":
        if not ClaudeCodeAgent.is_available():
            return (
                agent_type,
                "claude not found in PATH (config selects agent type 'claude')",
                "Install Claude Code, or use --agent-cmd / change [agent].type",
            )
        return "claude-code", None, None
    if canonical == "claude-sdk":
        if not ClaudeSdkAgent.is_available():
            return (
                agent_type,
                "claude-agent-sdk is not installed (config selects agent type 'claude-sdk')",
                "Install the sdk extra (uv sync --extra sdk), or change [agent].type",
            )
        return "claude-sdk", None, None
    if canonical == "codex":
        if not CodexAgent.is_available():
            return (
                agent_type,
                "codex not found in PATH (config selects agent type 'codex')",
                "Install codex, or use --agent-cmd / change [agent].type",
            )
        return "codex", None, None

    # auto: accept whichever agent is installed, like the factory does.
    if not ClaudeCodeAgent.is_available() and not CodexAgent.is_available():
        return (
            agent_type,
            "No agent available (codex and claude not found in PATH)",
            "Install an agent or use --agent-cmd to specify a custom one",
        )
    return "auto", None, None


def _probe_target_family(agent_cmd: str | None, canonical: str | None) -> str | None:
    """Which CLI family this config's turns would run on, or None.

    None means there is no CLI turn to probe. ``claude-sdk`` is the
    Claude FAMILY reached over the SDK transport rather than through the
    ``claude`` binary, so that carve-out is the only fact stated here;
    everything else defers to ``cli_family``, the mirror of
    ``get_agent``'s dispatch that the R7.1 rotation already depends on
    (a custom command returns None there, and the auto-detect preference
    order stays stated once).
    """
    if canonical == "claude-sdk":
        return None
    return cli_family(agent_cmd, canonical, ClaudeCodeAgent.is_available())


def _check_agent_preflight(config: KstrlConfig, ui_impl: UI) -> None:
    """Run the agent preflight against a resolved config; exit(2) on failure.

    On success, canonicalizes ``config.agent_type`` in place so every
    downstream ``get_agent`` call selects the same agent the preflight
    verified.

    A CLI on PATH that cannot complete a turn warns rather than exiting
    (#262). A probe wrong in the pessimistic direction - a network blip,
    a CLI startup quirk - would otherwise turn a working setup into a
    hard failure before any work happened, and a genuinely dead engineer
    CLI fails on its own first call anyway. The value is the message:
    the operator reads the CLI's own refusal instead of a confused
    downstream error.
    """
    canonical, error, hint = _agent_preflight(config.agent_cmd, config.agent_type)
    if error is not None:
        ui_impl.err(error)
        if hint is not None:
            ui_impl.info(hint)
        sys.exit(2)
    config.agent_type = canonical
    family = _probe_target_family(config.agent_cmd, canonical)
    if family is None:
        return
    probe = probe_family(family)
    if probe.live:
        return
    cli_name = "claude" if family == CLAUDE_FAMILY else "codex"
    detail = f": {probe.detail}" if probe.detail else ""
    ui_impl.warn(f"{cli_name} is installed but cannot run a turn{detail}")
    ui_impl.info(
        f"Check {cli_name}'s authentication, quota and config. The run "
        f"continues, but every {cli_name} call is likely to fail. "
        f"Set {PROBE_ENV_VAR}=0 to skip this check.{_USER_SETTINGS_HINT.get(family, '')}"
    )


#: The end of the hint of a failed probe, per CLI family. A claude login that works
#: in the operator's terminal can fail in kstrl, because kstrl starts claude
#: with --setting-sources project and takes only the apiKeyHelper and the env
#: of its user settings (#803).
_USER_SETTINGS_HINT = {
    CLAUDE_FAMILY: (
        " kstrl starts claude with --setting-sources project and takes only the "
        "apiKeyHelper and the env of the claude user settings "
        "($CLAUDE_CONFIG_DIR/settings.json, or ~/.claude/settings.json), so a "
        "different login key there does not apply. Set the provider in the "
        "environment of ks."
    ),
}


def _check_prd_preflight(prd_file: Path, ui_impl: UI) -> None:
    """Validate prd.json existence + schema BEFORE any agent spend (R2.4).

    Without this, the agent burns full iterations against a prompt
    referencing a nonexistent PRD before Phase 1 reports "Failed to load
    PRD". Error style mirrors ``ks init``'s per-field messages.
    """
    if not prd_file.exists():
        ui_impl.err(f"PRD file not found: {prd_file}")
        ui_impl.info(
            "Run `ks init` to scaffold scripts/kstrl/prd.json, "
            "or point --prd / PRD_FILE at an existing PRD."
        )
        sys.exit(2)

    try:
        with open(prd_file, encoding="utf-8") as f:
            data = read_json_file(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        ui_impl.err(f"Invalid JSON in {prd_file}: {exc}")
        sys.exit(2)
    except OSError as exc:
        ui_impl.err(f"Cannot read PRD file {prd_file}: {exc}")
        sys.exit(2)

    errors = PRD.validate_schema(data)
    if errors:
        ui_impl.err(f"PRD schema validation failed for {prd_file}:")
        for error in errors:
            ui_impl.info(f"  - {error}")
        sys.exit(2)


def _check_prompt_preflight(path: Path | None, ui_impl: UI) -> None:
    """Tell the OPERATOR when a prompt file is an older kstrl template (#286).

    Here, in the parent process, on the console UI, and BEFORE the
    command body. That placement is the whole
    point of this function existing rather than the check living in
    ``run_loop`` beside the read: on a factory run ``run_loop`` executes
    in a pool worker whose UI writes to that component's engineer.jsonl,
    so a warning there reaches nobody. #261's CLAUDE.md divergence
    warning was written that way and #275 had to lift it into the parent
    for exactly this reason.

    Advisory only, never fatal: the run is still correct with an older
    prompt, just missing whatever the newer template fixed.

    Two limits, stated rather than papered over.

    This lands on the plain terminal. Putting it in the feed would mean
    a typed event, which is the right shape for something DISCOVERED
    mid-run and the wrong shape for a start-up fact about a file on
    disk.

    ``ks init`` is the surface that both reports this and fixes it, and
    it is reachable from every path.
    """
    if path is None:
        return
    notice = staleness_notice(path)
    if notice is None:
        return
    ui_impl.warn(notice.headline)
    ui_impl.info(notice.advice)


def _apply_cli_overrides(
    ctx: click.Context,
    config: KstrlConfig,
    root_dir: Path,
    prompt_default: Path,
    prd_default: Path,
) -> set[str]:
    """Overlay explicitly-passed CLI flags onto a loaded KstrlConfig.

    Shared by ``run`` and ``config show`` so what the observability
    command prints is exactly what the run command executes. Only flags
    the invoking command declares are considered (``ctx.params``), and
    only when the user actually passed them. Returns the KstrlConfig
    field names a flag overrode, for per-value source reporting.
    """

    def passed(name: str) -> bool:
        return name in ctx.params and _use_cli_value(ctx, name)

    overridden: set[str] = set()
    if passed("max_iterations"):
        config.max_iterations = ctx.params["max_iterations"]
        overridden.add("max_iterations")
    if passed("prompt"):
        config.prompt_file = _resolve_path(root_dir, ctx.params["prompt"], prompt_default)
        overridden.add("prompt_file")
    if passed("prd"):
        config.prd_file = _resolve_path(root_dir, ctx.params["prd"], prd_default)
        overridden.add("prd_file")
    if passed("sleep"):
        config.sleep_seconds = ctx.params["sleep"]
        overridden.add("sleep_seconds")
    if passed("interactive"):
        config.interactive = ctx.params["interactive"]
        overridden.add("interactive")
    if passed("allowed_paths"):
        config.allowed_paths = parse_paths(ctx.params["allowed_paths"])
        overridden.add("allowed_paths")
    if passed("branch"):
        config.kstrl_branch = ctx.params["branch"]
        config.kstrl_branch_explicit = True
        overridden.add("kstrl_branch")
    if passed("agent_cmd"):
        config.agent_cmd = ctx.params["agent_cmd"]
        overridden.add("agent_cmd")
    if passed("model"):
        config.model = ctx.params["model"]
        overridden.add("model")
    if passed("reasoning"):
        config.model_reasoning_effort = ctx.params["reasoning"]
        overridden.add("model_reasoning_effort")
    if passed("agent_type"):
        config.agent_type = ctx.params["agent_type"]
        overridden.add("agent_type")
    if passed("ui"):
        config.ui_mode = _normalize_ui_mode(ctx.params["ui"])
        overridden.add("ui_mode")
    if passed("no_color"):
        config.no_color = ctx.params["no_color"]
        overridden.add("no_color")
    if passed("ascii"):
        config.ascii_only = ctx.params["ascii"]
        overridden.add("ascii_only")
    return overridden


def _collect_toml_notes(
    notes: list[str],
    section: str,
    loaded: Any,
    baseline: Any,
    flag_overridden: set[str],
) -> None:
    """Record which effective values kstrl.toml moved off the CLI default.

    Before R2.1 six kstrl.toml sections were silently ignored by the
    factory command, so a value that now takes effect is a behavior
    change for existing setups; the collected NOTE lines make that
    visible at startup. Comparing the loaded config against an env-only
    baseline isolates the toml contribution (env overlays are applied
    identically on both sides, so they cancel out). Fields whose CLI
    flag was explicitly passed are excluded: the flag wins, so the toml
    value is not effective.

    Fields marked ``metadata={"provenance": True}`` are skipped: they
    record where a value CAME FROM rather than being a value, so they
    have no toml key and no built-in default worth reporting. Asked at
    the declaration rather than matched by name here, so a second
    provenance field needs no edit to this function.
    """
    for f in dataclass_fields(loaded):
        if f.name in flag_overridden or f.metadata.get("provenance"):
            continue
        loaded_val = getattr(loaded, f.name)
        baseline_val = getattr(baseline, f.name)
        if loaded_val != baseline_val:
            notes.append(
                f"NOTE: [{section}] {f.name} = {json.dumps(loaded_val, default=str)} from "
                f"kstrl.toml (built-in default: {json.dumps(baseline_val, default=str)})"
            )


def _collect_evolution_notes(
    notes: list[str],
    root_dir: Path,
    ui_impl: UI,
) -> None:
    """The evolution section's NOTE sweep, guarded against a bad knob.

    Its own function because the guard has to sit BETWEEN the two loads:
    ``from_env`` applies the same env overrides as ``load``, so passing
    both as arguments to :func:`_collect_toml_notes` would evaluate the
    unguarded one first and raise before the guard could run.

    Why this section and not the others, stated precisely because the
    obvious reading is wrong (#257 review): evolution is NOT the only
    loader here that can raise. Measured on this tree, `ks factory`
    exits 1 with a raw ValueError traceback under
    ``KSTRL_MUTATION_THRESHOLD=many`` and ``KSTRL_SECURITY_TIMEOUT=many``
    too, and the toml paths of verify, security, contract, codebase scan
    and timeout raise the same way. Five sibling loads in this command
    are still unguarded.

    What separates evolution is what a failure MAY cost. The journal is
    an optional audit trail, so "unreadable, carry on without it" is an
    honest degrade. For verify, security, contract and timeout it is
    not: silently substituting defaults for the checks an operator
    configured is a semantic substitution, and those need an error
    boundary - a typed parse error caught where
    ``_KstrlGroup.invoke`` already catches ``BudgetConfigError`` - not a
    fallback. That boundary is a change to six config modules and is
    left out of #257.

    So this is one exit plugged, not the class closed. It is the exit
    worth plugging first: it runs AFTER the architect has been paid for
    and BEFORE ``run_factory``, with the spend live only in a local
    variable. It is not the only exit in that window either - the
    confirm prompt's "Quit" is an ordinary one - which is the honest
    argument for eventually making the architect's spend durable where
    it is earned rather than plugging exits one at a time.
    """
    from kstrl.learning import EvolutionConfig

    config = EvolutionConfig.load_or_none(root_dir, warn=ui_impl.warn)
    if config is None:
        return
    _collect_toml_notes(
        notes,
        "evolution",
        config,
        EvolutionConfig.from_env(root_dir),
        flag_overridden=set(),
    )


def _measure_base_before_architect(
    root_dir: Path,
    base_branch: str,
    factory_config: FactoryConfig,
    accept_red_base: str,
    no_verify: bool,
    run_id: str,
    ui_impl: UI,
) -> None:
    """Exit 2 before `ks factory --spec` pays the architect when the base refuses (#696 slice 7).

    The base gates the run would refuse on, measured first, into the
    architect's run (``run_id``); the factory run reuses the reading.
    ``--no-verify`` measures nothing, here as in the run.
    """
    from kstrl.verify import VerifyConfig

    if no_verify:
        return
    if base_refused_before_architect(
        base_branch,
        root_dir,
        factory_config,
        VerifyConfig.load(root_dir),
        accept_red_base,
        run_id,
        ui_impl,
    ):
        sys.exit(2)


def _stack_checkpoint(root_dir: Path, ui_impl: UI, *, no_verify: bool) -> None:
    """Exit 2 before anything is spent unless a person confirmed the ``[stack]`` (#696).

    Returns when its exact text is confirmed, and when kstrl.toml has no
    ``[stack]`` and ``--no-verify`` runs no check; with neither it refuses
    (slice 4, the flag day). An unconfirmed stack asks when someone can
    answer: Confirm, Reject, or Decide later, which is the default, so an
    accidental Enter confirms nothing. Anything but an answered Confirm
    refuses, after filing the one stack_confirmation item ``ks inbox
    approve`` decides.
    """
    from kstrl.config import SURFACE_REJECTIONS

    try:
        confirmed = confirmed_stack(root_dir)
    except StackRefused as refused:
        stack = refused.stack
    else:
        if confirmed is None and not no_verify:
            report_preflight(ui_impl, "kstrl.toml has no [stack]", [NO_STACK])
            sys.exit(2)
        return
    channel = UiInteractionChannel(ui_impl)
    response = channel.request(
        PromptRequest(
            kind=PromptKind.CHECKPOINT,
            header=_stack_question(stack),
            options=STACK_OPTIONS,
            default=len(STACK_OPTIONS) - 1,
        )
    )
    if response.choice in (0, 1):
        try:
            said = decide_at_prompt(root_dir, stack, confirm=response.choice == 0, actor=_actor())
        except SURFACE_REJECTIONS as exc:
            report_preflight(ui_impl, "the stack decision could not be recorded", [str(exc)])
            sys.exit(2)
        if response.choice == 0:
            ui_impl.ok(f"  Stack confirmed: {said}")
            return
        report_preflight(ui_impl, "the [stack] in kstrl.toml was rejected", [said])
        sys.exit(2)
    report_preflight(
        ui_impl, "the [stack] in kstrl.toml is not confirmed", unconfirmed_lines(root_dir, stack)
    )
    sys.exit(2)


def _stack_question(stack: Stack) -> str:
    checks = ", ".join(f"{name} `{command}`" for name, command in stack.checks)
    setup = f"setup `{stack.setup}`" if stack.setup else "no setup"
    return (
        f"The [stack] in kstrl.toml {stack.unconfirmed}. kstrl will run {checks} ({setup}) "
        f"on every change. Confirm {stack.digest[:12]}?"
    )
