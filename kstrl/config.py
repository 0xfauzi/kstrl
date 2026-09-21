"""Configuration handling for kstrl."""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

# Re-exported: the loaders below read it from this module's namespace,
# and it is defined in kstrl/config_keys.py because it is the part of
# this file that grows a row per new [paths] file. Same object, pinned
# by tests/test_string_keys_reach_every_surface.py.
from kstrl.config_keys import STRING_KEYS as STRING_KEYS

# Re-exported from kstrl/config_toml.py, where reading kstrl.toml lives
# (#366). The ``as`` spelling is what makes this a re-export under mypy
# --strict rather than a private import.
from kstrl.config_toml import UNPARSEABLE_TOML_MESSAGE as UNPARSEABLE_TOML_MESSAGE
from kstrl.config_toml import ConfigError as ConfigError
from kstrl.config_toml import load_toml_document as load_toml_document
from kstrl.config_toml import load_toml_section as load_toml_section
from kstrl.config_toml import toml_parse_scope as toml_parse_scope


def _parse_bool(value: str | None) -> bool:
    """Parse boolean from environment variable."""
    if value is None:
        return False
    return bool(re.match(r"^(1|true|yes)$", value.lower()))


def _parse_paths(value: str | None) -> list[str]:
    """Parse comma-separated paths, trimming whitespace."""
    if not value:
        return []
    return [p.strip() for p in value.split(",") if p.strip()]


def _resolve_path(value: str, root_dir: Path) -> Path:
    """Resolve a path string against root_dir if relative."""
    p = Path(value)
    if p.is_absolute():
        return p
    return root_dir / p


def relative_to_root(path: Path, root_dir: Path) -> str:
    """Render ``path`` relative to ``root_dir`` for use inside a
    per-component worktree. Falls back to the absolute path string when
    relativization fails (e.g. a path on a different mount)."""
    if not path.is_absolute():
        return str(path)
    try:
        return path.relative_to(root_dir).as_posix()
    except ValueError:
        try:
            return path.resolve().relative_to(root_dir.resolve()).as_posix()
        except ValueError:
            return str(path)


# A FACTORY component's progress log lives NEXT TO that component's PRD.
# DECOMPOSE_PROMPT rule 12 tells the architect that a component's
# allowedPaths include exactly `scripts/kstrl/feature/<component-id>/`
# "(the agent updates progress.txt and PRD passes there)", so a progress
# path derived from prdPath is inside the component's write scope BY
# CONSTRUCTION. Pointing the engineer at the repo-root
# scripts/kstrl/progress.txt instead put it one level ABOVE that
# subtree: the engineer wrote the file the harness told it to write,
# Phase 1 `diff_scope` reported "files outside allowed scope", and the
# component was failed and retried from base. Measured cost of one such
# retry on a real paid run: $12.93.
COMPONENT_PROGRESS_FILENAME = "progress.txt"

# Where the STANDALONE loop (`ks understand`, `ks feature`) writes its
# progress log when nothing is configured. There is no component PRD to
# derive a sibling from there, and the prompt template needs a concrete
# path, so this historical default is materialized by
# KstrlConfig.resolved_progress_file - never stored in the field itself,
# which stays None so "unset" remains distinguishable from "set to the
# default" (R8 review finding 2).
DEFAULT_PROGRESS_FILE = "scripts/kstrl/progress.txt"


def reconcile_progress_paths(
    writer_explicit: str | Path | None,
    reader_explicit: str | Path | None,
) -> tuple[str | None, str | None]:
    """Keep the progress log's WRITER and READER pointing at one file.

    ``[paths] progress`` (or ``PROGRESS_FILE``) configures where the
    engineer WRITES its progress log; ``[verify] progress_file_path``
    (or ``KSTRL_VERIFY_PROGRESS_FILE``) configures where the
    self-critique check READS it. Both default to the same derivation,
    so they agree until exactly one of them is set - and then the
    reader silently inspects a file the engineer never wrote, which
    fails the self-critique gate for a reason the operator cannot see.

    Setting one propagates it to the other. Setting BOTH is left alone,
    including when they differ: an operator who named two paths meant
    two paths, and the caller warns rather than overriding. Returns
    ``(writer, reader)`` as strings, or None where nothing is set.
    """
    writer = str(writer_explicit) if writer_explicit is not None else None
    reader = str(reader_explicit) if reader_explicit is not None else None
    if writer is not None and reader is None:
        return writer, writer
    if reader is not None and writer is None:
        return reader, reader
    return writer, reader


class ProgressReaderConfig(Protocol):
    """The one field ``reconcile_progress_config`` needs from a
    ``VerifyConfig``. Declared structurally so this module does not
    import kstrl.verify, which imports this one."""

    progress_file_path: str | None


def reconcile_progress_config(
    base_config: KstrlConfig,
    verify_config: ProgressReaderConfig,
    root_dir: Path,
) -> str | None:
    """Reconcile the progress log's writer and reader IN ONE PATH DOMAIN.

    Returns a warning message when both were set to different files, or
    None. Mutates both configs.

    The domain is ROOT-RELATIVE for anything inside ``root_dir``, which
    is the domain both consumers actually resolve in: the engineer's
    worker joins the writer path onto its WORKTREE
    (``factory._run_component``), and the self-critique check joins the
    reader path onto the same worktree (``verify.run_mechanical_
    verification``). Reconciling the raw values instead (R8 review
    finding 3) compared an ABSOLUTE writer - ``KstrlConfig.load``
    resolves ``[paths] progress`` against the main checkout - against a
    verbatim-relative reader. The two STRINGS came out equal while the
    runtime paths did not: the worker wrote ``<worktree>/docs/p.md`` and
    the check read ``<root>/docs/p.md``, so self-critique still failed,
    or passed against a stale file in the main checkout.

    A path OUTSIDE ``root_dir`` stays absolute (``relative_to_root``
    cannot relativize it), which is also self-consistent: joining an
    absolute path onto a worktree is a no-op, so writer and reader both
    land on that one file, in the main checkout, for every component.
    """
    writer_domain = (
        relative_to_root(base_config.progress_file, root_dir)
        if base_config.progress_file is not None
        else None
    )
    reader_domain = (
        relative_to_root(Path(verify_config.progress_file_path), root_dir)
        if verify_config.progress_file_path
        else None
    )
    writer, reader = reconcile_progress_paths(writer_domain, reader_domain)
    verify_config.progress_file_path = reader
    if writer is not None:
        base_config.progress_file = Path(writer)
    if writer is not None and reader is not None and writer != reader:
        # Both named, and named differently. Not overridden - an operator
        # who set two paths meant two paths - but said out loud, because
        # the failure it produces is otherwise unattributable.
        return (
            f"progress log writer ([paths] progress = {writer}) and "
            f"reader ([verify] progress_file_path = {reader}) point at "
            "different files; the self-critique check will inspect a "
            "file the engineer does not write"
        )
    return None


def component_progress_path(
    prd_path: str | Path,
    configured: str | Path | None = None,
) -> Path:
    """Progress-log path for the component whose PRD is at ``prd_path``.

    ``configured`` is an explicitly set progress path ([paths] progress
    or PROGRESS_FILE) and wins verbatim for every component - explicit
    configuration is never silently rewritten. With nothing configured
    the log is a SIBLING of the PRD, which reproduces the historical
    ``scripts/kstrl/progress.txt`` for the single-component layout (PRD
    at ``scripts/kstrl/prd.json``) and lands inside the component's own
    feature subtree for a decomposed one.
    """
    if configured is not None:
        return Path(configured)
    return Path(prd_path).parent / COMPONENT_PROGRESS_FILENAME


def component_harness_paths(
    prd_path: str | Path,
    progress_path: str | Path,
    codebase_map_path: str | Path,
) -> list[str]:
    """The harness's OWN files for one component, as EXACT paths.

    kstrl's mechanical checks require the engineer to write three files
    that are not product code: the component PRD (``check_prd_stories``
    re-reads it and only the agent can set ``passes``), the component
    progress log (``check_self_critique`` reads the Self-Critique block
    out of it), and the codebase map (the engineer prompt tells the
    agent to append durable facts to it). kstrl knows all three; the
    operator should not have to guess them into ``allowedPaths``.

    The list is the carve-out both scope guards apply on top of the
    AUTHORED ``allowedPaths`` - the in-loop guard through
    ``loop.run_loop(guard_ignored_paths=...)`` and Phase 1 through
    ``verify.check_diff_scope(harness_paths=...)``. It is reported
    separately from the authored list at both sites so an operator can
    still see what THEY authorised.

    Every entry is an exact path, never a directory prefix: a trailing
    slash would widen the carve-out to a whole subtree, and
    ``scripts/kstrl/`` is precisely the blanket prefix operators resort
    to today and that DECOMPOSE_PROMPT rule 12 refuses. Entries are
    de-duplicated (the single-component layout can point two of the
    three at one file) and sorted so the reported set is stable.

    An entry that is absolute, or that escapes the root, is kept rather
    than dropped, and is harmless: such a file lives outside every
    worktree (joining an absolute path onto one is a no-op), so it never
    appears in a component's ``git diff`` and can never be a scope
    violation in the first place. ``config.reconcile_progress_config``
    documents that configuration as supported.

    Every caller reaches this through
    ``KstrlConfig.component_harness_files``, or
    ``standalone_harness_files`` for the loop whose progress log is not
    a sibling of a component PRD. ``factory._run_component`` used to
    call it directly, from a pool worker that had the three paths as
    strings and no config to ask; #269 stopped that, because the worker
    now receives the carve-out as part of the plan-time scope snapshot
    rather than rebuilding one that merely agreed with Phase 1's.
    """
    return sorted(
        {Path(p).as_posix() for p in (prd_path, progress_path, codebase_map_path)},
    )


@dataclass
class KstrlConfig:
    """Configuration for the kstrl agentic loop."""

    max_iterations: int = 10
    # Path is immutable, so a plain default is safe and no
    # default_factory lambda is needed; anchored() rebinds, never mutates.
    prompt_file: Path = Path("scripts/kstrl/prompt.md")
    prd_file: Path = Path("scripts/kstrl/prd.json")
    # None = UNSET, and unset is the safe default: every factory
    # component then derives its own log next to its own PRD, inside its
    # allowedPaths (see component_progress_path). Any non-None value -
    # from [paths] progress, PROGRESS_FILE, a constructor argument, or a
    # plain attribute assignment - is an explicit setting and is forced
    # on every component verbatim.
    #
    # The sentinel replaced a separate progress_file_explicit flag (R8
    # review finding 2): the flag was set only by the toml/env loaders,
    # so a programmatic caller doing
    # KstrlConfig(progress_file=Path("docs/p.md")) had its value silently
    # ignored - a regression for tests, embedders and the SDK, which
    # could previously pass a base config to run_factory and be obeyed.
    # It is NOT a compare-against-default heuristic (R2.1 deliberately
    # removed that pattern from VerifyConfig.load): pinning the
    # historical path explicitly still counts as explicit, because the
    # field holds a Path only when someone put one there.
    #
    # Standalone callers that need a concrete path (the prompt template's
    # $progress_path) call resolved_progress_file(root_dir).
    progress_file: Path | None = None
    codebase_map_file: Path = Path("scripts/kstrl/codebase_map.md")
    # R10.8: operator-authored; operator_context.py says who reads it.
    golden_patterns_file: Path = Path("scripts/kstrl/golden-patterns.md")
    # R10.9: the operator's standing feedback, read AFTER the retry
    # context; operator_context.py says who reads it.
    memory_file: Path = Path("scripts/kstrl/memory.md")
    sleep_seconds: float = 2.0
    interactive: bool = False
    allowed_paths: list[str] = field(default_factory=list)

    # Branch config - None means use PRD, "" means skip
    kstrl_branch: str | None = None
    kstrl_branch_explicit: bool = False  # Was KSTRL_BRANCH env var set?
    auto_checkout: bool = True

    # Agent config
    agent_cmd: str | None = None
    model: str | None = None
    model_reasoning_effort: str | None = None
    # "claude-code", "claude-sdk", "codex", "auto", or None
    agent_type: str | None = None
    # R7.6: in-loop USD budget ceiling; enforced only by the claude-sdk
    # adapter (per-turn, inside the agent loop). None = no ceiling.
    agent_budget_usd: float | None = None

    # Timeouts live in kstrl.timeout.TimeoutConfig (the single source
    # for agent_iteration / component_total; R0.1). KstrlConfig used to
    # duplicate them as dead fields - deliberately deleted, do not re-add.

    # UI config
    ui_mode: str = "auto"  # auto|rich|plain
    no_color: bool = False
    ascii_only: bool = False

    @classmethod
    def anchored(cls, root_dir: Path) -> KstrlConfig:
        """Defaults with every default file path resolved against ``root_dir``.
        from_env, from_toml, load and config_report.kstrl_config_defaults held a
        copy each, so a path added to three of the four differed by entry point.
        Anchors the FIELD default; progress_file needs no case, being None."""
        config = cls()
        for _section, _key, _env, field_name, is_path in STRING_KEYS:
            if is_path and (default := getattr(config, field_name)) is not None:
                setattr(config, field_name, root_dir / default)
        return config

    @classmethod
    def from_env(cls, root_dir: Path | None = None) -> KstrlConfig:
        """Load configuration from environment variables only."""
        if root_dir is None:
            root_dir = Path.cwd()
        config = cls.anchored(root_dir)
        _apply_env_overrides(config, root_dir)
        return config

    @classmethod
    def from_toml(cls, toml_path: Path, root_dir: Path | None = None) -> KstrlConfig:
        """Load configuration from a kstrl.toml file (no env overlay)."""
        if root_dir is None:
            root_dir = toml_path.parent if toml_path.is_absolute() else Path.cwd()
        config = cls.anchored(root_dir)
        if toml_path.exists():
            _apply_toml_overrides(config, toml_path, root_dir)
        return config

    @classmethod
    def load(
        cls,
        root_dir: Path | None = None,
        toml_path: Path | None = None,
    ) -> KstrlConfig:
        """Load configuration with precedence: env > toml > dataclass defaults.

        If ``toml_path`` is omitted, ``<root_dir>/kstrl.toml`` is
        auto-discovered.
        Missing TOML file is fine (defaults are used). Malformed TOML raises.
        """
        if root_dir is None:
            root_dir = Path.cwd()
        if toml_path is None:
            toml_path = resolve_config_file(root_dir)

        config = cls.anchored(root_dir)
        if toml_path.exists():
            _apply_toml_overrides(config, toml_path, root_dir)
        _apply_env_overrides(config, root_dir)
        return config

    @classmethod
    def load_or_anchored(
        cls,
        root_dir: Path,
        warn: Callable[[str], None],
    ) -> KstrlConfig:
        """:meth:`load`, but a config that will not parse returns the
        anchored defaults instead of raising.

        Which exceptions that means is stated here rather than at a call
        site, because it is a fact about :meth:`load`, the same way
        :meth:`EvolutionConfig.load_or_none` states its own taxonomy
        rather than its caller's. ``ConfigError`` (``kstrl.config_toml``,
        a ``ValueError`` subclass) is the operator-input case:
        malformed TOML, non-UTF-8 bytes, or anything else
        ``load_toml_document``'s parse raises, all funnelled through one
        type precisely so a reader here does not have to enumerate the
        underlying parser's exception family itself (see that function's
        own docstring for why enumerating it directly has failed twice).
        Plain ``TypeError`` is the same operator-input case one layer
        up: ``_apply_toml_overrides`` calls ``int(run["max_iterations"])``
        on whatever TOML handed it, and a table or array there raises
        ``TypeError``, not ``ConfigError``, because the parse succeeded
        and the coercion is what failed.

        ``OSError`` from an unreadable ``kstrl.toml`` is included too,
        chosen rather than left implicit. ``load_toml_document`` hoists
        every read outside its parse guard (`path.read_bytes()` before
        the ``try``), so a file `kstrl.toml.exists()` sees but cannot
        open - wrong permissions, a race with a concurrent write, a
        path that resolves to a directory - raises ``OSError`` raw past
        :meth:`load`. That failure is exactly as much an operator
        problem as a syntax error: this call site's only reason to
        exist is to keep one degraded knob (the path named in the
        architect prompt) from aborting the whole run over a file nobody
        can currently read. Excluding ``OSError`` would leave that same
        run aborting on a locked-down file the way it aborts on a
        mistyped bracket, which is the exact asymmetry this method
        exists to remove. ``EvolutionConfig.load_or_none`` reached the
        same conclusion for the same file.

        Deliberately NOT covering anything else: a ``TypeError`` or
        ``ValueError`` from a defect INSIDE :meth:`load` unrelated to
        parsing config - a ``None`` where a path belongs, a signature
        that stopped matching - would also be caught by this taxonomy
        and misreported as "config unreadable" rather than surfacing.
        That is the same cost ``EvolutionConfig.load_or_none`` accepts
        and names for the identical reason: narrowing further would
        require inspecting the message, which is guessing, not
        deciding.

        Every other command still goes through the strict
        :meth:`load` at ``config_preflight``, which validates
        ``kstrl.toml`` once at entry and turns the same exception into
        an ``error:`` line and a non-zero exit. This method exists only
        for a call site that must not abort ahead of its own halt path;
        it is not a second, quieter way to accept a broken config for
        the run as a whole.

        Degrades loudly: ``warn`` is called with the path and the parse
        failure before the anchored defaults are returned.
        """
        try:
            return cls.load(root_dir)
        except (ValueError, TypeError, OSError) as exc:
            warn(f"{root_dir / 'kstrl.toml'} unreadable, using defaults: {exc}")
            return cls.anchored(root_dir)

    def component_progress_file(
        self,
        prd_path: str | Path,
        root_dir: Path,
    ) -> str:
        """Worktree-relative progress path for one factory component.

        The single place the factory decides where a component's
        engineer writes its progress log: an explicit configuration wins
        for every component, otherwise the path is derived from the
        component's own PRD so it sits inside the component's
        allowedPaths (see ``component_progress_path``).
        """
        configured = (
            relative_to_root(self.progress_file, root_dir)
            if self.progress_file is not None
            else None
        )
        return component_progress_path(prd_path, configured).as_posix()

    def component_harness_files(
        self,
        prd_path: str | Path,
        root_dir: Path,
    ) -> list[str]:
        """kstrl's OWN files for one component, as exact root-relative paths.

        The single place the factory decides WHICH files the harness
        requires a component's engineer to write, mirroring
        ``component_progress_file``'s role for the progress log alone.
        Both scope guards carve out exactly this list (#264): the in-loop
        guard via ``loop.run_loop(guard_ignored_paths=...)`` and Phase 1
        via ``verify.check_diff_scope(harness_paths=...)``. Deriving the
        three arguments here rather than at each call site is what stops
        the two guards judging different sets.
        """
        return component_harness_paths(
            prd_path,
            self.component_progress_file(prd_path, root_dir),
            relative_to_root(self.codebase_map_file, root_dir),
        )

    def standalone_harness_files(self, root_dir: Path) -> list[str]:
        """The same three files for the STANDALONE loop (``ks understand``).

        Deliberately NOT ``component_harness_files``: that derives the
        progress log as a SIBLING of the component PRD, while the
        standalone loop writes ``resolved_progress_file``. The two
        coincide only in the default layout - point ``[paths] prd`` at
        ``docs/prd.json`` and the factory rule yields
        ``docs/progress.txt`` while the loop still writes
        ``scripts/kstrl/progress.txt`` - so reusing the factory method
        here would carve out a file nothing writes and leave the real
        one exposed. The one place the two rules diverge belongs beside
        them both, not in the CLI.
        """
        return component_harness_paths(
            relative_to_root(self.prd_file, root_dir),
            relative_to_root(self.resolved_progress_file(root_dir), root_dir),
            relative_to_root(self.codebase_map_file, root_dir),
        )

    def resolved_progress_file(self, root_dir: Path) -> Path:
        """Concrete progress path for the STANDALONE loop.

        ``progress_file`` is None until someone sets it, but the loop
        substitutes ``$progress_path`` into the engineer prompt and needs
        a real path there. Standalone runs have no component PRD to
        derive a sibling from, so they get the historical repo-root
        default; a relative explicit setting is anchored to ``root_dir``
        the same way the loaders anchor one.

        The factory does NOT come through here: its workers are handed an
        already-concrete per-component path (factory._run_component), and
        this method returns it untouched.
        """
        if self.progress_file is None:
            return root_dir / DEFAULT_PROGRESS_FILE
        if self.progress_file.is_absolute():
            return self.progress_file
        return root_dir / self.progress_file

    def validate(self, root_dir: Path | None = None) -> list[str]:
        """Validate configuration, returning list of errors. ``root_dir``
        separates an explicitly set optional path from the anchored default,
        so that check is skipped without one. Measured: nothing in ``kstrl/``
        calls it, so the REACHABLE copy of that rule is run_factory's warning."""
        from kstrl.operator_context import configured_path_errors

        errors: list[str] = []
        if self.max_iterations < 0:
            errors.append(f"MAX_ITERATIONS must be non-negative (got: {self.max_iterations})")
        if not self.prompt_file.exists():
            errors.append(f"Prompt file not found: {self.prompt_file}")
        if root_dir is None:
            return errors
        return errors + configured_path_errors(self, KstrlConfig.anchored(root_dir), root_dir)


CONFIG_FILE_NAME = "kstrl.toml"


def resolve_config_file(root_dir: Path) -> Path:
    """Return the config file for ``root_dir``.

    Resolving the name in one place keeps it from drifting between the
    loaders. The path is returned whether or not it exists; loaders
    no-op on a missing file.
    """
    return root_dir / CONFIG_FILE_NAME


def _apply_toml_overrides(
    config: KstrlConfig,
    toml_path: Path,
    root_dir: Path,
) -> None:
    """Mutate config in place from a kstrl.toml file.

    Maps the documented section structure (agent, run, paths, git, ui) onto
    the flat KstrlConfig dataclass. Unknown keys are silently ignored.
    """
    data = load_toml_document(toml_path)

    for section, toml_key, _env_var, field_name, is_path in STRING_KEYS:
        block = data.get(section)
        if isinstance(block, dict) and isinstance(value := block.get(toml_key), str) and value:
            setattr(config, field_name, _resolve_path(value, root_dir) if is_path else value)

    if isinstance(agent := data.get("agent"), dict):
        budget = agent.get("budget_usd")
        if isinstance(budget, (int, float)) and not isinstance(budget, bool) and budget > 0:
            config.agent_budget_usd = float(budget)

    if isinstance(run := data.get("run"), dict):
        if "max_iterations" in run:
            config.max_iterations = int(run["max_iterations"])
        if "sleep_seconds" in run:
            config.sleep_seconds = float(run["sleep_seconds"])
        if "interactive" in run:
            config.interactive = bool(run["interactive"])

    if isinstance(paths := data.get("paths"), dict):
        allowed = paths.get("allowed")
        if isinstance(allowed, list):
            config.allowed_paths = [str(p) for p in allowed if isinstance(p, str)]

    if isinstance(git_section := data.get("git"), dict):
        if "branch" in git_section:
            branch = git_section["branch"]
            # Non-empty only: `branch = ""` in the shipped example means
            # "no override, fall back to PRD branchName", while
            # `KSTRL_BRANCH=""` below keeps its historical "explicit
            # skip". STRING_KEYS states both doors for the string rows.
            if isinstance(branch, str) and branch:
                config.kstrl_branch = branch
                config.kstrl_branch_explicit = True
        if "auto_checkout" in git_section:
            config.auto_checkout = bool(git_section["auto_checkout"])

    if isinstance(ui := data.get("ui"), dict) and "ascii" in ui:
        config.ascii_only = bool(ui["ascii"])


def _apply_env_overrides(config: KstrlConfig, root_dir: Path) -> None:
    """Mutate config in place from environment variables.

    Only env vars that are explicitly set in the environment are applied -
    unset vars leave the existing config value untouched.
    """
    if "MAX_ITERATIONS" in os.environ:
        config.max_iterations = int(os.environ["MAX_ITERATIONS"])
    for _section, _toml_key, env_var, field_name, is_path in STRING_KEYS:
        if (raw := os.environ.get(env_var)) is not None:
            setattr(config, field_name, _resolve_path(raw, root_dir) if is_path else raw)
    if "SLEEP_SECONDS" in os.environ:
        config.sleep_seconds = float(os.environ["SLEEP_SECONDS"])
    if "INTERACTIVE" in os.environ:
        config.interactive = _parse_bool(os.environ.get("INTERACTIVE"))
    if "ALLOWED_PATHS" in os.environ:
        config.allowed_paths = _parse_paths(os.environ.get("ALLOWED_PATHS"))
    if "KSTRL_BRANCH" in os.environ:
        config.kstrl_branch = os.environ["KSTRL_BRANCH"]
        config.kstrl_branch_explicit = True
    if "KSTRL_AUTO_CHECKOUT" in os.environ:
        config.auto_checkout = _parse_bool(os.environ.get("KSTRL_AUTO_CHECKOUT"))
    if "KSTRL_AGENT_BUDGET_USD" in os.environ:
        try:
            budget_value = float(os.environ["KSTRL_AGENT_BUDGET_USD"])
        except ValueError:
            budget_value = 0.0
        if budget_value > 0:
            config.agent_budget_usd = budget_value
    if "KSTRL_UI" in os.environ:
        config.ui_mode = os.environ["KSTRL_UI"]
    if "NO_COLOR" in os.environ:
        config.no_color = True
    if "KSTRL_ASCII" in os.environ:
        config.ascii_only = _parse_bool(os.environ.get("KSTRL_ASCII"))
