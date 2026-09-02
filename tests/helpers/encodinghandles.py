"""What happens to a handle once ``open`` has handed it over.

Split out of ``tests/helpers/encodingwalk.py`` when that file crossed the
800-line ratchet, along the cut #344 round 5 made visible: "which calls
in this module are text reads" is a different question from "and where
does the text actually get decoded", and every round-5 fix but one
landed on the second.

THE DECODE IS NOT AT THE ``open``. It is at the read, which may be a
``.read()`` call, an iteration, or a hand-over to somebody else - and
that last one is where four review rounds' worth of escapes lived,
because a hand-over defers the read to a point no AST can locate.

THREE THINGS THIS MODULE REFUSES TO GUESS, each measured escaping while
the walk said CLEAR before it refused:

* a handle name carrying a ``global`` or ``nonlocal`` declaration, whose
  readers are lexically outside the binding scope
* a hand-over whose value is not drained on the spot, because
  ``json.load(h)`` reads now and ``csv.reader(h)`` reads nothing and the
  syntax is identical
* ``detach()`` and ``reconfigure()``, which are ``TextIOWrapper``
  members and change what decoding happens, so membership in the derived
  safe set was answering the wrong question
"""

from __future__ import annotations

import ast
import io
from dataclasses import dataclass

from tests.helpers.astwalk import (
    Bindings,
    all_nodes,
    assignment_parts,
    bound_names,
    leaf_name,
    own_nodes,
    scopes,
)
from tests.helpers.encodingrules import decodes_text, is_text

#: The stdlib text readers, for the origin check that moved here with
#: :func:`is_somebody_elses`. Kept beside the code that asks.
STDLIB_READERS = frozenset(
    {
        "open",
        "builtins.open",
        "io.open",
        "codecs.open",
        "pathlib.Path.open",
        "pathlib.Path.read_text",
    }
)

#: How a text handle is read once ``open`` has handed it over. The walk
#: below chases the HANDLE because the decode is not at the ``open``: the
#: bytes become text at the read, and #320 found one live offender of
#: exactly this shape, ``factory.py``'s run-lock reading the holder pid
#: out of an ``"a+"`` handle under ``except OSError``.
HANDLE_READS = frozenset({"read", "readline", "readlines"})

#: Members that CHANGE WHAT DECODING HAPPENS, and so are neither a read
#: nor safe. #344 round 5 F6, and the lesson is about derivation:
#: ``dir(io.TextIOWrapper)`` is honest about MEMBERSHIP and wrong about
#: the PROPERTY. "is a TextIOWrapper member" is not "does not affect
#: decoding", and affecting decoding is the one thing the safe set exists
#: to exclude.
#:
#: ``detach()`` hands back the raw buffer, and the caller rewraps it in a
#: ``TextIOWrapper`` this walk has never seen. ``reconfigure()`` changes
#: ``errors=`` on a live handle: a file opened ``errors="replace"``,
#: which the walk correctly decides can never raise, is strict again
#: after one call. Both were measured escaping while the walk said CLEAR.
HANDLE_RECONFIGURES = frozenset({"detach", "reconfigure"})

#: Every OTHER member a text stream has: writing, seeking, closing and
#: metadata, none of which hands back decoded text. DERIVED from
#: ``io.TextIOWrapper`` rather than listed, for the reason #320's own
#: hand-written sets were replaced: a list is a clearing mechanism, and a
#: clearing mechanism nobody derives is one nobody notices is short.
#: The derivation is the floor, not the ceiling: two members are
#: SUBTRACTED below, because derivation answers the wrong question.
#:
#: A member that is on NEITHER set is not "probably fine": it is a use of
#: the handle this walk has never heard of, so :func:`touch` buckets it
#: ``unknown`` and :func:`handle_escapes` makes it ``undecided``.
#: Measured on ``kstrl/``: the tracked handles are
#: touched through ``fileno``, ``close``, ``seek``, ``truncate``,
#: ``write``, ``flush``, ``read``, ``readlines``, one ``for`` and seven
#: hand-overs, and every one of those lands in a modelled bucket.
HANDLE_SAFE = (
    frozenset(name for name in dir(io.TextIOWrapper) if not name.startswith("_"))
    - HANDLE_READS
    - HANDLE_RECONFIGURES
)


def text_handles(scope: ast.AST, table: Bindings) -> dict[str, ast.Call]:
    """Local name -> the text-mode ``open`` that bound it, in ONE scope.

    Per scope and not per module, because a handle name is short and
    reused: ``kstrl/factory.py`` binds ``fp`` in one function and uses
    the same spelling elsewhere, and a module-wide table would credit one
    function's ``fp.read()`` to another function's ``open``.

    A ``with ... as name`` and a plain single-target assignment count. A
    DOTTED target does not, and neither does a multi-target one: the walk
    matches uses by comparing ``ast.Name.id``, so ``self.h = open(...)``
    would be entered under the key ``"self.h"`` that no ``Name`` can ever
    equal, and the handle would be tracked in name only while every read
    through it went unseen. It is :func:`bound_opens` that then makes
    those two shapes ``undecided`` instead.

    A mode that does not fold is NOT entered, which is the reporting
    direction: the ``open`` itself is already a row under the mode rule.
    """
    found: dict[str, ast.Call] = {}
    for node in own_nodes(scope):
        opened = opened_here(node)
        if opened is None:
            continue
        name, call = opened
        if is_somebody_elses(call, table):
            continue
        if is_text(call) and decodes_text(call):
            found[name] = call
    return found


def opened_here(node: ast.AST) -> tuple[str, ast.Call] | None:
    """``(name, the open call)`` when this node binds a handle to a name."""
    if isinstance(node, ast.withitem):
        call, target = node.context_expr, node.optional_vars
    else:
        # BOTH counts, and #344 round 3 finding 4 is why. ``bound_names``
        # is ``assignment_parts`` with the dotted targets already dropped,
        # so testing only its length accepts ``h = self.g = open(...)``:
        # one plain name survives the filter, the site is tracked as
        # ``h``, and every read through ``self.g`` is invisible - which
        # is the shape the dotted-target rule exists to refuse. The
        # binding has to be a SINGLE target that is ALSO a plain local.
        targets, value = assignment_parts(node)
        names, _ = bound_names(node)
        if value is None or len(targets) != 1 or len(names) != 1:
            return None
        call, target = value, ast.Name(id=names[0])
    if not isinstance(call, ast.Call) or leaf_name(call.func) != "open":
        return None
    return (target.id, call) if isinstance(target, ast.Name) else None


@dataclass(frozen=True)
class Handle:
    """One ``open`` bound to one plain local name in one scope, and the
    loads of that name the walk could not account for."""

    name: str
    call: ast.Call
    scope: ast.AST
    #: Occurrences the one classifier could not bucket. Non-empty means
    #: the handle is not followable AND there are rows to report; the two
    #: cannot disagree because they are the same tuple.
    loose: tuple[tuple[ast.AST, str], ...]
    #: The nodes that DECODE through this handle, with the open that set
    #: the encoding. Same classifier, same pass.
    decodes: tuple[ast.AST, ...]


def parents_of(scope: ast.AST) -> dict[int, ast.AST]:
    """Child id -> parent, over EVERYTHING lexically inside this scope.

    ``all_nodes`` and not ``own_nodes``, so a load inside a nested
    function still has a parent to be judged by. It will not be
    accounted for either way - it is not owned - but a row with no
    parent would read as "no bucket" for the wrong reason.
    """
    found: dict[int, ast.AST] = {}
    for node in (scope, *all_nodes(scope)):
        for child in ast.iter_child_nodes(node):
            found[id(child)] = node
    return found


def handles_in(tree: ast.Module, table: Bindings) -> list[Handle]:
    """THE INVARIANT, computed once and used by everything downstream.

    An ``open`` is cleared only when exactly one scope binds its handle
    to a plain local name, and EVERY load of that name lexically inside
    that scope is both OWNED by that scope and classified into exactly
    one modelled bucket.

    #344 round 3 found nine more clearing shapes, and they were one
    asymmetry rather than nine bugs: followability was computed
    module-wide over ``all_nodes`` while the uses were classified
    scope-locally over ``own_nodes``, and every disagreement between the
    two resolved in the clearing direction. The two are one function now,
    so there is nothing left to disagree.

    The two halves of the sentence do different work, and dropping
    either reopens a measured escape.

    OWNED BY THAT SCOPE closes the lexical escapes. ``own_nodes`` stops
    at a nested ``def`` and at a ``lambda``; ``all_nodes`` does not. A
    module-level handle read inside a function, a closure over a handle,
    and a lambda reading one are each a load that is INSIDE the binding
    scope and not OWNED by it, and all three used to clear. Sweeping
    ``all_nodes(scope)`` rather than the whole module is what keeps an
    unrelated function's own local called ``f`` from being charged to
    this binding.

    CLASSIFIED INTO EXACTLY ONE BUCKET closes the alias escapes, and
    :func:`touch` is that half. ONE function, which is #344 round 4's
    third fix: followability and the decode rows now come out of the
    same pass over the same nodes, so a use cannot be "modelled" for one
    purpose and invisible for the other. Three rounds of review found
    that disagreement three separate times, and every instance of it
    resolved in the clearing direction.

    A name whose loads are all accounted for is followable. A name with
    even one loose load is not, and ``loose`` is literally the tuple
    :func:`handle_escapes` reports, so the guard cannot clear a site it
    is simultaneously complaining about.
    """
    found: list[Handle] = []
    declared = declared_names(tree)
    for scope, _qualified in scopes(tree):
        handles = text_handles(scope, table)
        if not handles:
            continue
        touches = _touches_in(scope, handles)
        for name, call in handles.items():
            seen = list(touches[name])
            if name in declared:
                seen.append(Touch("unknown", name, declared[name], _DECLARED_WHY))
            found.append(
                Handle(
                    name=name,
                    call=call,
                    scope=scope,
                    loose=tuple((one.at, one.why) for one in seen if one.kind == "unknown"),
                    decodes=tuple(one.at for one in seen if one.kind == "decode"),
                )
            )
    return found


def _touches_in(scope: ast.AST, handles: dict[str, ast.Call]) -> dict[str, list[Touch]]:
    """Every use of every tracked handle in ONE scope, bucketed by name."""
    owned = {id(node) for node in own_nodes(scope)}
    parents = parents_of(scope)
    touches: dict[str, list[Touch]] = {name: [] for name in handles}
    for node in occurrences(scope, handles):
        got = touch(node, parents)
        # Not OWNED by this scope means a closure, a lambda or a nested
        # def reached the handle. That is a use, and one this scope's
        # ladder cannot answer for, so it is unknown.
        touches[got.name].append(
            got if id(node) in owned else Touch("unknown", got.name, node, _ANOTHER_SCOPE)
        )
    return touches


#: One string, so the message and the mechanism cannot drift.
_ANOTHER_SCOPE = "reached from another scope"


#: The reason a declared name is unfollowable, written once so the
#: message and the mechanism cannot drift apart.
_DECLARED_WHY = (
    "carries a global/nonlocal declaration, so its readers are lexically "
    "outside the binding scope and this walk cannot see them"
)


def declared_names(tree: ast.Module) -> dict[str, ast.AST]:
    """Every name declared ``global`` or ``nonlocal`` anywhere, and where.

    #344 round 5 F2, and it is a hole in the walk's UNIVERSE rather than
    in any of its rules. Followability is decided over ``all_nodes(scope)``:
    every load of the handle name lexically inside the binding scope. A
    ``global`` or ``nonlocal`` declaration is precisely the construct that
    puts a load OUTSIDE that scope while still naming the same object, so
    no amount of care taken inside the scope can ever see it.

    Measured, CLEAR and escaping, before this existed::

        H = None
        def _open(p):
            global H
            H = open(p, encoding='utf-8')
        def f(p):
            try:
                _open(p)
                return H.read()      # attributed to nothing at all
            except OSError:
                return 'caught'

    MODULE-WIDE and not scope-local on purpose. The declaration sits in
    the scope that WRITES the name and the escaping read is in another
    one, so a scope-local test would be looking in the wrong place. A
    handle whose name is declared anywhere is unfollowable everywhere,
    which over-reports on an unrelated function that happens to reuse the
    spelling. Over-reporting is the safe direction, and the row says why.
    """
    found: dict[str, ast.AST] = {}
    for node in all_nodes(tree):
        if isinstance(node, ast.Global | ast.Nonlocal):
            for name in node.names:
                found.setdefault(name, node)
    return found


def bound_opens(tree: ast.Module, table: Bindings) -> set[int]:
    """The ids of every ``open`` call some scope BINDS to a plain local name.

    #344's round-2 review found the hole this closes, and it was the
    guard's own skip direction: :func:`_classify` never charged the
    handler rule to an ``open``, because the decode happens at the read -
    and the read was only ever found through a NAME. So every spelling
    that binds no name, or binds one this walk cannot match, was written
    into the CLEARED inventory with both faults ``None``:

        open(p, encoding="utf-8").read()
        json.load(open(p, encoding="utf-8"))
        h = g = open(p, encoding="utf-8")
        self.h = open(p, encoding="utf-8")

    All four decode, and a ``UnicodeDecodeError`` from any of them walks
    past an enclosing ``except OSError`` exactly as #320 describes.
    Measured on the first: ``b'{"total": 5\\xff}'`` raises straight past
    the handler while the guard said ``clear``.

    BINDING is the only question here, and round 5 is why that is worth
    saying. This used to exclude a handle with an unplaceable USE as
    well, which made every such site TWO undecided rows: one saying the
    handle "is never bound to a name this walk can follow", which was
    false and confusing because it plainly was, and one from
    :func:`handle_escapes` saying the true thing. On ``kstrl/`` that was
    13 rows carrying 7 facts. The two jobs are now split at the sentence
    they were always divided at: this answers "can a name be matched at
    all", and :func:`handle_escapes` answers "and is every use of that
    name placeable". Both still come out of ONE call to
    :func:`handles_in`, which is round 3's finding and is not weakened:
    no DECODE is cleared by this, because a use that cannot be placed is
    not in ``handle.decodes`` and so is never cleared as a read.

    The fix is CLAUDE.md's guard-design rule 3 applied literally: a
    clearing guard that cannot PROVE a site is compliant must flag.
    Chasing more spellings would have left the same hole one spelling
    further out, because the chase is what over-matches; refusing to
    clear what it cannot follow is what does not.
    """
    return {id(handle.call) for handle in handles_in(tree, table)}


@dataclass(frozen=True)
class Touch:
    """What one occurrence of a tracked handle name does with it.

    ``kind`` is ``decode``, ``safe`` or ``unknown``, and ``at`` is the
    node a decode should be reported against.
    """

    kind: str
    name: str
    at: ast.AST
    #: Why this use cannot be followed, for the row a reader gets.
    #: Empty for a use the walk can place.
    why: str = ""


def occurrences(scope: ast.AST, handles: dict[str, ast.Call]) -> list[ast.expr]:
    """Every expression lexically inside this scope that EVALUATES to a
    tracked handle.

    A plain load, and a walrus whose target is the handle: ``json.load(h
    := open(...))`` occupies an expression slot and hands the handle
    over in the same breath, so it belongs in the same list rather than
    in a special case beside it.
    """
    found: list[ast.expr] = []
    for node in all_nodes(scope):
        if isinstance(node, ast.Name) and node.id in handles and isinstance(node.ctx, ast.Load):
            found.append(node)
        elif (
            isinstance(node, ast.NamedExpr)
            and isinstance(node.target, ast.Name)
            and node.target.id in handles
        ):
            found.append(node)
    return found


def occurrence_name(node: ast.expr) -> str:
    """The handle name one :func:`occurrences` entry spells.

    The walrus is unwrapped to its target, because ``json.load(h :=
    open(p, encoding="utf-8"))`` hands the handle over in the same
    expression that binds it. #344 round 3 finding 3: without this the
    binding was tracked, the hand-over was not seen, no LOAD of ``h``
    existed to be unaccounted for, and the site CLEARED.
    """
    target = node.target if isinstance(node, ast.NamedExpr) else node
    assert isinstance(target, ast.Name), ast.dump(node)
    return target.id


def touch(node: ast.expr, parents: dict[int, ast.AST]) -> Touch:
    """ONE classifier, returning the bucket AND the node to report it at.

    #344 round 4's third fix, and the reason it is one function. There
    used to be two - one deciding whether a use was "modelled", the
    other finding the decode sites - walking different node sets and
    asking different questions. Two classifiers that must agree is a
    defect waiting to be found, and it was found three times: a node
    could be modelled in one and invisible in the other, and the
    disagreement always resolved in the clearing direction. Now a use
    has exactly one answer, and the answer carries the row.

    THREE BUCKETS, and round 5 is why the third one is wide.

    A DECODE is a use where the walk can prove the bytes become text
    HERE: ``fh.read()``, and a hand-over whose value is drained on the
    spot by a ``for`` or an eager comprehension. "Drained on the spot" is
    a property of the SYNTAX, not of the callee, which is what makes it
    provable: ``for row in csv.DictReader(handle, delimiter='\\t')``
    decodes at that ``for`` whatever ``DictReader`` does internally.

    A HAND-OVER THE WALK CANNOT PLACE is round 5's F4, and it is unknown
    now rather than a decode. ``rows = csv.reader(h)`` reads NOTHING; the
    decode happens wherever ``rows`` is drained, which may be under a
    different handler, in a different function, or never. Three forms
    were measured CLEAR and escaping, one of them raising ``ValueError:
    I/O operation on closed file`` because the ``with`` had already shut
    the handle. Modelling drain points is the chase that over-matches;
    the walk says it cannot place the read and hands the reader a row.

    SAFE is the narrow bucket: a member that provably neither decodes nor
    changes how decoding happens. :data:`HANDLE_RECONFIGURES` is
    subtracted from it for exactly that reason.
    """
    name = occurrence_name(node)
    up = parents.get(id(node))
    if isinstance(up, ast.Attribute):
        return attribute_touch(node, name, up, parents)
    if isinstance(up, ast.For) and up.iter is node:
        return Touch("decode", name, up)
    if isinstance(up, ast.comprehension) and up.iter is node:
        if isinstance(parents.get(id(up)), ast.GeneratorExp):
            return Touch("unknown", name, up, _DEFERRED_WHY)
        return Touch("decode", name, up)
    handed = handed_over(node, name, up, parents)
    if handed is not None:
        return handed
    return Touch("unknown", name, node, "a use of the handle this walk does not model")


#: Why a deferred read cannot be attributed to a handler. One string,
#: because F4's three forms are one fact.
_DEFERRED_WHY = (
    "the read is deferred to wherever this value is drained, which this "
    "walk cannot locate, so no handler can be credited with covering it"
)


def attribute_touch(
    node: ast.expr, name: str, up: ast.Attribute, parents: dict[int, ast.AST]
) -> Touch:
    """``h.something`` classified, and only when it is actually CALLED."""
    grand = parents.get(id(up))
    if not (isinstance(grand, ast.Call) and grand.func is up):
        # A bound method taken (``_read = h.read``) or the handle's
        # ``buffer`` handed on: the attribute VALUE escapes.
        return Touch("unknown", name, node, "an attribute of the handle taken, not called")
    if up.attr in HANDLE_READS:
        return Touch("decode", name, grand)
    if up.attr in HANDLE_RECONFIGURES:
        return Touch(
            "unknown",
            name,
            grand,
            f"{up.attr}() changes what decoding happens, so nothing decided above it holds",
        )
    if up.attr in HANDLE_SAFE:
        return Touch("safe", name, grand)
    return Touch("unknown", name, node, f"{up.attr} is not a member this walk knows")


def handed_over(
    node: ast.expr, name: str, up: ast.AST | None, parents: dict[int, ast.AST]
) -> Touch | None:
    """The handle passed to somebody else, bucketed by WHEN it is drained.

    The call's own result decides, and only two shapes prove a drain: the
    value is the ``iter`` of a ``for``, or of a comprehension that is not
    a generator expression. Everything else defers the read to a point
    this walk cannot locate.
    """
    if isinstance(up, ast.keyword):
        call = parents.get(id(up))
        if not isinstance(call, ast.Call):
            return Touch("unknown", name, node, "a keyword outside any call")
    elif isinstance(up, ast.Call) and node in up.args:
        call = up
    else:
        return None
    if drained_here(call, parents):
        return Touch("decode", name, call)
    return Touch("unknown", name, call, _DEFERRED_WHY)


def drained_here(call: ast.AST, parents: dict[int, ast.AST]) -> bool:
    """Is this call's result consumed by the statement it sits in?

    A ``for`` runs its body at that statement, and so does a list, set or
    dict comprehension. A GENERATOR expression does not, which is F4a.
    """
    up = parents.get(id(call))
    if isinstance(up, ast.For) and up.iter is call:
        return True
    if isinstance(up, ast.comprehension) and up.iter is call:
        return not isinstance(parents.get(id(up)), ast.GeneratorExp)
    return False


def handle_escapes(handles: list[Handle], where: str) -> list[str]:
    """The loose loads of :func:`handles_in`, as rows.

    The SAME computation that decides followability, so the guard cannot
    clear a site it is simultaneously complaining about. There is no
    fourth bucket and no benefit of the doubt: an alias (``other = h``),
    a bound method taken (``_read = h.read``), a closure over the
    handle, a return of it, a subscript, an attribute the walk has never
    heard of are all rows a reader has to answer for.
    """
    return [
        f"{where}:{read_lineno(node)} {handle.name} ({why})"
        for handle in handles
        for node, why in handle.loose
    ]


def is_somebody_elses(node: ast.Call, table: Bindings) -> bool:
    """Is this ``open``/``read_text`` provably NOT the stdlib's?

    The only step in this walk that CLEARS on a resolution, so it asks
    :meth:`Bindings.origin_of` and refuses a guess. The bare-name
    over-match answers for any receiver in the module, so four innocuous
    lines - ``class _M: open = shutil.copy`` - would otherwise make every
    ``x.open(p)`` in the file vanish from the inventory. #324 round 3
    measured exactly that against three other guards.

    What it decides out, measured on ``kstrl/``: ``os.open``, which
    returns an integer file descriptor and can decode nothing, and
    ``EvolutionJournal.open``, a kstrl method that happens to share the
    name. Three calls, all of which would otherwise sit in ``undecided``
    forever as rows nobody can action.
    """
    found = table.origin_of(node.func)
    return found is not None and not found.guessed and found.dotted not in STDLIB_READERS


def read_lineno(node: ast.AST) -> int:
    """The line a decode site sits on.

    ``ast.comprehension`` is not a statement and carries no ``lineno`` of
    its own, so the row is keyed on the iterable it walks - which is the
    handle, and the thing a reader would look for.
    """
    if isinstance(node, ast.comprehension):
        return node.iter.lineno
    return getattr(node, "lineno", 0)
