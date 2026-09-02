"""What one call says about itself, and what CPython says back.

The bottom layer of #320's encoding guard, split out when walk plus
rules crossed the repo's 800-line ratchet. The boundary is a real one:
nothing here looks at a module's STRUCTURE. Every function answers a
question about a single ``ast.Call``, or asks the interpreter a question
about codecs and the exception hierarchy. ``tests/helpers/encodingwalk.py``
is what walks a module and decides which calls to ask about, and it
imports this - there is no edge back.

THE FOUR PREDICATES ARE DERIVED, NOT LISTED, and that is the point of
them. Each replaced a hand-written frozenset, and #344's ``/simplify``
pass found three of the four sets holding a live hole: the ``OSError``
subclass list missed eleven of them, so a strict read under ``except
TimeoutError`` was CLEARED; the lenient-``errors`` list held
``xmlcharrefreplace``, which raises ``TypeError`` on a decode, so a site
was cleared on a value that cannot be used; the utf-8 spelling list
missed ``UTF_8``, ``cp65001`` and ``utf``.

CLAUDE.md states the rule those three broke: a guard that CLEARS must be
narrow, because over-matching converts a resolution into a clearing and
deletes the mechanism. A set somebody typed is exactly an over-match
nobody can see the edge of. So ``codecs.lookup`` answers the spelling
question, a real ``bytes.decode`` answers the handler question, and
``issubclass`` against the real builtin answers both exception
questions.
"""

from __future__ import annotations

import ast
import builtins
import codecs
from collections.abc import Iterable

from tests.helpers.astwalk import Clause, folded_str, leaf_name

_UNDECODABLE = b"\xff"


def is_utf8(spelling: str) -> bool:
    """Does this ``encoding=`` name the utf-8 codec?

    ``codecs`` owns the aliases, the way ``tomllib`` owns its error
    taxonomy in ``tests/test_toml_readers.py``. A hand-written list of
    six spellings reported ``UTF_8``, ``cp65001`` and ``utf`` as faults,
    all three of which normalise to utf-8; and it would have gone stale
    the first time CPython registered an alias. ``utf-8-sig`` normalises
    to its own name and correctly stays OUT: it is a different codec.
    """
    try:
        return codecs.lookup(spelling).name == "utf-8"
    except LookupError:
        return False


def is_lenient(handler: str) -> bool:
    """Can a decode with this ``errors=`` come back without raising?

    RUN, not listed. The list this replaced named ``xmlcharrefreplace``
    as lenient, which drifted from its own stated measurement inside the
    commit that wrote it: on CPython 3.12.8 ``b"\\xff".decode("utf-8",
    errors="xmlcharrefreplace")`` raises ``TypeError: don't know how to
    handle UnicodeDecodeError in error callback``, because that handler
    and ``namereplace`` are encode-only. A site naming either was CLEARED
    as "cannot raise" while raising at run time, which is the direction
    this guard exists to prevent.

    Any exception means STRICT, which is the reporting direction: an
    unregistered name (``LookupError``) and an encode-only one
    (``TypeError``) both leave the site's decode unanswered.
    """
    try:
        _UNDECODABLE.decode("utf-8", errors=handler)
    except Exception:
        return False
    return True


def builtin_exception(name: str) -> type[BaseException] | None:
    """The builtin exception this clause names, or None.

    A name that is not a builtin exception is not answered for here: the
    walk reports the site rather than deciding anything about it.
    """
    found = getattr(builtins, name, None)
    if isinstance(found, type) and issubclass(found, BaseException):
        return found
    return None


def answers_for_io(names: Iterable[str]) -> bool:
    """Does this handler answer for the READ's I/O failures?

    ``issubclass(cls, OSError)``, not a list. The seven-name list this
    replaced missed eleven live ``OSError`` subclasses - ``TimeoutError``,
    ``ConnectionError``, ``BlockingIOError``, ``FileExistsError``,
    ``InterruptedError`` and six more - so a strict read under any of
    them was CLEARED with nothing covering the decode. It also gets the
    ``IOError`` and ``EnvironmentError`` aliases for free, because they
    ARE ``OSError``.
    """
    return any(
        (found := builtin_exception(name)) is not None and issubclass(found, OSError)
        for name in names
    )


def covers_the_decode(names: Iterable[str]) -> bool:
    """Does this handler catch ``UnicodeDecodeError``?

    ``issubclass(UnicodeDecodeError, cls)``, which derives exactly the
    five names the hand-written list held - ``UnicodeDecodeError``,
    ``UnicodeError``, ``ValueError``, ``Exception``, ``BaseException`` -
    and cannot be short by one the way the I/O side was.

    ``UnicodeDecodeError`` itself is the precise clause and the one this
    sweep wrote, for #319's reason: ``read_text(encoding="utf-8")``
    raises no OTHER ``ValueError``, so a wider clause could only ever
    catch a kstrl defect and relabel it as the operator's bad byte. The
    wider ones are accepted because they do cover it, and eight sites
    predate the sweep with one of them.
    """
    return any(
        (found := builtin_exception(name)) is not None and issubclass(UnicodeDecodeError, found)
        for name in names
    )


def keyword_of(node: ast.Call, name: str) -> ast.expr | None:
    for keyword in node.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def mode_of(node: ast.Call) -> str | None:
    """``open``'s mode as a folded string, or None when it does not fold.

    ``Path.open(mode)`` takes it first positionally, ``open(file, mode)``
    second, and both accept ``mode=``. Absent means ``"r"``, which the
    stdlib says and this repeats rather than guessing.
    """
    named = keyword_of(node, "mode")
    if named is not None:
        return folded_str(named)
    positional = 0 if isinstance(node.func, ast.Attribute) else 1
    if len(node.args) > positional:
        return folded_str(node.args[positional])
    return "r"


def is_text(node: ast.Call) -> bool | None:
    """Does this call open a TEXT stream at all? None when undecidable.

    The population for the encoding rule, and it covers writes as well as
    reads, because the rule is two-sided: #291's argument is that kstrl
    must not become the SOURCE of bytes its own readers cannot decode,
    and a write whose encoding the locale picks is exactly that. Measured
    on ``kstrl/agents/logging.py``, which teed raw agent output through
    ``path.open("a")``: under ``LC_ALL=C PYTHONUTF8=0`` one accented
    character in a model's reply raised ``UnicodeEncodeError`` and killed
    the run mid-stream, while the same write naming utf-8 succeeded. Both
    variables, because a C locale alone turns PEP 540 UTF-8 mode ON.

    A mode that does not fold returns None, because clearing a site on a
    mode string this walk never saw is the skip direction the whole guard
    is against.
    """
    if leaf_name(node.func) == "read_text":
        return True
    mode = mode_of(node)
    return None if mode is None else "b" not in mode


def decodes_text(node: ast.Call) -> bool:
    """Can text be READ back through this call? The handler rule's half.

    ``"r"``, ``"r+"``, ``"a+"``, ``"w+"``. A write-only handle encodes and
    never decodes, so demanding a ``UnicodeDecodeError`` clause from it
    would be demanding a handler that can never fire.
    """
    if leaf_name(node.func) == "read_text":
        return True
    mode = mode_of(node) or ""
    return "r" in mode or "+" in mode


def encoding_fault(node: ast.Call) -> str | None:
    """Why this read's encoding is not proven utf-8, or None."""
    named = keyword_of(node, "encoding")
    if named is None:
        return "names no encoding, so it decodes as the locale says"
    folded = folded_str(named)
    if folded is None:
        return "names an encoding this walk cannot fold, so utf-8 is unproven"
    if not is_utf8(folded):
        return f"names encoding {folded!r} rather than utf-8"
    return None


def is_strict(node: ast.Call) -> bool:
    """Can this read raise ``UnicodeDecodeError`` at all?

    An ``errors=`` this walk cannot fold counts as STRICT, which is the
    reporting direction: a site is cleared on a lenient value only when
    the value was actually read.
    """
    named = keyword_of(node, "errors")
    folded = None if named is None else folded_str(named)
    return folded is None or not is_lenient(folded)


def clause_fault(clauses: list[Clause]) -> str | None:
    """Why this ``try`` does not answer for the decode, or None.

    Returns None when it covers the decode, and a reason when it answers
    for the IO and not the decode. An UNNAMEABLE clause is a reason too:
    ``Clause.decided`` is False when the walk could not read what a
    handler catches, and an empty name set reads exactly like "catches
    nothing", which is the worst possible misreading of "catches
    something I could not see".
    """
    names = {name for clause in clauses for name in clause.names}
    if any(not clause.decided for clause in clauses):
        return "sits under a handler this walk cannot name, so nothing is known about it"
    if covers_the_decode(names):
        return None
    if answers_for_io(names):
        return (
            f"sits under except {sorted(names)[0]} with nothing covering "
            "UnicodeDecodeError, which is a ValueError and escapes it"
        )
    return "keep-looking"
