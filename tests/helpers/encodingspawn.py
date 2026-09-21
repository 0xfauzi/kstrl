"""#409: every text-mode child-process decode in ``kstrl/`` names utf-8.

THE RULE, in one sentence: a ``subprocess`` call that decodes its child's
bytes names ``encoding="utf-8"`` and does not weaken the decode with
``errors=``.

WHY THIS IS A SIBLING OF ``encodingwalk`` AND NOT PART OF IT. #320's rule is
about files KSTRL WRITES, and it has two halves: name utf-8, and answer for
the decode with a handler that catches ``UnicodeDecodeError``. This rule has
one half. A child's stdout is produced by git, gh, uv or a model, so bytes
that are not utf-8 coming back are a genuine fault the caller should see
rather than a file kstrl must be able to read back. Sharing
``encodingwalk.Read`` would mean carrying ``guard_fault`` and ``unproven``
fields that are permanently None. What IS shared is the bottom layer:
``encodingrules`` answers the codec and strictness questions for both, by
asking ``codecs`` and CPython rather than by listing spellings.

TEXT MODE IS CPYTHON'S DEFINITION, NOT A LIST.
``inspect.getsource(subprocess.Popen.__init__)`` reads
``self.text_mode = encoding or errors or text or universal_newlines``, and
all five shapes were checked against a real child under ``LC_ALL=C
LANG=C PYTHONUTF8=0``. A ``text=`` or ``universal_newlines=`` value this
walk cannot fold is treated as TEXT, which is the reporting direction.

THE TARGET SET IS DERIVED. Every public callable in ``subprocess`` whose
signature names ``encoding`` or forwards ``**kwargs`` is a spawn entry
point that can decode: measured, exactly ``Popen``, ``call``,
``check_call``, ``check_output``, ``getoutput``, ``getstatusoutput`` and
``run``. A hand-written list is what #340 and #324 merged into a tree with
``SPAWN_FUNCS`` at zero readers and ``getoutput`` ungated, and CLAUDE.md
records that merge. ``tests/test_encoding_readers.py`` pins the derivation's
output against ``tests/test_timeout_enforcement.py``'s own set, so the two
cannot drift apart in silence.

CLEARING IS THE DANGEROUS DIRECTION. CLAUDE.md guard rule 3: a guard that
CLEARS must be narrow. Every step that cannot reach an answer here reports
rather than clears:

    a ``text=`` that does not fold      -> reported
    an ``encoding=`` that does not fold -> reported (cannot prove utf-8)
    an ``errors=`` that does not fold   -> treated as strict, then lenient
                                           only if it folds to a lenient value
    a callee the resolver cannot decide -> ``undecided``, a pinned row
"""

from __future__ import annotations

import ast
import functools
import inspect
import subprocess
from dataclasses import dataclass

from tests.helpers.astwalk import (
    Sites,
    calls_to,
    label,
    module_name,
    package_sources,
    parse,
    resolved_calls,
)
from tests.helpers.encodingrules import encoding_fault, is_strict, keyword_of


def _spawn_entry_points() -> frozenset[str]:
    """Every ``subprocess`` callable that can decode a child's output.

    DERIVED from the module, in the house style of ``encodingrules``: a
    signature that names ``encoding`` takes one directly, and one that
    forwards ``**kwargs`` hands it to ``Popen``. Exception classes are
    excluded because they are callable and spawn nothing.
    """
    found = set()
    for name in dir(subprocess):
        if name.startswith("_"):
            continue
        obj = getattr(subprocess, name)
        if not callable(obj) or (isinstance(obj, type) and issubclass(obj, BaseException)):
            continue
        try:
            params = inspect.signature(obj).parameters
        except (TypeError, ValueError):
            continue
        if "encoding" in params or any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
        ):
            found.add(f"subprocess.{name}")
    return frozenset(found)


#: The seven, derived once. Pinned in ``tests/test_encoding_readers.py`` so a
#: CPython release that drops one is loud rather than silent.
SPAWN_TARGETS = _spawn_entry_points()


def text_mode(node: ast.Call) -> bool | None:
    """Does this spawn decode its child's output? None when undecidable.

    ``encoding`` or ``errors`` present settles it on its own, whatever
    ``text`` says, because CPython's expression is an ``or``. Only when
    neither is present does a ``text=``/``universal_newlines=`` value have to
    fold, and one that does not fold returns None, which the caller reports.
    """
    if keyword_of(node, "encoding") is not None or keyword_of(node, "errors") is not None:
        return True
    flags: list[bool] = []
    for name in ("text", "universal_newlines"):
        value = keyword_of(node, name)
        if value is None:
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, bool):
            flags.append(value.value)
        else:
            return None
    return any(flags)


@dataclass(frozen=True)
class SpawnScan:
    """Every spawn in one module, partitioned with no fifth bucket."""

    #: One row per compliant text-mode spawn.
    clear: tuple[str, ...] = ()
    #: One row per text-mode spawn this walk could not clear, with the reason.
    reported: tuple[str, ...] = ()
    #: One row per call it could not even resolve.
    undecided: tuple[str, ...] = ()
    #: One row per text-mode spawn that names utf-8 and decodes LENIENTLY.
    #: Pinned separately rather than cleared, so ``errors=`` cannot be used
    #: to quiet a site (#409 plant 2).
    lenient: tuple[str, ...] = ()
    #: One row per spawn that decodes nothing. Pinned, so a text-mode site
    #: silently becoming bytes moves a list somebody reads.
    bytes_mode: tuple[str, ...] = ()
    #: The module label of every TEXT-MODE spawn, one entry per site, for the
    #: counting pin. The row pins above deduplicate and so cannot count.
    text_sites: tuple[str, ...] = ()

    def __add__(self, other: SpawnScan) -> SpawnScan:
        return SpawnScan(
            self.clear + other.clear,
            self.reported + other.reported,
            self.undecided + other.undecided,
            self.lenient + other.lenient,
            self.bytes_mode + other.bytes_mode,
            self.text_sites + other.text_sites,
        )


def scan_source(text: str, *, where: str = "", module: str = "") -> SpawnScan:
    """Every ``subprocess`` spawn in one module's source, partitioned.

    Takes TEXT rather than a path so the planted shapes in
    ``tests/test_encoding_readers.py`` exercise the same code the package
    sweep runs. ``resolved_calls`` answers the SEEN half only, so
    ``calls_to(...).undecided`` is taken over the same tree in the same
    function: the rule ``tests/test_astwalk_mechanisms.py`` enforces.
    """
    if "subprocess" not in text:
        return SpawnScan()
    tree = parse(text)
    clear: list[str] = []
    reported: list[str] = []
    lenient: list[str] = []
    bytes_mode: list[str] = []
    text_sites: list[str] = []
    for node, _origin in resolved_calls(tree, SPAWN_TARGETS, module=module):
        row = f"{where}:{node.lineno} {ast.unparse(node)[:70]}"
        mode = text_mode(node)
        if mode is None:
            reported.append(f"{row} (text mode does not fold, so a decode is unproven)")
            continue
        if not mode:
            bytes_mode.append(row)
            continue
        text_sites.append(where)
        fault = encoding_fault(node)
        if fault is not None:
            reported.append(f"{row} {fault}")
        elif not is_strict(node):
            lenient.append(row)
        else:
            clear.append(row)
    undecided = calls_to(tree, SPAWN_TARGETS, where=where, module=module).undecided
    return SpawnScan(
        tuple(clear),
        tuple(reported),
        undecided,
        tuple(lenient),
        tuple(bytes_mode),
        tuple(text_sites),
    )


@functools.cache
def package_scan() -> SpawnScan:
    """One sweep of ``kstrl/``, cached because six tests ask for it."""
    total = SpawnScan()
    for source in package_sources():
        total = total + scan_source(
            source.read_text(encoding="utf-8"),
            where=label(source),
            module=module_name(source),
        )
    return total


def reported_spawns(scan: SpawnScan) -> Sites:
    """The reported half as :class:`Sites`, so ``assert_sites`` pins both
    halves at once and neither can be left out."""
    return Sites(scan.reported, scan.undecided).sorted()


def text_mode_census(scan: SpawnScan) -> dict[str, int]:
    """How many TEXT-MODE spawns each module holds.

    The counting pin. ``Sites.without_line_numbers`` deduplicates through a
    set, so two identical calls in one module collapse to one row and the
    row pins cannot count. This can.
    """
    built: dict[str, int] = {}
    for where in scan.text_sites:
        built[where] = built.get(where, 0) + 1
    return built
