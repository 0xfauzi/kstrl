"""#318: a tomllib reader may not enumerate the exceptions it catches.

`load_toml_document` shipped a fix for this three times:

    round   handler                what still escaped
    0       TOMLDecodeError        UnicodeDecodeError (a ValueError)
    1       + UnicodeDecodeError   plain ValueError (4301-digit integer)
    2       + ValueError           RecursionError (a RuntimeError)
    3       Exception              nothing about the document

Each escape reached the CLI entry seam and took all thirteen non-exempt
commands down with a raw traceback.

The mechanism is the same every time, and it is not "the author was
careless about one file". It is that `tomllib`'s error taxonomy belongs
to `tomllib`: a reader enumerating subclasses is asserting something
about the standard library that it cannot check. Prose already said so -
`CLAUDE.md` has stated the encoding half since #291 - and round 1's own
docstring CITED `verify._default_typecheck_command` as the precedent
while writing a handler narrower than the precedent it cited.

Round 2 added a guard here, and the guard made the same mistake one level
up: it defined sufficiency as `ValueError`, so the `RecursionError` that
took the CLI down in round 3 would have passed it. A guard that encodes
the wrong ceiling is worse than no guard, because it converts a live hole
into a documented all-clear.

`handler_verdict` therefore checks `Exception` exactly, in four
directions, and each direction has a fixture that fails when only that
check is removed:

    too narrow   anything short of `Exception`, which is the defect
                 itself; `ValueError` is what round 2 shipped.
    too wide     `BaseException` or a bare `except:`, which swallow
                 `KeyboardInterrupt` and `SystemExit`. A first cut here
                 listed `BaseException` as sufficient, on the reasoning
                 that wider cannot be worse. It is: those two are about
                 the PROCESS, not the file.
    out of order a broad clause that is not last, which makes every
                 specific clause after it dead and every message they
                 were written to keep unreachable.
    unreadable   a clause whose type this walk cannot name at all. It
                 arrives as `Clause.decided is False` rather than as an
                 empty name set, because an empty set reads exactly like
                 "catches nothing", which is the worst possible
                 misreading of "catches something I could not see".

Only the first was checked before this round, so a reader could have
passed the guard with a bare `except:` above a `TOMLDecodeError` clause.

TWO LAYERS, since #324.

LAYER 1, :func:`TestNoTomlReaderEnumeratesItsExceptions.
test_no_module_gets_hold_of_tomllib_without_appearing_here`, is a census
of every expression in `kstrl/` that SPELLS `tomllib`, per module. It
enumerates no node types and no fields: a module cannot parse TOML with
the standard library's parser without naming it, so a fourth reader in
any shape has to change that dict first, whatever it does afterwards. It
reaches the import, the call, `getattr(tomllib, "load")` and
`importlib.import_module("tom" + "llib")` alike, because it asks for
every string a node holds and folds what it can.

LAYER 2 is the walk below, which resolves names and says which line and
which clause. It is not redundant: layer 1 can only say "config.py's
count moved", which is the wrong message when the answer is "this parse
ends on a `ValueError`". Layer 1 in turn catches what layer 2 cannot, and
the list is at the bottom of this docstring.

WHY THE POPULATION IS ZERO, AND WHY THAT MATTERS
------------------------------------------------
`tests/test_atomicio.py` and `tests/test_process_scoping.py` both landed
their AST walks at offender count zero, and both say why: a guard that
ships with a suppression list is a guard that rots. This one keys on the
CALL rather than on the handler, so its population is every tomllib parse
in the package - three sites, all compliant as of this change:

    kstrl/config.py       except Exception  (#318 round 3)
    kstrl/verify.py       except Exception  (#318 round 3, was ValueError)
    kstrl/feedforward.py  except Exception  (x2, pre-existing)

WHAT THIS GUARD SEES, STATED HONESTLY
-------------------------------------
Round 2's version matched only a literal `tomllib.` prefix, and a review
defeated it with a five-line module using `import tomllib as _tl` - three
tests passed, ruff and mypy clean, nothing objected, while the file
carried the exact round-0 defect. Round 3 wrote a private resolver for
that, and #324 measured two holes in it: `_p: object = tomllib` made a
parse invisible in BOTH directions, reported neither guarded nor
unguarded, and `from .tomllib import load` was reported as an unguarded
stdlib parse when it is a local module. Name resolution now comes from
`tests/helpers/astwalk`, which handles `ImportFrom.level` and annotated
assignment, and `TestTheWalkSeesWhatItClaimsTo` plants a fixture per form.

It attributes a parse to the innermost SCOPE, not to the innermost `try`
statement, so a helper DEFINED inside a `try` and CALLED elsewhere is
reported as unguarded rather than credited to a `try` that will never see
its exception. That boundary is `astwalk.own_nodes`.

A parse reached through a value the walk cannot name -
`TABLE["f"](text)`, a call on the result of a call - is no longer
silently absent. It lands in `Scan.undecided`, and the inventory test
pins that half at empty, so the day one appears the guard fails with the
site rather than passing.

The one thing layer 2 does not look at is a module whose text never
contains `tomllib`. That is the resolver's own precondition, not a
tolerated hole: every form it can see needs the literal token, and the
gate was measured result-identical over all 127 package files while
cutting the three package scans from 1342 ms to 84 ms. What it costs is
an UNDECIDED parse in such a file - `p = get_parser()` then `p.load(fh)`
with no `tomllib` anywhere - and that is layer 1's half, since the module
that handed `p` over had to spell the name. Pinned by
`test_a_module_that_never_names_tomllib_is_not_walked`.

The two halves are biased in OPPOSITE directions, which is worth saying
plainly rather than dressing up as symmetry. On the handler side an
exception name the walk cannot resolve counts as INSUFFICIENT, so an
unreadable handler fails loudly. On the call side a module or function
name it cannot resolve makes the call UNDECIDED, which is a row in a
pinned list rather than a warning this file emits. It emits none.
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.helpers.astwalk import (
    REPO_ROOT,
    Bindings,
    Clause,
    Sites,
    assert_census,
    assert_sites,
    bindings,
    blind_spot,
    calls_to,
    handler_clauses,
    label,
    module_name,
    own_nodes,
    package_sources,
    parse,
    spells,
)

#: The catch-all a tomllib reader must end on.
#:
#: ``Exception`` and not ``ValueError``: see the module docstring. Round
#: 2 set this to ``ValueError`` and thereby certified as safe the exact
#: handler that ``RecursionError`` walked through.
BROAD_HANDLER = "Exception"

#: Wider than ``Exception``, and therefore ALSO an offender.
#:
#: A first cut listed ``BaseException`` alongside ``Exception`` as
#: sufficient, on the reasoning that wider cannot be worse. It can: a
#: bare ``except:`` or ``except BaseException`` swallows
#: ``KeyboardInterrupt`` and ``SystemExit``, which the module this guard
#: polices says in its own docstring must never be relabelled as the
#: operator's broken config. The rule has two sides and this is the
#: second one.
OVER_BROAD_HANDLERS = frozenset({"BaseException"})

TOML_MODULE = "tomllib"

#: The functions this rule is about, as the DOTTED ORIGINS the resolver
#: reports rather than as bare names. ``tomllib.load`` decodes the stream
#: itself before it lexes, and parses by recursive descent, which is why
#: its failure family is wider than "bad TOML syntax" in two separate
#: directions.
#:
#: Origins and not names is what closed #324's second logged hole: a
#: bare ``load`` is only this parse when the resolver says it came from
#: ``tomllib``, and ``from .tomllib import load`` says it did not.
TOML_PARSE_TARGETS = frozenset({f"{TOML_MODULE}.load", f"{TOML_MODULE}.loads"})


@dataclass(frozen=True)
class Scan:
    """One module's parses, in the three states a parse can be in.

    ``undecided`` has no default, in the sense that :func:`scan_source`
    always fills it: a call the resolver could not follow is a row here
    rather than an absence from the other two, which is the whole subject
    of #324.
    """

    guarded: tuple[tuple[int, tuple[Clause, ...]], ...] = ()
    unguarded: tuple[int, ...] = ()
    undecided: tuple[str, ...] = ()
    #: One row per parse the walk resolved, keyed by module and origin
    #: rather than by line, so that editing a file above a parse does not
    #: fail the inventory while adding a parse still does.
    parses: tuple[str, ...] = ()


def handler_verdict(clauses: Sequence[Clause]) -> str | None:
    """``None`` if this ``try``'s clauses satisfy the rule, else why not.

    Four ways to fail, and the guard has to say which, because the
    remedy differs:

    - unreadable: a clause whose type the walk cannot name, so nothing
      can be concluded about what it catches;
    - too wide: any clause names ``BaseException``, or is a bare
      ``except:``, which swallows ``KeyboardInterrupt``;
    - too narrow: no clause names ``Exception``, which is the defect
      #318 shipped three times;
    - out of order: a clause names ``Exception`` and is not the last
      one, so every clause after it is dead and every specific message
      it was meant to keep is lost.

    Order is checked here rather than left to
    ``test_the_broad_handler_must_come_last``, which pins one function
    in one file. A new reader in a new file gets the same rule.
    """
    if not clauses:
        return "has no handler at all"
    if any(not clause.decided for clause in clauses):
        return "catches an expression this walk cannot name, so nothing is known about it"
    over = sorted(name for clause in clauses for name in clause.names & OVER_BROAD_HANDLERS)
    if over:
        return f"catches {over[0]}, which swallows KeyboardInterrupt and SystemExit"
    broad = [i for i, clause in enumerate(clauses) if BROAD_HANDLER in clause.names]
    if not broad:
        seen = sorted({name for clause in clauses for name in clause.names})
        return f"catches {seen} instead of Exception"
    if broad[-1] != len(clauses) - 1:
        return "catches Exception before a narrower clause, which can never run"
    return None


def _is_parse_call(node: ast.AST, table: Bindings) -> bool:
    """Is this node a call that RESOLVES to a tomllib parse function?

    The whole of the resolution lives in ``table``: module aliases,
    module rebinds through a plain or an annotated assignment, direct
    function imports, a rebind of a rebind, a relative import that only
    looks like the stdlib, and ``getattr`` with a foldable name.
    """
    return isinstance(node, ast.Call) and table.resolve(node.func) in TOML_PARSE_TARGETS


def _body_nodes(node: ast.Try) -> list[ast.AST]:
    """Every node in this ``try``'s BODY, stopping at a nested function.

    So a parse is attributed to the innermost scope containing it. A
    plain ``ast.walk`` credits a ``try`` with guarding a parse that lives
    in a function DEFINED in its body and CALLED somewhere else, which
    the round-3 docstring listed as a hole this walk could not close.
    ``astwalk.own_nodes`` is that boundary; the loop here is what applies
    it to a list of statements rather than to one node, and what stops a
    ``def`` at the top of the body being descended into.
    """
    found: list[ast.AST] = []
    for stmt in node.body:
        found.append(stmt)
        if not isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
            found.extend(own_nodes(stmt))
    return found


def _guarded_parses(
    tree: ast.Module, table: Bindings
) -> tuple[list[tuple[int, tuple[Clause, ...]]], set[int]]:
    """``(entries, covered ids)`` for every ``try`` holding a parse.

    An entry is ``(try line, one clause per handler, in source order)`` -
    a sequence and not a union, because :func:`handler_verdict` has to
    know which clause came last. A ``try`` whose body holds no parse is
    skipped, so the population is the parses and not the try statements.

    Identity, not ``lineno``, for the covered set: two parses can share a
    physical line, and a covered-set keyed on the line number would then
    hide one of them.
    """
    entries: list[tuple[int, tuple[Clause, ...]]] = []
    covered: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        found = {id(child) for child in _body_nodes(node) if _is_parse_call(child, table)}
        if not found:
            continue
        covered |= found
        entries.append((node.lineno, tuple(handler_clauses(node, table))))
    return entries, covered


def scan_source(text: str, *, where: str = "", module: str = "") -> Scan:
    """Every tomllib parse in one module's source, in its three states.

    Takes TEXT rather than a path so all three states can be exercised
    against planted fixtures. Round 2 walked files only, and its liveness
    check therefore short-circuited past the unguarded walk entirely -
    which meant that half could be neutered to ``return []`` with every
    test still passing (#318 round 3, F3).

    The substring gate is not an optimisation of last resort, it is the
    resolver's own precondition: every form the walk can see needs the
    literal token in the file. Measured over ``kstrl/``: 3 of 127 files
    contain it, and the three package scans in this module drop from
    1342 ms to 84 ms with identical results on every file. What it costs
    is pinned by ``test_a_module_that_never_names_tomllib_is_not_walked``.
    """
    if TOML_MODULE not in text:
        return Scan()
    tree = parse(text)
    table = bindings(tree, module=module)
    guarded, covered = _guarded_parses(tree, table)
    every = [node for node in ast.walk(tree) if _is_parse_call(node, table)]
    return Scan(
        guarded=tuple(guarded),
        unguarded=tuple(sorted(node.lineno for node in every if id(node) not in covered)),
        undecided=calls_to(tree, TOML_PARSE_TARGETS, where=where, module=module).undecided,
        parses=tuple(f"{where}: {table.resolve(node.func)}" for node in every),
    )


def _scan_file(source: Path) -> Scan:
    """:func:`scan_source` for a file, tolerating one that will not read.

    The read is OUTSIDE the guard and the guard catches only
    ``SyntaxError``, which is the same shape as the fix this module
    polices: a file that will not DECODE is a different fault from one
    that will not PARSE, and a ``ValueError`` raised by the walk itself
    must not be reported as a clean file. ``ValueError`` on the read
    covers ``UnicodeDecodeError``, which is the very lesson here.
    """
    try:
        text = source.read_text(encoding="utf-8")
    except ValueError:
        return Scan()
    try:
        return scan_source(text, where=label(source), module=module_name(source))
    except SyntaxError:
        return Scan()


#: Every expression in ``kstrl/`` that spells ``tomllib``, per module.
#: Layer 1, and it resolves nothing: a module cannot parse TOML with the
#: standard library's parser without naming it, so a fourth reader has to
#: change this dict whatever shape it takes.
#:
#: Adding a row is not forbidden, it is the point: the diff that adds one
#: is where somebody says why new code needs the TOML parser and how it
#: handles the four unrelated exception families ``tomllib.load`` raises.
EXPECTED_TOMLLIB_SPELLINGS: dict[str, int] = {
    # the import, the parse, and the TOMLDecodeError clause above it
    "config.py": 3,
    # the import and two parses
    "feedforward.py": 3,
    # a function-local import and one parse
    "verify.py": 2,
}

#: Every parse layer 2 resolves, keyed by module and origin. Four calls
#: in three modules, and the same three the docstring names.
EXPECTED_TOML_PARSES: tuple[str, ...] = (
    "config.py: tomllib.loads",
    "feedforward.py: tomllib.loads",
    "feedforward.py: tomllib.loads",
    "verify.py: tomllib.loads",
)


# --------------------------------------------------------------------------
# Fixtures for the liveness checks. Each is a module the walk MUST see;
# they are source strings rather than files on disk so that planting one
# cannot leave a stray importable module behind in `kstrl/`.
# --------------------------------------------------------------------------

#: ``(name, import prologue, the call expression)`` for the four shapes
#: that defeated the round-2 walk, plus the literal one it did handle,
#: plus the module rebind, plus the two #324 measured on round 3's
#: private resolver: an ANNOTATED module rebind, which was invisible in
#: both directions, and ``getattr`` with a foldable name.
#:
#: One table rather than two hand-written source lists. The two lists it
#: replaced had drifted: the unguarded half covered only 3 of the 6
#: forms, and nothing made that visible. Building both source strings
#: from one row makes the two halves cover the same population by
#: construction.
PARSE_FORMS: list[tuple[str, str, str]] = [
    ("literal", "import tomllib\n", "tomllib.load(fh)"),
    ("module_alias", "import tomllib as _tl\n", "_tl.load(fh)"),
    ("from_import", "from tomllib import load\n", "load(fh)"),
    ("from_import_alias", "from tomllib import loads as _l\n", "_l(fh)"),
    ("module_rebind", "import tomllib\n_p = tomllib\n", "_p.load(fh)"),
    ("function_rebind", "import tomllib\n_l = tomllib.load\n", "_l(fh)"),
    ("annotated_module_rebind", "import tomllib\n_p: object = tomllib\n", "_p.load(fh)"),
    ("getattr", "import tomllib\n", 'getattr(tomllib, "load")(fh)'),
]


def guarded_fixture(prologue: str, call: str, handler: str = "ValueError") -> str:
    """A module with exactly one parse, inside a try whose handler is
    whatever *handler* says. The default is INSUFFICIENT, so a walk that
    sees the parse reports one offender and a walk that misses it
    reports none."""
    return (
        f"{prologue}def f(fh):\n    try:\n        return {call}\n"
        f"    except {handler}:\n        return {{}}\n"
    )


def unguarded_fixture(prologue: str, call: str) -> str:
    """The same module with no ``try`` at all, for the other half of the
    walk. Round 2 had no fixture for this half and could not have had
    one, because the only thing exercising it was a repo scan that must
    find nothing."""
    return f"{prologue}def f(fh):\n    return {call}\n"


def _spellings(source: str) -> int:
    """How many nodes in one snippet spell ``tomllib``. Layer 1, on text."""
    sees = spells(TOML_MODULE)
    return sum(1 for node in ast.walk(parse(source)) if sees(node))


def _surfaced(source: str) -> int:
    """How many parses layer 2 reports about one snippet, in any state."""
    scan = scan_source(source)
    return len(scan.guarded) + len(scan.unguarded) + len(scan.undecided)


class TestTheWalkSeesWhatItClaimsTo:
    """The guard's own guard, against planted fixtures rather than
    against the repo.

    A net whose only exercise is a repo that must contain zero matches
    cannot tell "nothing is wrong" from "I am looking at nothing". Round
    2's liveness test asserted ``guarded or unguarded`` over real files,
    and because every real parse is guarded the ``or`` short-circuited:
    the unguarded half could be replaced with ``return []`` and all
    three tests still passed. Each half now has a fixture only it can
    satisfy, and each was watched failing with the other half neutered.
    """

    @pytest.mark.parametrize(
        ("name", "prologue", "call"), PARSE_FORMS, ids=[n for n, _, _ in PARSE_FORMS]
    )
    def test_a_guarded_parse_is_found_through_every_alias_form(
        self, name: str, prologue: str, call: str
    ) -> None:
        scan = scan_source(guarded_fixture(prologue, call))

        assert len(scan.guarded) == 1, f"{name}: walk did not see the parse"
        assert scan.unguarded == ()
        assert scan.undecided == ()

    @pytest.mark.parametrize(
        ("name", "prologue", "call"), PARSE_FORMS, ids=[n for n, _, _ in PARSE_FORMS]
    )
    def test_an_unguarded_parse_is_found_through_every_alias_form(
        self, name: str, prologue: str, call: str
    ) -> None:
        scan = scan_source(unguarded_fixture(prologue, call))

        assert scan.guarded == ()
        assert len(scan.unguarded) == 1, f"{name}: walk did not see the unguarded parse"
        assert scan.undecided == ()

    @pytest.mark.parametrize(
        ("name", "prologue", "call"), PARSE_FORMS, ids=[n for n, _, _ in PARSE_FORMS]
    )
    def test_a_narrow_handler_on_every_form_is_judged_narrow(
        self, name: str, prologue: str, call: str
    ) -> None:
        """The two halves joined: seeing the parse is worth nothing if
        the verdict on its handler is wrong."""
        scan = scan_source(guarded_fixture(prologue, call))

        verdict = handler_verdict(scan.guarded[0][1])

        assert verdict is not None and "instead of Exception" in verdict

    def test_a_sufficient_handler_is_judged_sufficient(self) -> None:
        """The other direction, so the rule is not trivially "always an
        offender" - which would pass every fixture above while failing
        the whole package."""
        scan = scan_source(guarded_fixture("import tomllib as _tl\n", "_tl.load(fh)", "Exception"))

        assert handler_verdict(scan.guarded[0][1]) is None

    def test_a_handler_wider_than_exception_is_an_offender_too(self) -> None:
        """The rule has two sides. ``except BaseException`` and a bare
        ``except:`` cover everything a parse can raise AND swallow
        ``KeyboardInterrupt``, which `load_toml_document`'s own docstring
        forbids. A first cut listed ``BaseException`` as sufficient."""
        for handler, source in (
            (
                "BaseException",
                guarded_fixture("import tomllib\n", "tomllib.load(fh)", "BaseException"),
            ),
            (
                "bare",
                "import tomllib\ndef f(fh):\n    try:\n        return tomllib.load(fh)\n"
                "    except:\n        return {}\n",
            ),
        ):
            scan = scan_source(source)

            verdict = handler_verdict(scan.guarded[0][1])

            assert verdict is not None and "swallows KeyboardInterrupt" in verdict, handler

    def test_a_handler_the_walk_cannot_name_is_an_offender(self) -> None:
        """The fourth direction, and the one an empty name set used to
        hide. ``except EXCEPTIONS[0]:`` yields no name, and a matcher
        that reported "catches []" was reporting a set that reads exactly
        like "catches nothing" for a clause that may catch everything.
        ``Clause.decided`` is what tells the two apart."""
        source = (
            "import tomllib\nEXCEPTIONS = (Exception,)\ndef f(fh):\n    try:\n"
            "        return tomllib.load(fh)\n    except EXCEPTIONS[0]:\n        return {}\n"
        )

        scan = scan_source(source)

        assert handler_verdict(scan.guarded[0][1]) == (
            "catches an expression this walk cannot name, so nothing is known about it"
        )

    def test_the_broad_clause_must_be_last_in_any_file_not_just_config(self) -> None:
        """Order is a property of the rule, not of one function.
        ``test_the_broad_handler_must_come_last`` in
        ``tests/test_config_toml.py`` pins it behaviourally for
        ``load_toml_document``; this pins it structurally for a reader
        that does not exist yet."""
        wrong = (
            "import tomllib\ndef f(fh):\n    try:\n        return tomllib.load(fh)\n"
            "    except Exception:\n        return {}\n"
            "    except tomllib.TOMLDecodeError:\n        return None\n"
        )
        right = (
            "import tomllib\ndef f(fh):\n    try:\n        return tomllib.load(fh)\n"
            "    except tomllib.TOMLDecodeError:\n        return None\n"
            "    except Exception:\n        return {}\n"
        )

        assert handler_verdict(scan_source(wrong).guarded[0][1]) == (
            "catches Exception before a narrower clause, which can never run"
        )
        assert handler_verdict(scan_source(right).guarded[0][1]) is None

    def test_a_parse_in_a_function_defined_inside_a_try_is_not_credited_to_it(
        self,
    ) -> None:
        """The hole the round-3 docstring called unfixable.

        ``def inner(fh): return tomllib.load(fh)`` written inside a
        ``try`` is not guarded by that ``try`` at all - ``inner`` runs
        wherever it is called. Attributing the parse to the innermost
        SCOPE rather than to the innermost ``try`` statement is what
        ``astwalk.own_nodes`` does, and the walk now does it too."""
        source = (
            "import tomllib\ndef outer(a):\n    try:\n"
            "        def inner(fh): return tomllib.load(fh)\n"
            "    except Exception:\n        inner = None\n    return inner\n"
        )

        scan = scan_source(source)

        assert scan.guarded == ()
        assert scan.unguarded == (4,)

    def test_two_parses_on_one_line_are_counted_separately(self) -> None:
        """The covered set keys on node identity, not on ``lineno``. A
        first cut keyed on the line number, which collapses two parses
        that share one physical line into a single entry."""
        source = (
            "import tomllib\ndef f(a, b):\n"
            "    try: x = tomllib.load(a)\n"
            "    except Exception: x = None\n"
            "    return x, tomllib.load(b)\n"
        )

        scan = scan_source(source)

        assert len(scan.guarded) == 1
        assert scan.unguarded == (5,)

    def test_a_local_module_of_the_same_name_is_not_the_stdlib(self) -> None:
        """#324's other measured hole in round 3's resolver.

        ``from .tomllib import load`` binds a LOCAL module's ``load``, and
        the private walk dropped ``ImportFrom.level`` and reported it as
        an unguarded stdlib parse: a false positive, in a guard whose
        whole population is supposed to be real parses. The resolver
        keys on the dotted ORIGIN now, and a relative import resolves
        against the module's own package."""
        scan = scan_source("from .tomllib import load\nload(fh)\n", module="kstrl.config")

        assert scan.guarded == ()
        assert scan.unguarded == ()
        assert scan.undecided == ()

    def test_a_parse_through_a_name_the_walk_cannot_follow_is_undecided(self) -> None:
        """Not a miss, and that is the #324 change.

        A parse fetched out of a table is a call with no name to read.
        Round 3 reported such a file as clean; there is no third bucket
        now, so it is a row the inventory has to account for."""
        scan = scan_source('import tomllib\nT = {"f": tomllib.load}\nT["f"](fh)\n')

        assert scan.guarded == ()
        assert scan.unguarded == ()
        assert scan.undecided == ("3 T['f']",)

    def test_the_package_scan_still_reaches_real_modules(self) -> None:
        """And the walk is pointed at code that exists. If kstrl stops
        parsing TOML in these modules the rule below has become a no-op
        and should be deleted rather than left as decoration."""
        modules = {source.name for source in package_sources() if _scan_file(source).parses}

        assert {"config.py", "verify.py", "feedforward.py"} <= modules, modules


class TestNoTomlReaderEnumeratesItsExceptions:
    """The class of defect #318 shipped three times, caught structurally."""

    def test_no_module_gets_hold_of_tomllib_without_appearing_here(self) -> None:
        """Layer 1, the net: pin every spelling of the module itself.

        A module cannot parse TOML with the standard library's parser
        without naming it, so NEW code that reaches for it has to change
        this dict, whatever shape the parse takes afterwards. That is why
        this layer resolves nothing and enumerates no node types: an
        exact count of spellings has no shape list to be incomplete.

        It reaches what layer 2 cannot: ``importlib.import_module``, a
        module handed in as a parameter, a parse through a table. What it
        cannot reach is a name the INTERPRETER has to build, pinned by
        ``test_a_module_name_the_interpreter_has_to_build_is_missed``.
        """
        assert_census(
            sources=package_sources(),
            sees=spells(TOML_MODULE),
            expected=EXPECTED_TOMLLIB_SPELLINGS,
            control="import tomllib\ntomllib.load(fh)\n",
            message=(
                "The set of places that name tomllib changed. A fourth TOML reader must "
                "end on a bare `except Exception`: tomllib.load raises TOMLDecodeError, "
                "UnicodeDecodeError, plain ValueError and RecursionError, and the taxonomy "
                "is tomllib's to extend. Add the row once the reader below passes."
            ),
        )

    def test_every_toml_parse_in_the_package_is_accounted_for(self) -> None:
        """Layer 2's population, and the half that used to be silence.

        ``undecided=()`` is a claim about the walk rather than an absence
        from it: a parse the resolver cannot follow is a row here, and
        pinning the list at empty is what makes the day one appears a
        failure rather than a quiet pass.
        """
        found = Sites()
        for source in package_sources():
            scan = _scan_file(source)
            found += Sites(scan.parses, scan.undecided)

        assert_sites(
            found.sorted(),
            seen=EXPECTED_TOML_PARSES,
            undecided=(),
            message="The set of tomllib parses in kstrl/ changed.",
        )

    def test_every_guarded_toml_parse_ends_on_a_bare_exception_clause(self) -> None:
        offenders: list[str] = []
        for source in package_sources():
            for lineno, clauses in _scan_file(source).guarded:
                verdict = handler_verdict(clauses)
                if verdict is not None:
                    offenders.append(f"{source.relative_to(REPO_ROOT)}:{lineno} {verdict}")

        assert offenders == [], (
            f"{offenders}. A tomllib parse must end on a bare `except "
            f"Exception`, and on nothing wider or narrower. tomllib.load "
            f"raises TOMLDecodeError, UnicodeDecodeError, plain ValueError "
            f"(CPython's integer-digit limit) AND RecursionError (nested "
            f"arrays, a RuntimeError) - and the taxonomy is tomllib's to "
            f"extend. #318 enumerated three times and was wrong three "
            f"times, each escape taking thirteen of sixteen CLI commands "
            f"down with a raw traceback. Report individually the causes "
            f"you can actually name, keep all I/O outside the guard, and "
            f"put the broad clause LAST, the way "
            f"kstrl.config.load_toml_document does."
        )

    def test_no_toml_parse_is_left_unguarded(self) -> None:
        """The other half: a handler that is wrong and no handler at all
        differ only in which line the traceback comes from."""
        offenders: list[str] = []
        for source in package_sources():
            offenders.extend(
                f"{source.relative_to(REPO_ROOT)}:{line}" for line in _scan_file(source).unguarded
            )

        assert offenders == [], (
            f"{offenders} parse TOML with no exception handler at all. "
            f"A malformed, non-utf-8 or deeply nested file there is a raw "
            f"traceback."
        )


class TestTheDisclosedLimits:
    """Every "this cannot see X" above, with a test behind it.

    Under ``xfail(strict=True)``, so the day a walk gets stronger the row
    XPASSes and the docstring has to be edited in the same diff. A
    disclosure with no test behind it rots silently.
    """

    @pytest.mark.xfail(strict=True, raises=AssertionError, reason="layer 1 folds, it does not run")
    def test_a_module_name_the_interpreter_has_to_build_is_missed(self) -> None:
        """Layer 1 folds ``"tom" + "llib"`` and every f-string it can
        decide. What it cannot decide is a value that needs the
        interpreter: ``"".join(...)``, ``%``-formatting, a name looked up
        at run time."""
        blind_spot(
            _spellings,
            'import importlib\nimportlib.import_module("".join(("tom", "llib"))).load(fh)\n',
        )

    @pytest.mark.xfail(strict=True, raises=AssertionError, reason="the resolver's precondition")
    def test_a_module_that_never_names_tomllib_is_not_walked(self) -> None:
        """Layer 2's gate, and its exact cost: an UNDECIDED parse in a
        file with no ``tomllib`` in its text. The module that handed the
        parser over had to spell the name, so layer 1 counts it there."""
        blind_spot(_surfaced, "p = get_parser()\np.load(fh)\n")
