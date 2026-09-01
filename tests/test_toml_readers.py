"""#318: a tomllib reader may not enumerate the exceptions it catches.

`load_toml_document` shipped a fix for this twice. Round 1 caught only
`tomllib.TOMLDecodeError`, so a non-utf-8 byte raised `UnicodeDecodeError`
and escaped. Round 2 added `UnicodeDecodeError`, so a 4301-digit integer
raised a PLAIN `ValueError` from CPython's `sys.get_int_max_str_digits`
limit and escaped that. Both times the escape reached the CLI entry seam
and took every non-exempt command down with a raw traceback.

The mechanism is the same both times and it is not "the author was
careless about one file". It is that `tomllib`'s error taxonomy belongs
to `tomllib`: a reader enumerating its subclasses is asserting something
about the standard library that it cannot check, and CPython is free to
add a fourth. Prose already said so - `CLAUDE.md` has stated "must catch
`ValueError` alongside `OSError`" since #291, and round 1's own docstring
CITED `verify._default_typecheck_command` as the precedent while writing
a handler narrower than the precedent it cited. Prose lost twice.

So this is the net, in the shape `tests/test_atomicio.py` and
`tests/test_process_scoping.py` already use for the same job: an AST walk
that fails on the CLASS of mistake rather than on any instance of it.

WHY IT CAN BE WRITTEN NOW AND COULD NOT HAVE BEEN BEFORE
--------------------------------------------------------
Both precedents landed at offender count ZERO, and both say so - a guard
that ships with a suppression list is a guard that rots, which
`test_process_scoping.py` records refusing once already. Round 1 declined
an AST guard because the natural rule ("any `except OSError` guarding a
decoding read") had roughly fifteen live offenders across ten modules,
and the narrow alternative ("any `except tomllib.TOMLDecodeError` must
also name `ValueError`") had exactly one match, which the change that
introduced it satisfied trivially.

This rule is neither. It keys on the CALL, not on the handler, so its
population is every `tomllib.load`/`loads` call site in the package -
three today, all three compliant:

    kstrl/config.py       except ValueError            (this PR)
    kstrl/verify.py       except (ValueError, OSError) (#288)
    kstrl/feedforward.py  except Exception             (x2, pre-existing)

Zero offenders, no allowlist, and it fails the instant a fourth reader is
written the way the first one was - including in a file that does not
exist yet, which is where round 3 would otherwise live.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.conftest import REPO_ROOT

KSTRL_PACKAGE = REPO_ROOT / "kstrl"

#: Handler types that cover the whole family. ``ValueError`` is the base
#: of ``TOMLDecodeError`` and ``UnicodeDecodeError`` both; ``Exception``
#: is wider still. Anything else is an enumeration of subclasses, which
#: is the mistake.
SUFFICIENT_HANDLERS = frozenset({"ValueError", "Exception", "BaseException"})

#: The calls this rule is about. ``tomllib.load`` decodes the stream
#: itself before it lexes, which is why its failure family is wider than
#: "bad TOML syntax" and why enumerating it goes wrong.
TOML_PARSE_CALLS = frozenset({"load", "loads"})


def _handler_names(handler: ast.ExceptHandler) -> set[str]:
    """Every exception name one ``except`` clause catches.

    A bare ``except:`` yields ``{"BaseException"}``, which is what it
    means. A tuple yields each member. Anything not a plain name or
    attribute (an aliased import, a computed tuple) yields nothing and
    so counts as insufficient - the conservative direction, because a
    guard that resolves names it cannot see is a guard that passes for
    reasons nobody checked.
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


def _parses_toml(node: ast.AST) -> bool:
    """Does this subtree call ``tomllib.load`` or ``tomllib.loads``?

    Matched on the attribute, so a function-local ``import tomllib``
    (which ``verify._default_typecheck_command`` uses) is covered the
    same as a module-level one.
    """
    return any(
        isinstance(child, ast.Call)
        and isinstance(child.func, ast.Attribute)
        and child.func.attr in TOML_PARSE_CALLS
        and isinstance(child.func.value, ast.Name)
        and child.func.value.id == "tomllib"
        for child in ast.walk(node)
    )


def _guarded_toml_parses(source: Path) -> list[tuple[int, set[str]]]:
    """``(line, handler names)`` for every guarded tomllib parse in a file."""
    try:
        tree = ast.parse(source.read_text(encoding="utf-8"))
    except (SyntaxError, ValueError):
        # A file that will not parse or decode is a defect for ruff and
        # mypy to report, not a reason for this walk to fail obscurely.
        # (``ValueError`` covers ``UnicodeDecodeError``, which is the
        # very lesson this module is about.)
        return []
    found: list[tuple[int, set[str]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        if not any(_parses_toml(stmt) for stmt in node.body):
            continue
        names: set[str] = set()
        for handler in node.handlers:
            names |= _handler_names(handler)
        found.append((node.lineno, names))
    return found


def _unguarded_toml_parses(source: Path) -> list[int]:
    """Lines where a tomllib parse sits outside any ``try`` in this file.

    Walked separately from the guarded case because "no handler at all"
    and "the wrong handler" are different defects with the same cause,
    and reporting them in one list would make the message useless.
    """
    try:
        tree = ast.parse(source.read_text(encoding="utf-8"))
    except (SyntaxError, ValueError):
        return []
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            for stmt in node.body:
                guarded.update(
                    child.lineno for child in ast.walk(stmt) if isinstance(child, ast.Call)
                )
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in TOML_PARSE_CALLS
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "tomllib"
        and node.lineno not in guarded
    ]


class TestNoTomlReaderEnumeratesItsExceptions:
    """The class of defect #318 shipped twice, caught structurally."""

    def test_every_guarded_toml_parse_catches_the_whole_value_error_family(
        self,
    ) -> None:
        offenders: list[str] = []
        for source in sorted(KSTRL_PACKAGE.rglob("*.py")):
            for lineno, names in _guarded_toml_parses(source):
                if not names & SUFFICIENT_HANDLERS:
                    rel = source.relative_to(REPO_ROOT)
                    offenders.append(f"{rel}:{lineno} catches {sorted(names)}")

        assert offenders == [], (
            f"{offenders} guard a tomllib parse without naming ValueError "
            f"(or something wider). tomllib.load raises TOMLDecodeError, "
            f"UnicodeDecodeError AND plain ValueError - the last from "
            f"CPython's integer-digit limit - and the taxonomy is "
            f"tomllib's to extend. #318 enumerated the subclasses twice "
            f"and was wrong twice; the second escape took thirteen of "
            f"sixteen CLI commands down with a raw traceback. Catch "
            f"ValueError and report the ones you can name individually, "
            f"the way kstrl.config.load_toml_document does."
        )

    def test_the_walk_actually_finds_the_call_sites(self) -> None:
        """The guard's own guard.

        A net that silently matches nothing passes forever and protects
        nothing, which is the failure mode of every AST check written
        against a pattern that later moved. This pins that the walk is
        still looking at real code: kstrl parses TOML in more than one
        module, and if that stops being true the rule above has quietly
        become a no-op and should be deleted rather than left as
        decoration.
        """
        modules = {
            source.name
            for source in KSTRL_PACKAGE.rglob("*.py")
            if _guarded_toml_parses(source) or _unguarded_toml_parses(source)
        }

        assert {"config.py", "verify.py", "feedforward.py"} <= modules, modules

    def test_no_toml_parse_is_left_unguarded(self) -> None:
        """The other half: a handler that is wrong and no handler at all
        differ only in which line the traceback comes from."""
        offenders: list[str] = []
        for source in sorted(KSTRL_PACKAGE.rglob("*.py")):
            for lineno in _unguarded_toml_parses(source):
                offenders.append(f"{source.relative_to(REPO_ROOT)}:{lineno}")

        assert offenders == [], (
            f"{offenders} parse TOML with no exception handler at all. "
            f"A malformed or non-utf-8 file there is a raw traceback."
        )
