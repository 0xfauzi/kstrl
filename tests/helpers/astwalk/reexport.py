"""Follow a name through a package ``__init__.py`` to its defining module.

#863 groups ``kstrl/`` into packages, and each package ``__init__.py``
re-exports its interface: ``from kstrl.io.statedir import
ControlStateError`` plus ``__all__``. A module in a different package
then writes ``from kstrl.io import ControlStateError``. Without this
module, the resolver gives ``kstrl.io.ControlStateError``, and a guard
that keys on ``kstrl.io.statedir.ControlStateError`` cannot find it. The
interface pass on ``io`` made 14 guards red for that cause.

:func:`defining_origin` reads each ``__init__.py`` with ``ast``, never
with an import, and follows a re-export to the module that defines the
name. It gives a second value, ``guessed``, when it cannot prove the
step. These shapes are guessed:

- a star import in the ``__init__.py``;
- a binding the walk cannot prove at import time: a binding in an
  ``if``, a ``try`` or a function (``global``), a module ``__getattr__``,
  a call to ``globals``, ``setattr``, ``vars``, ``exec``, ``eval``,
  ``__import__`` or ``import_module``, or ``sys.modules``;
- two or more bindings of the name, or an assignment of a name or an
  attribute to the name;
- a name that is a submodule and also has a binding in the
  ``__init__.py``;
- a cycle of re-exports.

A guessed answer is the best guess, so a guard that FLAGS can use it. A
guard that CLEARS must not clear on it (``Origin.guessed``).

A name that the ``__init__.py`` does not bind, and that is not a
submodule, stays as written and is not guessed, if the ``__init__.py``
has no shape from the list above. That is the answer the resolver gave
before this module, and a test snippet that names a module that does
not exist needs it. A binding that a DIFFERENT module makes
(``setattr(kstrl.io, "x", y)``) is not seen.
``tests/test_astwalk_reexports.py`` pins that limit with a strict
xfail.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from tests.helpers.astwalk.corpus import REPO_ROOT

#: Calls that can bind a module name at run time.
_DYNAMIC_CALLS = frozenset(
    {"globals", "vars", "locals", "setattr", "exec", "eval", "__import__", "import_module"}
)

#: The three kinds of binding site. ``import`` has a target, ``defined``
#: is a ``def``, a ``class`` or an assignment of a value that is not a
#: name, and ``doubt`` is a binding the walk cannot prove.
_IMPORT, _DEFINED, _DOUBT = "import", "defined", "doubt"


@dataclass(frozen=True)
class _Init:
    """The binding sites of one package ``__init__.py``."""

    sites: Mapping[str, tuple[tuple[str, str], ...]]
    stars: tuple[str, ...]
    dynamic: bool


def is_package(module: str, root: Path = REPO_ROOT) -> bool:
    """True when ``module`` is a directory with an ``__init__.py`` under ``root``."""
    return bool(module) and root.joinpath(*module.split(".")).joinpath("__init__.py").is_file()


def import_base(node: ast.ImportFrom, module: str, root: Path = REPO_ROOT) -> str:
    """The module a ``from ... import`` reads from, relative levels and all.

    ``ImportFrom.level`` is the field #324 records three guards dropping.
    Dropped, ``from .os import getpgid`` resolves to the stdlib ``os``, a
    false positive, and ``from .config_report import f`` to a module that
    can never match a package-qualified key, a false NEGATIVE and the
    direction that matters. ``from . import x`` has no module at all and
    one of them discarded it outright.

    IN A PACKAGE ``__init__.py`` the package is the module itself.
    ``module_name`` gives ``kstrl.io`` for ``kstrl/io/__init__.py``, and
    before #863 B4 ``from .statedir import x`` there resolved to
    ``kstrl.statedir.x``, one level too high. Thus a ``module`` that is a
    package under ``root`` counts as its own ``__init__``.
    """
    if not node.level:
        return node.module or ""
    if module:
        parts = module.split(".")
        if is_package(module, root):
            parts.append("__init__")
        package = ".".join(parts[: -node.level])
    else:
        package = "." * node.level
    if not node.module:
        return package
    return f"{package}{node.module}" if package.endswith(".") else f"{package}.{node.module}"


def defining_origin(path: str, root: Path = REPO_ROOT) -> tuple[str, bool]:
    """``path`` with each package re-export followed, and whether it guessed.

    ``kstrl.io.ControlStateError`` -> ``("kstrl.io.statedir.ControlStateError",
    False)`` when ``kstrl/io/__init__.py`` has the one line ``from
    kstrl.io.statedir import ControlStateError``. A path whose first part
    is not a package under ``root`` (``os.getpgid``) comes back as is.
    """
    return _defining_origin(path, root)


@cache
def _defining_origin(path: str, root: Path) -> tuple[str, bool]:
    return _follow(path, root, frozenset())


def _follow(path: str, root: Path, visited: frozenset[tuple[str, str]]) -> tuple[str, bool]:
    parts = path.split(".")
    doubt = False
    for cut in range(1, len(parts)):
        package = ".".join(parts[:cut])
        if not is_package(package, root):
            break
        name = parts[cut]
        init = _read_init(root, package)
        bound = init.sites.get(name, ())
        if _is_submodule(root, package, name):
            doubt = doubt or bool(bound) or init.dynamic or bool(init.stars)
            continue
        if (package, name) in visited:
            return path, True
        target, sure = _binding(init, name)
        if target is None:
            return path, doubt or not sure
        moved = ".".join([target, *parts[cut + 1 :]])
        found, guessed = _follow(moved, root, visited | {(package, name)})
        return found, guessed or doubt or not sure
    return path, doubt


def _is_submodule(root: Path, package: str, name: str) -> bool:
    directory = root.joinpath(*package.split("."))
    return (directory / f"{name}.py").is_file() or (directory / name / "__init__.py").is_file()


def _binding(init: _Init, name: str) -> tuple[str | None, bool]:
    """Where ``name`` comes from in one ``__init__.py``, and whether that is sure.

    ``(None, True)`` is a name this ``__init__.py`` defines. ``(None,
    False)`` is a name it does not bind, or binds in a way the walk
    cannot follow. A target with ``False`` is the best guess.
    """
    bound = init.sites.get(name, ())
    provable = not init.dynamic and not init.stars
    if not bound:
        if len(init.stars) == 1:
            return f"{init.stars[0]}.{name}", False
        return None, provable
    if len(bound) == 1 and provable:
        kind, target = bound[0]
        if kind == _IMPORT:
            return target, True
        return None, kind == _DEFINED
    imports = [target for kind, target in bound if kind == _IMPORT]
    return (imports[0], False) if imports else (None, False)


@cache
def _read_init(root: Path, package: str) -> _Init:
    """The binding sites of ``package``'s ``__init__.py``.

    A file that does not parse is all doubt: the walk cannot prove any
    name in it. The read is outside the ``try``, so only the parser's own
    errors land there.
    """
    source = root.joinpath(*package.split(".")).joinpath("__init__.py").read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except Exception:  # the parser's error taxonomy belongs to the parser
        return _Init({}, (), True)
    sites: dict[str, list[tuple[str, str]]] = {}
    stars: list[str] = []
    for statement in tree.body:
        for name, site in _top_level_sites(statement, package, root, stars):
            sites.setdefault(name, []).append(site)
    return _Init(
        {name: tuple(found) for name, found in sites.items()}, tuple(stars), _is_dynamic(tree)
    )


_Site = tuple[str, tuple[str, str]]


def _top_level_sites(
    statement: ast.stmt, package: str, root: Path, stars: list[str]
) -> list[_Site]:
    """The names one top-level statement binds, each with its site."""
    if isinstance(statement, ast.Import):
        return [_import_site(alias) for alias in statement.names]
    if isinstance(statement, ast.ImportFrom):
        return _from_sites(statement, import_base(statement, f"{package}.__init__", root), stars)
    if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [(statement.name, (_DEFINED, "")), *_doubts(_globals_in(statement))]
    if isinstance(statement, (ast.Assign, ast.AnnAssign)) and statement.value is not None:
        return _assign_sites(statement, statement.value)
    for node in ast.walk(statement):
        if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names):
            stars.append(import_base(node, f"{package}.__init__", root))
    return _doubts(_stored_names(statement))


def _import_site(alias: ast.alias) -> _Site:
    local = alias.asname or alias.name.split(".")[0]
    return local, (_IMPORT, alias.name if alias.asname else local)


def _from_sites(statement: ast.ImportFrom, base: str, stars: list[str]) -> list[_Site]:
    found: list[_Site] = []
    for alias in statement.names:
        if alias.name == "*":
            stars.append(base)
        else:
            found.append((alias.asname or alias.name, (_IMPORT, f"{base}.{alias.name}")))
    return found


def _assign_sites(statement: ast.Assign | ast.AnnAssign, value: ast.expr) -> list[_Site]:
    """An assignment of a name or an attribute is a rebind the walk does
    not follow here, thus doubt. A value of any other shape is defined
    here. A target that is not a plain name is doubt."""
    targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
    kind = _DOUBT if isinstance(value, (ast.Name, ast.Attribute)) else _DEFINED
    found: list[_Site] = []
    for target in targets:
        if isinstance(target, ast.Name):
            found.append((target.id, (kind, "")))
        else:
            found.extend(_doubts(_stored_names(target)))
    return found


def _doubts(names: set[str]) -> list[_Site]:
    return [(name, (_DOUBT, "")) for name in sorted(names)]


def _stored_names(node: ast.AST) -> set[str]:
    """Every name any construct in ``node`` can bind, nested or not."""
    names: set[str] = set()
    for inner in ast.walk(node):
        if isinstance(inner, ast.Name) and isinstance(inner.ctx, ast.Store):
            names.add(inner.id)
        elif isinstance(inner, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name.split(".")[0]) for a in inner.names)
        elif isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(inner.name)
        elif isinstance(inner, ast.ExceptHandler) and inner.name:
            names.add(inner.name)
        elif isinstance(inner, ast.Global):
            names.update(inner.names)
    return names


def _globals_in(node: ast.AST) -> set[str]:
    return {
        name for inner in ast.walk(node) if isinstance(inner, ast.Global) for name in inner.names
    }


def _is_dynamic(tree: ast.Module) -> bool:
    """True when the module can bind a name the walk does not see."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "__getattr__":
            return True
        if _binds_getattr(node):
            return True
        if isinstance(node, ast.Call) and _leaf(node.func) in _DYNAMIC_CALLS:
            return True
        if isinstance(node, ast.Attribute) and node.attr == "modules":
            return True
    return False


def _binds_getattr(node: ast.AST) -> bool:
    """A module ``__getattr__`` that is not a ``def``: an assignment
    (``__getattr__ = lambda name: ...``), a tuple target, or an import."""
    if isinstance(node, ast.Name):
        return node.id == "__getattr__" and isinstance(node.ctx, ast.Store)
    return isinstance(node, ast.alias) and (node.asname or node.name) == "__getattr__"


def _leaf(callee: ast.expr) -> str | None:
    if isinstance(callee, ast.Attribute):
        return callee.attr
    return callee.id if isinstance(callee, ast.Name) else None
