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
records that merge. ``tests/test_encoding_spawns.py`` pins the derivation's
output against ``tests/test_timeout_enforcement.py``'s own set, so the two
cannot drift apart in silence.

CLEARING IS THE DANGEROUS DIRECTION. CLAUDE.md guard rule 3: a guard that
CLEARS must be narrow. Every step that cannot reach an answer here reports
or defers rather than clears:

    a ``text=`` that does not fold        -> reported
    an ``encoding=`` that does not fold   -> reported (cannot prove utf-8)
    an ``errors=`` that does not fold     -> reported (cannot prove strictness)
    a call carrying a ``**something``     -> undecided; the caller's dict is
                                              not readable, so no explicit
                                              keyword on the same call can be
                                              trusted either
    a callee the resolver cannot decide   -> undecided, a pinned row

The ``errors=`` row above is deliberately NOT the same rule
``encodingrules.is_strict`` answers for #320's READ rule, and this module
must not change that function to make the two agree. The read rule treats
an unfoldable ``errors=`` as strict (compliant), on the reasoning that a
read whose handler this walk cannot name is still answered for by
whatever code wrote it. The spawn rule has no such reasoning available: an
unfoldable ``errors=`` here means a VARIABLE could hold ``"replace"`` at
run time, and clearing the site on nothing but the absence of proof is
exactly the over-match CLAUDE.md guard rule 3 forbids. So this file asks
its own question about ``errors=`` before it asks ``is_strict`` anything.

THE ``**kwargs`` ROW is here because a walk that clears
``subprocess.run(cmd, **kwargs)`` as bytes mode is answering a question it
was never handed enough information to answer: the dict merged in at the
call site can hold ``encoding=``, ``errors=`` or ``text=`` this walk never
sees, so no explicit keyword on the same call describes the site's real
behaviour either. ``kstrl/timeout.py``'s ``run_with_timeout`` is the live
instance: it names ``encoding="utf-8"`` explicitly and ALSO forwards
``**kwargs`` to the same call, so a caller passing ``errors="replace"``
through those kwargs would silently un-strict a site this walk would
otherwise have called clear.
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
    folded_str,
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


#: The seven, derived once. Pinned in ``tests/test_encoding_spawns.py`` so a
#: CPython release that drops one is loud rather than silent.
SPAWN_TARGETS = _spawn_entry_points()


def forwards_unknown_keywords(node: ast.Call) -> bool:
    """Does this call carry a ``**something`` keyword?

    ``ast.keyword.arg`` is ``None`` exactly for a ``**`` spread; every
    named keyword has a string there. A dict merged in this way is opaque
    to this walk, so a call carrying one is UNDECIDED regardless of what
    its other, explicit keywords say (A1, #409's simplify pass): an
    explicit ``encoding="utf-8"`` sitting beside ``**kwargs`` does not
    prove the call is strict, because the forwarded dict can still carry
    an ``errors=`` this walk will never see.
    """
    return any(keyword.arg is None for keyword in node.keywords)


def errors_unproven_fault(node: ast.Call) -> str | None:
    """Why an ``errors=`` keyword's strictness cannot be proven, or None.

    ``encodingrules.is_strict`` treats an unfoldable ``errors=`` as
    strict, which is the correct, REPORTING direction for #320's READ
    rule and the wrong, CLEARING direction for this SPAWN rule (A2). A
    read whose handler this walk cannot name is still answered for by
    whatever code wrote it; a spawn whose ``errors=`` is a variable could
    hold ``"replace"`` at run time, and clearing it would be exactly the
    over-match CLAUDE.md guard rule 3 forbids. So this question is asked
    here, once, and ``is_strict`` itself is left unchanged: the read rule
    still uses it and must keep its own answer.
    """
    named = keyword_of(node, "errors")
    if named is None:
        return None
    if folded_str(named) is None:
        return "errors= cannot be folded, so strictness is unproven"
    return None


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


def _classify_text_mode_spawn(node: ast.Call) -> tuple[str, str | None]:
    """Which bucket a spawn ALREADY DECIDED to be text mode belongs in.

    Returns ``(bucket, reason)`` where ``bucket`` is ``"reported"``,
    ``"lenient"`` or ``"clear"``, and ``reason`` is the message to attach
    when it is ``"reported"``. Split out of :func:`scan_source` so that
    function's loop reads as one dispatch over the modes a spawn can be
    in, rather than this decision nested inside it too.
    """
    fault = encoding_fault(node) or errors_unproven_fault(node)
    if fault is not None:
        return "reported", fault
    if not is_strict(node):
        return "lenient", None
    return "clear", None


def scan_source(text: str, *, where: str = "", module: str = "") -> SpawnScan:
    """Every ``subprocess`` spawn in one module's source, partitioned.

    Takes TEXT rather than a path so the planted shapes in
    ``tests/test_encoding_spawns.py`` exercise the same code the package
    sweep runs. ``resolved_calls`` answers the SEEN half only, so
    ``calls_to(...).undecided`` is taken over the same tree in the same
    function: the rule ``tests/test_astwalk_mechanisms.py`` enforces.
    """
    if "subprocess" not in text:
        return SpawnScan()
    tree = parse(text)
    clear: list[str] = []
    reported: list[str] = []
    undecided_kwargs: list[str] = []
    lenient: list[str] = []
    bytes_mode: list[str] = []
    text_sites: list[str] = []
    for node, _origin in resolved_calls(tree, SPAWN_TARGETS, module=module):
        if forwards_unknown_keywords(node):
            site = f"{where}:{node.lineno}" if where else str(node.lineno)
            undecided_kwargs.append(f"{site} {ast.unparse(node.func)}")
            continue
        row = f"{where}:{node.lineno} {ast.unparse(node)[:70]}"
        mode = text_mode(node)
        if mode is None:
            reported.append(f"{row} (text mode does not fold, so a decode is unproven)")
            continue
        if not mode:
            bytes_mode.append(row)
            continue
        text_sites.append(where)
        bucket, reason = _classify_text_mode_spawn(node)
        if bucket == "reported":
            reported.append(f"{row} {reason}")
        elif bucket == "lenient":
            lenient.append(row)
        else:
            clear.append(row)
    undecided = calls_to(tree, SPAWN_TARGETS, where=where, module=module).undecided
    undecided = undecided + tuple(undecided_kwargs)
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
    """One sweep of ``kstrl/``, cached because several tests ask for it."""
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


def bytes_mode_census(scan: SpawnScan) -> dict[str, int]:
    """How many BYTES-MODE spawns each module holds.

    Derived from ``scan.bytes_mode`` itself rather than a second parallel
    list (D2, #409's simplify pass): each row there already carries a
    ``module:lineno`` prefix, one row per site with its own line number, so
    splitting on the first ``:`` recovers the module without deduplicating
    anything. Counting rather than pinning the 19 (of 20 sites) deduplicated
    rows directly is what makes a site MISFILED into this bucket (A1: a
    ``**kwargs`` call wrongly cleared as bytes mode) show up as a moved
    number here, which a row inventory alone would not have caught it
    doing before A1 existed.
    """
    built: dict[str, int] = {}
    for row in scan.bytes_mode:
        where, _, _rest = row.partition(":")
        built[where] = built.get(where, 0) + 1
    return built
