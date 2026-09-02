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

THE INVARIANT, in one sentence: a read is CLEARED only when the walk
has PROVEN both halves of it - that the bytes are decoded as utf-8, and
that every construct enclosing the decode either does not swallow or
covers ``UnicodeDecodeError``. Everything else is a row.

That sentence took four review rounds to reach, and rounds one to three
are why it is phrased as a proof rather than as a list of cases. Each
round the walk cleared a shape that escaped at run time - eight, then
nine, then nineteen - and each time the shape was new but the mistake
was not: some step answered "I did not find a problem" and the caller
read it as "there is no problem". #344 round 4 replaced the ninth patch
with the rule itself. :class:`Verdict` has three answers and no fourth,
and ``unproven`` never collapses into ``clear``; :func:`handles_in`
decides followability in one place with one classifier;
:func:`swallowers` enumerates what can swallow rather than matching the
one spelling of it.

``tests/test_encoding_invariant.py`` tests the sentence against CPython
rather than against the shapes that motivated it: it plants each shape,
runs it against real undecodable bytes, and fails if anything the walk
cleared raised.

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
from dataclasses import dataclass
from pathlib import Path

from tests.helpers.astwalk import (
    Bindings,
    Sites,
    all_nodes,
    bindings,
    label,
    leaf_name,
    module_name,
    package_sources,
    parse,
    spells,
)
from tests.helpers.encodingguards import (
    Swallower,
    guard_verdict,
    swallowers,
    verdict_parts,
)
from tests.helpers.encodinghandles import (
    bound_opens,
    handle_escapes,
    handles_in,
    is_somebody_elses,
    read_lineno,
)
from tests.helpers.encodingrules import (
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
    #: None when the walk PROVED what swallows around this read; else why
    #: it could not. Option A's field: a site the walk cannot prove
    #: compliant is undecided, never cleared. #344 round 4.
    unproven: str | None = None

    def faults(self) -> list[str]:
        return [f for f in (self.encoding_fault, self.guard_fault) if f is not None]

    def row(self) -> str:
        return f"{self.where}:{self.lineno} {'; '.join(self.faults())}"

    def row_unproven(self) -> str:
        return f"{self.where}:{self.lineno} {self.expr} ({self.unproven})"


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


def _classify(
    node: ast.Call,
    swallowers: list[tuple[Swallower, set[int]]],
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
    if leaf not in READ_TOKENS or is_somebody_elses(node, table):
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
    guard, unproven = (
        verdict_parts(guard_verdict(node, swallowers, table)) if at_the_call else (None, None)
    )
    return Read(
        where=where,
        lineno=node.lineno,
        expr=ast.unparse(node)[:70],
        encoding_fault=encoding_fault(node),
        guard_fault=guard,
        unproven=unproven,
    )


def _decided_out(tree: ast.Module, table: Bindings, where: str) -> list[str]:
    """Every ``open``/``read_text`` call this walk decides is NOT its
    subject, as rows a test can pin.

    Without this the walk has a silent fourth bucket, which contradicts
    :class:`Scan`'s own claim to partition. :func:`is_somebody_elses` is
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
        and (is_somebody_elses(node, table) or is_text(node) is False)
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


def _through_handles(
    tree: ast.Module,
    swallowers: list[tuple[Swallower, set[int]]],
    table: Bindings,
    where: str,
) -> tuple[list[Read], list[str]]:
    """A :class:`Read` for every decode through an ``open``ed handle.

    The encoding is the OPEN's business and is already a row from
    :func:`_classify`, so these rows carry the handler rule only. What
    they add is the site #320 found live in ``kstrl/factory.py``: an
    ``"a+"`` run-lock handle whose ``fp.read(64)`` sits under ``except
    OSError`` with nothing for the decode.
    """
    tracked = handles_in(tree, table)
    found: list[Read] = []
    escaped = handle_escapes(tracked, where)
    for handle in tracked:
        if not is_strict(handle.call):
            continue
        for node in handle.decodes:
            guard, unproven = verdict_parts(guard_verdict(node, swallowers, table))
            found.append(
                Read(
                    where=where,
                    lineno=read_lineno(node),
                    expr=f"{_read_expr(node)} on an open() handle",
                    encoding_fault=None,
                    guard_fault=guard,
                    unproven=unproven,
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
    ladder = swallowers(tree)
    bound = bound_opens(tree, table)
    reads: list[Read] = []
    undecided: list[str] = []
    for node in all_nodes(tree):
        if not isinstance(node, ast.Call):
            continue
        got = _classify(node, ladder, table, where, bound)
        if isinstance(got, Read):
            reads.append(got)
        elif got is not None:
            undecided.append(got)
    through, escaped = _through_handles(tree, ladder, table, where)
    reads.extend(through)
    undecided.extend(escaped)
    return _partition(reads, undecided, tuple(_decided_out(tree, table, where)))


def _partition(reads: list[Read], undecided: list[str], decided_out: tuple[str, ...]) -> Scan:
    """The three buckets, and THE ORDER IS THE POLICY.

    A read with a fault is reported, because a named defect is the most
    actionable thing this walk can say. A read with no fault that it
    could not PROVE anything about is undecided. Only a read that is both
    fault-free and proven is cleared.

    #344 round 4's option A is exactly the last sentence: for three
    review rounds the walk answered "clear" where the honest answer was
    "I did not look inside that construct", and each round's review found
    more of it. The third answer never collapses into the first.
    """
    return Scan(
        tuple(
            f"{read.where}:{read.lineno} {read.expr}"
            for read in reads
            if not read.faults() and not read.unproven
        ),
        tuple(read.row() for read in reads if read.faults()),
        tuple(undecided)
        + tuple(read.row_unproven() for read in reads if not read.faults() and read.unproven),
        decided_out,
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
