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

LAMBDAS ARE SCOPES. ``astwalk.scopes`` enumerates modules, functions and
methods; ``own_nodes`` stops at a lambda. So a read inside one belonged
to no scope at all and was invisible to BOTH layers at once - it did not
appear as a layer-2 offender AND it left layer 1's ``attributed == raw``
control untouched, which is the one shape the census control exists to
prevent. Measured before the fix: a ``SecurityConfig.load`` inside
``(lambda: ...)()`` planted in a pipeline method gave raw+0,
attributed+0 and 0 offenders. :func:`scopes_with_lambdas` is the fix and
``TestTheWalkSeesEveryCallShape`` is the measurement.

NAMES ARE RESOLVED, NOT SPELLED. Layer 2 resolves a surface reference
through ``astwalk.bindings`` rather than by matching the text, so
``PolicyConfig.load``, ``mod.PolicyConfig.load`` after ``import
kstrl.policy as mod``, ``PC.load`` after ``import ... as PC``,
``getattr(PolicyConfig, "load")``, ``partial(PolicyConfig.load, root)``
and ``f = PolicyConfig.load`` are all the same site. It counts
REFERENCES rather than calls, which over-matches: ``config_sections()``
names 22 loaders as VALUES and every one of them is a site. That is the
direction a FLAGGING guard may be wrong in, and it is what makes the
partial and the bound-name shapes visible at all.

DISCLOSED LIMIT. Layer 1 still matches a primitive by bare ``Name``
only, so ``c.load_toml_section`` after ``import kstrl.config as c``
would not enrol its scope in the surface. Measured in ``kstrl/`` today:
54 bare-name primitive calls and 0 in the attribute form, so the limit
is latent rather than live.
``TestConfigSurface::test_a_module_qualified_primitive_is_invisible`` is
the strict xfail behind this paragraph; the day layer 1 is taught
``astwalk.bindings`` too it XPASSes and this text has to be edited in
the same diff.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from tests.helpers.astwalk import all_nodes, bindings, label, own_nodes, parsed, scopes
from tests.helpers.astwalk.resolve import Bindings

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


def scopes_with_lambdas(tree: ast.Module) -> list[tuple[ast.AST, str]]:
    """``astwalk.scopes`` plus every lambda, as a scope of its own.

    ``own_nodes`` stops at a lambda, and ``scopes`` does not enumerate
    one, so without this a read inside a lambda belonged to no scope and
    both layers went quiet about it together. The loop grows the list it
    is iterating on purpose: a lambda inside a lambda is found when the
    outer one is scanned as a scope, so the closure is over nesting
    depth rather than over a depth somebody guessed.
    """
    found = list(scopes(tree))
    index = 0
    while index < len(found):
        node, qualified = found[index]
        index += 1
        for child in own_nodes(node):
            if isinstance(child, ast.Lambda):
                found.append((child, f"{qualified}.<lambda>"))
    return found


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
        for node, qualified in scopes_with_lambdas(tree):
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
    """Every reference to ``found``'s surface in one module, by scope.

    REFERENCES, not calls. ``partial(PolicyConfig.load, root)`` and
    ``f = PolicyConfig.load`` never spell a call of the surface at all,
    and enumerating call shapes is how a walk ends up blind to the one
    somebody writes next; naming a loader is the thing this can see, and
    a name that is never called is a false positive a reader dismisses.
    """
    tree = parsed(source_file)
    binds = bindings(tree)
    module = label(source_file)
    sites: list[ReadSite] = []
    for node, qualified in scopes_with_lambdas(tree):
        for child in own_nodes(node):
            target = _target(child, found, qualified, binds)
            if target is not None:
                sites.append(ReadSite(module, child.lineno, qualified, target))
    return sites


def _target(node: ast.AST, found: Surface, qualified: str, binds: Bindings) -> str | None:
    """What this node reads, or None if it reads no config."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        name = node.func.id
        if name in PARSE_PRIMITIVES or name in found.free:
            # A surface member calling its own primitive is the
            # definition of the read, not a second one.
            return None if qualified in found.free or _is_surface(qualified, found) else name
    if isinstance(node, ast.Attribute | ast.Call):
        return _surface_reference(node, found, binds)
    return None


def _surface_reference(node: ast.AST, found: Surface, binds: Bindings) -> str | None:
    """``Cls.method`` for a surface class, however the name reached here.

    Resolved rather than spelled: the same site is written five ways in
    ``kstrl/`` and its tests, and a walk that matches the text sees one
    of them. A ``guessed`` origin is kept, because this layer FLAGS and
    an over-match here costs a reader one line, while dropping it is the
    skip direction this whole file exists to close.
    """
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        # The attribute is a node of its own and is matched there.
        return None
    for owner, attr in _candidates(node, binds):
        methods = found.classes.get(owner)
        if methods is not None and attr in methods | ENV_METHODS:
            return f"{owner}.{attr}"
    return None


def _candidates(node: ast.AST, binds: Bindings) -> list[tuple[str, str]]:
    """Every ``(owner, method)`` pair this expression could name.

    BOTH spellings are offered and the caller keeps whichever names a
    surface class, rather than the first that parses. Trying the literal
    one and stopping there is what an earlier draft did, and it lost
    ``PC.load`` after ``from kstrl.policy import PolicyConfig as PC``:
    the literal read gives ``PC``, which is not a config class, and the
    walk never asked the resolver.
    """
    pairs: list[tuple[str, str]] = []
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        pairs.append((node.value.id, node.attr))
    origin = binds.origin_of(node)
    if origin is not None:
        parts = origin.dotted.split(".")
        if len(parts) >= 2:
            pairs.append((parts[-2], parts[-1]))
    return pairs


def _is_surface(qualified: str, found: Surface) -> bool:
    parts = qualified.split(".")
    return len(parts) == 2 and parts[1] in found.classes.get(parts[0], frozenset())
