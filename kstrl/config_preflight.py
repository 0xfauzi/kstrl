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

from kstrl.config import (
    ConfigError,
    load_toml_document,
    load_toml_section,
    resolve_config_file,
    toml_parse_scope,
)
from kstrl.config_report import scrubbed_environ

#: Exceptions a loader raises for input the operator has to fix, and the
#: complete set of them: these loaders read a file and coerce values, so
#: anything outside this tuple is a defect in kstrl and keeps its
#: traceback rather than being reported as the operator's fault.
#:
#: ``ValueError`` is the house type (``ConfigError``,
#: ``PolicyConfigError`` and ``BudgetConfigError`` all derive from it).
#: ``BudgetConfigError`` is deliberately collected like any other and
#: not re-raised: re-raising it abandoned the traversal at ``[factory]``,
#: which is second in the list, so one bad ceiling hid every later
#: section and the operator fixed the ceiling only to meet ``[verify]``
#: on the next run. ``_KstrlGroup.invoke`` still renders it for the
#: paths that raise it outside this check, such as ``--max-cost-usd``.
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
    #: False for a section whose failure degrades rather than stops the
    #: command. Exactly one entry sets it; see the module docstring.
    fatal: bool = True

    @property
    def label(self) -> str:
        return "/".join(f"[{name}]" for name in self.sections)


def config_sections() -> list[ConfigSection]:
    """Every configuration section kstrl reads, with its loader.

    Imports are deferred the way ``config_report._phase_sections`` defers
    them, and the reason is ordering, not latency: this module is
    imported by ``kstrl.cli``, which several of these import from.
    Measured on this tree, only four of the twenty-two are new work
    (evolution, intake_github with workqueue, and serve; the rest arrive
    with ``kstrl.cli``), costing about 7 ms warm on a 151 ms process.

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

    return [
        ConfigSection(("agent", "run", "paths", "git", "ui"), KstrlConfig.load),
        ConfigSection(("factory",), FactoryConfig.load),
        ConfigSection(("verify",), VerifyConfig.load),
        ConfigSection(("security",), SecurityConfig.load),
        ConfigSection(("contract",), ContractConfig.load),
        ConfigSection(("adequacy",), AdequacyConfig.load),
        ConfigSection(("policy",), PolicyConfig.load),
        ConfigSection(("autonomy",), AutonomyConfig.load),
        ConfigSection(("divergence",), DivergenceConfig.load),
        ConfigSection(("breaker",), BreakerConfig.load),
        ConfigSection(("sandbox",), SandboxConfig.load),
        ConfigSection(("timeout",), TimeoutConfig.load),
        ConfigSection(("feedforward",), FeedforwardConfig.load),
        ConfigSection(("knowledge",), KnowledgeConfig.load),
        ConfigSection(("fixtures",), FixturesConfig.load),
        ConfigSection(("queue",), QueueConfig.load),
        ConfigSection(("inbox",), InboxConfig.load),
        ConfigSection(("intake_github",), GitHubIntakeConfig.load),
        ConfigSection(("serve",), ServeConfig.load),
        ConfigSection(("notify",), NotifyConfig.load),
        ConfigSection(("linear",), LinearConfig.load),
        ConfigSection(("evolution",), EvolutionConfig.load, fatal=False),
    ]


def preflight_config(
    root_dir: Path,
    warn: Callable[[str], None],
    *,
    required: frozenset[str] = frozenset(),
) -> None:
    """Resolve every configuration section, or say exactly what to fix.

    Raises :class:`ConfigError` naming the section, the offending input
    and the loader's own message. Degrading sections (see the module
    docstring) go to ``warn`` instead and the command continues.

    ``required`` promotes named sections to fatal for THIS caller. It
    exists because "degrading" means "an audit trail attached to work
    that is about something else": ``ks evolve`` IS the journal, so it
    passes ``{"evolution"}`` and gets the error line, with the key and
    the offending value, instead of a warning followed two lines later
    by the traceback the warning promised would not come. A command
    that is ABOUT a section declares that here rather than remembering
    its own guard.
    """
    problems = collect_config_problems(root_dir, warn, required=required)
    if problems:
        raise ConfigError(
            "configuration rejected before anything was started; "
            "fix it and run again:\n  " + "\n  ".join(problems)
        )


def collect_config_problems(
    root_dir: Path,
    warn: Callable[[str], None],
    *,
    required: frozenset[str] = frozenset(),
) -> list[str]:
    """:func:`preflight_config`, but returning the problems instead of
    raising on them.

    Split out for ``ks config show``, which has to REPORT every rejected
    section next to the rows it could resolve rather than stop at the
    first one. A raising check and a reporting one reading different
    section lists is exactly the drift this module exists to prevent, so
    there is one traversal and the raise sits on top of it.

    A malformed document still raises: no section can be resolved when
    the file will not parse, so there is nothing to report beside.
    """
    toml_path = resolve_config_file(root_dir)
    problems: list[str] = []
    # One parse of the file for the whole check, blame helpers included.
    # Without the scope the 22 loaders lex the same bytes 22 times:
    # measured on the shipped 21 KB kstrl.toml.example, this check costs
    # 9.4 ms without it and 0.6 ms with it.
    with toml_parse_scope():
        if toml_path.exists():
            # The document first: a syntax error breaks every section,
            # and one line naming the line and column beats 22 saying so.
            try:
                load_toml_document(toml_path)
            except OSError as exc:
                raise ConfigError(f"{toml_path} could not be read: {exc}") from exc

        for section in config_sections():
            try:
                section.loader(root_dir)
            except _REJECTIONS as exc:
                detail = _detail(section, toml_path, root_dir, exc)
                if section.fatal or not required.isdisjoint(section.sections):
                    problems.append(detail)
                else:
                    warn(f"{detail} - continuing without it")
    return problems


def _detail(
    section: ConfigSection,
    toml_path: Path,
    root_dir: Path,
    exc: Exception,
) -> str:
    """One line: which section, what the loader said, and which input.

    The environment is asked FIRST because the environment wins: with
    the same bad value in both places, the variable is the one taking
    effect, so naming the file's key would send the operator to a line
    that changing does not help.
    """
    message = str(exc)
    blamed = _blamed_env_var(section.loader, root_dir, message) or _blamed_toml_value(
        section.sections,
        toml_path,
        message,
    )
    line = f"{section.label} {message}"
    return f"{line} ({blamed})" if blamed else line


def _blamed_env_var(
    loader: Callable[[Path], Any],
    root_dir: Path,
    message: str,
) -> str | None:
    """The environment variable whose REMOVAL makes this loader accept
    the configuration, if EXACTLY ONE does.

    Measured, not guessed: a variable is named only when taking it out
    of the environment demonstrably fixes the load. The sweep runs to
    the end and reports nothing when two variables each fix it on their
    own, because then neither is "the one to change" and naming the
    alphabetically first contradicts the message beside it. Reproduced
    on ``[linear]`` with ``KSTRL_LINEAR_ENABLED=1`` and an empty
    ``KSTRL_LINEAR_TEAM_ID``: removing either satisfies the loader, and
    the earlier code blamed ENABLED while the message told the operator
    to set TEAM_ID.

    An EMPTY environment is tried first, and a loader that still fails
    there ends the search: the file is at fault, and no variable can be.
    That gate is what keeps a file fault at one extra load rather than
    one per variable (measured with 83 variables set: 34.3 ms of
    fruitless sweep before the gate, 0.5 ms after).

    Runs whenever a section is REJECTED, which includes a degrading
    section on an otherwise successful command. Both are before the
    command body, which is the property that matters: mutating
    ``os.environ`` is PROCESS-WIDE, and here nothing has been built and
    no other thread of ours is alive. That is the constraint
    ``config_report.scrubbed_environ``, reused here, already documents.
    """
    with scrubbed_environ():
        try:
            loader(root_dir)
        except Exception:
            return None

    culprits: list[str] = []
    for name in sorted(os.environ):
        saved = os.environ.pop(name)
        try:
            loader(root_dir)
        except Exception:
            # Still broken without it, or broken differently: either way
            # this variable is not the one thing to change.
            continue
        else:
            # The value is echoed only where the loader's own message
            # already quotes it. Nothing has decided that an arbitrary
            # environment value may be printed, so nothing prints one.
            culprits.append(
                f"set by {name}={saved}" if repr(saved) in message else f"set by {name}"
            )
        finally:
            os.environ[name] = saved
    return culprits[0] if len(culprits) == 1 else None


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
