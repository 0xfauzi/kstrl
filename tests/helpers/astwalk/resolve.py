"""Name resolution, and the calls it can and cannot decide.

The shape-enumerating layer. It is the half that can go blind, so
every answer it cannot give is a row in :class:`~..net.Sites`
undecided rather than an absence."""

from __future__ import annotations

import ast
from collections.abc import Iterable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from weakref import WeakKeyDictionary

from tests.helpers.astwalk.corpus import all_nodes, folded_str
from tests.helpers.astwalk.net import Sites

# --- name resolution ------------------------------------------------------


def dotted(node: ast.AST) -> str | None:
    """``a.b.c`` as written, or None if the expression is not a plain path."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted(node.value)
        return None if base is None else f"{base}.{node.attr}"
    return None


def leaf_name(node: ast.AST) -> str | None:
    """The last identifier of a callee, whatever precedes it.

    ``subprocess.run`` -> ``run``; ``helper().run`` -> ``run``;
    ``TABLE[key]`` -> None, because the AST holds no name to read. That
    None is a hard undecidable and it is small: measured over 13,145 calls
    in ``kstrl/``, four.
    """
    if isinstance(node, ast.Attribute):
        return node.attr
    return node.id if isinstance(node, ast.Name) else None


@dataclass(frozen=True)
class Bindings:
    """What the names in one module refer to, and which ones got away.

    ``origins`` maps a local spelling to a dotted origin: ``_tl`` ->
    ``tomllib``, ``Spawn`` -> ``subprocess.Popen``, ``self.lookup`` ->
    ``os.getpgid``. ``attributes`` maps a bare attribute name to what
    SOME receiver bound it to, and deliberately over-matches: after
    ``class G: lookup = os.getpgid`` every ``x.lookup(...)`` resolves,
    which two of ``tests/test_safe_pgid.py``'s pinned rows need and the
    AST cannot type. Per MODULE, not per scope. Both over-report, the
    direction a guard may be wrong in.

    THERE IS NO ``opaque`` FIELD, and #324 round 2 removed one. It held
    every name bound to something this resolver could not follow, and
    ``astwalk/__init__.py`` named it as one of the five places the API
    will not let a caller leave out. Measured: nothing in the repo read
    it, forcing it empty everywhere left 455 tests and 23 xfails
    unchanged, and it CONTRADICTED ``origins``, because ``_bind_one``
    adds a name on the sweep that cannot resolve it and never removes it
    when a later sweep can. On ``import os; b = a; a = os`` it said ``b``
    was opaque while ``origins`` said ``b`` was ``os``; two real modules,
    ``kstrl/loop.py`` and ``kstrl/pipeline.py``, were in both at once. A
    public field named as a mechanism, with no reader and a wrong answer,
    is documentation. What actually makes an unfollowable name loud is
    :func:`calls_to`'s rule that unresolved is undecided, and
    ``tests/test_astwalk.py`` pins that.
    """

    origins: Mapping[str, str] = field(default_factory=dict)
    attributes: Mapping[str, str] = field(default_factory=dict)

    def resolve(self, node: ast.AST) -> str | None:
        """The dotted origin this expression refers to, or None.

        Longest known prefix wins, so ``_tl.load`` resolves through
        ``_tl``, then the receiver, then ``getattr``."""
        path = dotted(node)
        if path is not None:
            through = self._through_prefix(path)
            if through is not None:
                return through
        if isinstance(node, ast.Attribute):
            return self._through_attribute(node)
        return self._through_getattr(node)

    def _through_attribute(self, node: ast.Attribute) -> str | None:
        """An attribute chain the flat prefix walk could not spell.

        The second step is the one #324's subprocess lane found missing:
        after ``class G: mod = os``, ``G.mod.getpgid`` has no known
        prefix, so an earlier draft gave up and reported the call
        undecided. Resolving the RECEIVER first answers it outright.
        """
        if node.attr in self.attributes:
            return self.attributes[node.attr]
        base = self.resolve(node.value)
        return None if base is None else f"{base}.{node.attr}"

    def _through_prefix(self, path: str) -> str | None:
        parts = path.split(".")
        for cut in range(len(parts), 0, -1):
            head = ".".join(parts[:cut])
            if head in self.origins:
                return ".".join([self.origins[head], *parts[cut:]])
        return None

    def _through_getattr(self, node: ast.AST) -> str | None:
        if not isinstance(node, ast.Call) or leaf_name(node.func) != "getattr":
            return None
        if not isinstance(node.func, ast.Name) or len(node.args) < 2:
            return None
        base = self.resolve(node.args[0])
        attr = folded_str(node.args[1])
        return f"{base}.{attr}" if base is not None and attr is not None else None


def bindings(tree: ast.Module, *, module: str = "") -> Bindings:
    """Resolve every name in one module, to a fixed point.

    Covers what #324 records eleven guards getting wrong in six different
    subsets: ``import x``, ``import x as y``, ``import x.y as z``,
    ``from x import f``, ``from x import f as g``, a RELATIVE
    ``from .x import f`` resolved against ``module``, ``_p = x``,
    ``_p: T = x``, a walrus, an attribute target (``self.lookup = ...``,
    ``C.run = ...``), ``getattr`` with a foldable name, and a rebind of a
    rebind at any chain length.

    FIRST BINDING WINS, which is what makes the fixed point terminate:
    ``origins`` only gains keys, so the loop is bounded by the number of
    distinct targets in the file. An earlier draft let a later binding
    overwrite an earlier one and ``p = p.parent`` grew the origin string
    without bound. It also means a name bound to a target and later
    rebound to something opaque stays resolved to the target, the
    over-reporting direction.

    A local ``def`` or ``class`` is deliberately NOT a binding. Measured
    both ways: binding ``ClassDef`` makes ``class G: lookup =
    os.getpgid`` then ``G.lookup(pid)`` resolve to ``G.lookup`` and
    vanish from the seen half, and binding ``FunctionDef`` lets a ``def
    load()`` above ``from tomllib import load`` mask the import under
    first-binding-wins. Both are the skip direction; opaque is not.

    ``module`` is this file's dotted name, needed only by relative
    imports. Left empty they resolve to a leading-dot origin that cannot
    collide with an absolute one, so the answer is useless rather than
    silently wrong.
    """
    per_module = _BINDINGS.setdefault(tree, {})
    hit = per_module.get(module)
    if hit is not None:
        return hit
    walked = all_nodes(tree)
    origins: dict[str, str] = {}
    for node in walked:
        _absorb_import(node, origins, module)
    table = _Table(origins, {}, set(), _class_body_names(tree))
    while _rebind_sweep(walked, table):
        continue
    built = Bindings(origins, table.attributes)
    per_module[module] = built
    return built


#: tree -> module name -> its bindings. Measured: resolving 127 modules
#: costs 132 ms, and every guard that asks about a different target set
#: would otherwise pay it again.
#:
#: A WEAK KEY, and the first draft was keyed on ``id(tree)`` with the tree
#: held in the value so the id could not be reused. That worked and it
#: leaked: measured at session end, 158 trees reaching 259,718 nodes were
#: reachable only through this dict, 71 MB of a 420 MB peak. A weak key
#: gives the same identity safety, because a dead tree takes its row with
#: it rather than leaving an id to be reused.
_BINDINGS: MutableMapping[ast.Module, dict[str, Bindings]] = WeakKeyDictionary()


@dataclass(frozen=True)
class _Table:
    """The three growing halves of a binding sweep, plus the class names.

    A dataclass rather than four parameters: the sweep and its per-target
    step both need all of them.
    """

    origins: dict[str, str]
    attributes: dict[str, str]
    #: Targets a sweep could not resolve. The memo that terminates the
    #: fixed point: without it a target nothing can resolve is "new"
    #: every pass. Private, and it stays private, because it is stale by
    #: construction the moment a later sweep resolves the name.
    unresolved: set[str]
    class_names: frozenset[str]

    def resolver(self) -> Bindings:
        return Bindings(self.origins, self.attributes)


def _class_body_names(tree: ast.Module) -> frozenset[str]:
    """Names bound directly in a ``class`` body: really attributes of it.

    ``class G: lookup = os.getpgid`` binds a bare ``lookup`` as far as the
    AST is concerned, but every use is spelled ``G.lookup`` or
    ``G().lookup``. Without this they are two different names.
    """
    found: set[str] = set()
    for node in all_nodes(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for item in node.body:
            targets, _value = assignment_parts(item)
            found.update(target for target in targets if target is not None)
    return frozenset(found)


def _absorb_import(node: ast.AST, origins: dict[str, str], module: str) -> None:
    """One ``import`` or ``from ... import``, into the origins table."""
    if isinstance(node, ast.Import):
        for alias in node.names:
            local = alias.asname or alias.name.split(".")[0]
            origins.setdefault(local, alias.name if alias.asname else local)
    elif isinstance(node, ast.ImportFrom):
        base = _import_base(node, module)
        for alias in node.names:
            origins.setdefault(alias.asname or alias.name, f"{base}.{alias.name}")


def _import_base(node: ast.ImportFrom, module: str) -> str:
    """The module a ``from ... import`` reads from, relative levels and all.

    ``ImportFrom.level`` is the field #324 records three guards dropping.
    Dropped, ``from .os import getpgid`` resolves to the stdlib ``os``, a
    false positive, and ``from .config_report import f`` to a module that
    can never match a package-qualified key, a false NEGATIVE and the
    direction that matters. ``from . import x`` has no module at all and
    one of them discarded it outright.
    """
    if not node.level:
        return node.module or ""
    package = ".".join(module.split(".")[: -node.level]) if module else "." * node.level
    if not node.module:
        return package
    return f"{package}{node.module}" if package.endswith(".") else f"{package}.{node.module}"


def _rebind_sweep(nodes: Sequence[ast.AST], table: _Table) -> bool:
    """One pass of rebinds. True if anything new was learned."""
    resolver = table.resolver()
    grew = False
    for node in nodes:
        targets, value = assignment_parts(node)
        if value is None:
            continue
        for target in targets:
            if target is not None:
                grew |= _bind_one(target, value, table, resolver)
    return grew


def _bind_one(target: str, value: ast.expr, table: _Table, resolver: Bindings) -> bool:
    """One target. True if this call learned something new about it."""
    if target in table.origins:
        return False
    origin = resolver.resolve(value)
    if origin is None:
        if target in table.unresolved:
            return False
        table.unresolved.add(target)
        return True
    table.origins[target] = origin
    if "." in target or target in table.class_names:
        table.attributes.setdefault(target.rsplit(".", 1)[-1], origin)
    return True


def assignment_parts(node: ast.AST) -> tuple[list[str | None], ast.expr | None]:
    """The dotted targets one binding binds, and what it binds them to.

    ``Assign``, ``AnnAssign`` and the walrus. ``AnnAssign`` is here
    because #324's second logged instance is a TOML parse made invisible
    by ``_p: object = tomllib``: that guard resolved ``Assign`` only, so
    the site was reported neither guarded nor unguarded. A target the AST
    cannot spell as a path yields ``None``, which the sweep skips rather
    than guessing at.
    """
    if isinstance(node, ast.Assign):
        return [dotted(target) for target in node.targets], node.value
    if isinstance(node, ast.AnnAssign):
        return [dotted(node.target)], node.value
    if isinstance(node, ast.NamedExpr):
        return [dotted(node.target)], node.value
    return [], None


def bound_names(node: ast.AST) -> tuple[list[str], ast.expr | None]:
    """The plain LOCAL names one binding binds, and what it binds them to.

    :func:`assignment_parts` answers with dotted targets too, because a
    resolver needs ``self.lookup`` to mean something. A guard building an
    alias table over local names does not, so an attribute target is not
    one of them and a target the AST cannot spell as a path is ``None``.

    Here rather than in a guard since #324 round 2: it lived in
    ``tests/test_journal_one_writer.py`` and
    ``tests/test_event_names_have_one_home.py`` imported it FROM there, so
    one guard's refactor was another guard's breakage and neither file
    said so. It is a projection of :func:`assignment_parts`, so it belongs
    beside what it projects.
    """
    targets, value = assignment_parts(node)
    return [name for name in targets if name is not None and "." not in name], value


def calls_to(
    tree: ast.Module,
    targets: Iterable[str],
    *,
    where: str = "",
    module: str = "",
) -> Sites:
    """Every call in one module that resolves to a target, and every call
    that could be one and could not be decided.

    The undecided rule is two questions and no third case, which is what
    keeps the skip direction out of it:

    - has the callee a last identifier a target ends in? If not it is not
      a candidate, which keeps ``path.mkdir`` and ``', '.join`` out. A
      callee with NO identifier at all (``TABLE[key](...)``) is a
      candidate for every target set: there is nothing to compare. Four
      of those in ``kstrl/``.
    - does it resolve? Resolved and wanted is ``seen``, resolved and
      unwanted is nothing, and UNRESOLVED IS UNDECIDED, with no third
      case. An earlier draft let a dotted callee whose head it never saw
      bound fall through as decided, and #324's own lane B measured a
      planted ``tempfile.mkstemp()`` in a module with no ``import
      tempfile`` going neither seen nor undecided.

    Measured over ``kstrl/`` against the five subprocess spawns: 68 seen,
    12 undecided, an inventory a guard can pin rather than a list it
    would be silenced for printing. What it leaves, stated rather than
    implied: a target passed IN as a parameter reads as a call on another
    object, because its leaf is the parameter's name. The caller had to
    obtain it to pass it, so a :func:`census` of the acquisition counts
    the site. Pin it with :func:`blind_spot`.
    """
    wanted = frozenset(targets)
    leaves = {target.rsplit(".", 1)[-1] for target in wanted}
    table = bindings(tree, module=module)
    leaves |= _bound_target_leaves(tree, table, wanted)
    seen: list[str] = []
    undecided: list[str] = []
    for node in all_nodes(tree):
        if isinstance(node, ast.Call):
            _classify_call(node, table, wanted, leaves, where, seen, undecided)
    return Sites(tuple(seen), tuple(undecided))


def resolved_calls(
    tree: ast.Module,
    targets: Iterable[str],
    *,
    module: str = "",
) -> list[tuple[ast.Call, str]]:
    """The SEEN half of :func:`calls_to`, as nodes rather than as strings.

    For a guard that has to look at a call's arguments, such as "does
    this spawn name a ``timeout=``". It is the ONE function here that
    hands back half an answer, because a guard reading a call's arguments
    needs the node and no signature can give it one without also giving
    it a seen half it could use alone. Measured, not feared:
    ``resolved_calls(parse("x.Popen(argv)"), {"subprocess.Popen"})``
    returns ``[]``, which is what a module with no spawn in it returns.

    So the mechanism for this one is a STATIC GUARD rather than a
    signature: ``tests/test_astwalk.py``'s
    ``TestResolvedCallsIsNotUsableOnItsOwn`` fails any module in
    ``tests/`` that calls this and never names the undecided half. Weaker
    than :func:`~..net.assert_sites`, which is why that class also pins by
    how much.
    """
    wanted = frozenset(targets)
    table = bindings(tree, module=module)
    return [
        (node, origin)
        for node in all_nodes(tree)
        if isinstance(node, ast.Call) and (origin := table.resolve(node.func)) in wanted
    ]


def _bound_target_leaves(tree: ast.Module, table: Bindings, wanted: frozenset[str]) -> set[str]:
    """Attribute names this module binds to a target, e.g. ``self.spawn``.

    Without these, ``self.spawn = subprocess.Popen`` in ``__init__`` and
    ``self.spawn(argv)`` in a method read as a call on another object.
    """
    found: set[str] = set()
    for node in all_nodes(tree):
        targets, value = assignment_parts(node)
        if value is not None and table.resolve(value) in wanted:
            found.update(t.rsplit(".", 1)[-1] for t in targets if t is not None)
    return found


def _classify_call(
    node: ast.Call,
    table: Bindings,
    wanted: frozenset[str],
    leaves: set[str],
    where: str,
    seen: list[str],
    undecided: list[str],
) -> None:
    """One call, into exactly one of the two halves or neither."""
    site = f"{where}:{node.lineno}" if where else str(node.lineno)
    origin = table.resolve(node.func)
    if origin is not None:
        if origin in wanted:
            seen.append(f"{site} {origin}")
        return
    leaf = leaf_name(node.func)
    if leaf is None or leaf in leaves:
        undecided.append(f"{site} {ast.unparse(node.func)}")
