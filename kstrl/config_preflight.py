"""One configuration check, at command entry, before anything is spent.

kstrl.toml used to be parsed lazily, by whichever config dataclass first
needed its section, at whatever point in the run that class happened to
be constructed. A typo therefore did not fail at startup: it failed at
the first loader that reached the bad section. On the decompose path one
of those loaders is ``LinearConfig.load``, which runs AFTER the
architect agent has been invoked and paid for - measured at 119 to 210
seconds against a frontier model on a real spec - so a syntax error in
kstrl.toml, or a bad value in a section the architect never needed,
spent the architect and then aborted (#272).

The blast radius of a typo therefore depended on which section it was in
and which command was run. Nobody chose that property. This module
replaces it with one: EVERY section is resolved once, at command entry,
before the command body constructs anything.

FATAL VERSUS DEGRADING
----------------------
Not every section can honestly be degraded past, and the difference is
not about how likely the failure is - it is about what continuing would
mean.

- ``[evolution]`` configures an optional AUDIT TRAIL. Losing the journal
  degrades the record and nothing else, so continuing without it is
  honest, and four mid-run call sites already do exactly that through
  ``EvolutionConfig.load_or_none``. This preflight makes the same call
  earlier and louder: the warning arrives at startup instead of in the
  middle of paid work.
- Every other section configures a GATE, a BUDGET, a BOUNDARY or a
  DESTINATION. Substituting defaults for a verify command, a security
  threshold, a policy envelope or a cost ceiling the operator
  configured is a semantic substitution: the run proceeds, reports
  success, and was measured by something other than what was asked for.
  CLAUDE.md names that failure directly ("No silent semantic
  substitution. Retry identically or surface the failure"), so these
  fail the command instead.

WHY THE LOADERS THEMSELVES, NOT A SCHEMA
----------------------------------------
The check calls each dataclass's own ``load(root_dir)``. That is the
same code the run will use later, so the preflight cannot drift from
what is actually enforced - the drift failure this codebase has already
recorded twice (see ``config.reconcile_progress_config``). It also means
ENV coercion is covered for free: ``load`` overlays env on top of toml,
so ``KSTRL_SECURITY_TIMEOUT=many`` is rejected here by the same
``float()`` that would have rejected it mid-run. A toml-only preflight
would have missed both of the failures measured on #272.

The per-section ``from_env()`` / ``load(root_dir)`` convention is
untouched: this module is a caller of it, not a replacement for it.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kstrl.config import ConfigError, _load_toml, load_toml_section, resolve_config_file

#: Exceptions a loader raises for input the operator has to fix, and the
#: complete set of them: these loaders read a file and coerce values, so
#: anything outside this tuple is a defect in kstrl and keeps its
#: traceback rather than being reported as the operator's fault.
#:
#: ``ValueError`` is the house type (``ConfigError``,
#: ``PolicyConfigError`` and ``BudgetConfigError`` all derive from it).
#: ``TypeError`` joins it because ``float(["600"])`` - a toml array where
#: a number belongs - raises that instead. ``RuntimeError`` is there for
#: the domain errors that derive from IT: ``ServeError`` rejects
#: ``[serve] max_consecutive_poison = 0`` that way, and ``QueueError``,
#: ``InboxError`` and ``IntakeError`` are its siblings.
_REJECTIONS = (ValueError, TypeError, RuntimeError)


@dataclass(frozen=True)
class ConfigSection:
    """One kstrl.toml section (or group of them) and how it is loaded.

    ``sections`` is a tuple because ``KstrlConfig`` fans out over five
    toml tables; every other entry names exactly one. ``fatal`` records
    the classification argued in the module docstring.
    """

    sections: tuple[str, ...]
    loader: Callable[[Path], Any]
    fatal: bool

    @property
    def label(self) -> str:
        return "/".join(f"[{name}]" for name in self.sections)


def config_sections() -> list[ConfigSection]:
    """Every configuration section kstrl reads, with its loader.

    Imports are deferred the way ``config_report._phase_sections`` defers
    them. Measured on this tree, importing the twenty modules not already
    pulled in by ``kstrl.cli`` costs under 5ms warm, so the reason is
    ordering (this module is imported by ``kstrl.cli``, which several of
    these import from) rather than latency.

    ``tests/test_config_preflight.py`` walks ``kstrl/`` for config
    dataclasses and fails if one is missing from this list, so a section
    added later cannot quietly go unchecked.
    """
    from kstrl.adequacy import AdequacyConfig
    from kstrl.autonomy import AutonomyConfig
    from kstrl.breaker import BreakerConfig
    from kstrl.config import KstrlConfig
    from kstrl.contract import ContractConfig
    from kstrl.divergence import DivergenceConfig
    from kstrl.evolution import EvolutionConfig
    from kstrl.factory import FactoryConfig
    from kstrl.feedforward import FeedforwardConfig
    from kstrl.fixtures import FixturesConfig
    from kstrl.inbox import InboxConfig
    from kstrl.intake_github import GitHubIntakeConfig
    from kstrl.knowledge import KnowledgeConfig
    from kstrl.linear import LinearConfig
    from kstrl.observability import NotifyConfig
    from kstrl.policy import PolicyConfig
    from kstrl.sandbox import SandboxConfig
    from kstrl.security import SecurityConfig
    from kstrl.serve import ServeConfig
    from kstrl.timeout import TimeoutConfig
    from kstrl.verify import VerifyConfig
    from kstrl.workqueue import QueueConfig

    fatal: list[tuple[tuple[str, ...], Callable[[Path], Any]]] = [
        (("agent", "run", "paths", "git", "ui"), KstrlConfig.load),
        (("factory",), FactoryConfig.load),
        (("verify",), VerifyConfig.load),
        (("security",), SecurityConfig.load),
        (("contract",), ContractConfig.load),
        (("adequacy",), AdequacyConfig.load),
        (("policy",), PolicyConfig.load),
        (("autonomy",), AutonomyConfig.load),
        (("divergence",), DivergenceConfig.load),
        (("breaker",), BreakerConfig.load),
        (("sandbox",), SandboxConfig.load),
        (("timeout",), TimeoutConfig.load),
        (("feedforward",), FeedforwardConfig.load),
        (("knowledge",), KnowledgeConfig.load),
        (("fixtures",), FixturesConfig.load),
        (("queue",), QueueConfig.load),
        (("inbox",), InboxConfig.load),
        (("intake_github",), GitHubIntakeConfig.load),
        (("serve",), ServeConfig.load),
        (("notify",), NotifyConfig.load),
        (("linear",), LinearConfig.load),
    ]
    entries = [ConfigSection(names, loader, fatal=True) for names, loader in fatal]
    entries.append(ConfigSection(("evolution",), EvolutionConfig.load, fatal=False))
    return entries


def preflight_config(root_dir: Path, warn: Callable[[str], None]) -> None:
    """Resolve every configuration section, or say exactly what to fix.

    Raises :class:`ConfigError` naming the section, the offending input
    and the loader's own message. Degrading sections (see the module
    docstring) go to ``warn`` instead and the command continues.

    Deliberately NOT swallowed here: ``BudgetConfigError``, which
    ``_KstrlGroup.invoke`` already renders with its own message, and
    which is about a value that parses fine and cannot bound anything.
    """
    from kstrl.factory import BudgetConfigError

    toml_path = resolve_config_file(root_dir)
    if toml_path.exists():
        # The document first: a syntax error breaks every section, and
        # one line naming the line and column beats twenty-two saying so.
        try:
            _load_toml(toml_path)
        except OSError as exc:
            raise ConfigError(f"{toml_path} could not be read: {exc}") from exc

    problems: list[str] = []
    for section in config_sections():
        try:
            section.loader(root_dir)
        except BudgetConfigError:
            raise
        except _REJECTIONS as exc:
            detail = _detail(section, root_dir, exc)
            if section.fatal:
                problems.append(detail)
            else:
                warn(f"{detail} - continuing without it")

    if problems:
        raise ConfigError(
            "configuration rejected before anything was started; "
            "fix it and run again:\n  " + "\n  ".join(problems)
        )


def _detail(section: ConfigSection, root_dir: Path, exc: Exception) -> str:
    """One line: which section, what the loader said, and which input."""
    message = str(exc)
    blamed = _blamed_env_var(section.loader, root_dir) or _blamed_toml_value(
        section.sections,
        resolve_config_file(root_dir),
        message,
    )
    line = f"{section.label} {message}"
    return f"{line} ({blamed})" if blamed else line


def _blamed_env_var(loader: Callable[[Path], Any], root_dir: Path) -> str | None:
    """The environment variable whose REMOVAL makes this loader accept
    the configuration, if exactly one does.

    Measured, not guessed: the variable is named only when taking it out
    of the environment demonstrably fixes the load. Nothing is reported
    when the fault is in the file, or when two inputs are wrong at once.

    Mutating ``os.environ`` is PROCESS-WIDE, so this runs only on the
    error path at command entry, where the command body has not started
    and no other thread of ours is alive - the same constraint
    ``config_report.scrubbed_environ`` documents.
    """
    for name in sorted(os.environ):
        saved = os.environ.pop(name)
        try:
            loader(root_dir)
        except Exception:
            # Still broken without it, or broken differently: either way
            # this variable is not the one thing to change.
            continue
        else:
            return f"set by {name}={saved}"
        finally:
            os.environ[name] = saved
    return None


def _blamed_toml_value(
    sections: tuple[str, ...],
    toml_path: Path,
    message: str,
) -> str | None:
    """The kstrl.toml key holding the value the loader complained about.

    Python's coercion errors quote the offending value verbatim ("could
    not convert string to float: 'many'"), so a key in these sections
    whose value reprs into that message is the one to look at. Reported
    only when exactly one key matches, and phrased as what the file
    says rather than as a diagnosis.
    """
    hits = []
    for name in sections:
        with suppress(*_REJECTIONS, OSError):
            for key, value in load_toml_section(toml_path, name).items():
                if repr(value) in message:
                    hits.append(f"kstrl.toml has [{name}] {key} = {value!r}")
    if len(hits) == 1:
        return hits[0]
    return None
