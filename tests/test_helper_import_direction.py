"""`tests/helpers/` may not import from a test module (#354 round 2).

The direction dependencies run in is one way: a test module imports a
helper, never the reverse. Two helpers had it backwards.
`tests/helpers/fakegh.py` imported the private `_write_executable` out of
`tests/test_serve_seam.py`, and `tests/helpers/proclifecycle.py` imported
`folded_str` out of `tests/test_journal_one_writer.py`, which is itself
only re-exporting it from `tests/helpers/astwalk`. Renaming either name
in its test module broke collection of two unrelated suites, and the
second routed a shared helper through a test file for no reason at all.

The census is over IMPORTS, keyed by the importing module, and it counts
every import of a `tests.test_*` module rather than enumerating the two
somebody has already noticed: a guard listing the known offenders is
closed only over those.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.helpers import astwalk

#: Modules under `tests/helpers/` that import a test module, counted.
#: Empty on purpose, and NOT the whole control: an empty inventory is
#: also what a switched-off net returns, which is why the four controls
#: below prove each half of the predicate separately.
EXPECTED_TEST_MODULE_IMPORTS: dict[str, int] = {}

_HELPERS = Path(__file__).resolve().parent / "helpers"


def _is_test_module(dotted: str) -> bool:
    tail = dotted.rsplit(".", 1)[-1]
    return dotted.startswith("tests.") and tail.startswith("test_")


def _imported_modules(source_file: Path, node: ast.AST) -> list[str]:
    """Every module name one import node names, relative ones resolved.

    `ImportFrom.level` is read rather than dropped: a relative import
    that this walk could not resolve would be an import it silently did
    not look at, which is the skip direction the whole guard class is
    about.
    """
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if not isinstance(node, ast.ImportFrom):
        return []
    if not node.level:
        return [node.module] if node.module else []
    package = astwalk.module_name(source_file).rsplit(".", node.level)[0]
    if node.module:
        return [f"{package}.{node.module}"]
    # `from . import sibling` names the siblings, not the package.
    return [f"{package}.{alias.name}" for alias in node.names]


def _hits(source_file: Path, tree: ast.AST) -> list[ast.AST]:
    return [
        node
        for node in astwalk.all_nodes(tree)
        if any(_is_test_module(name) for name in _imported_modules(source_file, node))
    ]


class TestHelpersDoNotImportTestModules:
    def test_no_helper_imports_a_test_module(self) -> None:
        sources = sorted(_HELPERS.rglob("*.py"))
        assert sources, "the walk found no helper modules; check the path it derived"
        built = {
            astwalk.label(source): len(found)
            for source in sources
            if (found := _hits(source, astwalk.parsed(source)))
        }
        assert built == EXPECTED_TEST_MODULE_IMPORTS, (
            "a module under tests/helpers/ imports a test module. Move the "
            f"name into tests/helpers/ and import it from there. Found: {built}"
        )

    def test_the_walk_sees_an_absolute_from_import(self) -> None:
        """The shape both real instances had."""
        tree = ast.parse("from tests.test_serve_seam import x\n")
        assert len(_hits(_HELPERS / "fakegh.py", tree)) == 1

    def test_the_walk_sees_a_plain_import(self) -> None:
        tree = ast.parse("import tests.test_serve\n")
        assert len(_hits(_HELPERS / "fakegh.py", tree)) == 1

    def test_the_walk_sees_a_relative_import(self) -> None:
        """The relative half, resolved against a real helper's own package."""
        tree = ast.parse("from ..test_journal_one_writer import folded_str\n")
        assert len(_hits(_HELPERS / "astwalk" / "net.py", tree)) == 1

    def test_a_helper_importing_a_helper_is_not_a_hit(self) -> None:
        """The other direction. Without it, a net that flagged every
        `tests.` import would read exactly like this one."""
        tree = ast.parse("from tests.helpers.executables import write_executable\n")
        assert _hits(_HELPERS / "fakegh.py", tree) == []
