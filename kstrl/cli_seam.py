"""The root `ks` group and the guarantee every command gets: the configuration is
resolved before a command body runs, and a rejected one exits 2. Also the small
helpers every command shares (console UI, parameter source, root, actor).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import click
from click.core import ParameterSource

from kstrl import __version__
from kstrl.config import ConfigError
from kstrl.config_report import normalize_ui_mode as _normalize_ui_mode
from kstrl.factory import BudgetConfigError, FactoryLockHeldError
from kstrl.manifest import Manifest
from kstrl.output import build_console
from kstrl.ui.base import UI


def _load_manifest_or_exit(path: Path, ui: UI) -> Manifest:
    """Load a manifest, or print why not and exit 2.

    A `Manifest.load` reachable from the CLI must never traceback at the
    operator: the input is a file a human or another tool wrote, so a bad
    one is an expected outcome. Shared so a new call site cannot forget
    the guard, which is how `ks inbox retry` came to lack it (#263).
    """
    try:
        return Manifest.load(path)
    except (OSError, ValueError) as exc:
        ui.err(f"Failed to load manifest {path}: {exc}")
        sys.exit(2)


def _console_ui(
    mode: str = "auto",
    no_color: bool = False,
    ascii_only: bool = False,
    force_rich: bool = False,
) -> UI:
    """Event-native drop-in for get_ui() (TUI rewrite chunk 7).

    Same signature and mode resolution; returns the console's
    EventBridgeUI so every line the command narrates becomes a typed
    Log event, rendered synchronously and byte-identically onto the
    same concrete UI get_ui() would have picked. run_factory discovers
    the bus via ``ui.bus`` to attach the run's file sinks.
    """
    return build_console(
        mode,
        no_color=no_color,
        ascii_only=ascii_only,
        force_rich=force_rich,
    ).ui


def _use_cli_value(ctx: click.Context, name: str) -> bool:
    return ctx.get_parameter_source(name) == ParameterSource.COMMANDLINE


def _resolve_root(root: Path | None, prompt: Path | None, prd: Path | None) -> Path:
    if root is not None:
        return root.resolve()

    for candidate in (prompt, prd):
        if candidate is None:
            continue
        resolved = candidate.resolve()
        parent = resolved.parent
        if parent.name == "kstrl" and parent.parent.name == "scripts":
            return parent.parent.parent

    return Path.cwd()


def _resolve_path(root: Path, value: str | None, default: Path) -> Path:
    if value is None or value == "":
        return default
    path = Path(value)
    if path.is_absolute():
        return path
    return root / path


#: `ks factory --progress-log` and `ks retry --progress-log`: retry hands the
#: value straight to factory, so the two describe one log in one sentence.
_PROGRESS_LOG_HELP = (
    "Path for the JSONL progress log (default: <root>/.kstrl/progress.jsonl; "
    "the log is on by default, disable via [factory].progress_log_enabled = "
    "false or KSTRL_FACTORY_PROGRESS_LOG_ENABLED=0)"
)


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


# The commands that must NOT be preflighted, and why each one is not a
# hole in the guarantee:
#
# - `ks init` WRITES a kstrl.toml, including over a broken one. Refusing
#   to run it because the file it is about to replace does not parse
#   takes away the tool the operator recovers with.
# - `ks config show` is the surface that must ALWAYS run and always
#   explain, because every other command refuses on a rejected section.
#   It prints every row it can resolve, then names each rejected section
#   with its key and value. That guarantee is what makes universal
#   fatality defensible without an escape flag: the way out of a bad
#   config is a command, not a way to skip the check.
# - `ks check` reports a config failure through a documented MACHINE
#   contract that the seam would destroy: a JSON error document on
#   stdout for `--json`. It calls the preflight itself, under that
#   contract.
# - `ks doctor` REPORTS a rejected configuration as one of its ten
#   checks, with the not-ready verdict and the exit 1 that go with
#   it, and calls `config_preflight.config_problem_lines` itself to
#   do so. Under the seam, the command an operator diagnoses WITH
#   would print one refusal and none of its checks, for the very file
#   it exists to report on.
#
# The last three are exempt from the SEAM, never from the check: each
# still resolves every section in its own body, under its own
# contract, but not all of them the same way. `check` calls
# `preflight_config` and turns a rejection into its own documented
# refusal; `config show` and `doctor` REPORT instead, through the
# reporting half of the same traversal (`collect_config_problems` /
# `config_problem_lines`) rather than the raising one. `init` is the
# one command exempt from the check itself, for the reason above. An
# exemption that skipped the check for any other reason would be the
# property #272 removed, smuggled back in under a name.
#
# Bare `ks` is NOT on this list and is not a command: its callback
# belongs to the group, so it preflights explicitly (see `cli`).
#
# Keyed by the TOP-LEVEL command name (see `_KstrlCommand._top_level_name`),
# so `ks config show` is covered by "config" while a later `ks queue init`
# is not exempted by its leaf name.
_PREFLIGHT_EXEMPT = frozenset({"init", "config", "check", "doctor"})

# Sections a command is ABOUT, promoted from degrading to fatal for that
# command only. `[evolution]` degrades everywhere because the journal is
# an audit trail attached to work about something else; `ks evolve` IS
# the journal, so warning and continuing would be a promise the next two
# lines break. Declared here, beside the exemptions, so a command does
# not carry its own remembered guard for a policy the seam already owns.
_PREFLIGHT_REQUIRED: dict[str, frozenset[str]] = {
    "evolve": frozenset({"evolution"}),
}

# The commands that derive their root from a prompt or PRD path, and so
# the only ones whose `--prompt` / `--prd` / `--understand-prompt` (and
# PROMPT_FILE / PRD_FILE) the entry check may read. See `_preflight_root`
# for why this is a list of commands and not "whichever command declares
# the option".
_ROOT_FROM_PROMPT = frozenset({"run", "understand", "feature"})


def _preflight_warn(message: str) -> None:
    """A degrading section's warning, on STDERR.

    Stdout belongs to the command's output, and `ks check --json` puts a
    single JSON document there that a script parses.
    """
    click.echo(f"warning: {message}", err=True)


def _preflight_root(ctx: click.Context) -> Path:
    """The root the command is about to use, derived before it runs.

    Reuses ``_resolve_root`` - the same inputs, in the same precedence -
    so the file the preflight validates is the file the command will
    load. Every other command uses ``root or Path.cwd()``, which is what
    this returns for them.

    The prompt and PRD inputs are read ONLY for the commands in
    ``_ROOT_FROM_PROMPT``, and that is the whole correctness argument.
    Reading them for every command broke this both ways with one stale
    ``PROMPT_FILE`` export: ``ks status`` refused on a broken kstrl.toml
    belonging to an unrelated checkout, and - worse, because nothing
    shows it - ``ks status`` in a project whose OWN config was broken
    PASSED, by validating that other checkout instead.

    Keyed by command rather than by "declares the option", which reads
    like the same rule and is not: `ks config show` declares ``--prompt``
    and ``--prd`` as ``[paths]`` OVERRIDES and still roots itself at the
    cwd, so the proxy already pointed that command at another checkout.
    ``tests/test_config_preflight.py`` fails on any new command that
    declares one of these options without a decision being recorded here.
    """

    def _param(name: str) -> str | None:
        value = ctx.params.get(name)
        return str(value) if value else None

    def _path(*names: str, env_var: str) -> Path | None:
        if _KstrlCommand._top_level_name(ctx) not in _ROOT_FROM_PROMPT:
            return None
        for name in names:
            value = _param(name)
            if value:
                return Path(value)
        from_env = os.environ.get(env_var)
        return Path(from_env) if from_env else None

    root = _param("root")
    return _resolve_root(
        Path(root) if root else None,
        # `ks feature` names its prompt option --understand-prompt and
        # feeds THAT to _resolve_root. No command declares both.
        _path("prompt", "understand_prompt", env_var="PROMPT_FILE"),
        _path("prd", env_var="PRD_FILE"),
    )


class _KstrlCommand(click.Command):
    """Every command, with one guarantee: the configuration is resolved
    before the command body constructs anything.

    THIS seam and not ``_KstrlGroup.invoke``, which is where the error is
    caught: at group level click has parsed the group's own arguments but
    not the subcommand's, so ``--root`` is not known yet and the
    preflight would validate the wrong file whenever an operator pointed
    a command at another checkout. ``Command.invoke`` runs after the
    subcommand's parameters are parsed and before its callback, which is
    the first moment both facts are available - the root, and that
    nothing has been built or paid for yet.

    Installed through ``_KstrlGroup.command_class`` rather than on each
    command, for the reason the group gives below: a guarantee that every
    entry point has to remember is one a later entry point will forget.
    """

    def invoke(self, ctx: click.Context) -> Any:
        from kstrl.config_preflight import preflight_config

        name = self._top_level_name(ctx)
        if name not in _PREFLIGHT_EXEMPT:
            preflight_config(
                _preflight_root(ctx),
                warn=_preflight_warn,
                required=_PREFLIGHT_REQUIRED.get(name, frozenset()),
            )
        return super().invoke(ctx)

    @staticmethod
    def _top_level_name(ctx: click.Context) -> str:
        """The command name directly under the root group.

        Both tables key off THIS, not off any name in the chain: keying
        off any would exempt a later ``ks queue init`` or ``ks inbox
        serve`` purely because of its leaf name, which is a decision
        nobody would have made. It is also what puts ``ks config show``
        under ``config``.
        """
        node = ctx
        while node.parent is not None and node.parent.parent is not None:
            node = node.parent
        return node.command.name or ""


class _KstrlGroup(click.Group):
    """The CLI group, with two guarantees: a rejected budget ceiling and
    unusable configuration are reported, never raised.

    ``BudgetConfigError`` is thrown deep inside config loading, which
    happens in `factory`, `run`, `retry`, the config report and the
    launch path - and, after a ``--max-cost-usd`` override, inside
    ``run_factory`` itself. Catching it per command meant every one of
    those sites had to remember; `factory` did not, and exited 1 with an
    empty stdout and a raw traceback (review finding on #180). Catching
    it HERE means no entry point can leak one, including entry points
    added later.

    ``ConfigError`` is the same contract for the same reason (#272).
    Before it, a typo's blast radius depended on which section it was in
    and which command was run: ``KSTRL_MUTATION_THRESHOLD=many`` and
    ``KSTRL_SECURITY_TIMEOUT=many`` both left a raw ``ValueError``
    traceback out of `ks factory`, and a bad ``[linear]`` value aborted
    `ks decompose` only after the architect had been paid for.
    ``command_class`` puts the check that raises it in front of every
    command body; this catches what it raises.

    Exit code 2 with an ``error:`` line: a configuration the entry check
    rejects is a command that cannot run, which is what 2 means on every
    command (#452).

    ``FactoryLockHeldError`` is the same contract (#597): a command that
    takes the run lock before its first change (`ks retry`, and
    ``decompose_spec`` under `ks decompose` and `ks factory --spec`) is
    refused here with nothing changed. The message keeps ``--force-lock``,
    which `ks serve` reads to classify the refusal as lock contention.
    """

    command_class = _KstrlCommand
    #: ``type`` is click's "same class as this group", so `ks config`,
    #: `ks queue` and every later subgroup inherit ``command_class``.
    group_class = type

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except ConfigError as exc:
            click.echo(f"error: {exc}", err=True)
            sys.exit(2)
        except BudgetConfigError as exc:
            click.echo(f"error: {exc}", err=True)
            sys.exit(2)
        except FactoryLockHeldError as exc:
            click.echo(f"error: {exc}", err=True)
            sys.exit(2)


@click.group(cls=_KstrlGroup, invoke_without_command=True)
@click.version_option(version=__version__)
@click.pass_context
def cli(ctx: click.Context) -> None:
    """kstrl - a software factory for AI coding agents.

    Hand it a spec. It plans, builds, measures the result with checks the
    agent did not write, feeds the gap back, and stops only when independent
    checks agree. Every run is recorded; boundary decisions come to you."""
    if ctx.invoked_subcommand is not None:
        return
    # Bare `ks` on a TTY opens the home shell (D1 user decision);
    # everywhere else stays byte-identical to click's no-args behavior
    # (help on stdout, exit 2) - the pipe/CI contract.
    if sys.stdout.isatty() and sys.stdin.isatty() and os.environ.get("KSTRL_NO_TUI") != "1":
        from kstrl.config_preflight import preflight_config
        from kstrl.tui.home import run_home_shell

        # The home shell is an ENTRY POINT, not a command: this callback
        # belongs to the group, so `_KstrlCommand.invoke` never runs for
        # it. Without this line it was a fifth exemption that nobody
        # declared, and the most expensive one, because the shell
        # launches runs IN-PROCESS (`tui/session.py` calls run_factory
        # and decompose_spec directly). A bad [linear] value would have
        # paid for the architect and then aborted: the original #272
        # defect, on the path a user reaches by typing `ks`.
        #
        # Bound once: the point of the check is that the root it
        # validates is the root the shell opens.
        root_dir = Path.cwd()
        preflight_config(root_dir, warn=_preflight_warn)
        ctx.exit(run_home_shell(root_dir))
    click.echo(ctx.get_help())
    ctx.exit(2)


def _autonomy_ui(ui: str, no_color: bool) -> UI:
    force_rich = os.environ.get("GUM_FORCE") == "1"
    return _console_ui(_normalize_ui_mode(ui), no_color, force_rich=force_rich)


def _actor() -> str:
    """Who is deciding. Best-effort identity for the audit trail."""
    return os.environ.get("USER") or os.environ.get("USERNAME") or "operator"
