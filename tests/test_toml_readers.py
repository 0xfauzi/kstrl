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

`handler_verdict` therefore checks `Exception` exactly, in three
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

Only the first was checked before this round, so a reader could have
passed the guard with a bare `except:` above a `TOMLDecodeError` clause.

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
carried the exact round-0 defect. The resolver below therefore follows
module aliases (`import tomllib as _tl`, `_p = tomllib`) and direct
function imports (`from tomllib import load as _load`), and
`TestTheWalkSeesWhatItClaimsTo` plants one fixture per form.

It attributes a parse to the innermost SCOPE, not to the innermost `try`
statement, so a helper DEFINED inside a `try` and CALLED elsewhere is
reported as unguarded rather than credited to a `try` that will never
see its exception. An earlier draft of this file listed that as a hole
it could not close; `tests/test_tui_config_walk.own_nodes` had already
closed the identical one 90 lines away, and `_own_nodes` is that
boundary applied here.

It still does NOT see a parse reached through a value the walk cannot
name: `getattr(mod, "load")`, a call on an object returned by a
function, a module fetched from a dict. That is stated rather than
silently absent, because round 2's docstring promised this guard "fails
the instant a fourth reader is written the way the first one was,
including in a file that does not exist yet" and that was not true of
the file the reviewer wrote.

It also does not see a file with no `tomllib` in its text, which is not
a limitation but the resolver's own precondition: every form above needs
the literal token. `_scan_file` gates on it, and the gate was measured
result-identical across all 127 package files while cutting the three
package scans from 1342 ms to 84 ms.

The two halves are biased in OPPOSITE directions, which is worth saying
plainly rather than dressing up as symmetry. On the handler side an
exception name the walk cannot resolve counts as INSUFFICIENT, so an
unreadable handler fails loudly. On the call side a module or function
name it cannot resolve makes the call INVISIBLE, so an unreadable call
passes silently. Only the first bias is safe; the second is the residue
of matching on names, and it is the reason the list above is written as
"does NOT see" rather than as a list of warnings this file emits. It
emits none.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT

KSTRL_PACKAGE = REPO_ROOT / "kstrl"

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

#: The functions this rule is about. ``tomllib.load`` decodes the stream
#: itself before it lexes, and parses by recursive descent, which is why
#: its failure family is wider than "bad TOML syntax" in two separate
#: directions.
TOML_PARSE_FUNCTIONS = frozenset({"load", "loads"})

TOML_MODULE = "tomllib"


def _imported_module_names(tree: ast.Module) -> set[str]:
    """Names bound to the ``tomllib`` module by an ``import`` statement.

    ``import tomllib`` and ``import tomllib as _tl``. Function-local
    imports count, since ``verify._default_typecheck_command`` uses one;
    the walk is over the whole tree rather than the module body.
    """
    names = {TOML_MODULE}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(
                alias.asname or alias.name for alias in node.names if alias.name == TOML_MODULE
            )
    return names


def _name_to_name_bindings(tree: ast.Module) -> list[tuple[str, str]]:
    """Every ``target = source`` where both sides are a bare name.

    Collected once so the fixed point below iterates over a list rather
    than re-walking the tree on each pass. Measured over ``kstrl/``:
    55 such bindings in 127 files, at most 8 in any one file, so the
    loop converges in two passes.
    """
    return [
        (target.id, node.value.id)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Name)
        for target in node.targets
        if isinstance(target, ast.Name)
    ]


def _module_aliases(tree: ast.Module) -> set[str]:
    """Every local name bound to the ``tomllib`` MODULE in this file.

    Imports plus rebinding through ``_p = tomllib``. The rebind pass runs
    to a fixed point so a chain (``a = tomllib`` then ``b = a``)
    resolves; it is bounded by the number of assignments in one file, so
    it terminates.
    """
    aliases = _imported_module_names(tree)
    bindings = _name_to_name_bindings(tree)
    changed = True
    while changed:
        changed = False
        for target, source in bindings:
            if source in aliases and target not in aliases:
                aliases.add(target)
                changed = True
    return aliases


def _from_import_names(tree: ast.Module) -> set[str]:
    """Names bound by ``from tomllib import load [as _l]``."""
    return {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == TOML_MODULE
        for alias in node.names
        if alias.name in TOML_PARSE_FUNCTIONS
    }


def _is_parse_attribute(value: ast.expr, aliases: set[str]) -> bool:
    """Is this expression ``<a tomllib alias>.load`` / ``.loads``?"""
    return (
        isinstance(value, ast.Attribute)
        and value.attr in TOML_PARSE_FUNCTIONS
        and isinstance(value.value, ast.Name)
        and value.value.id in aliases
    )


def _rebound_parse_names(tree: ast.Module, aliases: set[str]) -> set[str]:
    """Names bound by ``_l = tomllib.load``."""
    return {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and _is_parse_attribute(node.value, aliases)
        for target in node.targets
        if isinstance(target, ast.Name)
    }


def _direct_names(tree: ast.Module, aliases: set[str]) -> set[str]:
    """Local names bound directly to ``tomllib.load`` / ``.loads``.

    ``from tomllib import load``, ``from tomllib import loads as _l``,
    and ``_l = tomllib.load``. Without this a reader calling a bare
    ``load(fh)`` is invisible, which is one of the four forms the review
    used to defeat the round-2 walk.
    """
    return _from_import_names(tree) | _rebound_parse_names(tree, aliases)


def _is_parse_call(node: ast.AST, aliases: set[str], direct: set[str]) -> bool:
    """Is this node a call to a tomllib parse function?"""
    if not isinstance(node, ast.Call):
        return False
    if _is_parse_attribute(node.func, aliases):
        return True
    return isinstance(node.func, ast.Name) and node.func.id in direct


def _handler_names(handler: ast.ExceptHandler) -> set[str]:
    """Every exception name one ``except`` clause catches.

    A bare ``except:`` yields ``{"BaseException"}``, which is what it
    means and which :data:`OVER_BROAD_HANDLERS` then rejects. A tuple
    yields each member. Anything not a plain name or attribute (an
    aliased import, a computed tuple) yields nothing and so counts as
    insufficient - the conservative direction, because a guard that
    resolves names it cannot see is a guard that passes for reasons
    nobody checked.
    """
    if handler.type is None:
        return {"BaseException"}
    nodes = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    names: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


def handler_verdict(handlers: list[set[str]]) -> str | None:
    """``None`` if this ``try``'s clauses satisfy the rule, else why not.

    Three ways to fail, and the guard has to say which, because the
    remedy differs:

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
    if not handlers:
        return "has no handler at all"
    over = sorted(name for names in handlers for name in names & OVER_BROAD_HANDLERS)
    if over:
        return f"catches {over[0]}, which swallows KeyboardInterrupt and SystemExit"
    broad = [i for i, names in enumerate(handlers) if BROAD_HANDLER in names]
    if not broad:
        seen = sorted({name for names in handlers for name in names})
        return f"catches {seen} instead of Exception"
    if broad[-1] != len(handlers) - 1:
        return "catches Exception before a narrower clause, which can never run"
    return None


def _own_nodes(stmts: list[ast.stmt]) -> Iterator[ast.AST]:
    """Every node under *stmts* except those inside a nested function.

    So a parse is attributed to the innermost scope containing it. A
    plain ``ast.walk`` credits a ``try`` with guarding a parse that lives
    in a function DEFINED in its body and CALLED somewhere else, which
    the round-3 docstring listed as a hole this walk could not close. It
    can: ``tests/test_tui_config_walk.own_nodes`` closes the same one,
    and this is that boundary applied here.
    """
    stack: list[ast.AST] = list(stmts)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _parse_calls(nodes: Iterable[ast.AST], aliases: set[str], direct: set[str]) -> set[int]:
    """``id()`` of every tomllib parse call among *nodes*.

    Identity, not ``lineno``: two parses can share a physical line, and
    a covered-set keyed on the line number would then hide one of them.
    """
    return {id(node) for node in nodes if _is_parse_call(node, aliases, direct)}


def _guarded_parses(
    tree: ast.Module, aliases: set[str], direct: set[str]
) -> tuple[list[tuple[int, list[set[str]]]], set[int]]:
    """``(entries, covered ids)`` for every ``try`` holding a parse.

    An entry is ``(try line, one name set per handler, in source
    order)`` - a LIST and not a union, because :func:`handler_verdict`
    has to know which clause came last. A ``try`` whose body holds no
    parse is skipped, so the population is the parses and not the try
    statements.
    """
    entries: list[tuple[int, list[set[str]]]] = []
    covered: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        found = _parse_calls(_own_nodes(node.body), aliases, direct)
        if not found:
            continue
        covered |= found
        entries.append((node.lineno, [_handler_names(h) for h in node.handlers]))
    return entries, covered


def scan_source(text: str) -> tuple[list[tuple[int, list[set[str]]]], list[int]]:
    """``(guarded, unguarded)`` tomllib parses in one module's source.

    ``guarded`` is ``(try line, that try's handler name sets in order)``;
    ``unguarded`` is the line of each parse call with no enclosing
    ``try`` in this file.

    Takes TEXT rather than a path so both halves can be exercised against
    planted fixtures. Round 2 walked files only, and its liveness check
    therefore short-circuited past the unguarded walk entirely - which
    meant that half could be neutered to ``return []`` with every test
    still passing (#318 round 3, F3).
    """
    tree = ast.parse(text)
    aliases = _module_aliases(tree)
    direct = _direct_names(tree, aliases)
    guarded, covered = _guarded_parses(tree, aliases, direct)
    every = [node for node in ast.walk(tree) if _is_parse_call(node, aliases, direct)]
    unguarded = sorted(node.lineno for node in every if id(node) not in covered)
    return guarded, unguarded


def _scan_file(source: Path) -> tuple[list[tuple[int, list[set[str]]]], list[int]]:
    """:func:`scan_source` for a file, tolerating one that will not read.

    The read is OUTSIDE the guard and the guard catches only
    ``SyntaxError``, which is the same shape as the fix this module
    polices: a file that will not DECODE is a different fault from one
    that will not PARSE, and a ``ValueError`` raised by the walk itself
    must not be reported as a clean file. ``ValueError`` on the read
    covers ``UnicodeDecodeError``, which is the very lesson here.

    The substring gate is not an optimisation of last resort, it is the
    resolver's own precondition: every form the walk can see (``import
    tomllib``, ``import tomllib as _tl``, ``from tomllib import load``,
    ``_p = tomllib``, ``_l = tomllib.load``) needs the literal token in
    the file. Measured over ``kstrl/``: 3 of 127 files contain it, and
    the three package scans in this module drop from 1342 ms to 84 ms
    with identical results on every file.
    """
    try:
        text = source.read_text(encoding="utf-8")
    except ValueError:
        return [], []
    if TOML_MODULE not in text:
        return [], []
    try:
        return scan_source(text)
    except SyntaxError:
        return [], []


def _package_sources() -> list[Path]:
    return sorted(KSTRL_PACKAGE.rglob("*.py"))


# --------------------------------------------------------------------------
# Fixtures for the liveness checks. Each is a module the walk MUST see;
# they are source strings rather than files on disk so that planting one
# cannot leave a stray importable module behind in `kstrl/`.
# --------------------------------------------------------------------------

#: ``(name, import prologue, the call expression)`` for the four shapes
#: that defeated the round-2 walk, plus the literal one it did handle,
#: plus the module rebind.
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
        guarded, unguarded = scan_source(guarded_fixture(prologue, call))

        assert len(guarded) == 1, f"{name}: walk did not see the parse"
        assert unguarded == []

    @pytest.mark.parametrize(
        ("name", "prologue", "call"), PARSE_FORMS, ids=[n for n, _, _ in PARSE_FORMS]
    )
    def test_an_unguarded_parse_is_found_through_every_alias_form(
        self, name: str, prologue: str, call: str
    ) -> None:
        guarded, unguarded = scan_source(unguarded_fixture(prologue, call))

        assert guarded == []
        assert len(unguarded) == 1, f"{name}: walk did not see the unguarded parse"

    @pytest.mark.parametrize(
        ("name", "prologue", "call"), PARSE_FORMS, ids=[n for n, _, _ in PARSE_FORMS]
    )
    def test_a_narrow_handler_on_every_form_is_judged_narrow(
        self, name: str, prologue: str, call: str
    ) -> None:
        """The two halves joined: seeing the parse is worth nothing if
        the verdict on its handler is wrong."""
        guarded, _ = scan_source(guarded_fixture(prologue, call))

        verdict = handler_verdict(guarded[0][1])

        assert verdict is not None and "instead of Exception" in verdict

    def test_a_sufficient_handler_is_judged_sufficient(self) -> None:
        """The other direction, so the rule is not trivially "always an
        offender" - which would pass every fixture above while failing
        the whole package."""
        guarded, _ = scan_source(
            guarded_fixture("import tomllib as _tl\n", "_tl.load(fh)", "Exception")
        )

        assert handler_verdict(guarded[0][1]) is None

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
            guarded, _ = scan_source(source)

            verdict = handler_verdict(guarded[0][1])

            assert verdict is not None and "swallows KeyboardInterrupt" in verdict, handler

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

        wrong_guarded, _ = scan_source(wrong)
        right_guarded, _ = scan_source(right)

        assert handler_verdict(wrong_guarded[0][1]) == (
            "catches Exception before a narrower clause, which can never run"
        )
        assert handler_verdict(right_guarded[0][1]) is None

    def test_a_parse_in_a_function_defined_inside_a_try_is_not_credited_to_it(
        self,
    ) -> None:
        """The hole the round-3 docstring called unfixable.

        ``def inner(fh): return tomllib.load(fh)`` written inside a
        ``try`` is not guarded by that ``try`` at all - ``inner`` runs
        wherever it is called. Attributing the parse to the innermost
        SCOPE rather than to the innermost ``try`` statement is what
        ``tests/test_tui_config_walk.own_nodes`` already does, and the
        walk now does it too."""
        source = (
            "import tomllib\ndef outer(a):\n    try:\n"
            "        def inner(fh): return tomllib.load(fh)\n"
            "    except Exception:\n        inner = None\n    return inner\n"
        )

        guarded, unguarded = scan_source(source)

        assert guarded == []
        assert unguarded == [4]

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

        guarded, unguarded = scan_source(source)

        assert len(guarded) == 1
        assert unguarded == [5]

    def test_the_package_scan_still_reaches_real_modules(self) -> None:
        """And the walk is pointed at code that exists. If kstrl stops
        parsing TOML in these modules the rule below has become a no-op
        and should be deleted rather than left as decoration."""
        modules = {source.name for source in _package_sources() if any(_scan_file(source))}

        assert {"config.py", "verify.py", "feedforward.py"} <= modules, modules


class TestNoTomlReaderEnumeratesItsExceptions:
    """The class of defect #318 shipped three times, caught structurally."""

    def test_every_guarded_toml_parse_ends_on_a_bare_exception_clause(self) -> None:
        offenders: list[str] = []
        for source in _package_sources():
            guarded, _ = _scan_file(source)
            for lineno, handlers in guarded:
                verdict = handler_verdict(handlers)
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
        for source in _package_sources():
            _, unguarded = _scan_file(source)
            offenders.extend(f"{source.relative_to(REPO_ROOT)}:{line}" for line in unguarded)

        assert offenders == [], (
            f"{offenders} parse TOML with no exception handler at all. "
            f"A malformed, non-utf-8 or deeply nested file there is a raw "
            f"traceback."
        )
