"""The rules that turn a rejected kstrl.toml section into one line (#863 C11).

``config_preflight`` checks every section at command entry and
``RunEnvelope`` resolves six of them at run start. Both go through the
rules in this module: which exceptions are the operator's input
(:data:`REJECTIONS`), which are a defect in kstrl (:func:`raise_if_defect`),
and how the line names the environment variable or the toml key at fault.
The module is in the ``config`` layer so that a lower layer can use it
without an import of ``cli``. The environment lock is here for the same
reason: the blame sweep clears ``os.environ``, which is process-wide.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, TypeVar

from kstrl.config import load_toml_section, resolve_config_file
from kstrl.config_section import ConfigSection

T = TypeVar("T")


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
REJECTIONS = (ValueError, TypeError, RuntimeError)

#: :data:`REJECTIONS` plus the read failure a long-lived surface has to
#: survive, for anything loading config AFTER command entry.
#:
#: ``OSError`` carries two unrelated rationales and both are why this
#: cannot be fixed by normalizing it inside ``load_toml_document``.
#: First, the entry check reads the document once itself and turns an
#: unreadable kstrl.toml into a ``ConfigError`` before any loader runs;
#: a screen re-reading the file minutes later has no such pass in front
#: of it, and a ``chmod`` between two refreshes raises ``OSError``
#: straight out of ``load_toml_section``. Second, a reader may open a
#: file that is not kstrl.toml at all: the stack's inbox is one, read by
#: ``stack.stack_in_force``.
SURFACE_REJECTIONS = (*REJECTIONS, OSError)


def raise_if_defect(exc: BaseException) -> None:
    """Re-raise ``exc`` when it is kstrl's bug, not the operator's file.

    :data:`REJECTIONS` names ``RuntimeError`` only for the domain errors
    that DERIVE from it - ``ServeError`` for ``[serve]
    max_consecutive_poison = 0``, and its ``QueueError``, ``InboxError``
    and ``IntakeError`` siblings - which are operator input and are
    reported as such. Everything else that arrives as a ``RuntimeError``
    is ours: reporting it as "configuration unreadable" blames the
    operator for our defect and eats the traceback that would locate it.

    The test is DERIVED, not a list. The first cut wrote ``type(exc) is
    RuntimeError``, which is true only of a bare one, so
    ``NotImplementedError`` and ``RecursionError`` - both direct
    ``RuntimeError`` subclasses, both unambiguously defects - were
    reported as the operator's broken file. A hand-written tuple of the
    four domain errors would have fixed those two and gone stale the
    next time a fifth is added, which is the failure mode this codebase
    keeps recording. So the question asked is "did kstrl define this
    class": ten ``RuntimeError`` subclasses live in ``kstrl/`` today and
    every one of them is a condition we chose to raise, while
    ``builtins`` and any dependency's ``RuntimeError`` is not something
    we modelled and so not something we can honestly blame a file for.
    ``tests/test_config_defects_not_blamed.py`` pins both halves.

    ``RecursionError`` is the one that needs an argument rather than a
    rule, and the argument is layering rather than inspection (#323). A
    kstrl.toml with 600 nested arrays exhausts the stack at the default
    recursion limit inside tomllib's recursive descent, and that is the
    operator's file, not a defect of ours. It never arrives here. Every
    tomllib parse in ``kstrl/`` ends on a bare ``except Exception``, and
    the two-layer census in ``tests/test_toml_readers.py`` is what makes
    that a closed set rather than a count restated here - layer 1,
    ``EXPECTED_TOMLLIB_SPELLINGS``, resolves nothing and so cannot miss
    a reader that spells ``tomllib`` at all. ``load_toml_document``
    re-raises ``ConfigError`` naming the path, and the pyproject.toml
    and ruff.toml readers in ``verify`` fall back to a default. So a
    ``RecursionError`` that does reach this function is a cycle in
    kstrl's own code, and the traceback this re-raise keeps is what
    locates it. The closure is
    over the PARSES, not over the call graph: not every guarded block
    goes through ``load_toml_document``.

    Inspecting the exception could not have settled it anyway. Measured
    on 3.12.8 and 3.13.2, both directions give
    ``builtins.RecursionError`` with ``str(exc)`` "maximum recursion
    depth exceeded" and no attribute of its own; only the frames differ,
    and by the time they could be read ``load_toml_document`` has
    already turned the document's into a ``ConfigError``.
    ``tests/test_recursion_provenance.py`` pins both directions at
    :func:`preflight_config`, and direction (b) again at
    :func:`config_problem_lines` and at the ``ks status`` seam.
    """
    if isinstance(exc, RuntimeError) and type(exc).__module__.split(".")[0] != "kstrl":
        raise exc


def resolve_or_report(
    section: ConfigSection[T],
    root_dir: Path,
    *,
    blame_env: bool,
) -> tuple[T, None] | tuple[None, str]:
    """One section, resolved, or the line the entry check would have printed.

    For a caller that already owns a ``toml_parse_scope``: a NESTED scope
    does not inherit the outer cache, it replaces it, so a caller that
    resolves several sections inside one scope pays one parse per section
    if each call opens its own. ``RunEnvelope.resolve`` is that caller
    (#192).

    Exactly one of the pair is ever None. ``blame_env`` is required
    rather than defaulted: see :func:`section_detail`.

    Wider than :data:`REJECTIONS` by ``OSError``: see
    :data:`SURFACE_REJECTIONS`.
    """
    try:
        return section.loader(root_dir), None
    except SURFACE_REJECTIONS as exc:
        # A RuntimeError kstrl did not define is a defect in kstrl,
        # not the operator's file. This is the same line
        # EvolutionConfig.load_or_none draws, and it is drawn here
        # too because that method's reason (a widening can only ever
        # swallow a defect) is about the exception, not the site.
        raise_if_defect(exc)
        return None, section_detail(
            section,
            resolve_config_file(root_dir),
            root_dir,
            exc,
            blame_env=blame_env,
        )


def section_detail(
    section: ConfigSection[Any],
    toml_path: Path,
    root_dir: Path,
    exc: Exception,
    *,
    blame_env: bool,
) -> str:
    """One line: which section, what the loader said, and which input.

    The environment is asked FIRST because the environment wins: with
    the same bad value in both places, the variable is the one taking
    effect, so naming the file's key would send the operator to a line
    that changing does not help. ``blame_env`` False skips that question
    entirely rather than answering it unsafely; the caller that passes
    False says why (``config_preflight.load_or_report``).
    """
    message = str(exc)
    # Two statements, not one expression: the environment-then-file
    # order is the rule the docstring above states, and an `or` split
    # across a ternary hides it.
    blamed = _blamed_env_var(section.loader, root_dir, message) if blame_env else None
    blamed = blamed or _blamed_toml_value(section.sections, toml_path, message)
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
    That gate keeps a file fault at one extra load rather than one per
    variable. Measured on a file fault with the 21 KB example config,
    INSIDE the parse scope: 0.54 ms with the gate against 0.84 ms
    without at 83 variables, and 0.95 against 2.09 at 303. The saving is
    small and linear in the size of the environment, which is the honest
    reason to keep it: CI environments are the big ones.

    An earlier version of this docstring claimed 34.3 ms for the
    ungated sweep. That was measured before ``toml_parse_scope`` covered
    the blame helpers, when every one of those loads reparsed the file.
    It is off by 40x now, which is why it is written down here re-taken
    rather than carried forward.

    Runs whenever a section is REJECTED, which includes a degrading
    section on an otherwise successful command. Both are before the
    command body, which is the property that matters: mutating
    ``os.environ`` is PROCESS-WIDE, and here nothing has been built and
    no other thread of ours is alive. That is the constraint
    ``scrubbed_environ``, reused here, already documents.
    """
    # The lock spans the WHOLE sweep, not just the scrubbed_environ
    # block: the per-variable pops below mutate os.environ outside it,
    # and they are the longer window of the two.
    with environ_lock():
        return _blame_sweep(loader, root_dir, message)


def _blame_sweep(
    loader: Callable[[Path], Any],
    root_dir: Path,
    message: str,
) -> str | None:
    """:func:`_blamed_env_var`'s body, under the environment lock."""
    with scrubbed_environ():
        try:
            loader(root_dir)
        except Exception:
            return None

    blamed: str | None = None
    for name in sorted(os.environ):
        saved = os.environ.pop(name)
        try:
            loader(root_dir)
        except Exception:
            # Still broken without it, or broken differently: either way
            # this variable is not the one thing to change.
            continue
        else:
            if blamed is not None:
                # A second variable that fixes it on its own. Neither is
                # "the one to change", so the sweep stops here rather
                # than finishing to discard the answer.
                return None
            # The value is echoed only where the loader's own message
            # already quotes it. Nothing has decided that an arbitrary
            # environment value may be printed, so nothing prints one.
            blamed = f"set by {name}={saved}" if repr(saved) in message else f"set by {name}"
        finally:
            os.environ[name] = saved
    return blamed


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

    The ``suppress`` below would swallow a ``RuntimeError`` kstrl did
    not define, which :func:`raise_if_defect` exists to re-raise. None
    has been reproduced: this runs only AFTER that function has passed on
    the exception being reported, and everything ``load_toml_section``
    raises here has already been converted by ``load_toml_document``.
    """
    hits = []
    for name in sections:
        with suppress(*SURFACE_REJECTIONS):
            for key, value in load_toml_section(toml_path, name).items():
                if repr(value) in message:
                    hits.append(f"kstrl.toml has [{name}] {key} = {value!r}")
    if len(hits) == 1:
        return hits[0]
    return None


#: Held for the whole of any block that blanks or pops ``os.environ``,
#: and by every thread of ours that READS ``KSTRL_*`` while another
#: thread might be doing so.
#:
#: A lock rather than a refusal, and the difference was measured. #289
#: first closed this race by adding the app's safe-mode worker to
#: ``env_scrub_is_safe``'s refusal condition, which turned two working
#: surfaces intermittent: on an EMPTY project the worker's flag is set
#: for 51 to 84 ms out of every 5 s, so the config screen's refresh was
#: denied at random with a message falsely blaming a launched run, and
#: the evolve banner silently dropped the environment variable it was
#: supposed to name. On a project whose events.jsonl makes the check
#: exceed its own interval the flag never clears and the refusal is
#: permanent. Waiting 84 ms is not a cost worth making a feature
#: unreliable to avoid.
#:
#: RLock, not Lock: ``_blamed_env_var`` holds this across a
#: ``scrubbed_environ`` block that takes it again.
_ENVIRON_LOCK = threading.RLock()


@contextmanager
def environ_lock() -> Iterator[None]:
    """Exclusive access to ``os.environ`` against other kstrl threads.

    Take it around a block that MUTATES the environment, and around a
    read of ``KSTRL_*`` on any thread that could run beside one. It
    cannot help against a subprocess inheriting the environment.
    """
    with _ENVIRON_LOCK:
        yield


@contextmanager
def scrubbed_environ() -> Iterator[None]:
    """Temporarily clear os.environ so a loader sees toml + defaults only.

    A field whose value changes when the environment disappears was
    env-set. PROCESS-WIDE, and therefore serialized on
    :func:`environ_lock`: see the docstring of ``config_report`` for the thread warning.
    """
    with _ENVIRON_LOCK:
        saved = dict(os.environ)
        os.environ.clear()
        try:
            yield
        finally:
            os.environ.clear()
            os.environ.update(saved)
