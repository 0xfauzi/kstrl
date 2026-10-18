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

NAMES NO LOADER READS
---------------------
The same argument decides which names are known (#525). During the
check the document is served as a ``config_toml.RecordedTable``, which
records every name a loader asks for; a name in the file that no loader
asked for is refused, as is a section written as a value. So the known
names are whatever the loaders read, and a key added to a loader is
known without a second list to update.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from kstrl.acceptance import CONTRACT_SECTION
from kstrl.agents import SANDBOX_SECTION
from kstrl.architect import LINEAR_SECTION
from kstrl.config import (
    KSTRL_SECTION,
    REJECTIONS,
    RETIRED_ENV_VALUES,
    RETIRED_ENV_VARS,
    RETIRED_KEYS,
    RETIRED_SECTIONS,
    RETIRED_VALUES,
    SURFACE_REJECTIONS,
    ConfigError,
    ConfigSection,
    RecordedTable,
    raise_if_defect,
    record_reads,
    resolve_config_file,
    section_detail,
    toml_parse_scope,
)
from kstrl.divergence import DIVERGENCE_SECTION
from kstrl.events import NOTIFY_SECTION
from kstrl.factory_config import FACTORY_SECTION
from kstrl.intake_gh import INTAKE_GITHUB_SECTION
from kstrl.knowledge import KNOWLEDGE_SECTION
from kstrl.learning import AUTONOMY_SECTION, EVOLUTION_SECTION
from kstrl.loop import BREAKER_SECTION
from kstrl.owner import INBOX_SECTION
from kstrl.proc import TIMEOUT_SECTION
from kstrl.project import CODEBASE_SCAN_SECTION, STACK_SECTION, StackError
from kstrl.queue import QUEUE_SECTION
from kstrl.release import RELEASE_CONFIG_SECTION
from kstrl.review import SECURITY_SECTION
from kstrl.serve_config import SERVE_SECTION
from kstrl.signals import SIGNALS_SECTION
from kstrl.verify import POLICY_SECTION, VERIFY_SECTION


def config_sections() -> list[ConfigSection[Any]]:
    """Every configuration section kstrl reads, with its loader.

    Each module that owns a section declares its :class:`ConfigSection`
    constant beside the loader (#863 C11). This list only orders them.

    ``tests/test_config_preflight.py`` walks ``kstrl/`` for config
    dataclasses and fails if one is missing from this list, so a section
    added later cannot quietly go unchecked.
    """
    return [
        KSTRL_SECTION,
        # Before the three loaders that also read [stack] (factory, verify,
        # contract), so a bad stack is reported here, under its own name.
        STACK_SECTION,
        FACTORY_SECTION,
        VERIFY_SECTION,
        SECURITY_SECTION,
        CONTRACT_SECTION,
        POLICY_SECTION,
        AUTONOMY_SECTION,
        DIVERGENCE_SECTION,
        BREAKER_SECTION,
        SANDBOX_SECTION,
        TIMEOUT_SECTION,
        CODEBASE_SCAN_SECTION,
        KNOWLEDGE_SECTION,
        QUEUE_SECTION,
        INBOX_SECTION,
        INTAKE_GITHUB_SECTION,
        SERVE_SECTION,
        NOTIFY_SECTION,
        LINEAR_SECTION,
        EVOLUTION_SECTION,
        RELEASE_CONFIG_SECTION,
        SIGNALS_SECTION,
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
        # The document first: a file that will not parse breaks every
        # section, and one line naming the fault beats 22 saying so.
        # TWO faults reach here, not one - a syntax error and a
        # non-utf-8 byte (#318) - and only the first carries a line
        # and column. Both arrive as ``ConfigError`` and pass straight
        # through this ``except`` on purpose. No file at all is not a
        # fault: the retired-name check still has to run (the
        # environment-variable half does not depend on a document),
        # so an absent file reads as the empty document.
        if toml_path.exists():
            try:
                document = record_reads(toml_path)
            except OSError as exc:
                raise ConfigError(f"{toml_path} could not be read: {exc}") from exc
        else:
            document = RecordedTable({})
        problems.extend(retired_name_problems(document, toml_path))

        # Names another line already reports: a retired section, and every
        # section of a rejected loader, which stopped asking part way.
        reported = set(RETIRED_SECTIONS)
        for section in config_sections():
            try:
                section.loader(root_dir)
            except REJECTIONS as exc:
                repeat = _repeats_the_stack_line(exc, reported)
                reported.update(section.sections)
                # Same rule as every other catcher of this tuple: a
                # RuntimeError kstrl did not define is our defect, and
                # listing it under "configuration problems" blames the
                # operator's file for it. This is the seam all three
                # reporting surfaces route through, so the hole would
                # have been one call deep from each of them.
                raise_if_defect(exc)
                if repeat:
                    continue
                detail = section_detail(section, toml_path, root_dir, exc, blame_env=True)
                if section.fatal or not required.isdisjoint(section.sections):
                    problems.append(detail)
                else:
                    warn(f"{detail} - continuing without it")
        problems.extend(unread_name_problems(document, toml_path, reported))
    return problems


def _repeats_the_stack_line(exc: Exception, reported: set[str]) -> bool:
    """Whether ``exc`` is a ``[stack]`` fault the entry check already reported.

    #696: three loaders read the stack for their own sections (factory,
    verify, contract) and each raises the same :class:`StackError`. The
    ``[stack]`` section runs before them, so its line is the one kept.
    """
    return isinstance(exc, StackError) and "stack" in reported


def unread_name_problems(
    document: RecordedTable,
    toml_path: Path,
    reported: set[str],
) -> list[str]:
    """Every kstrl.toml name no loader asked for, and every section
    written as a value (#525).

    A name in ``reported`` is skipped: another line already names it.
    """
    problems: list[str] = []
    for name, value in dict.items(document):
        if name in reported:
            continue
        if name not in document.asked:
            label = f"[{name}]" if isinstance(value, dict) else name
            problems.append(
                f"{toml_path} names {label}, which no kstrl setting reads. "
                "Remove it or correct the spelling; kstrl will not guess."
            )
        elif not isinstance(value, RecordedTable):
            problems.append(
                f"{toml_path} sets {name} = {value!r}, but kstrl reads [{name}] "
                f"as a table; write it as a [{name}] section with its keys under it"
            )
        else:
            problems.extend(
                f"{toml_path} names [{name}] {key}, which no kstrl setting reads. "
                "Remove it or correct the spelling; kstrl will not guess."
                for key in value
                if key not in value.asked
            )
    return problems


def retired_value_problems(document: Mapping[str, Any], toml_path: Path) -> list[str]:
    """Every retired VALUE of a kstrl.toml key or environment variable in play."""
    problems: list[str] = []
    for (section, key, value), why in RETIRED_VALUES.items():
        table = document.get(section)
        if isinstance(table, Mapping) and str(table.get(key)).strip().lower() == value:
            problems.append(
                f"{toml_path} names [{section}] {key} = {value!r}, which was {why}. "
                "kstrl will not guess."
            )
    for (name, value), why in sorted(RETIRED_ENV_VALUES.items()):
        if os.environ.get(name, "").strip().lower() == value:
            problems.append(
                f"the environment sets {name}={value}, which was {why}. kstrl will not guess."
            )
    return problems


def retired_name_problems(document: Mapping[str, Any], toml_path: Path) -> list[str]:
    """Every retired kstrl.toml name and environment variable in play.

    #395 renamed a set of configuration names. A straight rename would
    leave an operator's existing file parsing, doing nothing, and a
    blocking gate reverting to advisory. So a retired name is REFUSED by
    name, and the message says what to rename it to, which the generic
    refusal of :func:`unread_name_problems` cannot. No alias layer: two
    spellings live forever and the old one never dies.
    """
    problems: list[str] = []
    for old, new in RETIRED_SECTIONS.items():
        if old in document:
            problems.append(
                f"{toml_path} names [{old}], which was renamed to [{new}]. "
                "Rename the section; kstrl will not guess."
            )
    for (section, key), why in RETIRED_KEYS.items():
        table = document.get(section)
        if isinstance(table, Mapping) and key in table:
            problems.append(
                f"{toml_path} names [{section}] {key}, which was {why}. kstrl will not guess."
            )
    problems.extend(retired_value_problems(document, toml_path))
    for old, why in sorted(RETIRED_ENV_VARS.items()):
        if old in os.environ:
            problems.append(f"the environment sets {old}, which was {why}. kstrl will not guess.")
    return problems


def config_problem_lines(
    root_dir: Path,
    *,
    warn: Callable[[str], None],
) -> list[str]:
    """Every line the entry check would print for ``root_dir``.

    :func:`collect_config_problems` with the one failure it does not
    return folded back in: a document that will not parse raises, and
    the answer to "what is wrong with this configuration" is then that
    parse error and nothing else, because no section could be resolved
    behind it.

    Split out because two surfaces explained a broken config, and each
    carried a copy of this. The copies had already drifted in their
    handling of the empty case. ``ks config show`` and ``ks doctor``
    call it now. Each caller supplies its own ``warn``.
    """
    try:
        return collect_config_problems(root_dir, warn=warn)
    except SURFACE_REJECTIONS as exc:
        raise_if_defect(exc)
        return [str(exc)]
