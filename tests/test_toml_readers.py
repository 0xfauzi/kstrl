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
into a documented all-clear. Hence `SUFFICIENT_HANDLERS` below is
`Exception`, which is a ceiling and not a fourth guess: everything a
parser can say about a DOCUMENT derives from it, and what does not
(`KeyboardInterrupt`, `SystemExit`) is about the process and must never
be swallowed.

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

It does NOT see: a parse reached through a value the walk cannot name -
`getattr(mod, "load")`, a call on an object returned by a function, a
module fetched from a dict - or a helper DEFINED inside a `try` and
CALLED outside it. Those are stated rather than silently absent, because
round 2's docstring promised this guard "fails the instant a fourth
reader is written the way the first one was, including in a file that
does not exist yet" and that was not true of the file the reviewer wrote.

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
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT

KSTRL_PACKAGE = REPO_ROOT / "kstrl"

#: Handler types that cover the whole family a parse can raise.
#:
#: ``Exception`` and not ``ValueError``: see the module docstring. Round
#: 2 set this to ``ValueError`` and thereby certified as safe the exact
#: handler that ``RecursionError`` walked through.
SUFFICIENT_HANDLERS = frozenset({"Exception", "BaseException"})

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
    than re-walking the tree on each pass.
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
    func = node.func
    if isinstance(func, ast.Attribute):
        return (
            func.attr in TOML_PARSE_FUNCTIONS
            and isinstance(func.value, ast.Name)
            and func.value.id in aliases
        )
    return isinstance(func, ast.Name) and func.id in direct


def _handler_names(handler: ast.ExceptHandler) -> set[str]:
    """Every exception name one ``except`` clause catches.

    A bare ``except:`` yields ``{"BaseException"}``, which is what it
    means. A tuple yields each member. Anything not a plain name or
    attribute (an aliased import, a computed tuple) yields nothing and so
    counts as insufficient - the conservative direction, because a guard
    that resolves names it cannot see is a guard that passes for reasons
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


def _parse_call_lines(nodes: list[ast.AST], aliases: set[str], direct: set[str]) -> set[int]:
    """Line numbers of every tomllib parse call under *nodes*.

    Called with a ``Try`` body to find the parses that node guards, and
    with the whole module to find every parse there is. The difference
    between the two is the unguarded set, which is how both halves of
    the walk are computed from one traversal rule instead of two that
    can drift apart.
    """
    return {
        child.lineno
        for node in nodes
        for child in ast.walk(node)
        if _is_parse_call(child, aliases, direct)
    }


def _guarded_parses(
    tree: ast.Module, aliases: set[str], direct: set[str]
) -> tuple[list[tuple[int, set[str]]], set[int]]:
    """``(entries, covered lines)`` for every ``try`` holding a parse.

    An entry is ``(try line, every handler name on that try)``. A ``try``
    whose body holds no parse is skipped entirely, so the population is
    the parses and not the try statements.
    """
    entries: list[tuple[int, set[str]]] = []
    covered: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        lines = _parse_call_lines(list(node.body), aliases, direct)
        if not lines:
            continue
        covered |= lines
        entries.append((node.lineno, _all_handler_names(node)))
    return entries, covered


def _all_handler_names(node: ast.Try) -> set[str]:
    """Every exception name across all of one ``try``'s handlers."""
    names: set[str] = set()
    for handler in node.handlers:
        names |= _handler_names(handler)
    return names


def scan_source(text: str) -> tuple[list[tuple[int, set[str]]], list[int]]:
    """``(guarded, unguarded)`` tomllib parses in one module's source.

    ``guarded`` is ``(try line, every handler name on that try)``;
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
    every = _parse_call_lines([tree], aliases, direct)
    return guarded, sorted(every - covered)


def _scan_file(source: Path) -> tuple[list[tuple[int, set[str]]], list[int]]:
    """:func:`scan_source` for a file, tolerating one that will not read.

    A file that will not parse or decode is a defect for ruff and mypy to
    report, not a reason for this walk to fail obscurely. ``ValueError``
    covers ``UnicodeDecodeError``, which is the very lesson this module
    is about.
    """
    try:
        return scan_source(source.read_text(encoding="utf-8"))
    except (SyntaxError, ValueError):
        return [], []


def _package_sources() -> list[Path]:
    return sorted(KSTRL_PACKAGE.rglob("*.py"))


# --------------------------------------------------------------------------
# Fixtures for the liveness checks. Each is a module the walk MUST see;
# they are source strings rather than files on disk so that planting one
# cannot leave a stray importable module behind in `kstrl/`.
# --------------------------------------------------------------------------

#: The four shapes that defeated the round-2 walk, plus the literal one
#: it did handle. Every entry contains exactly one guarded parse whose
#: handler is INSUFFICIENT, so a walk that sees it reports one offender
#: and a walk that misses it reports none.
ALIAS_FORMS: list[tuple[str, str]] = [
    (
        "literal",
        "import tomllib\ndef f(fh):\n    try:\n        return tomllib.load(fh)\n"
        "    except tomllib.TOMLDecodeError:\n        return {}\n",
    ),
    (
        "module_alias",
        "import tomllib as _tl\ndef f(fh):\n    try:\n        return _tl.load(fh)\n"
        "    except _tl.TOMLDecodeError:\n        return {}\n",
    ),
    (
        "from_import",
        "from tomllib import load\ndef f(fh):\n    try:\n        return load(fh)\n"
        "    except ValueError:\n        return {}\n",
    ),
    (
        "from_import_alias",
        "from tomllib import loads as _l\ndef f(s):\n    try:\n        return _l(s)\n"
        "    except ValueError:\n        return {}\n",
    ),
    (
        "module_rebind",
        "import tomllib\n_p = tomllib\ndef f(fh):\n    try:\n        return _p.load(fh)\n"
        "    except ValueError:\n        return {}\n",
    ),
    (
        "function_rebind",
        "import tomllib\n_l = tomllib.load\ndef f(fh):\n    try:\n        return _l(fh)\n"
        "    except ValueError:\n        return {}\n",
    ),
]

#: The same forms with no handler at all, for the unguarded walk. Round 2
#: had no fixture for this half and could not have had one, because the
#: only thing exercising it was a repo scan that must find nothing.
UNGUARDED_FORMS: list[tuple[str, str]] = [
    ("literal", "import tomllib\ndef f(fh):\n    return tomllib.load(fh)\n"),
    ("module_alias", "import tomllib as _tl\ndef f(fh):\n    return _tl.load(fh)\n"),
    ("from_import", "from tomllib import load\ndef f(fh):\n    return load(fh)\n"),
]


class TestTheWalkSeesWhatItClaimsTo:
    """The guard's own guard, against planted fixtures rather than
    against the repo.

    A net whose only exercise is a repo that must contain zero matches
    cannot tell "nothing is wrong" from "I am looking at nothing". Round
    2's liveness test asserted `_guarded or _unguarded` over real files,
    and because every real parse is guarded the `or` short-circuited:
    `_unguarded_toml_parses` could be replaced with `return []` and all
    three tests still passed.
    """

    @pytest.mark.parametrize(("name", "source"), ALIAS_FORMS, ids=[n for n, _ in ALIAS_FORMS])
    def test_a_guarded_parse_is_found_through_every_alias_form(
        self,
        name: str,
        source: str,
    ) -> None:
        guarded, unguarded = scan_source(source)

        assert len(guarded) == 1, f"{name}: walk did not see the parse"
        assert unguarded == []

    @pytest.mark.parametrize(
        ("name", "source"),
        UNGUARDED_FORMS,
        ids=[n for n, _ in UNGUARDED_FORMS],
    )
    def test_an_unguarded_parse_is_found_through_every_alias_form(
        self,
        name: str,
        source: str,
    ) -> None:
        guarded, unguarded = scan_source(source)

        assert guarded == []
        assert len(unguarded) == 1, f"{name}: walk did not see the unguarded parse"

    def test_the_insufficient_handlers_in_those_fixtures_are_judged_insufficient(
        self,
    ) -> None:
        """The two halves joined: seeing the parse is worth nothing if the
        verdict on its handler is wrong. Four of the six fixtures catch
        something narrower than ``Exception``."""
        verdicts = {}
        for name, source in ALIAS_FORMS:
            guarded, _ = scan_source(source)
            verdicts[name] = bool(guarded[0][1] & SUFFICIENT_HANDLERS)

        assert verdicts == dict.fromkeys(verdicts, False)

    def test_a_sufficient_handler_is_judged_sufficient(self) -> None:
        """The other direction, so the rule is not trivially "always
        insufficient" - which would pass every fixture above while
        failing the whole package."""
        guarded, _ = scan_source(
            "import tomllib as _tl\ndef f(fh):\n    try:\n        return _tl.load(fh)\n"
            "    except OSError:\n        raise\n"
            "    except Exception:\n        return {}\n"
        )

        assert len(guarded) == 1
        assert guarded[0][1] & SUFFICIENT_HANDLERS

    def test_the_package_scan_still_reaches_real_modules(self) -> None:
        """And the walk is pointed at code that exists. If kstrl stops
        parsing TOML in these modules the rule below has become a no-op
        and should be deleted rather than left as decoration."""
        modules = {source.name for source in _package_sources() if any(_scan_file(source))}

        assert {"config.py", "verify.py", "feedforward.py"} <= modules, modules


class TestNoTomlReaderEnumeratesItsExceptions:
    """The class of defect #318 shipped three times, caught structurally."""

    def test_every_guarded_toml_parse_catches_the_whole_family(self) -> None:
        offenders: list[str] = []
        for source in _package_sources():
            guarded, _ = _scan_file(source)
            for lineno, names in guarded:
                if not names & SUFFICIENT_HANDLERS:
                    rel = source.relative_to(REPO_ROOT)
                    offenders.append(f"{rel}:{lineno} catches {sorted(names)}")

        assert offenders == [], (
            f"{offenders} guard a tomllib parse with an enumeration of "
            f"exception types instead of the whole class. tomllib.load "
            f"raises TOMLDecodeError, UnicodeDecodeError, plain ValueError "
            f"(CPython's integer-digit limit) AND RecursionError (nested "
            f"arrays, a RuntimeError) - and the taxonomy is tomllib's to "
            f"extend. #318 enumerated three times and was wrong three "
            f"times, each escape taking thirteen of sixteen CLI commands "
            f"down with a raw traceback. Catch Exception, re-raise OSError "
            f"above it, and report individually the causes you can "
            f"actually name, the way kstrl.config.load_toml_document does."
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
