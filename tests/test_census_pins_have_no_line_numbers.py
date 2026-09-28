"""No census in tests/ pins a kstrl call site by line number (#645).

A pin such as ``"cli.py:3164 kstrl.interaction.PromptRequest"`` fails when an
unrelated edit lands above the site, and the fix is to type the new number in
without reading the row. Two censuses were re-pinned that way on main for line
shifts alone, with no site added, moved to another function or removed. They
now key by module and enclosing qualified scope (``cli.py::factory``); this
guard fails the next constant that goes back to a line number.

WHAT IT FLAGS: a string constant anywhere inside the value of an assignment
(``=``, annotated or augmented; module, class or function scope) to an
UPPER_CASE name, when the string holds ``<path>.py:<digits>`` and the path is
a file under ``kstrl/``, as written or with a ``kstrl/`` prefix. It FLAGS, so
over-matching costs a false positive somebody reads, never a site cleared.

WHAT IT DOES NOT SEE, each pinned as a strict xfail below. (1) A line-number
row passed as DATA to a call, such as the ``astwalk.Sites(...)`` fixtures in
``tests/test_astwalk.py``: test input for a parser, not a census pin. (2) A
line held as an int beside its path, ``("cli.py", 3164)``: a count dict has the
same shape. (3) A path that does not name a kstrl file as written or under
``kstrl/``, such as the bare basename ``retry.py`` of ``tui/screens/retry.py``:
without that check the guard flags 15 fixture strings in tests/ such as
``src/handler.py:42``. The census pins write the path ``label()`` gives, which
this guard does read.
"""

from __future__ import annotations

import ast
import re

import pytest

from tests.helpers.astwalk import (
    KSTRL_PACKAGE,
    REPO_ROOT,
    TESTS_DIR,
    all_nodes,
    blind_spot,
    label,
    parse,
    parsed,
    test_sources,
)

#: ``<path>.py:<digits>`` anywhere in a string. The path's own character
#: class is the only boundary, so ``[cli.py:3164]`` and ``at=cli.py:3164``
#: are seen too.
LINE_PIN = re.compile(r"([A-Za-z0-9_/.]+\.py):(\d+)")

#: A constant's name: ``EXPECTED_SEEN``, ``_ALLOWED``.
UPPER_CASE = re.compile(r"^_*[A-Z][A-Z0-9_]*$")


def _names_a_kstrl_module(path: str) -> bool:
    """Is ``path`` a real file under ``kstrl/``, as written or under it?"""
    package = KSTRL_PACKAGE.resolve()
    for candidate in (REPO_ROOT / path, KSTRL_PACKAGE / path):
        resolved = candidate.resolve()
        if resolved.is_file() and resolved.is_relative_to(package):
            return True
    return False


def _upper_case_targets(node: ast.AST) -> list[str]:
    """The UPPER_CASE names an assignment binds, including inside a tuple
    target and on an attribute (``cls.EXPECTED``)."""
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, ast.AnnAssign | ast.AugAssign):
        targets = [node.target]
    else:
        return []
    names = [
        part.id if isinstance(part, ast.Name) else part.attr
        for target in targets
        for part in ast.walk(target)
        if isinstance(part, ast.Name | ast.Attribute)
    ]
    return [name for name in names if UPPER_CASE.match(name)]


def _pinned_lines(value: ast.AST) -> list[str]:
    """Every ``path.py:N`` naming a kstrl module in the strings under ``value``."""
    strings = [
        inner.value
        for inner in ast.walk(value)
        if isinstance(inner, ast.Constant) and isinstance(inner.value, str)
    ]
    return [
        f"{match.group(1)}:{match.group(2)}"
        for text in strings
        for match in LINE_PIN.finditer(text)
        if _names_a_kstrl_module(match.group(1))
    ]


def line_number_pins(tree: ast.Module) -> list[str]:
    """Every ``NAME path.py:N`` pin of a kstrl module in this module's constants."""
    hits: list[str] = []
    for node in all_nodes(tree):
        names = _upper_case_targets(node)
        value = getattr(node, "value", None)
        if names and value is not None:
            hits.extend(f"{names[0]} {pin}" for pin in _pinned_lines(value))
    return hits


class TestNoCensusPinsALineNumber:
    def test_no_constant_in_tests_pins_a_kstrl_line(self) -> None:
        found = {
            label(source, TESTS_DIR): hits
            for source in test_sources()
            if (hits := line_number_pins(parsed(source)))
        }
        assert found == {}, (
            "these constants pin a kstrl call site by line number, so any edit "
            "above the site fails them. Key the row by module and enclosing "
            "scope instead: calls_to(..., owner=scope_of(tree, lambdas=True)) "
            f"gives 'cli.py::factory'. Found: {found}"
        )

    def test_a_planted_line_number_pin_is_flagged(self) -> None:
        """The control: a walk that has been switched off returns ``{}`` above
        too, so the same function must flag these planted pins, one per
        scope an assignment can sit in, and clear the scoped form."""
        source = (
            'EXPECTED = ("cli.py:3164 kstrl.interaction.PromptRequest",)\n'
            'SCOPED = ("cli.py::factory kstrl.interaction.PromptRequest",)\n'
            'UNREAL = ("no_such_module.py:12 x",)\n'
            "class TestX:\n"
            '    PINS: tuple[str, ...] = ("kstrl/linear.py:320 urlopen",)\n'
            "def test_y():\n"
            '    ROWS = {"tui/app.py:390": 1}\n'
            '    SITES += (f"gateparse.py:112 {x}",)\n'
            'WRAPPED = ["at [kstrl/signals.py:556]"]\n'
            '_PRIVATE = ("pipeline.py:1 x",)\n'
            "def setup(cls):\n"
            '    cls.ATTR = ("loop.py:2 x",)\n'
        )
        assert sorted(line_number_pins(parse(source))) == [
            "ATTR loop.py:2",
            "EXPECTED cli.py:3164",
            "PINS kstrl/linear.py:320",
            "ROWS tui/app.py:390",
            "SITES gateparse.py:112",
            "WRAPPED kstrl/signals.py:556",
            "_PRIVATE pipeline.py:1",
        ]


class TestTheDisclosedLimits:
    @pytest.mark.xfail(
        strict=True,
        raises=AssertionError,
        reason="each shape is outside what the guard reads; see the module docstring",
    )
    @pytest.mark.parametrize(
        "source",
        [
            'def test_z():\n    astwalk.Sites((), ("gateparse.py:111 TOOL_PARSERS[chosen]",))\n',
            'EXPECTED = (("cli.py", 3164),)\n',
            'EXPECTED = ("retry.py:395 kstrl.interaction.PromptRequest",)\n',
        ],
        ids=["row-passed-as-call-data", "line-held-as-an-int", "bare-basename-of-a-nested-module"],
    )
    def test_a_shape_the_guard_does_not_read_is_missed(self, source: str) -> None:
        blind_spot(lambda text: bool(line_number_pins(parse(text))), source)
