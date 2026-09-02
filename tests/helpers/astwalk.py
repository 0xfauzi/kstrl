"""One home for the AST walking the static guards in ``tests/`` do.

#324 is the record of what having eleven of them cost. Ten instances are
logged there, every one in the SKIP direction: a matcher could not resolve
a name or fold a value, so it silently did not look, and reported clean.
Two guards were holed a second time after being fixed once. One was
written by an author explicitly briefed on the pattern, which is the
evidence that briefing is not a control.

So a shared resolver is not the whole job: on its own it would move eleven
holes into one. What this has to do as well is make the skip direction
LOUD, in five places the API will not let a caller leave out.

1. :func:`assert_census` takes a ``control`` and refuses to pin an
   inventory whose predicate matched nothing in it. An empty inventory is
   also what a switched-off net returns; now the two are told apart.
2. :class:`Sites` has two halves and :func:`assert_sites` requires an
   expectation for BOTH. To report nothing undecided a guard must write
   ``undecided=()``, which is a claim, and a false one fails.
3. :class:`Clause` carries ``decided``, because a handler whose type the
   walk cannot name yields an empty name set, and an empty set reads
   exactly like "catches nothing".
4. :class:`Bindings` keeps ``opaque``: every name bound to a value the
   resolver could not follow. :func:`calls_to` turns a call through one
   into an UNDECIDED site rather than a decided miss.
5. :func:`blind_spot` is the body of a disclosed limit's anti-vacuity
   test, run under ``xfail(strict=True)`` so closing the hole goes red and
   the docstring has to be edited in the same diff.

THE DISTINCTION THAT DRIVES THE SHAPE. ``EXPECTED_JOURNAL_PATH_SITES`` in
``tests/test_journal_one_writer.py`` inventories every place the resource
is OBTAINED, so it is closed by construction: you cannot add a writer
without adding a row. A ledger of places the walk gave up is closed only
over the shapes the walk already enumerates, and #324's instance 10 is the
proof: a well-built ledger with reasons per row still missed a producer,
because the producer's SHAPE was never enumerated.

:func:`census` is that closed form generalised. It counts nodes satisfying
a predicate and enumerates no node types; :func:`spells` goes further and
enumerates no FIELDS either. That is why it catches shapes nobody thought
of. Prefer it wherever the guard's subject permits, and where it does not,
say so and pin the residual with :func:`blind_spot`.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

#: The checkout, located from this file rather than from a caller's, so
#: that ten guards stop each deriving it and disagreeing about the answer.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
KSTRL_PACKAGE = REPO_ROOT / "kstrl"
TESTS_DIR = REPO_ROOT / "tests"


# --- corpus ---------------------------------------------------------------


def label(source_file: Path, root: Path | None = None) -> str:
    """How a file is named in an inventory key and in a failure message.

    Not ``source_file.name``: ten basenames occur twice in ``kstrl/``,
    once at the top level and once under ``tui/screens/``, and a message
    naming a file the reader cannot find is worse than no message. Falls
    back to the repo-relative path, then to the basename, so a snippet
    written to a ``tmp_path`` still labels itself.
    """
    for base in (root or KSTRL_PACKAGE, REPO_ROOT):
        try:
            return str(source_file.relative_to(base))
        except ValueError:
            continue
    return source_file.name


def module_name(source_file: Path) -> str:
    """``kstrl/tui/session.py`` -> ``kstrl.tui.session``.

    The dotted name a relative import resolves against. ``__init__.py`` is
    stripped so ``from . import x`` inside a package's own ``__init__``
    lands on the package rather than one level below it.
    """
    try:
        relative = source_file.resolve().relative_to(REPO_ROOT)
    except ValueError:
        return source_file.stem
    dotted_path = str(relative.with_suffix("")).replace("/", ".")
    return dotted_path[: -len(".__init__")] if dotted_path.endswith(".__init__") else dotted_path


def package_sources() -> list[Path]:
    """Every module in ``kstrl/``, in a stable order."""
    return sorted(KSTRL_PACKAGE.rglob("*.py"))


def test_sources(exclude: Path | None = None) -> list[Path]:
    """Every module in ``tests/``, optionally minus the caller's own file.

    A guard that names the shapes it forbids in its own docstring or its
    own fixtures would otherwise scan itself.
    """
    skip = exclude.resolve() if exclude is not None else None
    return [path for path in sorted(TESTS_DIR.rglob("*.py")) if path.resolve() != skip]


#: Source text -> its parsed tree, shared by every guard in the suite.
#:
#: Keyed on the TEXT rather than the path, because the positive controls
#: in several guards rewrite one ``other.py`` several times inside a
#: single test, and a path-keyed cache would hand the second call the
#: first snippet's tree.
_PARSED: dict[str, ast.Module] = {}


def parsed(source_file: Path) -> ast.Module:
    """The module's AST, parsed once for the whole session.

    Measured: 127 modules, 237,105 nodes, 123 ms a pass. Before this
    cache ``tests/test_tui_config_walk.py`` alone made four of those
    passes per session and nine other guards made one each.
    """
    return parse(source_file.read_text(encoding="utf-8"))


def parse(source: str) -> ast.Module:
    """One snippet's AST, from the same cache. Reads well in a control."""
    tree = _PARSED.get(source)
    if tree is None:
        tree = ast.parse(source)
        _PARSED[source] = tree
    return tree


# --- constant folding -----------------------------------------------------


def folded_str(node: ast.AST) -> str | None:
    """The string this expression is KNOWN to evaluate to, or None.

    Round 2 of review on #327 is why this exists: two writers defeated
    three layers of a guard by never spelling in one piece what they
    reached, ``getattr(config, "journal_" + "path")`` and
    ``root / ".kstrl" / ("evolution" + ".jsonl")``. Neither is exotic;
    both are what somebody writes to get past a string search.

    CPython folds adjacent literals (``"a" "b"``) into one ``Constant`` at
    parse time, so that case needs nothing here. An f-string does NOT
    fold, measured on this interpreter, so ``JoinedStr`` and
    ``FormattedValue`` are handled explicitly alongside the ``+``.

    Decidable cases only. Anything whose value needs the interpreter
    (``"".join(parts)``, ``%``-formatting, ``str.replace``, a name, an env
    var) returns None, and a guard that folds must disclose that residual
    and pin it with :func:`blind_spot` rather than imply it away.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.BinOp):
        return _folded_concat(node)
    if isinstance(node, ast.FormattedValue):
        return _folded_placeholder(node)
    if isinstance(node, ast.JoinedStr):
        return _folded_parts(node.values)
    return None


def _folded_concat(node: ast.BinOp) -> str | None:
    """``"journal_" + "path"``, and nothing else that uses ``+``."""
    if not isinstance(node.op, ast.Add):
        return None
    return _folded_parts([node.left, node.right])


def _folded_placeholder(node: ast.FormattedValue) -> str | None:
    """The ``{...}`` of an f-string, when it is decidable.

    ``!r`` and a format spec both change the result, so only the plain
    case folds. Measured: ``ast.parse`` gives ``conversion == -1`` for a
    plain placeholder and for one carrying a format spec, and 114 for
    ``!r``; ``None`` never appears on the parse path.
    """
    if node.conversion == -1 and node.format_spec is None:
        return folded_str(node.value)
    return None


def _folded_parts(nodes: list[ast.expr]) -> str | None:
    """Every piece folded and joined, or None if any piece is unknown."""
    parts: list[str] = []
    for node in nodes:
        folded = folded_str(node)
        if folded is None:
            return None
        parts.append(folded)
    return "".join(parts)


# --- the net: a census that enumerates no node types -----------------------

#: A per-node predicate. The census applies it to every node in a module
#: and counts, so a predicate that enumerates no node types gives a net
#: with no shape list to be incomplete.
Sees = Callable[[ast.AST], bool]


def spells(token: str) -> Sees:
    """Does this node write ``token`` anywhere the AST can hold a string?

    The strongest net here, because it enumerates no node types AND no
    field names: it asks ``ast.iter_fields`` for every string the node
    holds. That reaches ``Name.id``, ``Attribute.attr``, ``alias.name``,
    ``alias.asname``, ``arg.arg``, ``keyword.arg``, ``FunctionDef.name``,
    ``ExceptHandler.name``, ``Global.names``, a string literal, and every
    identifier slot a future CPython adds, without naming one of them.

    EQUALITY, not substring, so prose folding to a whole docstring is not
    a spelling; that is what keeps the inventory readable rather than
    something to be silenced. Use :func:`folds_containing` when the
    subject really is a substring. At most one hit per node.
    """

    def sees(node: ast.AST) -> bool:
        for _name, value in ast.iter_fields(node):
            if isinstance(value, str):
                if value == token:
                    return True
            elif isinstance(value, list) and any(
                item == token for item in value if isinstance(item, str)
            ):
                return True
        return folded_str(node) == token

    return sees


def folds_to(value: str) -> Sees:
    """Does this expression fold to exactly ``value``?

    Narrower than :func:`spells`: the VALUE an expression evaluates to,
    not every string the node holds, so a parameter called ``event_type``
    is not a spelling of the event type.
    """
    return lambda node: folded_str(node) == value


def folds_containing(part: str) -> Sees:
    """Does this expression fold to a string CONTAINING ``part``?

    For a subject that legitimately sits inside a longer string: a
    filename in a path, where ``root / ".kstrl/evolution.jsonl"`` folds to
    the whole relative path and equality would miss it. The cost is that
    prose folds too, so the inventory carries docstrings.
    """
    return lambda node: part in (folded_str(node) or "")


def census(sources: Iterable[Path], sees: Sees) -> dict[str, int]:
    """How many nodes in each module satisfy ``sees``, keyed by label.

    Counted PER MODULE so an unrelated edit elsewhere does not fail the
    guard, and modules with no hits are left out so the pinned dict stays
    the size of the answer rather than the size of the package.
    """
    built: dict[str, int] = {}
    for source_file in sources:
        hits = sum(1 for node in ast.walk(parsed(source_file)) if sees(node))
        if hits:
            built[label(source_file)] = hits
    return built


def assert_census(
    *,
    sources: Iterable[Path],
    sees: Sees,
    expected: Mapping[str, int],
    control: str,
    message: str,
) -> None:
    """Pin an inventory, having first proved the net still fires.

    ``control`` is source the predicate MUST hit. It is required, not
    optional, because ``built == expected`` is also exactly what a
    switched-off predicate returns, and #324's whole subject is guards
    reporting clean because they stopped looking. One line, and it is the
    difference between an assertion about the package and about nothing.
    """
    proof = sum(1 for node in ast.walk(parse(control)) if sees(node))
    assert proof, (
        "the census predicate matched nothing in its own control, so the "
        "inventory below is indistinguishable from what a switched-off net "
        f"returns. Control: {control!r}"
    )
    built = census(sources, sees)
    assert built == expected, f"{message} Found: {built}"


# --- the partition: what a walk saw, and what it could not decide ---------


@dataclass(frozen=True)
class Sites:
    """A walk's complete answer about one corpus.

    Every candidate lands in exactly one half. There is no third "was not
    looked at" bucket, which is the whole point: an unresolvable callee
    becomes a row in ``undecided`` rather than an absence in ``seen``.
    """

    seen: tuple[str, ...] = ()
    undecided: tuple[str, ...] = ()

    def __add__(self, other: Sites) -> Sites:
        return Sites(self.seen + other.seen, self.undecided + other.undecided)

    def sorted(self) -> Sites:
        return Sites(tuple(sorted(self.seen)), tuple(sorted(self.undecided)))


def assert_sites(
    found: Sites,
    *,
    seen: tuple[str, ...],
    undecided: tuple[str, ...],
    message: str,
) -> None:
    """Assert BOTH halves. Neither keyword has a default, deliberately.

    A guard reporting nothing undecided has to write ``undecided=()``,
    which is a claim about the walk rather than silence about it, and a
    false claim fails here instead of passing quietly.
    """
    assert found.undecided == undecided, (
        "the walk could not decide these sites, so they are neither "
        "cleared nor flagged. Resolve them, or pin them with the reason "
        f"they cannot be resolved. Undecided: {list(found.undecided)}"
    )
    assert found.seen == seen, f"{message} Sites: {list(found.seen)}"


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
    ``os.getpgid``. ``opaque`` holds every name bound to a value this
    resolver could not follow, which is the half that matters: a call
    through an opaque name is UNDECIDED, never a decided miss.
    ``attributes`` maps a bare attribute name to what SOME receiver bound
    it to, and deliberately over-matches: after ``class G: lookup =
    os.getpgid`` every ``x.lookup(...)`` resolves, which two of
    ``tests/test_safe_pgid.py``'s pinned rows need and the AST cannot
    type. Collected per MODULE rather than per scope. Both choices
    over-report, the direction a guard may be wrong in.
    """

    origins: Mapping[str, str] = field(default_factory=dict)
    opaque: frozenset[str] = frozenset()
    attributes: Mapping[str, str] = field(default_factory=dict)

    def resolve(self, node: ast.AST) -> str | None:
        """The dotted origin this expression refers to, or None.

        Longest known prefix wins, so ``_tl.load`` resolves through
        ``_tl`` and ``subprocess.Popen`` through ``subprocess``.
        ``getattr(mod, "name")`` resolves when the name folds, which is
        the shape ``tests/test_safe_pgid.py`` carried as an accepted miss.
        """
        path = dotted(node)
        if path is not None:
            through = self._through_prefix(path)
            if through is not None:
                return through
        if isinstance(node, ast.Attribute) and node.attr in self.attributes:
            return self.attributes[node.attr]
        return self._through_getattr(node)

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

    FIRST BINDING WINS, and that is what makes the fixed point terminate:
    ``origins`` only ever gains keys, so the loop is bounded by the number
    of distinct targets in the file. An earlier draft let a later binding
    overwrite an earlier one and ``p = p.parent`` grew the origin string
    without bound. It also means a name bound to a target and later
    rebound to something opaque stays resolved to the target, which is the
    over-reporting direction.

    ``module`` is this file's dotted name, needed only by relative
    imports. Left empty they resolve to a leading-dot origin that cannot
    collide with an absolute one, so the answer is useless rather than
    silently wrong.
    """
    key = (id(tree), module)
    hit = _BINDINGS.get(key)
    if hit is not None:
        return hit[1]
    nodes = list(ast.walk(tree))
    origins: dict[str, str] = {}
    for node in nodes:
        _absorb_import(node, origins, module)
    table = _Table(origins, {}, set(), _class_body_names(tree))
    while _rebind_sweep(nodes, table):
        continue
    built = Bindings(origins, frozenset(table.opaque), table.attributes)
    _BINDINGS[key] = (tree, built)
    return built


#: ``(id(tree), module)`` -> ``(tree, its bindings)``. Measured: resolving
#: 127 modules costs 132 ms, and every guard that asks about a different
#: target set would otherwise pay it again. The tree is kept in the value
#: so its ``id`` cannot be reused by a later object while the row lives.
_BINDINGS: dict[tuple[int, str], tuple[ast.Module, Bindings]] = {}


@dataclass(frozen=True)
class _Table:
    """The three growing halves of a binding sweep, plus the class names.

    A dataclass rather than four parameters because the sweep and its
    per-target step both need all of them, and eight positional arguments
    is how a helper stops being read.
    """

    origins: dict[str, str]
    attributes: dict[str, str]
    opaque: set[str]
    class_names: frozenset[str]

    def resolver(self) -> Bindings:
        return Bindings(self.origins, frozenset(self.opaque), self.attributes)


def _class_body_names(tree: ast.Module) -> frozenset[str]:
    """Names bound directly in a ``class`` body: really attributes of it.

    ``class G: lookup = os.getpgid`` binds a bare ``lookup`` as far as the
    AST is concerned, but every use of it is spelled ``G.lookup`` or
    ``G().lookup``. Without this they would be two different names.
    """
    found: set[str] = set()
    for node in ast.walk(tree):
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
    false positive, and ``from .config_report import f`` resolves to a
    module that can never match a package-qualified key, a false NEGATIVE
    and the direction that matters. ``from . import x`` has no module at
    all and was discarded outright by one of them.
    """
    if not node.level:
        return node.module or ""
    package = ".".join(module.split(".")[: -node.level]) if module else "." * node.level
    if not node.module:
        return package
    return f"{package}{node.module}" if package.endswith(".") else f"{package}.{node.module}"


def _rebind_sweep(nodes: list[ast.AST], table: _Table) -> bool:
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
        if target in table.opaque:
            return False
        table.opaque.add(target)
        return True
    table.origins[target] = origin
    if "." in target or target in table.class_names:
        table.attributes.setdefault(target.rsplit(".", 1)[-1], origin)
    return True


def assignment_parts(node: ast.AST) -> tuple[list[str | None], ast.expr | None]:
    """The dotted targets one binding binds, and what it binds them to.

    ``Assign``, ``AnnAssign`` and the walrus. ``AnnAssign`` is here
    because #324's second logged instance is a TOML parse made invisible
    by ``_p: object = tomllib``: the guard resolved ``Assign`` only, so
    the site was reported neither guarded nor unguarded. A target the AST
    cannot spell as a path (a tuple unpack, a subscript) yields ``None``,
    which the sweep skips rather than guessing at.
    """
    if isinstance(node, ast.Assign):
        return [dotted(target) for target in node.targets], node.value
    if isinstance(node, ast.AnnAssign):
        return [dotted(node.target)], node.value
    if isinstance(node, ast.NamedExpr):
        return [dotted(node.target)], node.value
    return [], None


def calls_to(
    tree: ast.Module,
    targets: Iterable[str],
    *,
    where: str = "",
    module: str = "",
) -> Sites:
    """Every call in one module that resolves to a target, and every call
    that could be one and could not be decided.

    The undecided rule, measured against
    ``subprocess.{run,Popen,call,check_output,check_call}`` over
    ``kstrl/`` at 68 seen and 8 undecided, which is an inventory a guard
    can pin rather than a list it would be silenced for printing.

    - no last identifier at all (``TABLE[key](...)``): undecided, always,
      whatever the targets are. Four in ``kstrl/``.
    - a last identifier no target ends in: not a candidate. This is what
      keeps ``path.mkdir`` and ``', '.join`` out.
    - called through a name bound to something the resolver could not
      follow: undecided. ``proc = subprocess.Popen(...)`` then
      ``proc.wait()`` is this clause.
    - a bare name spelled like a target and bound nowhere: undecided.

    What that leaves, stated rather than implied: a target passed IN as a
    parameter reads as a call on some other object. The bound is that the
    caller had to obtain the target to pass it, so a :func:`census` of
    the acquisition still counts it. Pin it with :func:`blind_spot`.
    """
    wanted = frozenset(targets)
    leaves = {target.rsplit(".", 1)[-1] for target in wanted}
    table = bindings(tree, module=module)
    leaves |= _bound_target_leaves(tree, table, wanted)
    seen: list[str] = []
    undecided: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            _classify_call(node, table, wanted, leaves, where, seen, undecided)
    return Sites(tuple(seen), tuple(undecided))


def _bound_target_leaves(tree: ast.Module, table: Bindings, wanted: frozenset[str]) -> set[str]:
    """Attribute names this module binds to a target, e.g. ``self.spawn``.

    Without these, ``self.spawn = subprocess.Popen`` in ``__init__`` and
    ``self.spawn(argv)`` in a method would read as a call on some other
    object. With them the second is at worst undecided.
    """
    found: set[str] = set()
    for node in ast.walk(tree):
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
    if leaf is None:
        undecided.append(f"{site} {ast.unparse(node.func)}")
        return
    if leaf not in leaves:
        return
    path = dotted(node.func)
    if path is None or path in table.opaque or path.split(".")[0] in table.opaque:
        undecided.append(f"{site} {ast.unparse(node.func)}")
    elif "." not in path:
        undecided.append(f"{site} {path}")


# --- scope ----------------------------------------------------------------


def own_nodes(node: ast.AST) -> list[ast.AST]:
    """Every node belonging to this scope, stopping at a nested function.

    So a helper DEFINED inside a ``try`` and called elsewhere is not
    credited to that ``try``. ``ClassDef`` is deliberately not a stop: a
    method body belongs to its class, and the function stop is what keeps
    the attribution innermost.
    """
    found: list[ast.AST] = []
    for child in ast.iter_child_nodes(node):
        found.append(child)
        if not isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            found.extend(own_nodes(child))
    return found


def scopes(tree: ast.Module) -> list[tuple[ast.AST, str]]:
    """Every scope in a module and its qualified name.

    ``<module>``, then ``build``, ``EvolutionJournal.append_entries``,
    ``_prepare.build.target``. The qualified name is what lets an
    exemption table name ONE closure rather than every function of that
    name in the file.
    """
    found: list[tuple[ast.AST, str]] = [(tree, "<module>")]
    _walk_scopes(tree, "", found)
    return found


def _walk_scopes(node: ast.AST, prefix: str, found: list[tuple[ast.AST, str]]) -> None:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            qualified = f"{prefix}{child.name}"
            found.append((child, qualified))
            _walk_scopes(child, f"{qualified}.", found)
        elif isinstance(child, ast.ClassDef):
            _walk_scopes(child, f"{prefix}{child.name}.", found)
        else:
            _walk_scopes(child, prefix, found)


def declared_in(tree: ast.Module, class_name: str, method: str) -> set[int]:
    """The lines of one method of one class, resolved through the class.

    An exemption resolved by function NAME alone gives a free pass to an
    unrelated method that shares it, which is what round 1 of #327
    shipped. Located by walking, so editing the file above it changes
    nothing.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for item in node.body:
            if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef) and item.name == method:
                return set(range(item.lineno, (item.end_lineno or item.lineno) + 1))
    return set()


# --- try/except -----------------------------------------------------------


@dataclass(frozen=True)
class Clause:
    """One ``except`` clause: what it catches, and whether that is known.

    ``decided`` is the field that stops the skip direction. A handler
    whose type this walk cannot name yields an EMPTY ``names``, and an
    empty set reads exactly like "catches nothing", which is the worst
    possible misreading of "catches something I could not see".
    """

    names: frozenset[str]
    decided: bool
    lineno: int


def handler_clauses(node: ast.Try) -> list[Clause]:
    """The clauses of one ``try``, IN ORDER.

    Order is load-bearing: a broad clause above a narrow one makes the
    narrow one unreachable, so a guard that sorts the clauses cannot tell
    a correct ladder from a dead one. A bare ``except:`` reads as
    ``BaseException``, which is what it catches, and is not the same thing
    as an undecidable handler.
    """
    return [_clause(handler) for handler in node.handlers]


def _clause(handler: ast.ExceptHandler) -> Clause:
    if handler.type is None:
        return Clause(frozenset({"BaseException"}), True, handler.lineno)
    parts = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    names = {leaf_name(part) for part in parts}
    return Clause(
        frozenset(name for name in names if name is not None),
        None not in names,
        handler.lineno,
    )


# --- disclosed limits -----------------------------------------------------


def blind_spot(probe: Callable[[str], object], source: str) -> None:
    """The body of a disclosed limit's anti-vacuity test.

    Used under ``@pytest.mark.xfail(strict=True, raises=AssertionError)``.
    The assertion says the walk DOES see the source; the marker says it is
    expected not to. The row passes only while the limit holds, and the
    day somebody widens the walk it XPASSes, which ``strict=True`` makes a
    failure and the disclosure has to be edited in the same diff.
    ``raises=AssertionError`` is what makes a resolver that CRASHES fail
    too: #328 measured an open hole, a closed hole and a resolver raising
    on entry all passing green under a plain non-strict xfail.

    A disclosure with no test behind it rots silently; this is the test.
    """
    assert probe(source), (
        "the walk still cannot see this, which is what the guard's "
        "docstring discloses. If this row now XPASSes the walk got "
        "stronger: move it into the caught set and edit the disclosure."
    )
