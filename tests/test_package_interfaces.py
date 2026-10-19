"""Each package of ``kstrl/`` is reached only through its interface (#863 D1).

The rules are section 4.4 of the #863 plan.

- A module in unit P reads a name of a different unit Q only as ``from
  kstrl.q import name`` (or ``q.name`` through the package object), where
  ``name`` is in ``__all__`` of Q, and Q is lower than P in ``LAYERS``.
- A module reads its own package by the full path of the sibling module
  (``from kstrl.p.mod import name`` or ``import kstrl.p.mod as mod``),
  never through ``kstrl.p`` itself, which is the namespace that
  ``__init__.py`` fills.
- An ``__init__.py`` holds a docstring, ``from kstrl.<pkg>.<module>
  import`` lines and a sorted ``__all__`` equal to the names it binds.

Closed by construction: the walk parses every ``.py`` file under
``kstrl/`` (the census test compares it with an independent ``os.walk``)
and turns each ``import``, each ``from ... import`` (top-level, local,
TYPE_CHECKING and relative), each attribute read through a bound package
object and each ``import_module`` / ``__import__`` call into a ``Site``.
``site_problem`` has no third bucket: a site is allowed or it is a
problem, and a name the walk cannot read is a problem. The guard FLAGS, so
an over-match costs a false positive somebody reads, never a silent clear.

The one permitted exception is ``ALLOWED_EXCEPTIONS``: ``write_guard.py``
reads ``kstrl.io.jsonread`` directly, because the engineer's hook runs it
on each file tool call (plan section 6.3, rule 6). It is a record that
must match a site, not a skip.

The sizes of the interfaces and the counts of kstrl modules that ``import
kstrl.cli`` and ``import kstrl.write_guard`` load are pinned. A larger
value is red. A smaller value is red until the pin is lowered in the same
change, so the ratchet stays tight.

Disclosed limits, each a strict xfail in ``TestDisclosedLimits``:
``getattr`` with a string, a ``sys.modules`` lookup, and a package object
passed on as an argument. Each use of ``import_module`` or ``__import__``
that is not a direct call (an alias, a ``functools.partial``) is a site
the guard cannot read, thus it is red.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.helpers.astwalk import blind_spot

ROOT = Path(__file__).resolve().parent.parent

#: The layer sequence of plan section 4.3, lowest first. A unit imports
#: only units that come earlier. ``test_layers_name_each_unit_on_disk``
#: makes a unit on disk that is not here red, and the reverse.
LAYERS: tuple[str, ...] = (
    "io",
    "write_guard",
    "ui",
    "config",
    "git",
    "plan",
    "events",
    "proc",
    "queue",
    "owner",
    "project",
    "agents",
    "verify",
    "knowledge",
    "learning",
    "architect",
    "acceptance",
    "review",
    "calibration",
    "loop",
    "pipeline",
    "factory",
    "serve",
    "cli",
    "__main__",
)

#: ``kstrl/__init__.py`` holds the version. It is lower than each layer,
#: and its interface is the names that it binds.
ROOT_UNIT = "kstrl"

#: (importing file, module that it reads past an interface). Rule 6 of
#: plan section 4.4. A row that matches no site is red.
ALLOWED_EXCEPTIONS: frozenset[tuple[str, str]] = frozenset(
    {("kstrl/write_guard.py", "kstrl.io.jsonread")},
)

#: ``len(__all__)`` of each unit that other units import. Equality, not a
#: ceiling: a growth is red, and a shrink is red until this pin is lowered.
INTERFACE_SIZES: dict[str, int] = {
    "acceptance": 33,
    "agents": 26,
    "architect": 15,
    "calibration": 0,
    "cli": 1,
    "config": 39,
    "events": 80,
    "factory": 19,
    "git": 47,
    "io": 37,
    "knowledge": 8,
    "learning": 37,
    "loop": 8,
    "owner": 28,
    "pipeline": 50,
    "plan": 34,
    "proc": 52,
    "project": 43,
    "queue": 17,
    "review": 49,
    "serve": 36,
    "ui": 3,
    "verify": 64,
    "write_guard": 7,
}

#: The count of ``kstrl`` modules in ``sys.modules`` after one import in a
#: new process. Plan section 4.4: a count is deterministic, a time is not.
LOADED_MODULES: dict[str, int] = {
    "kstrl.cli": 226,
    "kstrl.write_guard": 9,
}

UNREADABLE = "<unreadable>"


@dataclass(frozen=True)
class Site:
    """One place where a ``kstrl`` file reaches a ``kstrl`` name."""

    path: str
    line: int
    unit: str
    target: str
    how: str


@dataclass(frozen=True)
class Layout:
    """What the disk holds: the modules, the units and their interfaces."""

    root: Path
    modules: frozenset[str]
    packages: frozenset[str]
    tops: frozenset[str]
    interfaces: dict[str, list[str] | None]
    root_names: frozenset[str]

    @property
    def units(self) -> frozenset[str]:
        return self.packages | self.tops


def kstrl_files(root: Path) -> list[Path]:
    """Every ``.py`` file under ``root/kstrl``: the guard's corpus."""
    return sorted((root / "kstrl").rglob("*.py"))


def files_on_disk(root: Path) -> set[Path]:
    """The same corpus by ``os.walk``, as an independent count."""
    found: set[Path] = set()
    for dirpath, _dirs, files in os.walk(root / "kstrl"):
        found.update(Path(dirpath) / f for f in files if f.endswith(".py"))
    return found


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def all_names(tree: ast.Module) -> list[str] | None:
    """``__all__`` of a module, read with ``ast``; None when it is not a literal."""
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else []
        if not any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
            continue
        assert isinstance(node, ast.Assign)
        if not isinstance(node.value, (ast.List, ast.Tuple)):
            return None
        names = [e.value for e in node.value.elts if isinstance(e, ast.Constant)]
        if len(names) != len(node.value.elts) or not all(isinstance(n, str) for n in names):
            return None
        return [str(n) for n in names]
    return None


def layout(root: Path) -> Layout:
    modules: set[str] = set()
    for path in kstrl_files(root):
        parts = path.relative_to(root).with_suffix("").parts
        modules.add(".".join(parts[:-1] if parts[-1] == "__init__" else parts))
    packages = frozenset(p.parent.name for p in (root / "kstrl").glob("*/__init__.py"))
    tops = frozenset(p.stem for p in (root / "kstrl").glob("*.py") if p.name != "__init__.py")
    interfaces = {u: all_names(_parse(root / "kstrl" / u / "__init__.py")) for u in packages}
    interfaces.update({u: all_names(_parse(root / "kstrl" / f"{u}.py")) for u in tops})
    root_tree = _parse(root / "kstrl" / "__init__.py")
    root_names = frozenset(
        t.id
        for node in root_tree.body
        if isinstance(node, ast.Assign)
        for t in node.targets
        if isinstance(t, ast.Name)
    )
    return Layout(root, frozenset(modules), packages, tops, interfaces, root_names)


def unit_of(root: Path, path: Path) -> str:
    parts = path.relative_to(root / "kstrl").parts
    if len(parts) == 1:
        return ROOT_UNIT if parts[0] == "__init__.py" else Path(parts[0]).stem
    return parts[0]


def _absolute(node: ast.ImportFrom, path: Path, root: Path) -> str:
    if not node.level:
        return node.module or ""
    package = list(path.relative_to(root).with_suffix("").parts)[:-1]
    base = package[: len(package) - node.level + 1]
    return ".".join([*base, *([node.module] if node.module else [])])


def dynamic_target(node: ast.Call) -> str | None:
    """The name of an ``import_module`` / ``__import__`` call, else None.

    ``UNREADABLE`` when the call is one of these but its name is not a
    constant absolute string: the guard cannot read it, so it flags it.
    """
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    if name not in DYNAMIC_IMPORTS:
        return None
    first = node.args[0] if node.args else None
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return UNREADABLE if first.value.startswith(".") else first.value
    return UNREADABLE


#: The functions that import a module by a string.
DYNAMIC_IMPORTS = frozenset({"import_module", "__import__"})


def _is_kstrl(dotted: str) -> bool:
    return dotted.split(".")[0] == "kstrl" or dotted == UNREADABLE


class _Walk:
    """The sites of one file, and the names it binds to a module object."""

    def __init__(self, root: Path, path: Path) -> None:
        self.root = root
        self.path = path
        self.rel = path.relative_to(root).as_posix()
        self.unit = unit_of(root, path)
        self.bound: dict[str, set[str]] = {}

    def site(self, node: ast.AST, target: str, how: str) -> Site:
        return Site(self.rel, getattr(node, "lineno", 0), self.unit, target, how)

    def bind(self, name: str, target: str) -> None:
        self.bound.setdefault(name, set()).add(target)

    def imports(self, node: ast.AST) -> Iterator[Site]:
        if isinstance(node, ast.Import):
            yield from self._import(node)
        elif isinstance(node, ast.ImportFrom):
            yield from self._import_from(node)
        elif isinstance(node, ast.Call):
            target = dynamic_target(node)
            if target is not None and _is_kstrl(target):
                yield self.site(node, target, "dynamic")

    def _import(self, node: ast.Import) -> Iterator[Site]:
        for alias in node.names:
            if _is_kstrl(alias.name):
                yield self.site(node, alias.name, "import")
            head = alias.name.split(".")[0]
            self.bind(alias.asname or head, alias.name if alias.asname else head)

    def _import_from(self, node: ast.ImportFrom) -> Iterator[Site]:
        module = _absolute(node, self.path, self.root)
        for alias in node.names:
            if _is_kstrl(module):
                yield self.site(node, f"{module}.{alias.name}", "from")
            if alias.name in DYNAMIC_IMPORTS and alias.asname not in (None, alias.name):
                yield self.site(node, UNREADABLE, "dynamic")
            self.bind(alias.asname or alias.name, f"{module}.{alias.name}")

    def dynamic_references(self, tree: ast.Module) -> Iterator[Site]:
        """``import_module`` or ``__import__`` used other than as a direct call.

        An alias, a ``functools.partial`` or a function given as an
        argument hides the name of the module from the walk, so each such
        reference is a site the guard cannot read.
        """
        called = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
        for node in ast.walk(tree):
            name = node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", None)
            if name in DYNAMIC_IMPORTS and isinstance(node, (ast.Name, ast.Attribute)):
                if id(node) not in called:
                    yield self.site(node, UNREADABLE, "dynamic")

    def dynamic_bindings(self, tree: ast.Module) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                target = dynamic_target(node.value)
                for name in node.targets:
                    if target not in (None, UNREADABLE) and isinstance(name, ast.Name):
                        self.bind(name.id, str(target))

    def attributes(self, tree: ast.Module, modules: frozenset[str]) -> Iterator[Site]:
        """A read through a bound package object: ``kstrl.io.x``, ``io.x``.

        Only the outermost attribute of a chain is read, so each chain
        gives one site. A name bound to more than one module gives a site
        for each kstrl package among them: an over-match, never a skip.
        """
        inner = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or id(node) in inner:
                continue
            chain = _chain(node)
            if chain is None:
                yield from self._call_head(node)
                continue
            head, attrs = chain
            for base in sorted(self.bound.get(head, ())):
                if base in modules and base.count(".") <= 1:
                    yield self.site(node, ".".join([base, *attrs]), "attr")

    def _call_head(self, node: ast.Attribute) -> Iterator[Site]:
        """A read on the module that a call returns: ``import_module("kstrl.q").x``."""
        attrs: list[str] = []
        cur: ast.expr = node
        while isinstance(cur, ast.Attribute):
            attrs.insert(0, cur.attr)
            cur = cur.value
        target = dynamic_target(cur) if isinstance(cur, ast.Call) else None
        if target is not None and target != UNREADABLE and _is_kstrl(target):
            yield self.site(node, ".".join([target, *attrs]), "attr")


def _chain(node: ast.Attribute) -> tuple[str, list[str]] | None:
    """``a.b.c`` as ``("a", ["b", "c"])``; None when the chain starts with a call."""
    attrs: list[str] = []
    cur: ast.expr = node
    while isinstance(cur, ast.Attribute):
        attrs.insert(0, cur.attr)
        cur = cur.value
    return (cur.id, attrs) if isinstance(cur, ast.Name) else None


def sites_of(root: Path, path: Path, modules: frozenset[str]) -> list[Site]:
    """Every site of one file. An import statement gives one site per name."""
    tree = _parse(path)
    walk = _Walk(root, path)
    found = [s for node in ast.walk(tree) for s in walk.imports(node)]
    found += walk.dynamic_references(tree)
    walk.dynamic_bindings(tree)
    return found + list(walk.attributes(tree, modules))


def collect(root: Path) -> tuple[list[Path], list[Site]]:
    lay = layout(root)
    files = kstrl_files(root)
    return files, [s for f in files for s in sites_of(root, f, lay.modules)]


def _rank(unit: str) -> int | None:
    if unit == ROOT_UNIT:
        return -1
    return LAYERS.index(unit) if unit in LAYERS else None


def _own_unit_problem(lay: Layout, site: Site, parts: list[str]) -> str | None:
    if site.unit not in lay.packages:
        return None
    if site.how == "from" and len(parts) <= 3:
        return (
            "imports through its own package, the namespace that __init__.py "
            "fills; import the sibling module by its full path"
        )
    if site.how == "attr" and len(parts) >= 3 and ".".join(parts[:3]) not in lay.modules:
        return f"reads {parts[2]!r} from the interface of its own package"
    return None


def _cross_unit_problem(lay: Layout, site: Site, parts: list[str]) -> str | None:
    q = parts[1]
    rank_q, rank_p = _rank(q), _rank(site.unit)
    if rank_q is None or rank_p is None:
        return f"{q if rank_q is None else site.unit} is not in LAYERS"
    if rank_q >= rank_p:
        return f"{site.unit} imports {q}, which is not lower in LAYERS"
    if len(parts) == 2:
        return None  # the unit object: each read through it is its own site
    module = ".".join(parts[:3])
    if module in lay.modules:
        if (site.path, module) in ALLOWED_EXCEPTIONS:
            return None
        return f"names the submodule {module}"
    names = lay.interfaces.get(q)
    if names is None or parts[2] not in names:
        return f"{parts[2]!r} is not in __all__ of kstrl.{q}"
    return None


def site_problem(lay: Layout, site: Site) -> str | None:
    """None when the site obeys section 4.4, else the rule that it breaks."""
    if site.target == UNREADABLE:
        return "an import_module or __import__ name that the guard cannot read"
    parts = site.target.split(".")
    if len(parts) == 1:
        return None  # ``import kstrl``: each read through it is its own site
    if parts[1] not in lay.units:
        if parts[1] in lay.root_names:
            return None
        return f"kstrl has no unit or name {parts[1]!r}"
    if parts[1] == site.unit:
        return _own_unit_problem(lay, site, parts)
    return _cross_unit_problem(lay, site, parts)


def problems(root: Path) -> list[str]:
    lay = layout(root)
    _files, sites = collect(root)
    found = set()
    for site in sites:
        why = site_problem(lay, site)
        if why is not None:
            found.add(f"{site.path}:{site.line}: {site.how} {site.target}: {why}")
    return sorted(found)


def _init_import_problems(lay: Layout, package: str, tree: ast.Module) -> Iterator[str]:
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom) or node.module == "__future__":
            continue
        source = node.module or "."
        if node.level or not source.startswith(f"kstrl.{package}.") or source not in lay.modules:
            yield f"{node.lineno}: imports from {source}, not a module of kstrl.{package}"


def _init_all_problems(lay: Layout, package: str, tree: ast.Module) -> Iterator[str]:
    names = all_names(tree)
    if names is None:
        yield "no __all__ list of string constants"
        return
    bound = {
        a.asname or a.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module != "__future__"
        for a in node.names
    }
    if names != sorted(names):
        yield "__all__ is not sorted"
    if len(set(names)) != len(names):
        yield "__all__ has a duplicate"
    if set(names) != bound:
        yield f"__all__ and the bound names differ: {sorted(set(names) ^ bound)}"
    for name in names:
        if name.startswith("_"):
            yield f"__all__ name {name!r} starts with _"
        if f"kstrl.{package}.{name}" in lay.modules:
            yield f"__all__ name {name!r} is also a submodule"


def init_problems(root: Path) -> list[str]:
    """Rules 1 and 5 of section 4.4 that ``TestMachineryPackageInits`` does not read.

    That guard (``tests/test_statedir.py``) flags each top-level statement
    that is not ``from ... import``, a docstring or a string ``__all__``.
    This one reads the rest: the docstring, each import from a module of
    the same package, and an ``__all__`` that is sorted, has no duplicate,
    no ``_`` name and no name of a submodule, and is equal to the names
    that the file binds. A name that is also a submodule hides the module
    from ``from kstrl.q import mod`` (the ``recheck`` and ``serve`` cases).
    """
    lay = layout(root)
    found: list[str] = []
    for package in sorted(lay.packages):
        tree = _parse(root / "kstrl" / package / "__init__.py")
        rel = f"kstrl/{package}/__init__.py"
        if ast.get_docstring(tree) is None:
            found.append(f"{rel}: no docstring")
        found.extend(f"{rel}: {p}" for p in _init_import_problems(lay, package, tree))
        found.extend(f"{rel}: {p}" for p in _init_all_problems(lay, package, tree))
    for top in sorted(lay.tops):
        names = lay.interfaces[top]
        if names is not None and names != sorted(set(names)):
            found.append(f"kstrl/{top}.py: __all__ is not sorted or has a duplicate")
    return found


def unit_problems(root: Path) -> list[str]:
    """Each file whose unit is not a package or a top-level module.

    A directory with no ``__init__.py`` is not a unit, so ``LAYERS`` and
    ``INTERFACE_SIZES`` would not name it. Its files are red here.
    """
    lay = layout(root)
    known = lay.units | {ROOT_UNIT}
    return [
        f"{f.relative_to(root).as_posix()}: unit {unit_of(root, f)!r} has no __init__.py"
        for f in kstrl_files(root)
        if unit_of(root, f) not in known
    ]


def loaded_modules(module: str) -> int:
    code = (
        "import importlib, json, sys; "
        f"sys.path.insert(0, {str(ROOT)!r}); "
        f"importlib.import_module({module!r}); "
        "import kstrl; "
        "print(json.dumps([kstrl.__file__, "
        "sorted(m for m in sys.modules if m == 'kstrl' or m.startswith('kstrl.'))]))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    origin, names = json.loads(out.stdout)
    assert Path(origin).resolve().parent == ROOT / "kstrl", origin
    return len(names)


# ---------------------------------------------------------------------------
# The guard on this tree
# ---------------------------------------------------------------------------


class TestTheTree:
    def test_the_census_reads_every_file_on_disk(self) -> None:
        files, sites = collect(ROOT)
        assert set(files) == files_on_disk(ROOT)
        assert len(files) > 200, "the walk found almost nothing: it is blind"
        assert len(sites) > 2000, "the walk found almost no import: it is blind"

    def test_layers_name_each_unit_on_disk(self) -> None:
        lay = layout(ROOT)
        assert sorted(lay.units) == sorted(LAYERS)
        assert unit_problems(ROOT) == []

    def test_no_import_goes_around_an_interface(self) -> None:
        assert problems(ROOT) == []

    def test_each_allowed_exception_matches_a_site(self) -> None:
        _files, sites = collect(ROOT)
        seen = {(s.path, ".".join(s.target.split(".")[:3])) for s in sites}
        assert sorted(ALLOWED_EXCEPTIONS - seen) == []

    def test_each_init_obeys_the_rules(self) -> None:
        assert init_problems(ROOT) == []

    def test_the_interface_sizes_are_pinned(self) -> None:
        lay = layout(ROOT)
        sizes = {u: len(n) for u, n in lay.interfaces.items() if n is not None}
        assert sizes == INTERFACE_SIZES

    @pytest.mark.parametrize("module", sorted(LOADED_MODULES))
    def test_the_count_of_loaded_modules_is_pinned(self, module: str) -> None:
        assert loaded_modules(module) == LOADED_MODULES[module]


# ---------------------------------------------------------------------------
# Controls: each shape the guard exists for, in a small tree of its own
# ---------------------------------------------------------------------------


def _init(doc: str, imports: dict[str, list[str]]) -> str:
    lines = [f'"""{doc}."""', "from __future__ import annotations"]
    lines += [f"from {m} import {', '.join(n)}" for m, n in imports.items()]
    names = sorted(n for ns in imports.values() for n in ns)
    return "\n".join([*lines, f"__all__ = {names!r}", ""])


BASE_TREE: dict[str, str] = {
    "kstrl/__init__.py": '__version__ = "0"\n',
    "kstrl/io/__init__.py": _init("Io", {"kstrl.io.jsonread": ["read_json"]}),
    "kstrl/io/jsonread.py": "def read_json() -> None: ...\ndef hidden() -> None: ...\n",
    "kstrl/write_guard.py": '__all__ = ["refusal"]\ndef refusal() -> None: ...\n',
    "kstrl/events/__init__.py": _init("Events", {"kstrl.events.bus": ["EventBus"]}),
    "kstrl/events/bus.py": "class EventBus: ...\n",
    "kstrl/events/other.py": "",
    "kstrl/proc/__init__.py": _init("Proc", {"kstrl.proc.procgroup": ["kill"]}),
    "kstrl/proc/procgroup.py": "def kill() -> None: ...\n",
    "kstrl/agents/__init__.py": _init("Agents", {}),
    "kstrl/agents/base.py": "",
    "kstrl/pipeline/__init__.py": _init(
        "Pipeline", {"kstrl.pipeline.pipeline_state": ["RunState"]}
    ),
    "kstrl/pipeline/pipeline_state.py": "class RunState: ...\n",
    "kstrl/factory/__init__.py": _init("Factory", {"kstrl.factory.factory_run": ["X"]}),
    "kstrl/factory/factory_run.py": "X = 1\n",
    "kstrl/serve/__init__.py": _init("Serve", {}),
    "kstrl/serve/serve_loop.py": "",
    "kstrl/cli/__init__.py": _init("Cli", {}),
    "kstrl/cli/cli.py": "",
}


def _tree(root: Path, changes: dict[str, str]) -> Path:
    for rel, text in {**BASE_TREE, **changes}.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


#: (file, source, a text the problem must hold). Plants 1 to 7 of plan
#: section 7 come first, then the shapes the exceptions of D1 add.
CAUGHT: list[tuple[str, str, str]] = [
    ("kstrl/serve/serve_loop.py", "from kstrl.factory.factory_run import X\n", "submodule"),
    ("kstrl/agents/base.py", "import kstrl.proc.procgroup\n", "submodule"),
    ("kstrl/agents/base.py", "from ..proc import procgroup\n", "submodule"),
    (
        "kstrl/agents/base.py",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n"
        "    from kstrl.proc.procgroup import kill\n",
        "submodule",
    ),
    (
        "kstrl/cli/cli.py",
        'import importlib\nimportlib.import_module("kstrl.pipeline.pipeline_state")\n',
        "submodule",
    ),
    ("kstrl/io/jsonread.py", "from kstrl.events import EventBus\n", "not lower in LAYERS"),
    ("kstrl/cli/cli.py", "from kstrl.io import hidden\n", "not in __all__"),
    ("kstrl/cli/cli.py", "def f() -> None:\n    from kstrl.io import hidden\n", "not in __all__"),
    ("kstrl/cli/cli.py", "from kstrl import io\nio.hidden()\n", "not in __all__"),
    ("kstrl/cli/cli.py", "import kstrl.io as kio\nkio.jsonread.hidden()\n", "submodule"),
    ("kstrl/cli/cli.py", "import kstrl.io\nkstrl.io.hidden()\n", "not in __all__"),
    ("kstrl/cli/cli.py", '__import__("kstrl.proc.procgroup")\n', "submodule"),
    ("kstrl/cli/cli.py", "import importlib\nimportlib.import_module(name)\n", "cannot read"),
    ("kstrl/cli/cli.py", 'from importlib import import_module\nimport_module(".x")\n', "cannot"),
    ("kstrl/events/other.py", "from kstrl.events import EventBus\n", "its own package"),
    ("kstrl/events/other.py", "from kstrl.events import bus\n", "its own package"),
    ("kstrl/events/other.py", "import kstrl.events\nkstrl.events.EventBus\n", "own package"),
    ("kstrl/agents/base.py", "from kstrl.io.jsonread import read_json\n", "submodule"),
    ("kstrl/agents/base.py", "from kstrl.write_guard import hidden\n", "not in __all__"),
    ("kstrl/cli/cli.py", "from kstrl import nothing\n", "no unit or name"),
    ("kstrl/__init__.py", '__version__ = "0"\nfrom kstrl.io import read_json\n', "not lower"),
    (
        "kstrl/cli/cli.py",
        'import importlib\nim = importlib.import_module\nim("kstrl.proc.procgroup")\n',
        "cannot read",
    ),
    ("kstrl/cli/cli.py", 'from importlib import import_module as im\nim("kstrl.x")\n', "cannot"),
    (
        "kstrl/cli/cli.py",
        'import importlib\nimportlib.import_module("kstrl.io").hidden\n',
        "__all__",
    ),
    ("kstrl/cli/cli.py", '__import__("kstrl.proc", fromlist=["x"]).procgroup\n', "submodule"),
]


class TestTheGuardCatchesEachShape:
    @pytest.mark.parametrize(("rel", "source", "why"), CAUGHT)
    def test_the_shape_is_red(self, tmp_path: Path, rel: str, source: str, why: str) -> None:
        found = problems(_tree(tmp_path, {rel: source}))
        assert len(found) >= 1, found
        assert all(rel in p for p in found), found
        assert any(why in p for p in found), found

    def test_the_base_tree_is_clean(self, tmp_path: Path) -> None:
        """The control of the controls: each red above comes from its plant."""
        root = _tree(tmp_path, {})
        assert problems(root) == []
        assert init_problems(root) == []

    def test_the_allowed_exception_is_clean_only_in_write_guard(self, tmp_path: Path) -> None:
        source = (
            '__all__ = ["refusal"]\nfrom kstrl.io.jsonread import read_json\n'
            "def refusal() -> None: ...\n"
        )
        assert problems(_tree(tmp_path, {"kstrl/write_guard.py": source})) == []

    def test_a_sibling_by_its_full_path_is_clean(self, tmp_path: Path) -> None:
        source = "import kstrl.events.bus as bus\nfrom kstrl.events.bus import EventBus\n"
        assert problems(_tree(tmp_path, {"kstrl/events/other.py": source})) == []


INIT_CAUGHT: list[tuple[str, str]] = [
    (
        '"""Io."""\nfrom kstrl.io.jsonread import read_json\n__all__ = ["read_json", "a"]\n',
        "differ",
    ),
    (
        '"""Io."""\nfrom kstrl.io.jsonread import read_json, hidden\n__all__ = ["read_json"]\n',
        "differ",
    ),
    (
        '"""Io."""\nfrom kstrl.io.jsonread import read_json, hidden\n'
        '__all__ = ["read_json", "hidden"]\n',
        "sorted",
    ),
    (
        '"""Io."""\nfrom kstrl.events.bus import EventBus\n__all__ = ["EventBus"]\n',
        "not a module of kstrl.io",
    ),
    (
        '"""Io."""\nfrom .jsonread import read_json\n__all__ = ["read_json"]\n',
        "not a module of kstrl.io",
    ),
    (
        '"""Io."""\nfrom kstrl.io.jsonread import read_json as _r\n__all__ = ["_r"]\n',
        "starts with _",
    ),
    (
        '"""Io."""\nfrom kstrl.io.jsonread import read_json as jsonread\n__all__ = ["jsonread"]\n',
        "submodule",
    ),
    ('from kstrl.io.jsonread import read_json\n__all__ = ["read_json"]\n', "no docstring"),
    ('"""Io."""\nfrom kstrl.io.jsonread import read_json\n', "no __all__"),
]


@pytest.mark.parametrize(("source", "why"), INIT_CAUGHT)
def test_an_init_that_breaks_a_rule_is_red(tmp_path: Path, source: str, why: str) -> None:
    found = init_problems(_tree(tmp_path, {"kstrl/io/__init__.py": source}))
    assert any(why in p for p in found), found


def test_a_directory_with_no_init_is_red(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"kstrl/orphan/mod.py": "import os\n"})
    assert unit_problems(root) == ["kstrl/orphan/mod.py: unit 'orphan' has no __init__.py"]
    assert unit_problems(_tree(tmp_path / "clean", {})) == []


def test_an_interface_that_grows_is_red(tmp_path: Path) -> None:
    """The pin compares the sizes; this is the comparison on a planted growth."""
    root = _tree(tmp_path, {})
    before = {u: len(n) for u, n in layout(root).interfaces.items() if n is not None}
    grown = _init("Io", {"kstrl.io.jsonread": ["hidden", "read_json"]})
    _tree(tmp_path, {"kstrl/io/__init__.py": grown})
    after = {u: len(n) for u, n in layout(root).interfaces.items() if n is not None}
    assert after != before
    assert after["io"] == before["io"] + 1


# ---------------------------------------------------------------------------
# Disclosed limits: each passes only while the walk cannot see it
# ---------------------------------------------------------------------------


def _flags(source: str) -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        root = _tree(Path(tmp), {"kstrl/cli/cli.py": source})
        return bool(problems(root))


class TestDisclosedLimits:
    @pytest.mark.xfail(strict=True, raises=AssertionError)
    def test_getattr_with_a_string(self) -> None:
        blind_spot(_flags, 'from kstrl import io\ngetattr(io, "hidden")()\n')

    @pytest.mark.xfail(strict=True, raises=AssertionError)
    def test_a_sys_modules_lookup(self) -> None:
        blind_spot(_flags, 'import sys\nsys.modules["kstrl.proc.procgroup"].kill()\n')

    @pytest.mark.xfail(strict=True, raises=AssertionError)
    def test_a_package_object_passed_on(self) -> None:
        source = "from kstrl import io\ndef f(m: object) -> object:\n    return m.hidden\nf(io)\n"
        blind_spot(_flags, source)
