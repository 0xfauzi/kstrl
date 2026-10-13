"""The resolver follows a package re-export to its defining module (#863 B4).

#863 groups ``kstrl/`` into packages. Each package ``__init__.py``
re-exports its interface, and code in a different package writes ``from
kstrl.io import ControlStateError``. The interface pass on ``io`` made 14
guards red, because the resolver gave ``kstrl.io.ControlStateError`` and
the guards key on ``kstrl.io.statedir.ControlStateError``.

Each test builds a small tree in ``tmp_path`` and resolves against it
with ``root=``, thus no test depends on the packages in ``kstrl/``. Each
changed resolution path has a test here. The guessed paths have a test
each, because a guard that CLEARS must not clear on a guess
(``Origin.guessed``), and a guessed answer that came back as proven is
the skip direction.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.helpers import astwalk

STATEDIR = "app.io.statedir.ControlStateError"


def _tree(root: Path, files: dict[str, str]) -> Path:
    """Write ``files`` (path relative to ``root``: source) and return ``root``."""
    for relative, source in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    return root


def _io(root: Path, init: str) -> Path:
    """A package ``app.io`` with the module ``statedir`` and the given ``__init__.py``."""
    return _tree(
        root,
        {
            "app/__init__.py": "",
            "app/io/__init__.py": init,
            "app/io/statedir.py": "class ControlStateError(RuntimeError):\n    pass\n",
            "app/io/appendio.py": "REPAIR_DETAIL = 'x'\n",
        },
    )


def _origin(root: Path, source: str, expression: str, module: str = "app.user") -> astwalk.Origin:
    """What the resolver says ``expression``, read in ``source``, refers to."""
    tree = astwalk.parse(source + f"\n{expression}\n")
    node = tree.body[-1]
    assert isinstance(node, ast.Expr)
    found = astwalk.bindings(tree, module=module, root=root).origin_of(node.value)
    assert found is not None
    return found


PROVEN = "from app.io.statedir import ControlStateError\n"
LIB = "app.lib.ControlStateError"


def _star_elsewhere(root: Path) -> Path:
    """``app.io`` with a star import from ``app.lib``, a module in no package of ``app.io``."""
    _io(root, "from app.lib import *\n")
    return _tree(root, {"app/lib.py": "class ControlStateError(RuntimeError):\n    pass\n"})


class TestAProvableReExportIsFollowed:
    def test_a_from_import_of_the_package(self, tmp_path: Path) -> None:
        """The path the interface pass made red: 12 of the 14 guards."""
        root = _io(tmp_path, PROVEN)
        source = "from app.io import ControlStateError\n"
        assert _origin(root, source, "ControlStateError") == astwalk.Origin(STATEDIR)

    def test_the_origins_table_holds_the_defining_module(self, tmp_path: Path) -> None:
        """Two guards read ``origins`` directly, not ``origin_of``."""
        root = _io(tmp_path, PROVEN)
        tree = astwalk.parse("from app.io import ControlStateError\n")
        table = astwalk.bindings(tree, module="app.user", root=root)
        assert table.origins["ControlStateError"] == STATEDIR

    def test_a_package_alias_and_an_attribute(self, tmp_path: Path) -> None:
        root = _io(tmp_path, PROVEN)
        assert _origin(root, "import app.io as kio\n", "kio.ControlStateError").dotted == STATEDIR
        assert _origin(root, "from app import io\n", "io.ControlStateError").dotted == STATEDIR
        assert _origin(root, "import app.io\n", "app.io.ControlStateError").dotted == STATEDIR

    def test_a_relative_re_export_in_the_init(self, tmp_path: Path) -> None:
        """``from .statedir import x`` in ``app/io/__init__.py`` is
        ``app.io.statedir``, not ``app.statedir``."""
        root = _io(tmp_path, "from .statedir import ControlStateError\n")
        found = _origin(root, "from app.io import ControlStateError\n", "ControlStateError")
        assert found == astwalk.Origin(STATEDIR)

    def test_a_re_export_of_a_re_export(self, tmp_path: Path) -> None:
        root = _io(tmp_path, PROVEN)
        _tree(root, {"app/facade/__init__.py": "from app.io import ControlStateError\n"})
        found = _origin(root, "from app.facade import ControlStateError\n", "ControlStateError")
        assert found == astwalk.Origin(STATEDIR)

    def test_a_renamed_re_export_goes_to_the_name_it_renames(self, tmp_path: Path) -> None:
        """The offender shape: the package name is the guard's name, the
        object is a different one."""
        root = _io(tmp_path, "from app.io.appendio import REPAIR_DETAIL as ControlStateError\n")
        found = _origin(root, "from app.io import ControlStateError\n", "ControlStateError")
        assert found == astwalk.Origin("app.io.appendio.REPAIR_DETAIL")

    def test_a_rebind_of_a_re_exported_name(self, tmp_path: Path) -> None:
        root = _io(tmp_path, PROVEN)
        source = "from app.io import ControlStateError\n_E = ControlStateError\n"
        tree = astwalk.parse(source)
        table = astwalk.bindings(tree, module="app.user", root=root)
        assert table.origins["_E"] == STATEDIR

    def test_a_name_the_init_defines_stays_where_it_is(self, tmp_path: Path) -> None:
        root = _io(tmp_path, "class Local:\n    pass\n")
        assert _origin(root, "from app.io import Local\n", "Local") == astwalk.Origin(
            "app.io.Local"
        )

    def test_a_submodule_stays_a_submodule(self, tmp_path: Path) -> None:
        root = _io(tmp_path, PROVEN)
        found = _origin(root, "from app.io import statedir\n", "statedir.ControlStateError")
        assert found == astwalk.Origin(STATEDIR)

    def test_a_name_with_the_letters_of_a_submodule_is_not_that_submodule(
        self, tmp_path: Path
    ) -> None:
        """``kstrl.plan`` re-exports the class ``Manifest`` from its module
        ``manifest.py``. A case-insensitive file system finds ``Manifest.py``
        too, and the walk read the class as a submodule that the
        ``__init__.py`` also binds: a guess (#863 M2). Python imports a
        module only by its exact case, and so does the walk."""
        root = _tree(
            tmp_path,
            {
                "app/__init__.py": "",
                "app/plan/__init__.py": "from app.plan.manifest import Manifest\n",
                "app/plan/manifest.py": "class Manifest:\n    pass\n",
            },
        )
        found = _origin(root, "from app.plan import Manifest\n", "Manifest.load")
        assert found == astwalk.Origin("app.plan.manifest.Manifest.load")

    def test_a_relative_import_inside_an_init_walked_as_a_module(self, tmp_path: Path) -> None:
        """A guard that walks the ``__init__.py`` itself gives the package
        name (``module_name`` strips ``__init__``). Before B4 that put
        ``from . import statedir`` one level too high."""
        root = _io(tmp_path, "")
        tree = astwalk.parse("from . import statedir\nfrom .appendio import REPAIR_DETAIL\n")
        table = astwalk.bindings(tree, module="app.io", root=root)
        assert table.origins["statedir"] == "app.io.statedir"
        assert table.origins["REPAIR_DETAIL"] == "app.io.appendio.REPAIR_DETAIL"


GUESSED_INITS = {
    "a star import": "from app.io.statedir import *\n",
    "a module __getattr__": PROVEN + "def __getattr__(name):\n    raise AttributeError(name)\n",
    "a module __getattr__ bound by an assignment": PROVEN + "__getattr__ = lambda name: None\n",
    "a module __getattr__ bound by an import": PROVEN + "from app.lib import __getattr__\n",
    "a globals() call": PROVEN + "globals()['ControlStateError'] = 1\n",
    "a binding in an if": "import sys\nif sys:\n    " + PROVEN,
    "a binding in a try": "try:\n    "
    + PROVEN
    + "except ImportError:\n    ControlStateError = None\n",
    "two bindings": PROVEN + "from app.io.appendio import REPAIR_DETAIL as ControlStateError\n",
    "an assignment of a name": (
        "from app.io import statedir as _s\nControlStateError = _s.ControlStateError\n"
    ),
    "a global in a function": PROVEN + "def f():\n    global ControlStateError\n",
}


class TestAReExportTheWalkCannotProveIsGuessed:
    @pytest.mark.parametrize("init", GUESSED_INITS.values(), ids=list(GUESSED_INITS))
    def test_the_shape_is_guessed(self, tmp_path: Path, init: str) -> None:
        root = _io(tmp_path, init)
        found = _origin(root, "from app.io import ControlStateError\n", "ControlStateError")
        assert found.guessed, found

    def test_a_star_import_gives_its_source_as_the_best_guess(self, tmp_path: Path) -> None:
        """A guard that FLAGS can use the guess: a guessed origin in the
        target set stays a hit. The source is OUTSIDE the package, because
        a star import also makes each path into its own package guessed
        (a star can bind a submodule name), and that second rule alone
        made this test pass with the first rule removed."""
        root = _star_elsewhere(tmp_path)
        found = _origin(root, "from app.io import ControlStateError\n", "ControlStateError")
        assert found == astwalk.Origin(LIB, guessed=True)

    def test_the_table_keeps_the_spelling_so_the_guess_is_not_laundered(
        self, tmp_path: Path
    ) -> None:
        """A stored best guess would come back as proven on the next
        lookup. The spelling as written gives the same guess again, also
        through a rebind. The star source is outside the package, for the
        cause in the test above."""
        root = _star_elsewhere(tmp_path)
        source = "from app.io import ControlStateError\n_E = ControlStateError\n"
        table = astwalk.bindings(astwalk.parse(source), module="app.user", root=root)
        assert table.origins["ControlStateError"] == "app.io.ControlStateError"
        assert _origin(root, source, "_E") == astwalk.Origin(LIB, guessed=True)

    def test_a_cycle_of_re_exports_is_guessed_and_ends(self, tmp_path: Path) -> None:
        root = _io(tmp_path, "from app.loop import ControlStateError\n")
        _tree(root, {"app/loop/__init__.py": "from app.io import ControlStateError\n"})
        found = _origin(root, "from app.io import ControlStateError\n", "ControlStateError")
        assert found.guessed

    def test_a_submodule_that_the_init_also_binds_is_guessed(self, tmp_path: Path) -> None:
        root = _io(tmp_path, "from app.io.appendio import REPAIR_DETAIL as statedir\n")
        found = _origin(root, "from app.io import statedir\n", "statedir.ControlStateError")
        assert found == astwalk.Origin(STATEDIR, guessed=True)

    def test_a_clearing_clause_does_not_name_a_guess(self, tmp_path: Path) -> None:
        """The clearing direction: ``tests/test_inbox_write_guards.py``
        clears an inbox write only when a handler names the error by
        origin. A guessed origin is not in ``Clause.origins``."""
        source = (
            "from app.io import ControlStateError\n"
            "try:\n    pass\nexcept ControlStateError:\n    pass\n"
        )
        for init, expected in ((PROVEN, {STATEDIR}), (GUESSED_INITS["a star import"], set())):
            root = _io(tmp_path / str(len(expected)), init)
            tree = astwalk.parse(source)
            block = next(n for n in astwalk.all_nodes(tree) if isinstance(n, ast.Try))
            table = astwalk.bindings(tree, module="app.user", root=root)
            assert astwalk.handler_clauses(block, table)[0].origins == expected

    def test_a_guessed_call_outside_the_targets_is_undecided(self, tmp_path: Path) -> None:
        """The flagging direction: a guessed origin that is not a target
        decides nothing, thus the call is undecided, not silent."""
        root = _io(tmp_path, "from app.io.appendio import *\n")
        source = "from app.io import write\nwrite(1)\n"
        found = astwalk.calls_to(astwalk.parse(source), {"os.write"}, module="app.user", root=root)
        assert found == astwalk.Sites((), ("2 write",))

    def test_a_name_the_init_does_not_bind_stays_as_written(self, tmp_path: Path) -> None:
        """No dynamic shape in the ``__init__.py``: the answer the resolver
        gave before B4, which a snippet naming a missing module needs."""
        root = _io(tmp_path, PROVEN)
        assert _origin(root, "from app.io import absent\n", "absent") == astwalk.Origin(
            "app.io.absent"
        )

    @pytest.mark.xfail(strict=True, raises=AssertionError)
    def test_a_binding_made_by_a_different_module_is_not_seen(self, tmp_path: Path) -> None:
        """Disclosed limit: ``setattr(app.io, "absent", x)`` in another
        module binds the name at run time, and the walk reads only the
        ``__init__.py``. The probe says the answer is guessed; it is not."""
        root = _io(tmp_path, PROVEN)
        _tree(root, {"app/other.py": "import app.io\nsetattr(app.io, 'absent', 1)\n"})
        astwalk.blind_spot(
            lambda source: _origin(root, source, "absent").guessed,
            "from app.io import absent\n",
        )


def test_a_path_outside_the_root_packages_comes_back_as_is(tmp_path: Path) -> None:
    assert astwalk.defining_origin("os.getpgid", _io(tmp_path, PROVEN)) == ("os.getpgid", False)
