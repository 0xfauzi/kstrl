"""Who reads ``kstrl.toml``, and from which scope (#192).

Two layers, and the split is the point.

Layer 1 discovers the CONFIG SURFACE without naming a single class: any
scope whose own body calls one of the three primitives that actually
reach the file. A new config dataclass therefore shows up as an
unexplained delta in the surface census rather than as a hole in a list
somebody forgot to extend. That is the closed-by-construction shape
``EXPECTED_JOURNAL_PATH_SITES`` uses in
``tests/test_journal_one_writer.py``.

Layer 2 counts every CALL of a layer-1 method and attributes it to the
innermost scope that owns it, so a guard can ask "which scope of
``ComponentPipeline`` reads config" rather than "does the file contain
the string".

Both layers FLAG. Per CLAUDE.md's split, a guard that flags may
over-match and costs a false positive somebody reads; a guard that
clears must be narrow, and over-matching there deletes the mechanism.
Neither layer is asked to prove a site is fine.

DISCLOSED LIMIT. Both layers resolve a callee syntactically and only in
two shapes: a bare ``Name`` for a primitive, and ``Name.attr`` for a
surface class. A read reached through anything deeper - ``mod.Cls.load``
after ``import kstrl.policy as mod``, or ``c.load_toml_section`` after
``import kstrl.config as c`` - is invisible, and invisible to a FLAGGING
guard means it goes quiet rather than red. Measured in ``kstrl/`` today:
54 bare-name primitive calls and 0 in the attribute form, so the limit
is latent rather than live.
``TestPipelineReadsConfigOnlyAtConstruction::test_a_module_qualified_load_is_invisible``
is the strict xfail behind this paragraph; the day the walk is taught
``astwalk.bindings`` it XPASSes and this text has to be edited in the
same diff.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from tests.helpers.astwalk import all_nodes, label, own_nodes, parsed, scopes

#: The three functions in ``kstrl/config.py`` that reach the file. Every
#: config dataclass's ``load`` bottoms out in one of them, which is what
#: lets layer 1 enumerate no class names of its own.
PARSE_PRIMITIVES = frozenset({"load_toml_section", "load_toml_document", "resolve_config_file"})

#: The environment half of the same per-section resolution. Not a parse,
#: but a second answer to "what is this setting", so a site that calls it
#: mid-run diverges from a run-start resolution exactly as a parse does.
ENV_METHODS = frozenset({"from_env"})


@dataclass(frozen=True)
class Surface:
    """Layer 1: everything in ``kstrl/`` that reads config from disk.

    ``classes`` maps a class name to the methods of it that read;
    ``free`` is the bare functions that read without owning a class.
    ``attributed`` and ``raw`` are the census control: every primitive
    call the walk found must land in exactly one scope, so an
    attribution that silently loses a call fails here rather than
    quietly shrinking the surface.
    """

    classes: dict[str, frozenset[str]]
    free: frozenset[str]
    attributed: int
    raw: int


def _primitive_calls(node: ast.AST) -> int:
    """How many bare calls to a parse primitive this scope owns."""
    return sum(
        1
        for child in own_nodes(node)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Name)
        and child.func.id in PARSE_PRIMITIVES
    )


def _raw_primitive_calls(tree: ast.Module) -> int:
    """The same count taken over the WHOLE module, ignoring scope.

    The control for the attribution: ``own_nodes`` stops at a nested
    function and at a lambda, so a primitive called from inside one is
    invisible to the per-scope walk. That is a real blind spot and it is
    measured rather than assumed - if the two counts ever disagree the
    surface is under-reported and this says so.
    """
    return sum(
        1
        for node in all_nodes(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in PARSE_PRIMITIVES
    )


def surface(sources: Iterable[Path]) -> Surface:
    """Layer 1 over a corpus of modules."""
    classes: dict[str, set[str]] = {}
    free: set[str] = set()
    attributed = 0
    raw = 0
    for source_file in sources:
        tree = parsed(source_file)
        raw += _raw_primitive_calls(tree)
        class_names = {n.name for n in all_nodes(tree) if isinstance(n, ast.ClassDef)}
        for node, qualified in scopes(tree):
            hits = _primitive_calls(node)
            if not hits:
                continue
            attributed += hits
            parts = qualified.split(".")
            if len(parts) == 2 and parts[0] in class_names:
                classes.setdefault(parts[0], set()).add(parts[1])
            else:
                free.add(qualified)
    return Surface(
        classes={name: frozenset(methods) for name, methods in classes.items()},
        free=frozenset(free),
        attributed=attributed,
        raw=raw,
    )


@dataclass(frozen=True)
class ReadSite:
    """Layer 2: one call of the config surface, and the scope that owns it."""

    module: str
    line: int
    scope: str
    target: str


def read_sites(source_file: Path, found: Surface) -> list[ReadSite]:
    """Every call of ``found``'s surface in one module, by scope.

    Three shapes count, and they are the three the census measured:
    ``Cls.method(...)`` for a surface class, a bare call to a free
    reader, and a bare call to a primitive from a scope that is not
    itself part of the surface.
    """
    tree = parsed(source_file)
    module = label(source_file)
    sites: list[ReadSite] = []
    for node, qualified in scopes(tree):
        for child in own_nodes(node):
            if not isinstance(child, ast.Call):
                continue
            target = _target(child, found, qualified)
            if target is not None:
                sites.append(ReadSite(module, child.lineno, qualified, target))
    return sites


def _target(node: ast.Call, found: Surface, qualified: str) -> str | None:
    """What this call reads, or None if it reads no config."""
    callee = node.func
    if isinstance(callee, ast.Attribute) and isinstance(callee.value, ast.Name):
        methods = found.classes.get(callee.value.id)
        if methods is not None and callee.attr in methods | ENV_METHODS:
            return f"{callee.value.id}.{callee.attr}"
        return None
    if isinstance(callee, ast.Name):
        if callee.id in PARSE_PRIMITIVES or callee.id in found.free:
            # A surface member calling its own primitive is the
            # definition of the read, not a second one.
            return None if qualified in found.free or _is_surface(qualified, found) else callee.id
    return None


def _is_surface(qualified: str, found: Surface) -> bool:
    parts = qualified.split(".")
    return len(parts) == 2 and parts[1] in found.classes.get(parts[0], frozenset())
