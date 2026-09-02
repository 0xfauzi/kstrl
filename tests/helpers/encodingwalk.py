"""#320's encoding walk: the machinery, with no inventory in it.

``tests/test_encoding_readers.py`` holds the four inventories this
walk feeds, and argues the rule it enforces; read that first. The
split is the one the repo's 800-line ratchet asks for, and it falls
where the two jobs already divided: this module answers "what does
this call do", and that one answers "and is that the set we expect".

TWO LAYERS, and they fail in opposite directions on purpose.

LAYER 1 is a census of every expression in ``kstrl/`` that SPELLS
``read_text`` or ``open``, per module. It enumerates no node types and no
field names, so a reader in ANY shape built on those two has to move the
dict, whatever it does afterwards.

What it does NOT claim is that no module can obtain a file's text without
naming one of them. That is false in general and #344's review said so:
``configparser.ConfigParser().read``, ``linecache`` and
``fileinput.input()`` all decode with the locale and name neither token.
None is live in ``kstrl/`` today, and the population this walk declares
is the two tokens, not "every decode". It catches exactly what layer 2 is
blind to - ``f = path.read_text`` then ``f()`` - and
``TestTheTwoLayersTogether`` plants that and watches layer 2 miss it, so
the claim is measured rather than asserted. The churn is the
point: the diff that adds a row is where somebody says why new code opens
a file.

LAYER 2 is the walk below. It says which line, which rule and which
clause, which layer 1 cannot: "workqueue.py's count moved" is the wrong
message when the answer is "this read's OSError handler does not cover
the decode".

WHY THE POPULATION IS ZERO, AND WHY THAT MATTERS. ``tests/test_atomicio.
py`` and ``tests/test_process_scoping.py`` both landed at offender count
zero and both say why: a guard that ships with a suppression list is a
guard that rots, and ``test_process_scoping`` refused one once already.
#320 said the guard belongs at the end of the sweep for that reason. It
is here, in the same change, with the count at zero and no exemptions -
the six ``fcntl`` lock files it turned up were FIXED rather than listed.

THE INVARIANT, in one sentence, because #344 needed three review rounds
to find that a collection of cases is not one: an ``open`` is CLEARED
only when exactly one scope binds its handle to a plain local name, and
every load of that name lexically inside that scope is both OWNED by
that scope and classified into exactly one modelled bucket.
:func:`_handles_in` is that sentence as code, and it is the only place
followability is decided. ``tests/test_encoding_invariant.py`` tests the
sentence against CPython rather than against the shapes that motivated
it.

CLEARING IS THE DANGEROUS DIRECTION, so every undecidable is a flag.
CLAUDE.md guard-design rule 3: a guard that CLEARS must be narrow,
because over-matching converts a resolution into a clearing and deletes
the mechanism, and one that cannot PROVE a site compliant must flag.
This guard's job is to say "this site is fine", and #324 records eleven
guards that said so because they had stopped looking. Every step that
cannot reach an answer here therefore reports rather than clears:

    a mode that does not fold           -> reported (cannot prove binary)
    an ``encoding=`` that does not fold -> reported (cannot prove utf-8)
    an ``errors=`` that does not fold   -> treated as strict
    a handler this walk cannot NAME     -> reported (``Clause.decided``)
    a callee with no identifier at all  -> ``undecided``, a pinned row
    an ``open`` whose handle it cannot   -> ``undecided``, a pinned row
      follow to a name
    a use of a tracked handle it does    -> ``undecided``, a pinned row
      not model

What it cannot see is named beside the control that covers it, in
``tests/test_encoding_walk.py``, rather than listed here where a
disclosure can rot without anything failing.
"""

from __future__ import annotations

import ast
import functools
import io
from dataclasses import dataclass
from pathlib import Path

from tests.helpers.astwalk import (
    Bindings,
    Sites,
    all_nodes,
    assignment_parts,
    bindings,
    bound_names,
    handler_clauses,
    label,
    leaf_name,
    module_name,
    own_nodes,
    package_sources,
    parse,
    scopes,
    spells,
    try_body_nodes,
)
from tests.helpers.encodingrules import (
    clause_fault,
    decodes_text,
    encoding_fault,
    is_strict,
    is_text,
)

#: The two tokens a module has to name to get a file's text. Layer 1
#: counts them; layer 2 uses them as its own precondition.
READ_TOKENS = ("read_text", "open")

#: The stdlib readers this rule is about, as the DOTTED ORIGINS the
#: resolver reports. A call spelled ``open(...)`` or ``p.read_text(...)``
#: resolves to nothing at all - ``open`` is a builtin nobody imports and
#: ``p`` is a local the AST cannot type - so the normal case never
#: consults this set. It exists for the calls that DO resolve: a
#: non-guessed origin outside it belongs to somebody else's ``open``, of
#: which ``kstrl/`` has three (``os.open`` twice removed and
#: ``EvolutionJournal.open``).
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


#: A BYTE no utf-8 decoder accepts, for the probes below. Every question
#: this module asks about the standard library is answered by asking the
#: standard library, not by transcribing an answer into a list.
@dataclass(frozen=True)
class Read:
    """One read-mode text decode, and everything decidable about it."""

    where: str
    lineno: int
    #: The expression, for a message a reader can act on.
    expr: str
    #: None when the encoding is proven utf-8; else why it is not proven.
    encoding_fault: str | None
    #: None when no enclosing handler answers for the IO, when none needs
    #: to, or when the one that does is enough; else why it is not.
    guard_fault: str | None

    def faults(self) -> list[str]:
        return [f for f in (self.encoding_fault, self.guard_fault) if f is not None]

    def row(self) -> str:
        return f"{self.where}:{self.lineno} {'; '.join(self.faults())}"


@dataclass(frozen=True)
class Scan:
    """Every read in one module, partitioned with no third bucket."""

    #: One row per compliant read, keyed by module and expression.
    clear: tuple[str, ...] = ()
    #: One row per read this walk could not clear, with the reason.
    reported: tuple[str, ...] = ()
    #: One row per call it could not even classify.
    undecided: tuple[str, ...] = ()
    #: One row per call it decided is not its subject. Not a silent
    #: fourth bucket: pinned, so a clearing step that starts dropping
    #: real readers moves a list somebody has to look at.
    decided_out: tuple[str, ...] = ()


#: Layer 1's net, one predicate per token, built ONCE. Rebuilding the
#: closures per node cost a fresh allocation for each of the package's
#: ~237k nodes on every pass; ``tests/test_state_dir_scope.py`` binds
#: them at module scope for the same reason.
_TOKEN_NETS = tuple(spells(token) for token in READ_TOKENS)


def spells_a_token(node: ast.AST) -> bool:
    """Layer 1's net, applied to one node. Shared so the two layers
    cannot drift into disagreeing about which modules hold a read."""
    return any(sees(node) for sees in _TOKEN_NETS)


def _guard_fault(
    node: ast.Call, tries: list[tuple[ast.Try, set[int]]], table: Bindings
) -> str | None:
    """The first enclosing ``try`` that answers for the IO, and why it is
    not enough. None when nothing answers, or when something answers
    fully.

    OUTWARD FROM THE INNERMOST, not the innermost alone. An inner ``try``
    that already covers the decode means the decode never reaches an
    outer ``except OSError``, so the outer one is not an escape; and an
    inner ``try`` catching only ``JSONDecodeError`` does not stop the
    decode reaching an outer ``except OSError`` that has no clause for
    it. Only the innermost handler that ANSWERS decides, either way.
    """
    holding = sorted(
        (statement for statement, owned in tries if id(node) in owned),
        key=lambda statement: statement.lineno,
        reverse=True,
    )
    for statement in holding:
        verdict = clause_fault(handler_clauses(statement, table))
        if verdict != "keep-looking":
            return verdict
    return None


#: How a text handle is read once ``open`` has handed it over. The walk
#: below chases the HANDLE because the decode is not at the ``open``: the
#: bytes become text at the read, and #320 found one live offender of
#: exactly this shape, ``factory.py``'s run-lock reading the holder pid
#: out of an ``"a+"`` handle under ``except OSError``.
HANDLE_READS = frozenset({"read", "readline", "readlines"})

#: Every OTHER member a text stream has: writing, seeking, closing and
#: metadata, none of which hands back decoded text. DERIVED from
#: ``io.TextIOWrapper`` rather than listed, for the reason #320's own
#: hand-written sets were replaced: a list is a clearing mechanism, and a
#: clearing mechanism nobody derives is one nobody notices is short.
#:
#: A member that is on NEITHER set is not "probably fine": it is a use of
#: the handle this walk has never heard of, and :func:`_handle_escapes`
#: makes it ``undecided``. Measured on ``kstrl/``: the tracked handles are
#: touched through ``fileno``, ``close``, ``seek``, ``truncate``,
#: ``write``, ``flush``, ``read``, ``readlines``, one ``for`` and seven
#: hand-overs, and every one of those lands in a modelled bucket.
HANDLE_SAFE = (
    frozenset(name for name in dir(io.TextIOWrapper) if not name.startswith("_")) - HANDLE_READS
)


def _text_handles(scope: ast.AST, table: Bindings) -> dict[str, ast.Call]:
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
    through it went unseen. It is :func:`_bound_opens` that then makes
    those two shapes ``undecided`` instead.

    A mode that does not fold is NOT entered, which is the reporting
    direction: the ``open`` itself is already a row under the mode rule.
    """
    found: dict[str, ast.Call] = {}
    for node in own_nodes(scope):
        opened = _opened_here(node)
        if opened is None:
            continue
        name, call = opened
        if _is_somebody_elses(call, table):
            continue
        if is_text(call) and decodes_text(call):
            found[name] = call
    return found


def _opened_here(node: ast.AST) -> tuple[str, ast.Call] | None:
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
class _Handle:
    """One ``open`` bound to one plain local name in one scope, and the
    loads of that name the walk could not account for."""

    name: str
    call: ast.Call
    scope: ast.AST
    loose: tuple[ast.Name, ...]


def _parents_of(scope: ast.AST) -> dict[int, ast.AST]:
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


def _handles_in(tree: ast.Module, table: Bindings) -> list[_Handle]:
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

    CLASSIFIED INTO EXACTLY ONE BUCKET closes the alias escapes;
    :func:`_is_modelled` is that half.

    A name whose loads are all accounted for is followable. A name with
    even one loose load is not, and the loose loads are also the rows
    :func:`_handle_escapes` reports, so the guard cannot clear a site it
    is simultaneously complaining about.
    """
    found: list[_Handle] = []
    for scope, _qualified in scopes(tree):
        handles = _text_handles(scope, table)
        if not handles:
            continue
        owned = {id(node) for node in own_nodes(scope)}
        parents = _parents_of(scope)
        inside = all_nodes(scope)
        for name, call in handles.items():
            loose = tuple(
                node
                for node in inside
                if isinstance(node, ast.Name)
                and node.id == name
                and not isinstance(node.ctx, ast.Store)
                and not (id(node) in owned and _is_modelled(node, parents))
            )
            found.append(_Handle(name=name, call=call, scope=scope, loose=loose))
    return found


def _bound_opens(tree: ast.Module, table: Bindings) -> set[int]:
    """The ids of every ``open`` call whose handle this walk can follow.

    #344's review found the hole this closes, and it was the guard's own
    skip direction: :func:`_classify` never charged the handler rule to an
    ``open``, because the decode happens at the read - and the read was
    only ever found through a NAME. So every spelling that binds no name,
    or binds one this walk cannot match, was written into the CLEARED
    inventory with both faults ``None``:

        open(p, encoding="utf-8").read()
        json.load(open(p, encoding="utf-8"))
        h = g = open(p, encoding="utf-8")
        self.h = open(p, encoding="utf-8")

    All four decode, and a ``UnicodeDecodeError`` from any of them walks
    past an enclosing ``except OSError`` exactly as #320 describes.
    Measured on the first: ``b'{"total": 5\xff}'`` raises straight past
    the handler while the guard said ``clear``.

    The fix is CLAUDE.md's guard-design rule 3 applied literally: a
    clearing guard that cannot PROVE a site is compliant must flag.
    Chasing more spellings would have left the same hole one spelling
    further out, because the chase is what over-matches; refusing to
    clear what it cannot follow is what does not.
    """
    return {id(handle.call) for handle in _handles_in(tree, table) if not handle.loose}


def _handle_reads(scope: ast.AST, handles: dict[str, ast.Call]) -> list[tuple[ast.AST, ast.Call]]:
    """Every decode through one of ``handles``, with the ``open`` that
    set the encoding.

    Three shapes, and each is a decode the ``open`` call itself is not:
    ``fh.read()``, iterating the handle, and handing the handle to
    somebody else (``json.load(fh)``, ``csv.reader(fh)``). The third is
    wide on purpose - it cannot know what the callee does with it - and
    wide is the reporting direction.

    Iterating covers a comprehension as well as a ``for``, and handing
    over covers a KEYWORD argument as well as a positional one. #344's
    review measured both gaps: ``json.load(fp=h)`` and ``[x for x in h]``
    each decoded under a bare ``except OSError`` while this walk cleared
    them.
    """
    found: list[tuple[ast.AST, ast.Call]] = []
    for node in own_nodes(scope):
        touched = _handle_touched(node, handles)
        if touched is not None:
            found.append((node, handles[touched]))
    return found


def _handle_touched(node: ast.AST, handles: dict[str, ast.Call]) -> str | None:
    """The handle name this node decodes through, or None."""
    if isinstance(node, ast.For | ast.comprehension):
        return _named(node.iter, handles)
    if not isinstance(node, ast.Call):
        return None
    receiver = node.func
    if isinstance(receiver, ast.Attribute) and receiver.attr in HANDLE_READS:
        return _named(receiver.value, handles)
    handed = [*node.args, *(keyword.value for keyword in node.keywords)]
    return next((got for arg in handed if (got := _named(arg, handles))), None)


def _handle_escapes(handles: list[_Handle], where: str) -> list[str]:
    """The loose loads of :func:`_handles_in`, as rows.

    The SAME computation that decides followability, so the guard cannot
    clear a site it is simultaneously complaining about. There is no
    fourth bucket and no benefit of the doubt: an alias (``other = h``),
    a bound method taken (``_read = h.read``), a closure over the
    handle, a return of it, a subscript, an attribute the walk has never
    heard of are all rows a reader has to answer for.
    """
    return [
        f"{where}:{node.lineno} {node.id} (a use of an open() handle this walk "
        "does not model, so the decode cannot be found)"
        for handle in handles
        for node in handle.loose
    ]


def _is_modelled(node: ast.Name, parents: dict[int, ast.AST]) -> bool:
    """Is this load of a handle name one the walk has a bucket for?

    The Attribute arm is what #344 round 3 finding 1 corrected, and the
    correction is to make it agree with :func:`_handle_touched`. That
    function models ``h.read()``: an ``ast.Call`` whose ``func`` IS the
    attribute. This one used to pass ANY attribute whose name was in
    either set, so ``_read = handle.read`` - the bound method taken and
    called later - was cleared here and never looked at there. Detached,
    the two halves miss in opposite directions. Attached, an attribute
    that is not immediately called is the handle's method or its
    ``buffer`` ESCAPING, which is a row.
    """
    up = parents.get(id(node))
    if isinstance(up, ast.Attribute):
        grand = parents.get(id(up))
        if not (isinstance(grand, ast.Call) and grand.func is up):
            return False
        return up.attr in HANDLE_READS or up.attr in HANDLE_SAFE
    if isinstance(up, ast.Call):
        return node in up.args
    if isinstance(up, ast.keyword):
        return True
    if isinstance(up, ast.For | ast.comprehension):
        return up.iter is node
    return False


def _named(node: ast.expr, handles: dict[str, ast.Call]) -> str | None:
    """The handle name this expression IS, or None.

    A walrus is unwrapped to its target, because ``json.load(h :=
    open(p, encoding="utf-8"))`` hands the handle over in the same
    expression that binds it. #344 round 3 finding 3: without this the
    binding was tracked, the hand-over was not seen, no LOAD of ``h``
    existed to be unaccounted for, and the site CLEARED - so adding
    ``h :=`` to the walk's own counterexample made it compliant again,
    and the churn pin then invited the author to record it as such.
    """
    if isinstance(node, ast.NamedExpr):
        node = node.target
    return node.id if isinstance(node, ast.Name) and node.id in handles else None


def _try_bodies(tree: ast.Module) -> list[tuple[ast.Try, set[int]]]:
    """Every ``try`` and the ids of the nodes its BODY owns.

    ``own_nodes`` stops at a nested function, so a read inside a ``def``
    written in a ``try`` body and called elsewhere is not credited to a
    handler that will never see its exception.
    """
    return [
        (node, {id(child) for child in try_body_nodes(node)})
        for node in all_nodes(tree)
        if isinstance(node, ast.Try)
    ]


def _is_somebody_elses(node: ast.Call, table: Bindings) -> bool:
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


def _classify(
    node: ast.Call,
    tries: list[tuple[ast.Try, set[int]]],
    table: Bindings,
    where: str,
    bound: set[int],
) -> Read | str | None:
    """One call, into a :class:`Read`, into ``undecided`` as a string, or
    into the decided-out set as ``None``.

    Decided out is a leaf that is not one of the two tokens, somebody
    else's ``open``, or an ``open`` whose folded mode is binary.
    :func:`_decided_out` is what stops that being a silent fourth bucket.

    ONLY a call the text can be READ back through gets the handler rule,
    and only at a ``read_text``. ``open`` does not decode; it hands back a
    handle, and the bytes become text at the read. Charging the handler
    rule to the ``open`` would have demanded a ``UnicodeDecodeError``
    clause from six ``fcntl.flock`` lock files that never read a byte
    through the handle - a handler that can never fire, written to
    satisfy a guard, which is worse than no guard. The reads themselves
    are covered by :func:`_through_handles`.
    """
    leaf = leaf_name(node.func)
    if leaf is None:
        return f"{where}:{node.lineno} {ast.unparse(node.func)}"
    if leaf not in READ_TOKENS or _is_somebody_elses(node, table):
        return None
    text = is_text(node)
    if text is None:
        return f"{where}:{node.lineno} {ast.unparse(node)[:70]} (mode does not fold)"
    if not text:
        return None
    if leaf == "open" and decodes_text(node) and is_strict(node) and id(node) not in bound:
        return (
            f"{where}:{node.lineno} {ast.unparse(node)[:70]} "
            "(the handle is never bound to a name this walk can follow, "
            "so the decode cannot be found)"
        )
    at_the_call = leaf == "read_text" and decodes_text(node) and is_strict(node)
    return Read(
        where=where,
        lineno=node.lineno,
        expr=ast.unparse(node)[:70],
        encoding_fault=encoding_fault(node),
        guard_fault=_guard_fault(node, tries, table) if at_the_call else None,
    )


def _decided_out(tree: ast.Module, table: Bindings, where: str) -> list[str]:
    """Every ``open``/``read_text`` call this walk decides is NOT its
    subject, as rows a test can pin.

    Without this the walk has a silent fourth bucket, which contradicts
    :class:`Scan`'s own claim to partition. :func:`_is_somebody_elses` is
    the step that clears on a resolution, so the day ``STDLIB_READERS``
    misses a real stdlib text reader with a resolvable origin -
    ``tokenize.open``, ``gzip.open`` - the site would vanish with no row
    and no failing test. Now it moves this pin instead.
    """
    return [
        f"{where}:{node.lineno} {ast.unparse(node.func)}"
        for node in all_nodes(tree)
        if isinstance(node, ast.Call)
        and leaf_name(node.func) in READ_TOKENS
        and (_is_somebody_elses(node, table) or is_text(node) is False)
    ]


def _read_expr(node: ast.AST) -> str:
    """One decode site as a short, stable string.

    A ``for`` statement unparses with its whole BODY attached, so the row
    for ``observability.py`` carried three lines of unrelated code and
    would have churned on any edit inside the loop. Only the header
    identifies the site.
    """
    if isinstance(node, ast.For | ast.comprehension):
        return f"for {ast.unparse(node.target)} in {ast.unparse(node.iter)}"
    return ast.unparse(node)[:60]


def _read_lineno(node: ast.AST) -> int:
    """The line a decode site sits on.

    ``ast.comprehension`` is not a statement and carries no ``lineno`` of
    its own, so the row is keyed on the iterable it walks - which is the
    handle, and the thing a reader would look for.
    """
    if isinstance(node, ast.comprehension):
        return node.iter.lineno
    return getattr(node, "lineno", 0)


def _through_handles(
    tree: ast.Module, tries: list[tuple[ast.Try, set[int]]], table: Bindings, where: str
) -> tuple[list[Read], list[str]]:
    """A :class:`Read` for every decode through an ``open``ed handle.

    The encoding is the OPEN's business and is already a row from
    :func:`_classify`, so these rows carry the handler rule only. What
    they add is the site #320 found live in ``kstrl/factory.py``: an
    ``"a+"`` run-lock handle whose ``fp.read(64)`` sits under ``except
    OSError`` with nothing for the decode.
    """
    tracked = _handles_in(tree, table)
    found: list[Read] = []
    escaped = _handle_escapes(tracked, where)
    for scope, _name in scopes(tree):
        handles = _text_handles(scope, table)
        if not handles:
            continue
        for node, opened in _handle_reads(scope, handles):
            if not is_strict(opened):
                continue
            found.append(
                Read(
                    where=where,
                    lineno=_read_lineno(node),
                    expr=f"{_read_expr(node)} on an open() handle",
                    encoding_fault=None,
                    guard_fault=_guard_fault(node, tries, table),
                )
            )
    return found, escaped


def scan_source(text: str, *, where: str = "", module: str = "") -> Scan:
    """Every read-mode text decode in one module's source, partitioned.

    Takes TEXT rather than a path so both halves can be exercised against
    planted fixtures. #318 round 3 records what walking files only cost
    there: the liveness check short-circuited past one half entirely, so
    that half could be neutered to ``return []`` with every test still
    green.

    The token gate is the walk's own precondition rather than an
    optimisation: every shape below needs ``read_text`` or ``open``
    written as an IDENTIFIER, and that net is :func:`spells`, which is
    layer 1's net exactly. So layer 2 walks precisely the modules layer 1
    counts, and the two halves cannot disagree about the population.

    The substring test first is the cheap half of the same gate. It is
    not the whole gate: ``kstrl/tui/app.py`` contains the letters
    ``open`` inside longer words and obtains no file text at all, and
    walking it produced two ``undecided`` rows about a call on the result
    of a call - noise a reader would learn to edit rather than read.
    """
    if not any(token in text for token in READ_TOKENS):
        return Scan()
    tree = parse(text)
    if not any(spells_a_token(node) for node in all_nodes(tree)):
        return Scan()
    table = bindings(tree, module=module)
    tries = _try_bodies(tree)
    bound = _bound_opens(tree, table)
    reads: list[Read] = []
    undecided: list[str] = []
    for node in all_nodes(tree):
        if not isinstance(node, ast.Call):
            continue
        got = _classify(node, tries, table, where, bound)
        if isinstance(got, Read):
            reads.append(got)
        elif got is not None:
            undecided.append(got)
    through, escaped = _through_handles(tree, tries, table, where)
    reads.extend(through)
    undecided.extend(escaped)
    return Scan(
        tuple(f"{read.where}:{read.lineno} {read.expr}" for read in reads if not read.faults()),
        tuple(read.row() for read in reads if read.faults()),
        tuple(undecided),
        tuple(_decided_out(tree, table, where)),
    )


def _scan_file(source: Path) -> Scan:
    """:func:`scan_source` for a file, tolerating one that will not read.

    The read is guarded on ``ValueError`` and not on ``OSError``, which is
    the same shape as the fix this module polices: a file that will not
    DECODE is a different fault from one that will not open, and a guard
    crashing on its own corpus must not be reported as a clean package.
    """
    try:
        text = source.read_text(encoding="utf-8")
    except ValueError:
        return Scan()
    try:
        return scan_source(text, where=label(source), module=module_name(source))
    except SyntaxError:
        return Scan()


@functools.cache
def package_scan() -> Scan:
    """One sweep of ``kstrl/``, cached because four tests ask for it.

    ``Scan`` is frozen and holds only tuples, so a caller cannot mutate
    what the next caller gets back. Measured: 302 ms a sweep warm.
    """
    total = Scan()
    for source in package_sources():
        one = _scan_file(source)
        total = Scan(
            total.clear + one.clear,
            total.reported + one.reported,
            total.undecided + one.undecided,
            total.decided_out + one.decided_out,
        )
    return total


def reported_sites(scan: Scan) -> Sites:
    """The reported half as :class:`Sites`, so ``assert_sites`` can pin
    both halves at once and neither can be left out."""
    return Sites(scan.reported, scan.undecided).sorted()
